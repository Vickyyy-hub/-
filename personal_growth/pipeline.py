from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .adapters import ADAPTERS, ExcludedArticle, SourceAdapter
from .ai import (
    FILTER_STAGE,
    FILTER_VERSION,
    SUMMARY_STAGE,
    SUMMARY_VERSION,
    ArkAPIError,
    ArkAnalyzer,
    ArkContentSafetyError,
    ArkRateLimitError,
)
from .evidence import content_hash, effective_chars, evidence_packets
from .feishu import FeishuBitable
from .http import HttpClient
from .models import Article, RunReport, SourceResult
from .state import StateStore
from .text import clean_text, normalize_title, normalize_url, similarity


class Pipeline:
    ANALYSIS_SOURCE_ORDER = ("虎嗅", "少数派", "界面新闻", "IT之家", "钛媒体", "36氪")
    PRIMARY_SOURCE = "虎嗅"

    @staticmethod
    def _record_write_time(report: RunReport) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        if not report.first_write_at:
            report.first_write_at = timestamp
        report.last_write_at = timestamp

    @classmethod
    def _sync_feishu(
        cls,
        feishu: FeishuBitable,
        valuable: list[Article],
        report: RunReport,
        *,
        final: bool,
    ) -> list[Article]:
        selected, duplicates = cls._assign_statuses(valuable)
        url_index, title_index = feishu.record_index()
        targets: list[Article] = []
        for article in valuable:
            record = url_index.get(normalize_url(article.url)) or title_index.get(normalize_title(article.title))
            if record:
                article.record_id = str(record.get("record_id", ""))
                targets.append(article)
            elif article.record_status != "普通重复稿":
                targets.append(article)
        if targets:
            written, updated, failed = feishu.upsert(targets)
            invalid = feishu.verify_article_states(targets)
            report.write_failed += max(failed, len(invalid))
            cls._record_write_time(report)
            if not final:
                report.progressive_write_batches += 1
                report.progressive_written += written
                report.progressive_updated += updated
            if (failed or invalid) and not final:
                raise RuntimeError(
                    f"飞书阶段写入验收失败：API失败={failed}，记录不一致={len(invalid)}"
                )
        if final:
            selected_invalid = feishu.verify_article_states(selected)
            report.event_duplicates = duplicates
            report.selected = len(selected)
            report.written = max(0, len(selected) - len(selected_invalid))
            report.write_failed = max(report.write_failed, len(selected_invalid))
            report.final_output_verified = report.write_failed == 0 and not selected_invalid
        return selected

    def __init__(self, source_keys: list[str]) -> None:
        self.http = HttpClient(timeout=int(os.environ.get("HTTP_TIMEOUT", "35")))
        self.deadline = time.monotonic() + int(os.environ.get("MAX_RUNTIME_MINUTES", "280")) * 60
        max_pages = int(os.environ.get("MAX_PAGES", "40"))
        self.adapters: dict[str, SourceAdapter] = {
            key: ADAPTERS[key](self.http, max_pages=max_pages) for key in source_keys
        }

    def _discover_one(self, key: str, day: date) -> tuple[str, SourceResult]:
        adapter = self.adapters[key]
        try:
            return key, adapter.discover(day)
        except Exception as exc:
            return key, SourceResult(adapter.name, errors=[f"采集失败：{exc}"])

    def discover(self, day: date) -> dict[str, SourceResult]:
        results: dict[str, SourceResult] = {}
        with ThreadPoolExecutor(max_workers=len(self.adapters)) as pool:
            futures = [pool.submit(self._discover_one, key, day) for key in self.adapters]
            for future in as_completed(futures):
                key, result = future.result()
                results[key] = result
                print(
                    f"[{result.source}] 扫描 {result.scanned}，目标日 {result.discovered}，"
                    f"覆盖完整={result.coverage_complete}，错误 {len(result.errors)}"
                )
        return results

    def fetch_bodies(self, results: dict[str, SourceResult]) -> list[Article]:
        work: list[tuple[str, Article]] = []
        for key, result in results.items():
            for article in result.articles:
                if article.excluded_reason:
                    result.excluded += 1
                    continue
                work.append((key, article))

        def fetch(key: str, article: Article) -> tuple[str, Article, str, str, bool]:
            try:
                body = clean_text(self.adapters[key].fetch_body(article))
                if not body:
                    raise ValueError("正文为空")
                if len(body) < 400:
                    raise ExcludedArticle(f"正文信息不足（{len(body)}字）")
                promo_sample = article.title + "\n" + body[:800]
                if any(keyword in promo_sample for keyword in ("领券购买", "立即下单", "限时优惠", "品牌赞助", "推广合作")):
                    raise ExcludedArticle("正文命中促销或推广规则")
                return key, article, body, "", False
            except ExcludedArticle as exc:
                return key, article, "", str(exc), True
            except Exception as exc:
                return key, article, "", str(exc), False

        completed: list[Article] = []
        workers = min(int(os.environ.get("FETCH_WORKERS", "8")), max(1, len(work)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fetch, key, article) for key, article in work]
            for future in as_completed(futures):
                key, article, body, error, excluded = future.result()
                result = results[key]
                if excluded:
                    result.excluded += 1
                elif error:
                    result.body_unavailable_skipped += 1
                    result.body_unavailable_items.append(
                        {"title": article.title, "url": article.url, "error": error}
                    )
                    result.warnings.append(f"正文不可用跳过 {article.url}: {error}")
                else:
                    article.body = body
                    result.body_success += 1
                    completed.append(article)
        for result in results.values():
            if result.body_unavailable_skipped > 0 and result.body_success == 0:
                result.errors.append(
                    f"站点正文整体不可用：{result.body_unavailable_skipped}篇均读取失败"
                )
        return completed

    @staticmethod
    def _pre_dedupe(articles: Iterable[Article]) -> list[Article]:
        chosen: dict[tuple[str, str], Article] = {}
        for article in articles:
            key = (normalize_url(article.url), normalize_title(article.title))
            previous = chosen.get(key)
            if previous is None or len(article.body) > len(previous.body):
                chosen[key] = article
        return list(chosen.values())

    @classmethod
    def _analysis_order(cls, articles: Iterable[Article]) -> list[Article]:
        groups: dict[str, list[Article]] = defaultdict(list)
        for article in articles:
            groups[article.source].append(article)
        for items in groups.values():
            items.sort(key=lambda item: (item.published_at, item.url), reverse=True)

        other_known = [
            source for source in cls.ANALYSIS_SOURCE_ORDER
            if source != cls.PRIMARY_SOURCE and source in groups
        ]
        unknown = sorted(set(groups) - set(cls.ANALYSIS_SOURCE_ORDER))
        other_order = other_known + unknown
        primary = groups.get(cls.PRIMARY_SOURCE, [])
        other_index = 0
        ordered: list[Article] = []
        while primary or any(groups[source] for source in other_order):
            if primary:
                ordered.append(primary.pop(0))
            if other_order:
                for _ in range(len(other_order)):
                    source = other_order[other_index % len(other_order)]
                    other_index += 1
                    if groups[source]:
                        ordered.append(groups[source].pop(0))
                        break
        return ordered

    @classmethod
    def _sample_articles(cls, articles: list[Article], limit: int) -> list[Article]:
        ordered = cls._analysis_order(articles)
        if limit <= 0:
            return ordered
        return ordered[:limit]

    @staticmethod
    def _cached_ai_result(
        state: StateStore,
        analyzer: ArkAnalyzer,
        cache_key: str,
    ) -> tuple[dict | None, int]:
        filter_result = state.get_stage(
            cache_key,
            FILTER_STAGE,
            f"{analyzer.model}:{FILTER_VERSION}",
        )
        if filter_result is None:
            return None, 0
        if not filter_result.get("valuable"):
            return filter_result, 1
        summary_result = state.get_stage(
            cache_key,
            SUMMARY_STAGE,
            f"{analyzer.model}:{SUMMARY_VERSION}",
        )
        if summary_result is None:
            return None, 0
        return {**filter_result, **summary_result}, 2

    @staticmethod
    def _quality(article: Article) -> tuple[int, int, int, int]:
        ai = article.ai_result or {}
        scores = ai.get("scores") or {}
        return (
            int(ai.get("score_total", 0)),
            int(scores.get("information_gain", 0)),
            int(scores.get("evidence_density", 0)),
            len(ai.get("core_facts") or []),
        )

    @staticmethod
    def _same_event(article: Article, kept: Article) -> bool:
        event = normalize_title(str((article.ai_result or {}).get("event_key", "")))
        kept_event = normalize_title(str((kept.ai_result or {}).get("event_key", "")))
        return bool(
            (event and kept_event and (event == kept_event or similarity(event, kept_event) >= 0.84))
            or similarity(article.title, kept.title) >= 0.90
        )

    @staticmethod
    def _has_increment(article: Article, group: list[Article]) -> bool:
        ai = article.ai_result or {}
        scores = ai.get("scores") or {}
        if int(scores.get("information_gain", 0)) < 4 or int(scores.get("evidence_density", 0)) < 3:
            return False
        known = [
            point
            for item in group
            for point in ((item.ai_result or {}).get("unique_points") or [])
        ]
        points = ai.get("unique_points") or []
        return bool(points) and any(not any(similarity(point, old) >= 0.80 for old in known) for point in points)

    @classmethod
    def _assign_statuses(cls, articles: list[Article]) -> tuple[list[Article], int]:
        groups: list[list[Article]] = []
        duplicates = 0
        for article in sorted(articles, key=cls._quality, reverse=True):
            group = next((items for items in groups if cls._same_event(article, items[0])), None)
            if group is None:
                article.record_status = "主稿"
                groups.append([article])
            elif cls._has_increment(article, group):
                article.record_status = "增量稿"
                group.append(article)
            else:
                article.record_status = "普通重复稿"
                group.append(article)
                duplicates += 1
        selected = [item for group in groups for item in group if item.record_status != "普通重复稿"]
        selected.sort(key=lambda item: item.published_at, reverse=True)
        return selected, duplicates

    def analyze(
        self,
        articles: list[Article],
        report: RunReport,
        *,
        job_id: str,
        include_rejected: bool = False,
        progressive_callback: Callable[[list[Article]], None] | None = None,
    ) -> tuple[list[Article], bool]:
        if not articles:
            return [], True
        analyzer = ArkAnalyzer()
        state = StateStore()
        analyzed: list[Article] = []
        ordered = self._analysis_order(articles)
        manifest = [content_hash(article) for article in ordered]
        source_by_name = {value.source: value for value in report.sources.values()}
        for article in ordered:
            source_by_name[article.source].ai_pending += 1

        resume_index = 0
        previous = state.get_progress(job_id)
        if previous:
            try:
                previous_manifest = json.loads(previous["manifest_json"])
                previous_index = int(previous["next_index"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                previous_manifest = []
                previous_index = 0
            if previous_manifest == manifest and 0 <= previous_index <= len(ordered):
                resume_index = previous_index
        state.save_progress(job_id, manifest, resume_index, None, "running")
        complete = True
        progressive_interval = max(60, int(os.environ.get("PROGRESSIVE_WRITE_INTERVAL_MINUTES", "60")) * 60)
        next_progressive_write = time.monotonic() + progressive_interval
        last_progressive_count = 0
        try:
            for index, article in enumerate(ordered):
                source_result = source_by_name[article.source]
                if time.monotonic() >= self.deadline:
                    source_result.errors.append(f"达到280分钟运行上限，已保存断点：{article.url}")
                    state.save_progress(job_id, manifest, index, article.url, "time_limit")
                    report.checkpoint_index = index
                    report.pending_articles = len(ordered) - index
                    complete = False
                    break
                cache_key = manifest[index]
                filter_evidence, summary_evidence = evidence_packets(article)
                cache_before = analyzer.cache_hits
                try:
                    if index < resume_index:
                        result, stage_hits = self._cached_ai_result(state, analyzer, cache_key)
                        analyzer.cache_hits += stage_hits
                        if result is None:
                            resume_index = index
                    else:
                        result = None
                    if result is None:
                        state.save_progress(job_id, manifest, index, article.url, "running")
                        result = analyzer.analyze(
                            article,
                            filter_evidence,
                            summary_evidence,
                            cache_key,
                            state,
                        )
                except ArkContentSafetyError as exc:
                    result = {
                        "status": "淘汰",
                        "valuable": False,
                        "reason": "内容安全限制跳过",
                        "event_key": article.title,
                        "topics": [],
                        "category": "",
                        "unique_points": [],
                        "scores": {
                            "importance": 0,
                            "information_gain": 0,
                            "social_impact": 0,
                            "actionability": 0,
                            "evidence_density": 0,
                        },
                        "score_total": 0,
                        "content_safety": True,
                        "content_safety_error": clean_text(exc.detail)[:500],
                    }
                    state.put_stage(
                        cache_key,
                        FILTER_STAGE,
                        f"{analyzer.model}:{FILTER_VERSION}",
                        result,
                    )
                except (ArkRateLimitError, ArkAPIError) as exc:
                    source_result.errors.append(f"AI失败 {article.url}: {exc}")
                    state.save_progress(job_id, manifest, index, article.url, "waiting_retry")
                    report.checkpoint_index = index
                    report.pending_articles = len(ordered) - index
                    complete = False
                    break
                except Exception as exc:
                    source_result.errors.append(f"AI结果失败 {article.url}: {exc}")
                    state.save_progress(job_id, manifest, index, article.url, "invalid_result")
                    report.checkpoint_index = index
                    report.pending_articles = len(ordered) - index
                    complete = False
                    break
                report.ai_evaluated += 1
                source_result.ai_processed += 1
                source_result.ai_pending -= 1
                source_result.cache_hits += analyzer.cache_hits - cache_before
                article.ai_result = result
                if result.get("content_safety"):
                    source_result.content_safety_skipped += 1
                    report.content_safety_skipped += 1
                    source_result.content_safety_items.append(
                        {
                            "title": article.title,
                            "url": article.url,
                            "error": str(result.get("content_safety_error", "内容安全限制")),
                        }
                    )
                    source_result.warnings.append(f"内容安全限制跳过 {article.url}")
                if result.get("invalid_ai"):
                    source_result.invalid_ai_skipped += 1
                    report.invalid_ai_skipped += 1
                    source_result.invalid_ai_items.append(
                        {
                            "title": article.title,
                            "url": article.url,
                            "error": str(result.get("invalid_ai_error", "未知格式错误")),
                        }
                    )
                    source_result.warnings.append(
                        f"AI格式错误跳过 {article.url}: {result.get('invalid_ai_error', '未知格式错误')}"
                    )
                if str(result.get("reason", "")).startswith("摘要"):
                    source_result.warnings.append(
                        f"摘要淘汰 {article.url}: {result.get('reason')}"
                    )
                    report.summary_rejected += 1
                if include_rejected or result.get("valuable"):
                    analyzed.append(article)
                state.save_progress(job_id, manifest, index + 1, None, "running")
                if (
                    progressive_callback is not None
                    and len(analyzed) > last_progressive_count
                    and time.monotonic() >= next_progressive_write
                ):
                    try:
                        progressive_callback(list(analyzed))
                    except Exception as exc:
                        source_result.errors.append(f"阶段写入失败 {article.url}: {exc}")
                        state.save_progress(job_id, manifest, index + 1, article.url, "output_retry")
                        report.checkpoint_index = index + 1
                        report.pending_articles = len(ordered) - index - 1
                        complete = False
                        break
                    last_progressive_count = len(analyzed)
                    now = time.monotonic()
                    while next_progressive_write <= now:
                        next_progressive_write += progressive_interval
            if complete:
                state.save_progress(job_id, manifest, len(ordered), None, "complete")
                report.checkpoint_index = len(ordered)
                report.pending_articles = 0
        finally:
            report.filter_calls = analyzer.filter_calls
            report.summary_calls = analyzer.summary_calls
            report.cache_hits = analyzer.cache_hits
            report.rate_limit_count = analyzer.rate_limit_count
            report.retry_wait_seconds = analyzer.retry_wait_seconds
            state.close()
        return analyzed, complete

    @staticmethod
    def _record_source_stats(report: RunReport, all_valuable: list[Article]) -> None:
        for article in all_valuable:
            source = next(value for value in report.sources.values() if value.source == article.source)
            if article.record_status == "主稿":
                source.selected_main += 1
            elif article.record_status == "增量稿":
                source.selected_incremental += 1
            elif article.record_status == "普通重复稿":
                source.event_duplicates += 1
        huxiu = next((value for value in report.sources.values() if value.source == "虎嗅"), None)
        if (
            huxiu and huxiu.ai_pending > 0 and huxiu.ai_processed == 0
        ):
            huxiu.errors.append("虎嗅存在待分析正文但AI处理数为0，五站轮询或断点恢复异常")
        elif (
            huxiu and huxiu.ai_processed > 0 and not huxiu.errors
            and huxiu.selected_main + huxiu.selected_incremental == 0
        ):
            huxiu.warnings.append("虎嗅正文读取成功但入选为0，请检查评分、正文质量或跨站去重")

    def _prepare_backfill(self, results: dict[str, SourceResult], feishu: FeishuBitable) -> tuple[int, int]:
        url_index, title_index = feishu.record_index()
        matched_ids: set[str] = set()
        supported_records = {
            record.get("record_id", "")
            for record in url_index.values()
            if record.get("record_id")
        }
        for result in results.values():
            matched: list[Article] = []
            for article in result.articles:
                record = url_index.get(normalize_url(article.url)) or title_index.get(normalize_title(article.title))
                if not record:
                    continue
                article.record_id = record.get("record_id", "")
                matched_ids.add(article.record_id)
                matched.append(article)
            result.articles = matched
        return len(matched_ids), len(supported_records - matched_ids)

    def write_cached(
        self,
        day: date,
        *,
        complete_recovery: bool = False,
        report_path: Path = Path("run-report.json"),
    ) -> RunReport:
        report = RunReport(
            day.isoformat(),
            cached_only=True,
            truncated_by_user=not complete_recovery,
        )
        feishu = FeishuBitable(self.http)
        report.sources = self.discover(day)
        for source_result in report.sources.values():
            for article in source_result.articles:
                article.daily_date = day
        articles = self._pre_dedupe(self.fetch_bodies(report.sources))
        report.body_unavailable_skipped = sum(
            result.body_unavailable_skipped for result in report.sources.values()
        )
        existing_urls, existing_titles = feishu.existing_keys()
        unseen_articles = [
            article for article in articles
            if normalize_url(article.url) not in existing_urls
            and normalize_title(article.title) not in existing_titles
        ]
        report.existing_duplicates = len(articles) - len(unseen_articles)

        model = os.environ.get("ARK_MODEL", "doubao-seed-2-0-mini-260215")
        state = StateStore()
        cached_valuable: list[Article] = []
        try:
            for article in unseen_articles:
                key = content_hash(article)
                filter_result = state.get_stage(key, FILTER_STAGE, f"{model}:{FILTER_VERSION}")
                if filter_result is None:
                    report.cache_misses += 1
                    continue
                report.ai_evaluated += 1
                if not filter_result.get("valuable"):
                    continue
                summary_result = state.get_stage(key, SUMMARY_STAGE, f"{model}:{SUMMARY_VERSION}")
                if summary_result is None:
                    report.cache_misses += 1
                    continue
                content = str(summary_result.get("core_content", ""))
                size = effective_chars(content)
                paragraphs = [item.strip() for item in content.splitlines() if item.strip()]
                if summary_result.get("status") != "入选" or not 300 <= size <= 500 or len(paragraphs) != 3:
                    continue
                article.ai_result = {**filter_result, **summary_result, "summary_chars": size, "valuable": True}
                cached_valuable.append(article)
        finally:
            state.close()

        selected, report.event_duplicates = self._assign_statuses(cached_valuable)
        self._record_source_stats(report, cached_valuable)
        report.cache_candidates = len(cached_valuable)
        report.selected = len(selected)
        report.cache_hits = report.ai_evaluated + len(cached_valuable)
        report.pending_articles = report.cache_misses
        lengths = [int((article.ai_result or {}).get("summary_chars", 0)) for article in selected]
        if lengths:
            report.summary_char_min = min(lengths)
            report.summary_char_max = max(lengths)
        self._sync_feishu(feishu, cached_valuable, report, final=True)
        report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def run(
        self,
        day: date,
        *,
        dry_run: bool = False,
        collect_only: bool = False,
        backfill: bool = False,
        max_articles: int = 0,
        trigger_source: str = "manual",
        report_path: Path = Path("run-report.json"),
    ) -> RunReport:
        report = RunReport(
            day.isoformat(),
            trigger_source=trigger_source,
            dry_run=dry_run,
            collect_only=collect_only,
            backfill=backfill,
        )
        feishu: FeishuBitable | None = None
        if not collect_only and (backfill or not dry_run):
            feishu = FeishuBitable(self.http)
        report.sources = self.discover(day)
        for source_result in report.sources.values():
            for article in source_result.articles:
                article.daily_date = day
        if backfill:
            assert feishu is not None
            _, report.unchanged = self._prepare_backfill(report.sources, feishu)
        articles = self._pre_dedupe(self.fetch_bodies(report.sources))
        report.body_unavailable_skipped = sum(
            result.body_unavailable_skipped for result in report.sources.values()
        )
        if max_articles > 0:
            articles = self._sample_articles(articles, max_articles)
        if not collect_only:
            if not backfill and not dry_run:
                assert feishu is not None
                existing_urls, existing_titles = feishu.existing_keys()
                report.existing_duplicates = sum(
                    1 for article in articles
                    if normalize_url(article.url) in existing_urls
                    or normalize_title(article.title) in existing_titles
                )
            mode = "backfill" if backfill else ("dry" if dry_run else "daily")
            source_part = "-".join(sorted(self.adapters))
            progressive_callback = None
            if not backfill and not dry_run:
                assert feishu is not None
                progressive_callback = lambda current: self._sync_feishu(
                    feishu, current, report, final=False
                )
            analyzed, analysis_complete = self.analyze(
                articles,
                report,
                job_id=f"{day.isoformat()}:{mode}:{source_part}",
                include_rejected=backfill,
                progressive_callback=progressive_callback,
            )
            valuable = [article for article in analyzed if (article.ai_result or {}).get("valuable")]
            selected, report.event_duplicates = self._assign_statuses(valuable)
            rejected = [article for article in analyzed if not (article.ai_result or {}).get("valuable")]
            for article in rejected:
                article.record_status = "普通重复稿"
            self._record_source_stats(report, valuable)
            report.selected = len(selected)
            lengths = [int((article.ai_result or {}).get("summary_chars", 0)) for article in selected]
            lengths = [value for value in lengths if value]
            if lengths:
                report.summary_char_min = min(lengths)
                report.summary_char_max = max(lengths)
            if not analysis_complete:
                pass  # Never write a partial daily report or partial backfill.
            elif backfill and not dry_run:
                assert feishu is not None
                update_articles = valuable + rejected
                updated, failed = feishu.update(update_articles)
                missing = feishu.verify_updates(update_articles)
                report.updated = max(0, len(update_articles) - len(missing))
                report.update_failed = max(failed, len(missing))
            elif not dry_run:
                assert feishu is not None
                selected = self._sync_feishu(feishu, valuable, report, final=True)
                report.selected = len(selected)
        report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return report
