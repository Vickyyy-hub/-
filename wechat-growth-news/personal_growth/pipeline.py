from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from .adapters import ADAPTERS, ExcludedArticle, SourceAdapter
from .ai import ArkAPIError, ArkAnalyzer, ArkContentSafetyError, ArkRateLimitError
from .config import load_config, output_config, source_config
from .evidence import content_hash, effective_chars, evidence_packets
from .feishu import FeishuBitable
from .http import HttpClient
from .models import Article, RunReport, SourceResult
from .outputs import write_local_outputs
from .state import StateStore
from .text import clean_text, normalize_title, normalize_url


class Pipeline:
    ANALYSIS_SOURCE_ORDER = ("简单心理", "丁香医生", "地球知识局", "格隆", "利维坦")

    def __init__(self, source_keys: list[str]) -> None:
        self.http = HttpClient(timeout=int(os.environ.get("HTTP_TIMEOUT", "60")))
        self.deadline = time.monotonic() + int(os.environ.get("MAX_RUNTIME_MINUTES", "280")) * 60
        self.adapters: dict[str, SourceAdapter] = {
            key: ADAPTERS[key](self.http, max_pages=1) for key in source_keys
        }
        self.key_by_source = {adapter.name: key for key, adapter in self.adapters.items()}

    def _discover_one(self, key: str, day: date) -> tuple[str, SourceResult]:
        adapter = self.adapters[key]
        try:
            return key, adapter.discover(day)
        except Exception as exc:
            return key, SourceResult(adapter.name, errors=[f"采集失败：{exc}"])

    def discover(self, day: date) -> dict[str, SourceResult]:
        results: dict[str, SourceResult] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(self.adapters))) as pool:
            futures = [pool.submit(self._discover_one, key, day) for key in self.adapters]
            for future in as_completed(futures):
                key, result = future.result()
                results[key] = result
                print(
                    f"[{result.source}] 扫描{result.scanned}，目标日{result.discovered}，"
                    f"覆盖完整={result.coverage_complete}，错误{len(result.errors)}"
                )
        self._mark_globally_stale(results, day)
        return results

    @staticmethod
    def _mark_globally_stale(results: dict[str, SourceResult], day: date) -> None:
        """Fail loudly when every healthy feed is older than the target day.

        Five independent publishers returning no target-day items while every
        feed stops on an earlier day is the observable signal for an expired
        WeRead login or a stalled WeWeRSS refresh.  A single current feed is
        enough to avoid the global alarm.
        """
        if not results or any(result.errors or result.scanned == 0 for result in results.values()):
            return
        if any(result.discovered > 0 for result in results.values()):
            return
        latest_values = [result.latest_published_at for result in results.values()]
        if any(value is None for value in latest_values):
            return
        latest = max(value for value in latest_values if value is not None)
        if latest.date() >= day:
            return
        first_key = sorted(results)[0]
        results[first_key].errors.append(
            "WeWeRSS全部来源未发现目标日文章，"
            f"最新文章停留在{latest.isoformat()}；可能账号失效或上游未刷新，"
            "请检查微信读书账号并重新扫码"
        )

    @staticmethod
    def _dedupe(articles: Iterable[Article]) -> list[Article]:
        ordered = sorted(
            articles,
            key=lambda item: (bool(item.record_id), len(item.body), item.published_at),
            reverse=True,
        )
        seen_urls: set[str] = set()
        seen_titles: set[str] = set()
        chosen: list[Article] = []
        for article in ordered:
            url = normalize_url(article.url)
            title = normalize_title(article.title)
            if url in seen_urls or title in seen_titles:
                continue
            seen_urls.add(url)
            seen_titles.add(title)
            chosen.append(article)
        return sorted(chosen, key=lambda item: item.published_at, reverse=True)

    def _candidate_articles(
        self,
        results: dict[str, SourceResult],
        feishu: FeishuBitable | None,
        day: date,
        report: RunReport,
    ) -> list[Article]:
        current = [article for result in results.values() for article in result.articles]
        for article in current:
            article.daily_date = day
        if feishu is None:
            return self._dedupe(current)

        url_index, title_index = feishu.record_index()
        candidates: list[Article] = []
        pending_statuses = {"正文待补全", "AI待重试"}
        for article in current:
            record = url_index.get(normalize_url(article.url)) or title_index.get(normalize_title(article.title))
            if not record:
                candidates.append(article)
                continue
            fields = record.get("fields") or {}
            status = feishu._extract_text(fields.get("记录状态"))
            if status in pending_statuses:
                article.record_id = str(record.get("record_id") or "")
                article.record_status = status
                candidates.append(article)
            else:
                report.existing_duplicates += 1

        retry_days = int((load_config().get("project") or {}).get("retry_days", 7))
        cutoff = day - timedelta(days=max(0, retry_days - 1))
        retry_items = feishu.pending_articles(cutoff, set(self.key_by_source))
        current_urls = {normalize_url(article.url) for article in candidates}
        for article in retry_items:
            if normalize_url(article.url) not in current_urls:
                candidates.append(article)
                current_urls.add(normalize_url(article.url))
        return self._dedupe(candidates)

    def fetch_bodies(
        self,
        results: dict[str, SourceResult],
        candidates: list[Article],
    ) -> tuple[list[Article], list[Article]]:
        ready: list[Article] = []
        pending: list[Article] = []
        attempted_by_source: dict[str, int] = defaultdict(int)

        def fetch(article: Article) -> tuple[Article, str, str, bool]:
            key = self.key_by_source[article.source]
            if article.excluded_reason:
                return article, "", article.excluded_reason, True
            try:
                body = clean_text(self.adapters[key].fetch_body(article))
                if not body:
                    raise RuntimeError("正文为空")
                minimum = int(source_config(key).get("min_body_chars", 400))
                if len(body) < minimum:
                    raise ExcludedArticle(f"正文信息不足（{len(body)}字）")
                promo_sample = article.title + "\n" + body[:1000]
                keywords = ("领券购买", "立即下单", "限时优惠", "品牌赞助", "推广合作") + tuple(
                    source_config(key).get("exclude_keywords") or []
                )
                if any(keyword in promo_sample for keyword in keywords):
                    raise ExcludedArticle("正文命中促销或推广规则")
                return article, body, "", False
            except ExcludedArticle as exc:
                return article, "", str(exc), True
            except Exception as exc:
                return article, "", str(exc), False

        workers = min(int(os.environ.get("FETCH_WORKERS", "5")), max(1, len(candidates)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fetch, article) for article in candidates]
            for future in as_completed(futures):
                article, body, error, excluded = future.result()
                source_result = results[self.key_by_source[article.source]]
                if excluded:
                    source_result.excluded += 1
                    continue
                attempted_by_source[article.source] += 1
                if error:
                    article.record_status = "正文待补全"
                    article.meta["failure_reason"] = error
                    article.ai_result = {"topics": [], "category": "", "core_content": ""}
                    source_result.body_failed += 1
                    source_result.body_unavailable_skipped += 1
                    source_result.body_unavailable_items.append({"title": article.title, "url": article.url})
                    source_result.warnings.append(f"正文待补全 {article.url}: {error}")
                    pending.append(article)
                    continue
                article.body = body
                article.meta["failure_reason"] = ""
                source_result.body_success += 1
                ready.append(article)
        for source, attempted in attempted_by_source.items():
            source_result = results[self.key_by_source[source]]
            if attempted >= 2 and source_result.body_failed == attempted and source_result.body_success == 0:
                source_result.errors.append(f"站点正文整体不可用：目标日期{attempted}篇文章全部失败")
        return self._dedupe(ready), self._dedupe(pending)

    def _analysis_order(self, articles: Iterable[Article]) -> list[Article]:
        groups: dict[str, list[Article]] = defaultdict(list)
        for article in articles:
            groups[article.source].append(article)
        for items in groups.values():
            items.sort(key=lambda item: item.published_at, reverse=True)
        order = [source for source in self.ANALYSIS_SOURCE_ORDER if source in groups]
        output: list[Article] = []
        while any(groups[source] for source in order):
            for source in order:
                if groups[source]:
                    output.append(groups[source].pop(0))
        return output

    @staticmethod
    def _mark_ai_pending(article: Article, reason: str) -> Article:
        article.record_status = "AI待重试"
        article.meta["failure_reason"] = reason[:1000]
        article.ai_result = article.ai_result or {"topics": [], "category": "", "core_content": ""}
        return article

    def analyze(
        self,
        articles: list[Article],
        results: dict[str, SourceResult],
        report: RunReport,
        *,
        job_id: str,
    ) -> tuple[list[Article], list[Article], bool]:
        if not articles:
            return [], [], True
        analyzer = ArkAnalyzer()
        state = StateStore()
        ordered = self._analysis_order(articles)
        manifest = [content_hash(article) for article in ordered]
        completed: list[Article] = []
        pending: list[Article] = []
        complete = True
        for article in ordered:
            results[self.key_by_source[article.source]].ai_pending += 1
        try:
            for index, article in enumerate(ordered):
                source_result = results[self.key_by_source[article.source]]
                if time.monotonic() >= self.deadline:
                    reason = "达到运行时间上限，等待自动重试"
                    source_result.errors.append(f"{reason}：{article.url}")
                    for remaining in ordered[index:]:
                        pending.append(self._mark_ai_pending(remaining, reason))
                    report.pending_articles = len(ordered) - index
                    report.checkpoint_index = index
                    state.save_progress(job_id, manifest, index, article.url, "time_limit")
                    complete = False
                    break
                filter_evidence, summary_evidence = evidence_packets(article)
                cache_key = manifest[index]
                state.save_progress(job_id, manifest, index, article.url, "running")
                try:
                    result = analyzer.analyze(article, filter_evidence, summary_evidence, cache_key, state)
                except ArkContentSafetyError as exc:
                    reason = str(exc)
                    pending.append(self._mark_ai_pending(article, reason))
                    source_result.ai_pending -= 1
                    source_result.content_safety_skipped += 1
                    source_result.content_safety_items.append({"title": article.title, "url": article.url})
                    source_result.warnings.append(f"内容安全待重试 {article.url}: {reason}")
                    state.save_progress(job_id, manifest, index + 1, None, "running")
                    continue
                except (ArkRateLimitError, ArkAPIError, RuntimeError) as exc:
                    reason = f"AI服务失败：{exc}"
                    source_result.errors.append(f"{reason[:300]} {article.url}")
                    for remaining in ordered[index:]:
                        pending.append(self._mark_ai_pending(remaining, reason))
                    report.pending_articles = len(ordered) - index
                    report.checkpoint_index = index
                    state.save_progress(job_id, manifest, index, article.url, "waiting_retry")
                    complete = False
                    break
                report.ai_evaluated += 1
                source_result.ai_processed += 1
                source_result.ai_pending -= 1
                article.ai_result = result
                reason = str(result.get("reason") or "")
                if result.get("valuable"):
                    article.record_status = "已完成"
                    article.meta["failure_reason"] = ""
                    completed.append(article)
                    source_result.selected_main += 1
                elif result.get("invalid_ai") or reason.startswith("摘要"):
                    pending.append(self._mark_ai_pending(article, reason or "AI输出格式错误"))
                    source_result.warnings.append(f"AI待重试 {article.url}: {reason}")
                    if result.get("invalid_ai"):
                        source_result.invalid_ai_skipped += 1
                        source_result.invalid_ai_items.append({"title": article.title, "url": article.url})
                    report.summary_rejected += 1
                else:
                    source_result.excluded += 1
                state.save_progress(job_id, manifest, index + 1, None, "running")
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
        return self._dedupe(completed), self._dedupe(pending), complete

    def run(
        self,
        day: date,
        *,
        dry_run: bool = False,
        collect_only: bool = False,
        max_articles: int = 0,
        trigger_source: str = "manual",
        report_path: Path = Path("run-report.json"),
    ) -> RunReport:
        report = RunReport(
            day.isoformat(), trigger_source=trigger_source, dry_run=dry_run, collect_only=collect_only
        )
        feishu_enabled = bool(output_config("feishu").get("enabled", True))
        feishu = None if collect_only or dry_run or not feishu_enabled else FeishuBitable(self.http)
        report.sources = self.discover(day)
        candidates = self._candidate_articles(report.sources, feishu, day, report)
        ready, body_pending = self.fetch_bodies(report.sources, candidates)
        report.body_pending_count = len(body_pending)
        report.body_unavailable_skipped = sum(item.body_unavailable_skipped for item in report.sources.values())
        if max_articles > 0:
            ready = self._analysis_order(ready)[:max_articles]
            report.truncated_by_user = True
        if collect_only:
            report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            return report

        completed, ai_pending, _ = self.analyze(
            ready,
            report.sources,
            report,
            job_id=f"{day.isoformat()}:{'dry' if dry_run else 'daily'}:{'-'.join(sorted(self.adapters))}",
        )
        report.ai_retry_count = len(ai_pending)
        report.content_safety_skipped = sum(item.content_safety_skipped for item in report.sources.values())
        report.invalid_ai_skipped = sum(item.invalid_ai_skipped for item in report.sources.values())
        records = self._dedupe(completed + body_pending + ai_pending)
        report.selected = len(completed)
        lengths = [effective_chars(str((article.ai_result or {}).get("core_content") or "")) for article in completed]
        if lengths:
            report.summary_char_min = min(lengths)
            report.summary_char_max = max(lengths)
        write_local_outputs(records, day)

        if feishu is not None:
            url_index, title_index = feishu.record_index()
            for article in records:
                if article.record_id:
                    continue
                existing = url_index.get(normalize_url(article.url)) or title_index.get(normalize_title(article.title))
                if existing:
                    article.record_id = str(existing.get("record_id") or "")
            updates = [article for article in records if article.record_id]
            creates = [article for article in records if not article.record_id]
            report.written, write_failed = feishu.write(creates)
            report.updated, update_failed = feishu.update(updates)
            missing = feishu.verify_urls(records)
            report.write_failed = max(write_failed + update_failed, len(missing))
            report.update_failed = update_failed

        report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return report
