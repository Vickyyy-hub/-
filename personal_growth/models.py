from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class Article:
    source: str
    title: str
    url: str
    published_at: datetime
    daily_date: date | None = None
    summary: str = ""
    body: str = ""
    author: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    excluded_reason: str = ""
    ai_result: dict[str, Any] | None = None
    record_status: str = ""
    record_id: str = ""


@dataclass
class SourceResult:
    source: str
    scanned: int = 0
    discovered: int = 0
    body_success: int = 0
    body_failed: int = 0
    excluded: int = 0
    ai_processed: int = 0
    ai_pending: int = 0
    cache_hits: int = 0
    selected_main: int = 0
    selected_incremental: int = 0
    event_duplicates: int = 0
    invalid_ai_skipped: int = 0
    invalid_ai_items: list[dict[str, str]] = field(default_factory=list)
    content_safety_skipped: int = 0
    content_safety_items: list[dict[str, str]] = field(default_factory=list)
    body_unavailable_skipped: int = 0
    body_unavailable_items: list[dict[str, str]] = field(default_factory=list)
    coverage_complete: bool = False
    articles: list[Article] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    target_date: str
    trigger_source: str = "manual"
    sources: dict[str, SourceResult] = field(default_factory=dict)
    ai_evaluated: int = 0
    selected: int = 0
    event_duplicates: int = 0
    existing_duplicates: int = 0
    written: int = 0
    write_failed: int = 0
    dry_run: bool = False
    collect_only: bool = False
    backfill: bool = False
    updated: int = 0
    update_failed: int = 0
    unchanged: int = 0
    filter_calls: int = 0
    summary_calls: int = 0
    cache_hits: int = 0
    rate_limit_count: int = 0
    retry_wait_seconds: float = 0.0
    pending_articles: int = 0
    checkpoint_index: int = 0
    summary_char_min: int = 0
    summary_char_max: int = 0
    summary_rejected: int = 0
    cached_only: bool = False
    truncated_by_user: bool = False
    cache_candidates: int = 0
    cache_misses: int = 0
    invalid_ai_skipped: int = 0
    content_safety_skipped: int = 0
    body_unavailable_skipped: int = 0
    progressive_write_batches: int = 0
    progressive_written: int = 0
    progressive_updated: int = 0
    first_write_at: str = ""
    last_write_at: str = ""
    final_output_verified: bool = False

    @property
    def partial(self) -> bool:
        if self.cached_only:
            return self.write_failed > 0
        return any(
            (not result.coverage_complete)
            or result.body_failed > 0
            or bool(result.errors)
            for result in self.sources.values()
        ) or self.write_failed > 0 or self.update_failed > 0

    def to_dict(self) -> dict[str, Any]:
        source_data: dict[str, Any] = {}
        for name, result in self.sources.items():
            source_data[name] = {
                "scanned": result.scanned,
                "discovered": result.discovered,
                "body_success": result.body_success,
                "body_failed": result.body_failed,
                "excluded": result.excluded,
                "ai_processed": result.ai_processed,
                "ai_pending": result.ai_pending,
                "cache_hits": result.cache_hits,
                "selected_main": result.selected_main,
                "selected_incremental": result.selected_incremental,
                "event_duplicates": result.event_duplicates,
                "invalid_ai_skipped": result.invalid_ai_skipped,
                "invalid_ai_items": result.invalid_ai_items,
                "content_safety_skipped": result.content_safety_skipped,
                "content_safety_items": result.content_safety_items,
                "body_unavailable_skipped": result.body_unavailable_skipped,
                "body_unavailable_items": result.body_unavailable_items,
                "coverage_complete": result.coverage_complete,
                "errors": result.errors,
                "warnings": result.warnings,
            }
        return {
            "target_date": self.target_date,
            "trigger_source": self.trigger_source,
            "partial": self.partial,
            "dry_run": self.dry_run,
            "collect_only": self.collect_only,
            "backfill": self.backfill,
            "cached_only": self.cached_only,
            "truncated_by_user": self.truncated_by_user,
            "sources": source_data,
            "ai_evaluated": self.ai_evaluated,
            "selected": self.selected,
            "event_duplicates": self.event_duplicates,
            "existing_duplicates": self.existing_duplicates,
            "written": self.written,
            "write_failed": self.write_failed,
            "updated": self.updated,
            "update_failed": self.update_failed,
            "unchanged": self.unchanged,
            "filter_calls": self.filter_calls,
            "summary_calls": self.summary_calls,
            "cache_hits": self.cache_hits,
            "rate_limit_count": self.rate_limit_count,
            "retry_wait_seconds": round(self.retry_wait_seconds, 1),
            "pending_articles": self.pending_articles,
            "checkpoint_index": self.checkpoint_index,
            "summary_char_min": self.summary_char_min,
            "summary_char_max": self.summary_char_max,
            "summary_rejected": self.summary_rejected,
            "cache_candidates": self.cache_candidates,
            "cache_misses": self.cache_misses,
            "invalid_ai_skipped": self.invalid_ai_skipped,
            "content_safety_skipped": self.content_safety_skipped,
            "body_unavailable_skipped": self.body_unavailable_skipped,
            "progressive_write_batches": self.progressive_write_batches,
            "progressive_written": self.progressive_written,
            "progressive_updated": self.progressive_updated,
            "first_write_at": self.first_write_at,
            "last_write_at": self.last_write_at,
            "final_output_verified": self.final_output_verified,
        }
