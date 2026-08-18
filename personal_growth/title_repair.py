from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .feishu import FeishuBitable
from .http import HttpClient
from .text import clean_text, normalize_url


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
