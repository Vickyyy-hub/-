from __future__ import annotations

import hashlib
import os
import re
from datetime import date, datetime, time
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import feedparser
from bs4 import BeautifulSoup

from .http import HttpClient
from .models import Country, Signal, SourceSpec
from .text import SHANGHAI, clean_text, html_to_text, parse_datetime


def signal_id(source_key: str, url: str, title: str) -> str:
    value = f"{source_key}|{url.strip().lower()}|{clean_text(title).lower()}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _entry_datetime(entry: Any, fallback_day: date) -> datetime:
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if not value:
            continue
        try:
            parsed = parsedate_to_datetime(str(value))
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI)).astimezone(SHANGHAI)
        except (TypeError, ValueError, OverflowError):
            try:
                return parse_datetime(str(value)).astimezone(SHANGHAI)
            except ValueError:
                pass
    return datetime.combine(fallback_day, time.min, SHANGHAI)


def _same_day(value: datetime, day: date) -> bool:
    return value.astimezone(SHANGHAI).date() == day


class Collector:
    def __init__(self, http: HttpClient, country_map: dict[str, Country]) -> None:
        self.http = http
        self.country_map = country_map

    def collect(self, spec: SourceSpec, day: date) -> tuple[list[Signal], list[str]]:
        if spec.auth_env and not os.environ.get(spec.auth_env):
            return [], [f"缺少可选授权 {spec.auth_env}，已跳过"]
        method = getattr(self, f"collect_{spec.kind.replace('-', '_')}", None)
        if method is None:
            raise ValueError(f"不支持的采集类型：{spec.kind}")
        return method(spec, day)

    def collect_rss(self, spec: SourceSpec, day: date) -> tuple[list[Signal], list[str]]:
        response = self.http.get(spec.url)
        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            raise RuntimeError(f"RSS解析失败：{feed.bozo_exception}")
        signals: list[Signal] = []
        for entry in feed.entries:
            published = _entry_datetime(entry, day)
            if not _same_day(published, day):
                continue
            title = clean_text(entry.get("title", ""))
            url = str(entry.get("link") or spec.url)
            summary = html_to_text(str(entry.get("summary") or entry.get("description") or ""))
            if not title:
                continue
            signals.append(
                Signal(
                    signal_id(spec.key, url, title), spec.key, spec.name, spec.signal_type,
                    title, url, published, list(spec.countries), evidence=summary[:4000],
                    confidence="high", meta={"collector": "rss"},
                )
            )
        return signals, []

    def collect_google_trends(self, spec: SourceSpec, day: date) -> tuple[list[Signal], list[str]]:
        signals: list[Signal] = []
        warnings: list[str] = []
        for code in spec.countries:
            country = self.country_map[code]
            url = spec.url.format(geo=country.trends_geo)
            try:
                response = self.http.get(url)
                feed = feedparser.parse(response.content)
                if feed.bozo and not feed.entries:
                    raise RuntimeError(str(feed.bozo_exception))
                for entry in feed.entries:
                    published = _entry_datetime(entry, day)
                    if not _same_day(published, day):
                        continue
                    title = clean_text(entry.get("title", ""))
                    item_url = str(entry.get("link") or url)
                    traffic = clean_text(entry.get("ht_approx_traffic", ""))
                    number = re.sub(r"[^0-9.]", "", traffic)
                    heat = float(number) if number else None
                    news = entry.get("ht_news_item") or []
                    evidence = clean_text(" ".join(
                        str(item.get("ht_news_item_title") or item.get("title") or "")
                        for item in news if isinstance(item, dict)
                    ))
                    signals.append(
                        Signal(
                            signal_id(f"{spec.key}-{code}", item_url, title),
                            spec.key, f"{spec.name}｜{country.name_zh}", spec.signal_type,
                            title, item_url, published, [code], original_language=country.language,
                            original_keyword=title, heat=heat, evidence=evidence[:4000],
                            confidence="high", meta={"traffic": traffic, "geo": country.trends_geo},
                        )
                    )
            except Exception as exc:
                warnings.append(f"{country.name_zh} Google Trends失败：{exc}")
        return signals, warnings

    def collect_federal_register(self, spec: SourceSpec, day: date) -> tuple[list[Signal], list[str]]:
        params: list[tuple[str, Any]] = [
            ("per_page", 100),
            ("order", "newest"),
            ("conditions[publication_date][is]", day.isoformat()),
        ]
        params.extend(("conditions[agencies][]", agency) for agency in spec.params.get("agencies", []))
        payload = self.http.get(spec.url, params=params).json()
        signals: list[Signal] = []
        for item in payload.get("results", []):
            published = parse_datetime(f"{item['publication_date']} 00:00")
            title = clean_text(item.get("title", ""))
            url = item.get("html_url") or item.get("pdf_url") or spec.url
            evidence = clean_text(item.get("abstract", ""))
            signals.append(
                Signal(
                    signal_id(spec.key, url, title), spec.key, spec.name, spec.signal_type,
                    title, url, published, list(spec.countries), evidence=evidence,
                    confidence="high", meta={"document_number": item.get("document_number")},
                )
            )
        return signals, []

    def collect_html(self, spec: SourceSpec, day: date) -> tuple[list[Signal], list[str]]:
        raw = self.http.get(spec.url).text
        soup = BeautifulSoup(raw, "lxml")
        signals: list[Signal] = []
        seen: set[str] = set()
        containers = soup.select("article, .views-row, .news-item, .card, li")
        for container in containers:
            link = container.select_one("a[href]")
            if link is None:
                continue
            title = clean_text(link.get_text(" ", strip=True))
            if len(title) < 8:
                continue
            url = urljoin(spec.url, link.get("href", ""))
            if not url.startswith("http") or url in seen:
                continue
            date_node = container.select_one("time[datetime], time, .date, .published, [class*='date']")
            date_text = ""
            if date_node is not None:
                date_text = str(date_node.get("datetime") or date_node.get_text(" ", strip=True))
            try:
                published = parse_datetime(date_text)
            except ValueError:
                match = re.search(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", container.get_text(" ", strip=True))
                if not match:
                    continue
                published = parse_datetime(match.group(0))
            if not _same_day(published, day):
                continue
            seen.add(url)
            evidence = clean_text(container.get_text(" ", strip=True))
            signals.append(
                Signal(
                    signal_id(spec.key, url, title), spec.key, spec.name, spec.signal_type,
                    title, url, published, list(spec.countries), evidence=evidence[:4000],
                    confidence="medium", meta={"collector": "html"},
                )
            )
        # A valid page with no entry on the target day is a normal empty result.
        # Only the complete absence of recognizable list containers indicates drift.
        warning = [] if containers else ["页面结构变化：未发现资讯列表容器"]
        return signals, warning

    def collect_reference(self, spec: SourceSpec, day: date) -> tuple[list[Signal], list[str]]:
        fallback_used = False
        try:
            raw = self.http.get(spec.url).text
        except Exception:
            raw = self.http.get(f"https://r.jina.ai/{spec.url}").text
            fallback_used = True
        soup = BeautifulSoup(raw, "lxml")
        title_node = soup.select_one("h1") or soup.select_one("title")
        title = clean_text(title_node.get_text(" ", strip=True) if title_node else spec.name)
        body = ""
        for selector in (spec.body_selector, "main", "article", "body"):
            body = html_to_text(raw, selector)
            if len(body) >= 300:
                break
        if len(body) < 100 and not fallback_used:
            raw = self.http.get(f"https://r.jina.ai/{spec.url}").text
            fallback_used = True
            marker = "Markdown Content:"
            body = clean_text(raw.split(marker, 1)[-1] if marker in raw else raw)
        if len(body) < 100:
            return [], ["参考页正文不足100字"]
        content_key = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        published = datetime.combine(day, time.min, SHANGHAI)
        item = Signal(
            signal_id(spec.key, spec.url, content_key), spec.key, spec.name, spec.signal_type,
            title or spec.name, spec.url, published, list(spec.countries), evidence=body[:12000],
            confidence="high", meta={"collector": "reference", "content_hash": content_key, "reader_fallback": fallback_used},
        )
        return [item], []

    def collect_mercadolibre(self, spec: SourceSpec, day: date) -> tuple[list[Signal], list[str]]:
        token = os.environ[spec.auth_env]
        signals: list[Signal] = []
        warnings: list[str] = []
        for code in spec.countries:
            country = self.country_map[code]
            try:
                payload = self.http.get(
                    spec.url.format(site=country.marketplace_site),
                    headers={"Authorization": f"Bearer {token}"},
                ).json()
                for item in payload:
                    keyword = clean_text(item.get("keyword", ""))
                    url = item.get("url") or spec.url.format(site=country.marketplace_site)
                    published = datetime.combine(day, time.min, SHANGHAI)
                    signals.append(
                        Signal(
                            signal_id(f"{spec.key}-{code}", url, keyword), spec.key,
                            f"{spec.name}｜{country.name_zh}", spec.signal_type, keyword, url,
                            published, [code], original_language=country.language,
                            original_keyword=keyword, confidence="high", meta={"rank": item.get("rank")},
                        )
                    )
            except Exception as exc:
                warnings.append(f"{country.name_zh} Mercado Libre失败：{exc}")
        return signals, warnings
