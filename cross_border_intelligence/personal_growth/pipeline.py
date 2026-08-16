from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .ai import ArkContentSafetyError, MarketAnalyzer, ModelRateLimitError, ModelSystemError
from .collectors import Collector
from .config import countries, feishu_config, load_config, product_config, sources
from .feishu import (
    FeishuMarketBase,
    direction_rows,
    profile_rows,
    signal_rows,
    source_rows,
)
from .http import HttpClient
from .models import CountryProfile, MarketRunReport, Signal
from .outputs import write_rows
from .store import MarketStore

PROGRESSIVE_WRITE_INTERVAL_MINUTES = int(os.environ.get("PROGRESSIVE_WRITE_INTERVAL_MINUTES", "60"))


class MarketPipeline:
    def __init__(self) -> None:
        self.http = HttpClient()
        self.country_map = countries()
        self.collector = Collector(self.http, self.country_map)

    @staticmethod
    def _analyzer_or_warning(report: MarketRunReport) -> MarketAnalyzer | None:
        try:
            return MarketAnalyzer()
        except RuntimeError as exc:
            report.errors.append(f"AI鉴权未就绪：{exc}")
            report.partial = True
            return None

    def _collect(self, cadence: str, day: date, report: MarketRunReport) -> list[Signal]:
        collected: list[Signal] = []
        specs = sources(cadence=cadence)

        def collect_one(spec):
            collector = Collector(HttpClient(), self.country_map)
            return collector.collect(spec, day)

        outcomes: dict[str, tuple[list[Signal], list[str]] | Exception] = {}
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(specs)))) as executor:
            futures = {executor.submit(collect_one, spec): spec for spec in specs}
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    outcomes[spec.key] = future.result()
                except Exception as exc:
                    outcomes[spec.key] = exc

        for spec in specs:
            status = {"name": spec.name, "kind": spec.kind, "count": 0, "warnings": [], "error": ""}
            outcome = outcomes[spec.key]
            if not isinstance(outcome, Exception):
                items, warnings = outcome
                collected.extend(items)
                status["count"] = len(items)
                status["warnings"] = warnings
                if warnings and not spec.optional:
                    report.warnings.extend(f"{spec.name}：{value}" for value in warnings)
                    if not items and spec.kind == "reference":
                        report.errors.append(f"站点正文整体不可用：{spec.name}")
                        report.body_unavailable_skipped += 1
                        report.partial = True
            else:
                exc = outcome
                status["error"] = str(exc)
                message = f"{spec.name}采集失败：{exc}"
                if spec.optional:
                    report.warnings.append(message)
                else:
                    report.errors.append(message)
                    report.partial = True
            report.source_status[spec.key] = status
        report.collected += len(collected)
        return collected

    def _store_and_output(self, signals: list[Signal], day: date, report: MarketRunReport) -> list[dict[str, Any]]:
        store = MarketStore()
        try:
            inserted, duplicates = store.upsert_signals(signals)
        finally:
            store.close()
        report.stored += inserted
        report.skipped_duplicates += duplicates
        rows = [item.to_dict() for item in signals]
        write_rows("signals", rows, day)
        return rows

    def _feishu(self, *, read_only: bool = False) -> FeishuMarketBase | None:
        if not feishu_config().get("enabled", True):
            return None
        return FeishuMarketBase(self.http, read_only=read_only)

    def sync_sources(self, day: date, report: MarketRunReport, *, dry_run: bool) -> None:
        items = []
        for key, value in load_config()["sources"].items():
            items.append({"key": key, **value})
        write_rows("sources", items, day)
        if not dry_run:
            feishu = self._feishu()
            if feishu:
                created, updated = feishu.upsert("sources", source_rows(items))
                report.written += created
                report.updated += updated
                report.final_output_verified = feishu.verify_article_states("sources", source_rows(items))

    def run_signals(
        self,
        day: date,
        report: MarketRunReport,
        *,
        cadences: tuple[str, ...],
        dry_run: bool,
        collect_only: bool,
    ) -> None:
        signals: list[Signal] = []
        for cadence in cadences:
            signals.extend(self._collect(cadence, day, report))
        report.pending_articles = len(signals)
        feishu = None if dry_run or collect_only else self._feishu()
        progressive_interval = int(
            feishu_config().get("progressive_write_interval_minutes", PROGRESSIVE_WRITE_INTERVAL_MINUTES)
        ) * 60
        last_progressive = time.monotonic()

        def progressive_callback(processed: list[Signal]) -> None:
            nonlocal last_progressive
            if feishu is None or time.monotonic() - last_progressive < progressive_interval:
                return
            rows = signal_rows([item.to_dict() for item in processed], day.isoformat())
            created, updated = feishu.upsert("signals", rows)
            report.progressive_write_batches += 1
            report.progressive_written += created
            report.progressive_updated += updated
            now = datetime.now().astimezone().isoformat()
            report.first_write_at = report.first_write_at or now
            report.last_write_at = now
            last_progressive = time.monotonic()

        if not collect_only:
            analyzer = self._analyzer_or_warning(report)
            if analyzer:
                try:
                    signals = analyzer.enrich_signals(signals, progressive_callback=progressive_callback)
                    report.ai_calls += analyzer.calls
                    report.rate_limits += analyzer.rate_limits
                    report.retry_wait_seconds += analyzer.retry_wait_seconds
                    report.content_safety_skipped += analyzer.content_safety_skipped
                    report.invalid_ai_skipped += analyzer.invalid_ai_skipped
                    report.summary_rejected += analyzer.summary_rejected
                    report.warnings.extend(analyzer.warnings)
                except (ModelRateLimitError, ModelSystemError) as exc:
                    report.errors.append(f"AI信号处理失败：{exc}")
                    report.partial = True
                    raise
                except Exception as exc:
                    report.errors.append(f"AI信号处理异常：{exc}")
                    report.partial = True
                    raise
            else:
                raise ModelSystemError("AI未启用，禁止将未分析信号标记为完整")
        report.pending_articles = 0
        rows = self._store_and_output(signals, day, report)
        if feishu:
            output_rows = signal_rows(rows, day.isoformat())
            created, updated = feishu.upsert("signals", output_rows)
            report.written += created
            report.updated += updated
            report.final_output_verified = feishu.verify_article_states("signals", output_rows)

    def run_profiles(self, day: date, report: MarketRunReport, *, dry_run: bool, collect_only: bool) -> None:
        evidence = self._collect("profile_monthly", day, report)
        by_country: dict[str, list[dict[str, Any]]] = {code: [] for code in self.country_map}
        for signal in evidence:
            for code in signal.countries:
                by_country.setdefault(code, []).append(signal.to_dict())
        analyzer = None if collect_only else self._analyzer_or_warning(report)
        if not collect_only and analyzer is None:
            raise ModelSystemError("AI未启用，禁止生成低证据画像")
        profiles: list[CountryProfile] = []
        for code, country in self.country_map.items():
            if analyzer and by_country.get(code):
                try:
                    profiles.append(analyzer.build_profile(country, by_country[code]))
                    continue
                except Exception as exc:
                    report.errors.append(f"{country.name_zh}画像生成失败：{exc}")
                    report.partial = True
            profiles.append(
                CountryProfile(
                    code, country.name_zh, country.region, country.language, country.currency,
                    country.timezone, evidence_urls=[item.get("url", "") for item in by_country.get(code, [])],
                    updated_at=datetime.now().astimezone(), confidence="low",
                    culture_notes="待官方证据与AI月度刷新；禁止用单条社媒内容概括国家文化。",
                )
            )
        rows = [item.to_dict() for item in profiles]
        store = MarketStore()
        try:
            store.upsert_profiles(profiles)
        finally:
            store.close()
        report.profiles = len(profiles)
        write_rows("profiles", rows, day)
        if not dry_run and not collect_only:
            feishu = self._feishu()
            if feishu:
                created, updated = feishu.upsert("profiles", profile_rows(rows))
                report.written += created
                report.updated += updated
                report.final_output_verified = feishu.verify_article_states("profiles", profile_rows(rows))

    def run_directions(self, day: date, report: MarketRunReport, *, dry_run: bool, collect_only: bool) -> None:
        if collect_only:
            write_rows("directions", [], day)
            return
        analyzer = self._analyzer_or_warning(report)
        if analyzer is None:
            raise ModelSystemError("AI未启用，禁止生成产品方向")
        config = product_config()
        lookback = int(config.get("lookback_days", 30))
        minimum_types = int(config.get("minimum_source_types", 2))
        store = MarketStore()
        directions = []
        try:
            since = datetime.now().astimezone() - timedelta(days=lookback)
            for code, country in self.country_map.items():
                items = store.load_signals(since, code)
                try:
                    directions.extend(analyzer.product_directions(country, items, minimum_source_types=minimum_types))
                except Exception as exc:
                    report.errors.append(f"{country.name_zh}产品方向生成失败：{exc}")
                    report.partial = True
        finally:
            store.close()
        rows = [item.to_dict() for item in directions]
        report.directions = len(rows)
        write_rows("directions", rows, day)
        if not dry_run:
            feishu = self._feishu()
            if feishu:
                created, updated = feishu.upsert("directions", direction_rows(rows))
                report.written += created
                report.updated += updated
                report.final_output_verified = feishu.verify_article_states("directions", direction_rows(rows))

    def run(
        self,
        job: str,
        day: date,
        *,
        dry_run: bool = False,
        collect_only: bool = False,
        report_path: Path = Path("run-report.json"),
    ) -> MarketRunReport:
        report = MarketRunReport(job, day, dry_run=dry_run, collect_only=collect_only)
        job_key = f"{job}:{day.isoformat()}"
        state = MarketStore()
        try:
            state.save_progress(job_key, {"stage": "started", "job": job, "target_date": day.isoformat()})
            if job in {"sources", "all"}:
                self.sync_sources(day, report, dry_run=dry_run or collect_only)
            if job in {"policy", "all"}:
                self.run_signals(day, report, cadences=("policy_4x",), dry_run=dry_run, collect_only=collect_only)
            if job in {"demand", "all"}:
                self.run_signals(day, report, cadences=("demand_daily",), dry_run=dry_run, collect_only=collect_only)
            if job in {"weekly", "all"}:
                self.run_signals(day, report, cadences=("data_weekly",), dry_run=dry_run, collect_only=collect_only)
                self.run_directions(day, report, dry_run=dry_run, collect_only=collect_only)
            if job in {"profiles", "all"}:
                self.run_profiles(day, report, dry_run=dry_run, collect_only=collect_only)
        except Exception as exc:
            if str(exc) not in report.errors:
                report.errors.append(str(exc))
            report.partial = True
        report.coverage_complete = not report.partial and all(
            not value.get("error") for value in report.source_status.values()
        )
        if dry_run or collect_only:
            report.final_output_verified = False
        elif report.pending_articles == 0 and report.coverage_complete and report.write_failed == 0 and report.final_output_verified:
            report.completion_marker = True
            state.mark_complete(job_key, report.to_dict())
        else:
            state.save_progress(job_key, report.to_dict())
        state.close()
        report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def check_feishu(self) -> dict[str, Any]:
        feishu = self._feishu(read_only=True)
        return {"enabled": False} if feishu is None else feishu.status()

    def setup_feishu(self) -> dict[str, Any]:
        feishu = self._feishu(read_only=False)
        return {"enabled": False} if feishu is None else feishu.status()
