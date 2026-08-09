from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Iterable

from .adapters import ADAPTERS, ExcludedArticle, SourceAdapter
from .ai import ArkAnalyzer
from .feishu import FeishuBitable
from .http import HttpClient
from .models import Article, RunReport, SourceResult
from .text import clean_text, normalize_title, normalize_url, similarity


class Pipeline:
    def __init__(self, source_keys: list[str]) -> None:
        self.http = HttpClient(timeout=int(os.environ.get("HTTP_TIMEOUT", "35")))
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
                if len(body) < 250:
                    raise ValueError(f"正文过短（{len(body)}字）")
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
                    result.body_failed += 1
                    result.errors.append(f"正文失败 {article.url}: {error}")
                else:
                    article.body = body
                    result.body_success += 1
                    completed.append(article)
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

    @staticmethod
    def _event_dedupe(articles: list[Article]) -> tuple[list[Article], int]:
        selected: list[Article] = []
        duplicates = 0
        for article in sorted(articles, key=lambda item: len(item.body), reverse=True):
            event = normalize_title(str((article.ai_result or {}).get("event_key", "")))
            duplicate = False
            for kept in selected:
                kept_event = normalize_title(str((kept.ai_result or {}).get("event_key", "")))
                if event and kept_event and (event == kept_event or similarity(event, kept_event) >= 0.86):
                    duplicate = True
                    break
                if similarity(article.title, kept.title) >= 0.90:
                    duplicate = True
                    break
            if duplicate:
                duplicates += 1
            else:
                selected.append(article)
        selected.sort(key=lambda item: item.published_at, reverse=True)
        return selected, duplicates

    def analyze(self, articles: list[Article], report: RunReport) -> list[Article]:
        analyzer = ArkAnalyzer(self.http)

        def evaluate(article: Article) -> tuple[Article, dict | None, str]:
            try:
                return article, analyzer.analyze(article), ""
            except Exception as exc:
                return article, None, str(exc)

        valuable: list[Article] = []
        workers = min(int(os.environ.get("AI_WORKERS", "4")), max(1, len(articles)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(evaluate, article) for article in articles]
            for future in as_completed(futures):
                article, result, error = future.result()
                if error:
                    source_result = next(value for value in report.sources.values() if value.source == article.source)
                    source_result.errors.append(f"AI失败 {article.url}: {error}")
                    continue
                report.ai_evaluated += 1
                article.ai_result = result
                if result and result.get("valuable"):
                    valuable.append(article)
        return valuable

    def run(
        self,
        day: date,
        *,
        dry_run: bool = False,
        collect_only: bool = False,
        report_path: Path = Path("run-report.json"),
    ) -> RunReport:
        report = RunReport(day.isoformat(), dry_run=dry_run, collect_only=collect_only)
        report.sources = self.discover(day)
        articles = self._pre_dedupe(self.fetch_bodies(report.sources))
        if not collect_only:
            valuable = self.analyze(articles, report)
            selected, report.event_duplicates = self._event_dedupe(valuable)
            report.selected = len(selected)
            if not dry_run:
                feishu = FeishuBitable(self.http)
                existing_urls, existing_titles = feishu.existing_keys()
                new_articles = [
                    article
                    for article in selected
                    if normalize_url(article.url) not in existing_urls
                    and normalize_title(article.title) not in existing_titles
                ]
                report.existing_duplicates = len(selected) - len(new_articles)
                feishu.write(new_articles)
                missing = feishu.verify_urls(new_articles)
                report.write_failed = len(missing)
                report.written = len(new_articles) - len(missing)
        report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return report
