from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class Country:
    code: str
    name_zh: str
    region: str
    language: str
    currency: str
    timezone: str
    trends_geo: str
    marketplace_site: str = ""


@dataclass(frozen=True)
class SourceSpec:
    key: str
    name: str
    url: str
    kind: str
    cadence: str
    signal_type: str
    countries: tuple[str, ...] = ()
    enabled: bool = True
    optional: bool = False
    body_selector: str = "article, main"
    notes: str = ""
    auth_env: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Signal:
    signal_id: str
    source_key: str
    source_name: str
    signal_type: str
    title: str
    url: str
    published_at: datetime
    countries: list[str]
    original_language: str = ""
    original_keyword: str = ""
    keyword_zh: str = ""
    summary_zh: str = ""
    ai_conclusion: str = ""
    topics: list[str] = field(default_factory=list)
    heat: float | None = None
    evidence: str = ""
    confidence: str = "medium"
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["published_at"] = self.published_at.isoformat()
        return data


@dataclass
class CountryProfile:
    country_code: str
    country_name: str
    region: str
    language: str
    currency: str
    timezone: str
    consumption_structure: str = ""
    ecommerce_channels: str = ""
    payment_preferences: str = ""
    price_and_promotion: str = ""
    delivery_and_returns: str = ""
    decision_factors: str = ""
    festivals_and_seasonality: str = ""
    culture_notes: str = ""
    evidence_urls: list[str] = field(default_factory=list)
    updated_at: datetime | None = None
    confidence: str = "low"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["updated_at"] = self.updated_at.isoformat() if self.updated_at else ""
        return data


@dataclass
class ProductDirection:
    direction_id: str
    country_code: str
    country_name: str
    product_direction: str
    target_user: str
    use_case: str
    evidence_count: int
    source_types: list[str]
    trend_7d: str
    trend_30d: str
    pain_points: str
    preferences: str
    competition: str
    logistics_risk: str
    compliance_risk: str
    recommendation: str
    rationale: str
    confidence: str
    evidence_signal_ids: list[str]
    generated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["generated_at"] = self.generated_at.isoformat()
        return data


@dataclass
class MarketRunReport:
    job: str
    target_date: date
    collected: int = 0
    stored: int = 0
    written: int = 0
    updated: int = 0
    skipped_duplicates: int = 0
    write_failed: int = 0
    profiles: int = 0
    directions: int = 0
    ai_calls: int = 0
    cache_hits: int = 0
    rate_limits: int = 0
    retry_wait_seconds: float = 0.0
    pending_articles: int = 0
    content_safety_skipped: int = 0
    body_unavailable_skipped: int = 0
    invalid_ai_skipped: int = 0
    summary_rejected: int = 0
    progressive_write_batches: int = 0
    progressive_written: int = 0
    progressive_updated: int = 0
    first_write_at: str = ""
    last_write_at: str = ""
    coverage_complete: bool = False
    final_output_verified: bool = False
    completion_marker: bool = False
    partial: bool = False
    dry_run: bool = False
    collect_only: bool = False
    source_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["target_date"] = self.target_date.isoformat()
        return data
