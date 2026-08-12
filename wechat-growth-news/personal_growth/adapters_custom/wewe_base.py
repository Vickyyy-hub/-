from __future__ import annotations

import os
import threading
from datetime import date
from typing import Any

from personal_growth.adapters import SourceAdapter
from personal_growth.models import Article, SourceResult
from personal_growth.text import clean_text, html_to_text, is_obvious_exclusion, normalize_url, parse_datetime, target_bounds


BLOCKED_MARKERS = ("环境异常", "完成验证", "访问过于频繁", "当前环境异常")


class WeweRssAdapter(SourceAdapter):
    name = ""
    mp_id = ""
    feed_limit = 500

    def __init__(self, http, max_pages: int = 1) -> None:
        super().__init__(http, max_pages=max_pages)
        root = os.environ.get("WEWE_RSS_BASE_URL", "").rstrip("/")
        if not root:
            raise RuntimeError("缺少 WEWE_RSS_BASE_URL")
        self.feed_url = f"{root}/feeds/{self.mp_id}.json"
        self._items: list[dict[str, Any]] = []
        self._fulltext: dict[str, str] = {}
        self._fulltext_loaded_to = 0
        self._fulltext_lock = threading.Lock()

    @staticmethod
    def _items_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
        items = payload.get("items")
        if not isinstance(items, list):
            raise RuntimeError("WeWeRSS JSON缺少items列表")
        return [item for item in items if isinstance(item, dict)]

    def discover(self, day: date) -> SourceResult:
        result = SourceResult(self.name)
        payload = self.http.get(self.feed_url, params={"limit": self.feed_limit}).json()
        self._items = self._items_from_payload(payload)
        start, _ = target_bounds(day)
        oldest = None
        for position, item in enumerate(self._items):
            result.scanned += 1
            raw_time = str(item.get("date_modified") or "")
            if not raw_time:
                result.errors.append(f"发布时间缺失：{item.get('url') or item.get('id') or position}")
                continue
            try:
                published = parse_datetime(raw_time)
            except (TypeError, ValueError) as exc:
                result.errors.append(f"发布时间格式错误：{raw_time} ({exc})")
                continue
            oldest = published if oldest is None else min(oldest, published)
            if not self._in_target(published, day):
                continue
            title = clean_text(str(item.get("title") or ""))
            url = str(item.get("url") or "").strip()
            if not title or not url:
                result.errors.append(f"文章元数据不完整：{item.get('id') or position}")
                continue
            result.articles.append(
                Article(
                    source=self.name,
                    title=title,
                    url=url,
                    published_at=published,
                    meta={"mp_id": self.mp_id, "feed_position": position, "image": str(item.get("image") or "")},
                    excluded_reason=is_obvious_exclusion(title),
                )
            )
            result.discovered += 1

        result.coverage_complete = len(self._items) < self.feed_limit or bool(oldest and oldest < start)
        if not result.coverage_complete:
            result.errors.append(f"达到{self.feed_limit}篇上限仍未越过目标日期")
        return result

    def _position_for(self, url: str) -> int:
        normalized = normalize_url(url)
        for position, item in enumerate(self._items):
            if normalize_url(str(item.get("url") or "")) == normalized:
                return position
        return -1

    def _load_fulltext(self, required_position: int) -> None:
        if required_position < self._fulltext_loaded_to:
            return
        limit = min(self.feed_limit, max(10, required_position + 1))
        with self._fulltext_lock:
            if required_position < self._fulltext_loaded_to:
                return
            payload = self.http.get(self.feed_url, params={"limit": limit, "mode": "fulltext"}).json()
            items = self._items_from_payload(payload)
            for item in items:
                raw_url = str(item.get("url") or "").strip()
                if raw_url:
                    self._fulltext[normalize_url(raw_url)] = str(item.get("content_html") or "")
            self._fulltext_loaded_to = max(self._fulltext_loaded_to, len(items))

    def fetch_body(self, article: Article) -> str:
        position = int(article.meta.get("feed_position", -1))
        if position < 0:
            position = self._position_for(article.url)
        if position < 0:
            raise RuntimeError("文章已不在WeWeRSS最近500篇范围内")
        self._load_fulltext(position)
        raw = self._fulltext.get(normalize_url(article.url), "")
        body = html_to_text(raw)
        if not body or any(marker in body for marker in BLOCKED_MARKERS):
            raise RuntimeError("WeWeRSS全文为空或被微信验证拦截")
        return body
