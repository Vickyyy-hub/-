from __future__ import annotations

import json
import re
import threading
import time
from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .http import HttpClient
from .models import Article, SourceResult
from .text import SHANGHAI, clean_text, from_epoch, html_to_text, is_obvious_exclusion, parse_datetime, target_bounds


class ExcludedArticle(ValueError):
    """The source explicitly identifies content that is outside task scope."""


class SourceAdapter(ABC):
    name: str

    def __init__(self, http: HttpClient, max_pages: int = 40) -> None:
        self.http = http
        self.max_pages = max_pages

    @abstractmethod
    def discover(self, day: date) -> SourceResult:
        raise NotImplementedError

    @abstractmethod
    def fetch_body(self, article: Article) -> str:
        raise NotImplementedError

    @staticmethod
    def _in_target(published: datetime, day: date) -> bool:
        start, end = target_bounds(day)
        return start <= published <= end


class HuxiuAdapter(SourceAdapter):
    name = "虎嗅"
    list_url = "https://article-api.huxiu.com/web/article/articleList"
    detail_url = "https://article-api.huxiu.com/web/article/detail"

    def discover(self, day: date) -> SourceResult:
        result = SourceResult(self.name)
        start, _ = target_bounds(day)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(self.max_pages):
            payload = {"platform": "www", "pagesize": "22"}
            if cursor:
                # The response calls this value last_dateline, while the current
                # web endpoint expects it back as recommend_time.
                payload["recommend_time"] = cursor
            data = self.http.post(self.list_url, data=payload).json()
            if not data.get("success"):
                raise RuntimeError(data.get("message") or "虎嗅列表接口失败")
            page = data.get("data") or {}
            items = page.get("dataList") or []
            if not items:
                break
            oldest: datetime | None = None
            for item in items:
                result.scanned += 1
                published = from_epoch(item["dateline"])
                oldest = published if oldest is None else min(oldest, published)
                if not self._in_target(published, day):
                    continue
                title = clean_text(item.get("title", ""))
                reason = is_obvious_exclusion(title)
                if item.get("is_sponsor") in (1, "1"):
                    reason = "商业赞助"
                if item.get("is_vip_column_article"):
                    reason = "VIP文章"
                article = Article(
                    source=self.name,
                    title=title,
                    url=f"https://www.huxiu.com/article/{item['aid']}.html",
                    published_at=published,
                    summary=clean_text(item.get("summary", "")),
                    meta={"aid": str(item["aid"])},
                    excluded_reason=reason,
                )
                result.articles.append(article)
                result.discovered += 1
            if oldest and oldest < start:
                result.coverage_complete = True
                break
            next_cursor = str(page.get("last_dateline") or "")
            if not next_cursor or next_cursor in seen_cursors:
                result.errors.append("虎嗅分页游标未继续推进")
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            result.errors.append("虎嗅达到最大分页数仍未越过目标日期")
        return result

    def fetch_body(self, article: Article) -> str:
        data = self.http.post(
            self.detail_url,
            data={"platform": "www", "aid": article.meta["aid"]},
        ).json()
        if not data.get("success"):
            raise RuntimeError(data.get("message") or "虎嗅正文接口失败")
        detail = data.get("data") or {}
        if str(detail.get("is_video_article", "0")) == "1":
            raise ExcludedArticle("虎嗅视频稿")
        if detail.get("is_vip_column_article") or not detail.get("is_allow_read", True):
            raise ExcludedArticle("虎嗅正文不可公开完整阅读")
        return html_to_text(detail.get("content", ""))


class SspaiAdapter(SourceAdapter):
    name = "少数派"
    list_url = "https://sspai.com/api/v1/article/index/page/get"
    detail_url = "https://sspai.com/api/v1/article/info/get"

    def discover(self, day: date) -> SourceResult:
        result = SourceResult(self.name)
        start, _ = target_bounds(day)
        offset = 0
        for _ in range(self.max_pages):
            payload = self.http.get(self.list_url, params={"limit": 50, "offset": offset}).json()
            items = payload.get("data") or []
            if not items:
                break
            oldest: datetime | None = None
            for item in items:
                result.scanned += 1
                published = from_epoch(item["released_time"])
                oldest = published if oldest is None else min(oldest, published)
                if not self._in_target(published, day):
                    continue
                title = clean_text(item.get("title", ""))
                reason = is_obvious_exclusion(title)
                if item.get("post_type") != 1:
                    reason = "非文章内容"
                if item.get("free") is False:
                    reason = "付费文章"
                author = (item.get("author") or {}).get("nickname", "")
                result.articles.append(
                    Article(
                        source=self.name,
                        title=title,
                        url=f"https://sspai.com/post/{item['id']}",
                        published_at=published,
                        summary=clean_text(item.get("summary", "")),
                        author=author,
                        meta={"id": str(item["id"])},
                        excluded_reason=reason,
                    )
                )
                result.discovered += 1
            if oldest and oldest < start:
                result.coverage_complete = True
                break
            offset += len(items)
        else:
            result.errors.append("少数派达到最大分页数仍未越过目标日期")
        return result

    def fetch_body(self, article: Article) -> str:
        payload = self.http.get(self.detail_url, params={"id": article.meta["id"]}).json()
        detail = payload.get("data") or {}
        if detail.get("free") is False:
            raise ExcludedArticle("少数派付费正文")
        return html_to_text(detail.get("body") or detail.get("body_extend") or "")


class JiemianAdapter(SourceAdapter):
    name = "界面新闻"
    channel_ids = (2, 800, 801)
    load_url = "https://a.jiemian.com/index.php"

    def _parse_article(self, url: str) -> tuple[datetime, str, str, bool]:
        raw = self.http.get(url).text
        soup = BeautifulSoup(raw, "lxml")
        title_node = soup.select_one("h1")
        title = clean_text(title_node.get_text(" ", strip=True) if title_node else "")
        is_paid = bool(soup.select_one(".vip-mask,.article-vip,.jm-mask-vip"))
        published: datetime | None = None
        node = soup.select_one("[data-article-publish-time]")
        if node:
            value = node.get("data-article-publish-time", "")
            if str(value).isdigit():
                published = from_epoch(value)
        if published is None:
            for selector, attribute in (
                ('meta[property="article:published_time"]', "content"),
                ('meta[name="publishdate"]', "content"),
            ):
                meta = soup.select_one(selector)
                if meta and meta.get(attribute):
                    try:
                        published = parse_datetime(meta[attribute])
                        break
                    except ValueError:
                        pass
        if published is None:
            match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', raw)
            if match:
                published = parse_datetime(match.group(1))
        if published is None:
            match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})[^0-9]{0,4}(\d{1,2}):(\d{2})", raw)
            if match:
                published = datetime(*map(int, match.groups()), tzinfo=SHANGHAI)
        if published is None:
            raise ValueError(f"无法解析界面发布日期：{url}")
        body = ""
        for selector in (".article-content", ".article-main", ".article-view", "article"):
            body = html_to_text(raw, selector)
            if len(body) >= 300:
                break
        return published, title, body, is_paid

    @staticmethod
    def _links(container: BeautifulSoup) -> list[str]:
        links: list[str] = []
        for node in container.select('a[href*="jiemian.com/article/"]'):
            url = node.get("href", "").split("?", 1)[0]
            if re.fullmatch(r"https://www\.jiemian\.com/article/\d+\.html", url) and url not in links:
                links.append(url)
        return links

    def discover(self, day: date) -> SourceResult:
        result = SourceResult(self.name)
        start, _ = target_bounds(day)
        seen: set[str] = set()
        all_channels_complete = True
        for cid in self.channel_ids:
            html_page = self.http.get(f"https://www.jiemian.com/lists/{cid}.html").text
            soup = BeautifulSoup(html_page, "lxml")
            more = soup.select_one(f'.channel-load-more[data-cid="{cid}"]')
            if more is None:
                result.errors.append(f"界面频道{cid}未找到加载配置")
                all_channels_complete = False
                continue
            container = more.find_previous("ul", id="load-list")
            config = {
                "tid": more.get("data-tid", ""),
                "page": int(more.get("data-page", 2)),
                "tpl": more.get("data-tpl", "full-card"),
                "cid": str(cid),
                "repeat": more.get("data-repeat", ""),
                "list_type": more.get("data-list_type", ""),
            }
            channel_complete = False
            page_links = self._links(container or soup)
            for page_index in range(self.max_pages):
                oldest: datetime | None = None
                for url in page_links:
                    if url in seen:
                        continue
                    seen.add(url)
                    result.scanned += 1
                    try:
                        published, title, body, is_paid = self._parse_article(url)
                    except Exception as exc:
                        result.errors.append(str(exc))
                        continue
                    oldest = published if oldest is None else min(oldest, published)
                    if not self._in_target(published, day):
                        continue
                    reason = is_obvious_exclusion(title)
                    if is_paid:
                        reason = "VIP或付费文章"
                    result.articles.append(
                        Article(
                            source=self.name,
                            title=title,
                            url=url,
                            published_at=published,
                            body=body,
                            excluded_reason=reason,
                            meta={"channel": cid},
                        )
                    )
                    result.discovered += 1
                if oldest and oldest < start:
                    channel_complete = True
                    break
                if page_index == self.max_pages - 1:
                    break
                params = {"m": "newLists", "a": "loadMore", **config, "callback": "cb"}
                response = self.http.get(self.load_url, params=params).text
                match = re.match(r"^cb\((.*)\)\s*;?$", response, re.S)
                if not match:
                    result.errors.append(f"界面频道{cid}分页返回不是JSONP")
                    break
                payload = json.loads(match.group(1))
                fragment = payload.get("html") or ""
                if not fragment or payload.get("hideLoadBtn") is True:
                    break
                page_links = self._links(BeautifulSoup(fragment, "lxml"))
                if not page_links:
                    break
                config["page"] = int(config["page"]) + 1
            if not channel_complete:
                all_channels_complete = False
                result.errors.append(f"界面频道{cid}未完整越过目标日期")
        result.coverage_complete = all_channels_complete
        return result

    def fetch_body(self, article: Article) -> str:
        if article.body:
            return article.body
        return self._parse_article(article.url)[2]


class ITHomeAdapter(SourceAdapter):
    name = "IT之家"

    @staticmethod
    def _parse_items(fragment: str) -> Iterable[tuple[str, str, datetime]]:
        soup = BeautifulSoup(fragment, "lxml")
        for item in soup.select("li"):
            category = item.select_one("a.c[data-ot]")
            title_node = item.select_one("a.t[href]")
            if category is None or title_node is None:
                continue
            timestamp = category.get("data-ot", "")
            try:
                published = parse_datetime(timestamp)
            except (ValueError, TypeError):
                continue
            url = urljoin("https://www.ithome.com", title_node.get("href", ""))
            title = clean_text(title_node.get_text(" ", strip=True))
            yield title, url, published

    def discover(self, day: date) -> SourceResult:
        result = SourceResult(self.name)
        start, _ = target_bounds(day)
        seen: set[str] = set()
        for page in range(1, self.max_pages + 1):
            if page == 1:
                fragment = self.http.get("https://www.ithome.com/list/").text
            else:
                payload = self.http.post(
                    "https://www.ithome.com/category/listpage",
                    params={"page": page},
                    data="",
                ).json()
                if not payload.get("success"):
                    raise RuntimeError(f"IT之家第{page}页接口失败")
                fragment = (payload.get("content") or {}).get("html", "")
            page_items = list(self._parse_items(fragment))
            if not page_items:
                break
            oldest = min(item[2] for item in page_items)
            for title, url, published in page_items:
                result.scanned += 1
                if url in seen or not self._in_target(published, day):
                    continue
                seen.add(url)
                reason = is_obvious_exclusion(title)
                if "lapin.ithome.com" in url or "辣品" in title:
                    reason = "促销导购"
                result.articles.append(
                    Article(
                        source=self.name,
                        title=title,
                        url=url,
                        published_at=published,
                        excluded_reason=reason,
                    )
                )
                result.discovered += 1
            if oldest < start:
                result.coverage_complete = True
                break
        else:
            result.errors.append("IT之家达到最大分页数仍未越过目标日期")
        return result

    def fetch_body(self, article: Article) -> str:
        raw = self.http.get(article.url).text
        for selector in (".post_content", "#paragraph", "article"):
            body = html_to_text(raw, selector)
            if len(body) >= 80:
                return body
        return ""


class TMTPostAdapter(SourceAdapter):
    name = "钛媒体"
    list_url = "https://api.tmtpost.com/v1/lists/new"
    api_headers = {
        "device": "pc",
        "app-version": "web1.0",
        "app-key": "2015042403",
        "app-secret": "F3x47g39Wc4M96nwA28T",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    def discover(self, day: date) -> SourceResult:
        result = SourceResult(self.name)
        start, _ = target_bounds(day)
        offset = 0
        for _ in range(self.max_pages):
            params = {
                "limit": 30,
                "offset": offset,
                "subtype": "post",
                "post_fields": (
                    "access;authors;number_of_reads;is_pro_post;"
                    "is_paid_special_column_post;available;"
                ),
            }
            payload = self.http.get(self.list_url, params=params, headers=self.api_headers).json()
            if payload.get("result") != "ok":
                raise RuntimeError(f"钛媒体列表接口失败：{payload.get('errors')}")
            items = payload.get("data") or []
            if not items:
                break
            oldest: datetime | None = None
            for item in items:
                result.scanned += 1
                published = from_epoch(item["time_published"])
                oldest = published if oldest is None else min(oldest, published)
                if not self._in_target(published, day):
                    continue
                title = clean_text(item.get("title", ""))
                reason = is_obvious_exclusion(title)
                if item.get("item_type") != "post":
                    reason = "非正式文章"
                if item.get("is_pro_post") or item.get("is_paid_special_column_post"):
                    reason = "PRO或付费文章"
                if item.get("available") is False:
                    reason = "正文不可用"
                if str(item.get("is_popularize", "0")) == "1":
                    reason = "推广文章"
                authors = item.get("authors") or []
                result.articles.append(
                    Article(
                        source=self.name,
                        title=title,
                        url=item.get("short_url") or f"https://www.tmtpost.com/{item['guid']}.html",
                        published_at=published,
                        summary=clean_text(item.get("summary", "")),
                        author=authors[0].get("username", "") if authors else "",
                        excluded_reason=reason,
                    )
                )
                result.discovered += 1
            if oldest and oldest < start:
                result.coverage_complete = True
                break
            offset += len(items)
        else:
            result.errors.append("钛媒体达到最大分页数仍未越过目标日期")
        return result

    def fetch_body(self, article: Article) -> str:
        raw = self.http.get(article.url).text
        for selector in (".article-content", ".post-content", ".article_detail", "article"):
            body = html_to_text(raw, selector)
            if len(body) >= 250:
                return body
        return ""


class Kr36Adapter(SourceAdapter):
    name = "36氪"
    list_url = "https://gateway.36kr.com/api/mis/nav/ifm/subNav/flow"

    def __init__(self, http: HttpClient, max_pages: int = 40) -> None:
        super().__init__(http, max_pages)
        self.body_lock = threading.Lock()
        self.last_body_request = 0.0

    def discover(self, day: date) -> SourceResult:
        result = SourceResult(self.name)
        start, _ = target_bounds(day)
        callback = ""
        for page in range(self.max_pages):
            params = {
                "subnavType": 1,
                "subnavNick": "web_news",
                "pageSize": 50,
                "pageEvent": 0 if page == 0 else 1,
                "pageCallback": callback,
                "siteId": 1,
                "platformId": 2,
            }
            payload = self.http.post(
                self.list_url,
                json={"partner_id": "web", "timestamp": int(time.time() * 1000), "param": params},
                headers={
                    "Origin": "https://36kr.com",
                    "Referer": "https://36kr.com/",
                    "Content-Type": "application/json",
                },
            ).json()
            if payload.get("code") != 0:
                raise RuntimeError(f"36氪列表接口失败：{payload.get('msg')}")
            data = payload.get("data") or {}
            items = data.get("itemList") or []
            if not items:
                break
            oldest: datetime | None = None
            for wrapper in items:
                item = wrapper.get("templateMaterial") or {}
                if not item.get("publishTime") or not item.get("widgetTitle"):
                    continue
                result.scanned += 1
                published = from_epoch(item["publishTime"])
                oldest = published if oldest is None else min(oldest, published)
                if not self._in_target(published, day):
                    continue
                item_id = item.get("itemId") or wrapper.get("itemId")
                title = clean_text(item.get("widgetTitle", ""))
                result.articles.append(
                    Article(
                        source=self.name,
                        title=title,
                        url=f"https://36kr.com/p/{item_id}",
                        published_at=published,
                        summary=clean_text(item.get("summary", "")),
                        author=clean_text(item.get("authorName", "")),
                        excluded_reason=is_obvious_exclusion(title),
                    )
                )
                result.discovered += 1
            if oldest and oldest < start:
                result.coverage_complete = True
                break
            callback = str(data.get("pageCallback") or "")
            if data.get("hasNextPage") != 1 or not callback:
                break
        else:
            result.errors.append("36氪达到最大分页数仍未越过目标日期")
        return result

    def fetch_body(self, article: Article) -> str:
        with self.body_lock:
            delay = 1.5 - (time.monotonic() - self.last_body_request)
            if delay > 0:
                time.sleep(delay)
            raw = self.http.get(f"https://r.jina.ai/{article.url}").text
            self.last_body_request = time.monotonic()
        marker = "Markdown Content:"
        if marker in raw:
            raw = raw.split(marker, 1)[1]
        raw = re.sub(r"!\[[^\]]*\]\([^\)]+\)", "", raw)
        return clean_text(raw)


ADAPTERS = {
    "huxiu": HuxiuAdapter,
    "sspai": SspaiAdapter,
    "jiemian": JiemianAdapter,
    "ithome": ITHomeAdapter,
    "tmtpost": TMTPostAdapter,
    "36kr": Kr36Adapter,
}
