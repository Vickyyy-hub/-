from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any

from .ai import FILTER_VERSION, SUMMARY_VERSION, ArkContentSafetyError, MarketAnalyzer, ModelRateLimitError, ModelSystemError
from .collectors import Collector
from .config import countries, feishu_config, sources
from .feishu import FeishuMarketBase, signal_rows
from .http import HttpClient
from .models import MarketRunReport, Signal
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
            return Collector(HttpClient(), self.country_map).collect(spec, day)

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
            if isinstance(outcome, Exception):
                status["error"] = str(outcome)
                message = f"{spec.name}采集失败：{outcome}"
                report.warnings.append(message)
            else:
                items, warnings = outcome
                collected.extend(items)
                status["count"] = len(items)
                status["warnings"] = warnings
                report.warnings.extend(f"{spec.name}：{value}" for value in warnings)
                if any("正文" in value for value in warnings):
                    report.body_unavailable_skipped += 1
                    if not items and not spec.optional:
                        report.warnings.append(f"站点正文整体不可用：{spec.name}")
            report.source_status[spec.key] = status
        report.collected += len(collected)
        return collected

    def _feishu(self, *, read_only: bool = False) -> FeishuMarketBase | None:
        if not feishu_config().get("enabled", True):
            return None
        return FeishuMarketBase(self.http, read_only=read_only)

    @staticmethod
    def _valid_cached(item: dict[str, Any]) -> bool:
        summary = str(item.get("summary_zh") or "")
        meta = item.get("meta") or {}
        return (
            80 <= len(re.sub(r"\s+", "", summary)) <= 150
            and bool(item.get("topics"))
            and meta.get("filter_version") == FILTER_VERSION
            and meta.get("summary_version") == SUMMARY_VERSION
            and meta.get("ai_status") == "selected"
        )

    @staticmethod
    def _valid_cached_rejection(item: dict[str, Any]) -> bool:
        meta = item.get("meta") or {}
        return (
            meta.get("filter_version") == FILTER_VERSION
            and meta.get("summary_version") == SUMMARY_VERSION
            and meta.get("ai_status") == "rejected"
        )

    def run_signals(self, day: date, report: MarketRunReport, *, cadence: str,
                    dry_run: bool, collect_only: bool) -> None:
        signals = self._collect(cadence, day, report)
        if collect_only:
            write_rows("collected", [item.to_dict() for item in signals], day)
            report.pending_articles = len(signals)
            return
        store = MarketStore()
        try:
            cached = store.load_signals_by_ids([item.signal_id for item in signals])
        finally:
            store.close()
        ready: list[Signal] = []
        pending: list[Signal] = []
        for signal in signals:
            previous = cached.get(signal.signal_id, {})
            if self._valid_cached(previous):
                signal.keyword_zh = previous.get("keyword_zh", "")
                signal.summary_zh = previous.get("summary_zh", "")
                signal.ai_conclusion = previous.get("ai_conclusion", "")
                signal.topics = list(previous.get("topics") or [])
                signal.meta = dict(previous.get("meta") or {})
                ready.append(signal)
                report.cache_hits += 1
            elif self._valid_cached_rejection(previous):
                signal.meta = dict(previous.get("meta") or {})
                report.cache_hits += 1
            else:
                pending.append(signal)
        report.pending_articles = len(pending)
        analyzer = self._analyzer_or_warning(report)
        if analyzer is None:
            raise ModelSystemError("AI未启用，禁止写入未筛选资讯")
        feishu = None if dry_run else self._feishu()
        progressive_interval = int(feishu_config().get(
            "progressive_write_interval_minutes", PROGRESSIVE_WRITE_INTERVAL_MINUTES
        )) * 60
        last_progressive = time.monotonic()

        def progressive_callback(processed: list[Signal]) -> None:
            nonlocal last_progressive
            if feishu is None or not processed or time.monotonic() - last_progressive < progressive_interval:
                return
            rows = signal_rows([item.to_dict() for item in processed], day.isoformat())
            created, updated = feishu.upsert(rows)
            report.progressive_write_batches += 1
            report.progressive_written += created
            report.progressive_updated += updated
            last_progressive = time.monotonic()

        try:
            ready.extend(analyzer.enrich_signals(pending, progressive_callback=progressive_callback))
        except (ArkContentSafetyError, ModelRateLimitError, ModelSystemError):
            raise
        finally:
            report.ai_calls += analyzer.calls
            report.rate_limits += analyzer.rate_limits
            report.retry_wait_seconds += analyzer.retry_wait_seconds
            report.content_safety_skipped += analyzer.content_safety_skipped
            report.invalid_ai_skipped += analyzer.invalid_ai_skipped
            report.summary_rejected += analyzer.summary_rejected
            report.warnings.extend(analyzer.warnings)
        report.pending_articles = 0
        state = MarketStore()
        try:
            inserted, duplicates = state.upsert_signals(signals)
        finally:
            state.close()
        report.stored += inserted
        report.skipped_duplicates += duplicates
        output = [item.to_dict() for item in ready]
        write_rows("news", output, day)
        if dry_run:
            return
        if feishu is None:
            raise RuntimeError("飞书输出未启用")
        rows = signal_rows(output, day.isoformat())
        created, updated = feishu.upsert(rows)
        report.written += created
        report.updated += updated
        report.final_output_verified = feishu.verify(rows) if rows else True

    def run(self, job: str, day: date, *, dry_run: bool = False, collect_only: bool = False,
            report_path: Path = Path("run-report.json")) -> MarketRunReport:
        report = MarketRunReport(job, day, dry_run=dry_run, collect_only=collect_only)
        job_key = f"v2:{job}:{day.isoformat()}"
        state = MarketStore()
        if not dry_run and not collect_only and job == "demand" and state.is_complete(job_key):
            report.coverage_complete = True
            report.final_output_verified = True
            report.completion_marker = True
            report.warnings.append("同日需求任务已有完成标记，新增0条")
            state.close()
            report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            return report
        try:
            state.save_progress(job_key, {"stage": "started", "job": job, "target_date": day.isoformat()})
            if job in {"policy", "all"}:
                self.run_signals(day, report, cadence="policy_4x", dry_run=dry_run, collect_only=collect_only)
            if job in {"demand", "all"}:
                self.run_signals(day, report, cadence="demand_daily", dry_run=dry_run, collect_only=collect_only)
        except Exception as exc:
            if str(exc) not in report.errors:
                report.errors.append(str(exc))
            report.partial = True
        report.coverage_complete = not report.partial and any(
            not value.get("error") for value in report.source_status.values()
        )
        if dry_run or collect_only:
            report.final_output_verified = False
        elif report.coverage_complete and report.write_failed == 0 and report.final_output_verified:
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

    def delete_legacy_feishu(self) -> dict[str, str]:
        feishu = self._feishu(read_only=False)
        if feishu is None:
            raise RuntimeError("飞书输出未启用")
        return feishu.delete_legacy_tables()

    def reset_feishu_news(self) -> dict[str, Any]:
        feishu = self._feishu(read_only=False)
        if feishu is None:
            raise RuntimeError("飞书输出未启用")
        return feishu.reset_news_table()
