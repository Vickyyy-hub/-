from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .adapters import Kr36Adapter
from .feishu import FeishuBitable
from .http import HttpClient
from .text import SHANGHAI, clean_text, normalize_url


INVALID_PAGE_TITLES = (
    "安全验证",
    "访问验证",
    "无法访问",
    "页面不存在",
    "404",
    "just a moment",
)


def _clean_page_title(value: str) -> str:
    title = clean_text(value).splitlines()[0] if clean_text(value) else ""
    title = re.sub(
        r"\s*[-_|｜]\s*(?:虎嗅(?:网)?|少数派|界面新闻|IT之家|钛媒体|36氪).*$",
        "",
        title,
        flags=re.I,
    ).strip()
    if not title or any(marker in title.lower() for marker in INVALID_PAGE_TITLES):
        return ""
    return title[:1000]


def _html_title(raw: str) -> str:
    soup = BeautifulSoup(raw or "", "lxml")
    for selector, attribute in (
        ('meta[property="og:title"]', "content"),
        ('meta[name="twitter:title"]', "content"),
        ('meta[name="title"]', "content"),
    ):
        node = soup.select_one(selector)
        if node and node.get(attribute):
            title = _clean_page_title(str(node.get(attribute)))
            if title:
                return title
    heading = soup.select_one("h1")
    if heading:
        title = _clean_page_title(heading.get_text(" ", strip=True))
        if title:
            return title
    if soup.title:
        return _clean_page_title(soup.title.get_text(" ", strip=True))
    return ""


def resolve_original_title(url: str) -> str:
    """Resolve an original title without model calls or summary inference."""
    client = HttpClient(timeout=35, min_delay=0)
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    path = urlsplit(url).path
    try:
        if host == "huxiu.com":
            match = re.search(r"/article/(\d+)\.html", path)
            if match:
                payload = client.post(
                    "https://article-api.huxiu.com/web/article/detail",
                    data={"platform": "www", "aid": match.group(1)},
                ).json()
                title = _clean_page_title(str((payload.get("data") or {}).get("title") or ""))
                if title:
                    return title
        elif host == "sspai.com":
            match = re.search(r"/post/(\d+)", path)
            if match:
                payload = client.get(
                    "https://sspai.com/api/v1/article/info/get",
                    params={"id": match.group(1)},
                ).json()
                title = _clean_page_title(str((payload.get("data") or {}).get("title") or ""))
                if title:
                    return title
        elif host == "36kr.com":
            raw = client.get(f"https://r.jina.ai/{url}").text
            match = re.search(r"^Title:\s*(.+)$", raw, flags=re.M)
            if match:
                title = _clean_page_title(match.group(1))
                if title:
                    return title
        return _html_title(client.get(url).text)
    except Exception:
        return ""


def repair_all_missing_titles(*, workers: int = 6) -> dict[str, Any]:
    """Repair every URL-backed blank title in Feishu, in place and without AI."""
    feishu = FeishuBitable(HttpClient())
    records = feishu.missing_title_records()
    urls = {
        FeishuBitable._extract_text((record.get("fields") or {}).get("来源网站")).strip()
        for record in records
    }
    urls.discard("")
    titles: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(resolve_original_title, url): url for url in sorted(urls)}
        for future in as_completed(futures):
            url = futures[future]
            title = future.result()
            if title:
                titles[normalize_url(url)] = title
    result = feishu.repair_missing_titles(titles)
    result["looked_up"] = len(urls)
    result["title_matches"] = len(titles)
    return result


def _record_day(fields: dict[str, Any]) -> date | None:
    for name in ("日报日期", "发布日期"):
        raw = fields.get(name)
        try:
            if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
                return datetime.fromtimestamp(int(raw) / 1000, tz=SHANGHAI).date()
            if isinstance(raw, str) and raw:
                return date.fromisoformat(raw[:10])
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    return None


def resolve_36kr_titles_from_lists(records: list[dict[str, Any]]) -> tuple[dict[str, str], list[str]]:
    """Resolve 36kr titles from its dated list API, never from summaries."""
    days: set[date] = set()
    candidate_urls: set[str] = set()
    for record in records:
        fields = record.get("fields") or {}
        url = FeishuBitable._extract_text(fields.get("来源网站")).strip()
        if "36kr.com/" not in url:
            continue
        day = _record_day(fields)
        if day:
            days.add(day)
            candidate_urls.add(normalize_url(url))
    titles: dict[str, str] = {}
    errors: list[str] = []
    adapter = Kr36Adapter(HttpClient(timeout=35, min_delay=0), max_pages=60)
    for target_day in sorted(days):
        try:
            result = adapter.discover(target_day)
        except Exception as exc:
            errors.append(f"{target_day.isoformat()}: {exc}")
            continue
        if not result.coverage_complete:
            errors.append(f"{target_day.isoformat()}: 36氪列表未完整越过日期边界")
        for article in result.articles:
            key = normalize_url(article.url)
            if key in candidate_urls and clean_text(article.title):
                titles[key] = clean_text(article.title)
    return titles, errors


def migrate_titles_to_primary() -> dict[str, Any]:
    """Move recovered titles into the actual Feishu primary field without AI calls."""
    feishu = FeishuBitable(HttpClient(), allow_title_migration=True)
    schema = feishu.prepare_primary_title_migration()
    backup_name = str(schema.get("backup_field_name") or "")
    records = feishu.list_records()
    blank_36kr = [
        record for record in records
        if "36kr.com/" in FeishuBitable._extract_text((record.get("fields") or {}).get("来源网站"))
        and not clean_text(FeishuBitable._extract_text((record.get("fields") or {}).get("标题")))
        and not (
            backup_name
            and clean_text(FeishuBitable._extract_text((record.get("fields") or {}).get(backup_name)))
        )
    ]
    titles_36kr, errors = resolve_36kr_titles_from_lists(blank_36kr)
    result = feishu.migrate_titles_to_primary(
        backup_field_name=backup_name,
        titles_by_url=titles_36kr,
    )
    result.update(schema)
    result["36kr_candidates"] = len(blank_36kr)
    result["36kr_title_matches"] = len(titles_36kr)
    result["36kr_list_errors"] = errors
    result["title_source_field_retained"] = bool(backup_name)
    return result
