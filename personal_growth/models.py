from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Article:
    source: str
    title: str
    url: str
    published_at: datetime
    summary: str = ""
    body: str = ""
    author: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    excluded_reason: str = ""
    ai_result: dict[str, Any] | None = None


@dataclass
class SourceResult:
    source: str
    scanned: int = 0
    discovered: int = 0
    body_success: int = 0
    body_failed: int = 0
    excluded: int = 0
    coverage_complete: bool = False
    articles: list[Article] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    target_date: str
    sources: dict[str, SourceResult] = field(default_factory=dict)
    ai_evaluated: int = 0
    selected: int = 0
    event_duplicates: int = 0
    existing_duplicates: int = 0
    written: int = 0
    write_failed: int = 0
    dry_run: bool = False
    collect_only: bool = False

    @property
    def partial(self) -> bool:
        return any(
            (not result.coverage_complete)
            or result.body_failed > 0
            or bool(result.errors)
            for result in self.sources.values()
        ) or self.write_failed > 0

    def to_dict(self) -> dict[str, Any]:
        source_data: dict[str, Any] = {}
        for name, result in self.sources.items():
            source_data[name] = {
                "scanned": result.scanned,
                "discovered": result.discovered,
                "body_success": result.body_success,
                "body_failed": result.body_failed,
                "excluded": result.excluded,
                "coverage_complete": result.coverage_complete,
                "errors": result.errors,
            }
        return {
            "target_date": self.target_date,
            "partial": self.partial,
            "dry_run": self.dry_run,
            "collect_only": self.collect_only,
            "sources": source_data,
            "ai_evaluated": self.ai_evaluated,
            "selected": self.selected,
            "event_duplicates": self.event_duplicates,
            "existing_duplicates": self.existing_duplicates,
            "written": self.written,
            "write_failed": self.write_failed,
        }
