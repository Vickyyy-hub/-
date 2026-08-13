from __future__ import annotations

import os
import re
import time as clock
from collections import Counter
from datetime import date, datetime, time
from typing import Any, Iterable

from .http import HttpClient
from .models import Article
from .text import SHANGHAI, normalize_title, normalize_url


class FeishuBitable:
    base_url = "https://open.feishu.cn/open-apis"
    token_refresh_margin_seconds = 600
    batch_size = 50
    token_error_codes = {99991661, 99991663, 99991668}
    required_fields = {
        "标题": {"field_name": "标题", "type": 1},
        "来源名称": {"field_name": "来源名称", "type": 3},
        "来源网站": {"field_name": "来源网站", "type": 15},
        "封面": {"field_name": "封面", "type": 15},
        "发布日期": {"field_name": "发布日期", "type": 5, "property": {"date_formatter": "yyyy-MM-dd HH:mm"}},
        "日报日期": {"field_name": "日报日期", "type": 5, "property": {"date_formatter": "yyyy-MM-dd"}},
        "内容主题": {"field_name": "内容主题", "type": 4},
        "归档板块": {"field_name": "归档板块", "type": 3},
        "记录状态": {"field_name": "记录状态", "type": 3},
        "核心干货": {"field_name": "核心干货", "type": 1},
        "失败原因": {"field_name": "失败原因", "type": 1},
    }

    def __init__(self, http: HttpClient, *, read_only: bool = False) -> None:
        self.http = http
        self.app_id = os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        self.app_token = os.environ.get("FEISHU_APP_TOKEN", "")
        self.table_id = os.environ.get("FEISHU_WECHAT_TABLE_ID") or os.environ.get("FEISHU_TABLE_ID", "")
        if not self.app_id or not self.app_secret:
            raise RuntimeError("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET")
        if not self.app_token:
            raise RuntimeError("缺少 FEISHU_APP_TOKEN")
        if not self.table_id:
            raise RuntimeError("缺少 FEISHU_WECHAT_TABLE_ID")
        self.token = ""
        self.token_expires_at = 0.0
        self.headers = {"Content-Type": "application/json"}
        self._refresh_token()
        self.field_types = self._field_types()
        if read_only:
            self._validate_required_fields()
        else:
            self.ensure_fields()

    @classmethod
    def _admin(cls, http: HttpClient) -> "FeishuBitable":
        client = object.__new__(cls)
        client.http = http
        client.app_id = os.environ.get("FEISHU_APP_ID", "")
        client.app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        client.app_token = os.environ.get("FEISHU_APP_TOKEN", "")
        client.table_id = ""
        if not client.app_id or not client.app_secret or not client.app_token:
            raise RuntimeError("创建飞书表需要 FEISHU_APP_ID、FEISHU_APP_SECRET 和 FEISHU_APP_TOKEN")
        client.token = ""
        client.token_expires_at = 0.0
        client.headers = {"Content-Type": "application/json"}
        client._refresh_token()
        client.field_types = {}
        return client

    @classmethod
    def bootstrap_table(cls, http: HttpClient, name: str = "公众号每日资讯") -> dict[str, Any]:
        client = cls._admin(http)
        path = f"/bitable/v1/apps/{client.app_token}/tables"
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = client._api("GET", path, params=params)["data"]
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
        existing = next((item for item in items if item.get("name") == name), None)
        created = False
        if existing:
            table_id = str(existing.get("table_id") or "")
        else:
            payload = client._api(
                "POST",
                path,
                json={
                    "table": {
                        "name": name,
                        "default_view_name": "全部文章",
                        "fields": list(cls.required_fields.values()),
                    }
                },
            )
            table_id = str((payload.get("data") or {}).get("table_id") or "")
            created = True
        if not table_id:
            raise RuntimeError("飞书创建或查找数据表后未返回table_id")
        client.table_id = table_id
        client.field_types = client._field_types()
        client.ensure_fields()
        return {"table_id": table_id, "table_name": name, "created": created, "fields": sorted(client.field_types)}

    def _tenant_token(self) -> str:
        payload = self.http.post(
            f"{self.base_url}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        ).json()
        if payload.get("code") != 0:
            raise RuntimeError(f"飞书授权失败：code={payload.get('code')} msg={payload.get('msg')}")
        self.token_expires_at = clock.monotonic() + max(1, int(payload.get("expire", 7200)))
        return payload["tenant_access_token"]

    def _refresh_token(self) -> None:
        self.token = self._tenant_token()
        self.headers["Authorization"] = f"Bearer {self.token}"

    def _ensure_token(self) -> None:
        if clock.monotonic() >= self.token_expires_at - self.token_refresh_margin_seconds:
            self._refresh_token()

    @classmethod
    def _is_token_error(cls, status: int, payload: dict[str, Any]) -> bool:
        code = payload.get("code")
        message = str(payload.get("msg") or payload.get("message") or "").lower()
        return status == 401 or code in cls.token_error_codes or (
            "token" in message and any(word in message for word in ("expired", "invalid", "unauthorized"))
        )

    @staticmethod
    def _response_detail(response: Any, payload: dict[str, Any]) -> str:
        request_id = ""
        if response is not None:
            request_id = str(response.headers.get("x-tt-logid") or response.headers.get("x-request-id") or "")
        detail = {
            "http_status": getattr(response, "status_code", None),
            "code": payload.get("code"),
            "msg": payload.get("msg") or payload.get("message"),
            "request_id": request_id or None,
        }
        return str({key: value for key, value in detail.items() if value is not None})

    def _api(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(2):
            self._ensure_token()
            response = None
            payload: dict[str, Any] = {}
            try:
                response = self.http.request(method, f"{self.base_url}{path}", headers=self.headers, **kwargs)
                payload = response.json()
            except Exception as exc:
                response = getattr(exc, "response", response)
                if response is not None:
                    try:
                        payload = response.json()
                    except (TypeError, ValueError):
                        payload = {"msg": str(getattr(response, "text", ""))[:500]}
                status = int(getattr(response, "status_code", 0) or 0)
                if attempt == 0 and self._is_token_error(status, payload):
                    self._refresh_token()
                    continue
                raise RuntimeError(f"飞书API HTTP失败：{self._response_detail(response, payload)}") from exc
            status = int(getattr(response, "status_code", 200) or 200)
            if payload.get("code") == 0:
                return payload
            if attempt == 0 and self._is_token_error(status, payload):
                self._refresh_token()
                continue
            raise RuntimeError(f"飞书API失败：{self._response_detail(response, payload)}")
        raise RuntimeError("飞书API失败：凭证刷新后仍无法调用")

    def _field_types(self) -> dict[str, int]:
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields"
        payload = self._api("GET", path, params={"page_size": 100})
        return {item["field_name"]: int(item["type"]) for item in payload["data"].get("items", [])}

    def ensure_fields(self) -> None:
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields"
        for name, spec in self.required_fields.items():
            if name not in self.field_types:
                self._api("POST", path, json=spec)
        self.field_types = self._field_types()
        self._validate_required_fields()

    def _validate_required_fields(self) -> None:
        missing = sorted(set(self.required_fields) - set(self.field_types))
        if missing:
            raise RuntimeError(f"飞书缺少字段：{missing}")
        wrong = {
            name: (self.field_types.get(name), spec["type"])
            for name, spec in self.required_fields.items()
            if self.field_types.get(name) != spec["type"]
        }
        if wrong:
            raise RuntimeError(f"飞书字段类型不匹配：{wrong}")

    def read_only_status(self) -> dict[str, Any]:
        records = self.list_records()
        statuses: Counter[str] = Counter()
        url_counts: Counter[str] = Counter()
        title_counts: Counter[str] = Counter()
        completed_lengths: list[int] = []
        completed_missing_summary = 0
        failed_with_summary = 0
        for record in records:
            fields = record.get("fields") or {}
            status = self._extract_text(fields.get("记录状态")) or "未设置"
            title = normalize_title(self._extract_text(fields.get("标题")))
            url = normalize_url(self._extract_text(fields.get("来源网站")))
            summary = self._extract_text(fields.get("核心干货")).strip()
            statuses[status] += 1
            if title:
                title_counts[title] += 1
            if url:
                url_counts[url] += 1
            if status == "已完成":
                if summary:
                    completed_lengths.append(len(re.sub(r"\s+", "", summary)))
                else:
                    completed_missing_summary += 1
            elif summary:
                failed_with_summary += 1
        return {
            "readable": True,
            "table_id": self.table_id,
            "record_count": len(records),
            "required_fields": sorted(self.required_fields),
            "status_counts": dict(sorted(statuses.items())),
            "completed_summary_chars": {
                "minimum": min(completed_lengths) if completed_lengths else None,
                "maximum": max(completed_lengths) if completed_lengths else None,
            },
            "completed_missing_summary": completed_missing_summary,
            "failed_with_summary": failed_with_summary,
            "duplicate_urls": sum(count - 1 for count in url_counts.values() if count > 1),
            "duplicate_titles": sum(count - 1 for count in title_counts.values() if count > 1),
        }

    @staticmethod
    def _extract_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return str(value.get("link") or value.get("text") or "")
        if isinstance(value, list):
            return " ".join(FeishuBitable._extract_text(item) for item in value)
        return ""

    def list_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token = ""
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"
        while True:
            params: dict[str, Any] = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self._api("GET", path, params=params)["data"]
            records.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
        return records

    def record_index(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        urls: dict[str, dict[str, Any]] = {}
        titles: dict[str, dict[str, Any]] = {}
        for record in self.list_records():
            fields = record.get("fields") or {}
            title = self._extract_text(fields.get("标题"))
            url = self._extract_text(fields.get("来源网站"))
            if title:
                titles[normalize_title(title)] = record
            if url:
                urls[normalize_url(url)] = record
        return urls, titles

    def existing_keys(self) -> tuple[set[str], set[str]]:
        urls, titles = self.record_index()
        return set(urls), set(titles)

    def pending_articles(self, cutoff: date, allowed_sources: set[str]) -> list[Article]:
        pending: list[Article] = []
        for record in self.list_records():
            fields = record.get("fields") or {}
            status = self._extract_text(fields.get("记录状态"))
            source = self._extract_text(fields.get("来源名称"))
            if status not in {"正文待补全", "AI待重试"} or source not in allowed_sources:
                continue
            try:
                published = datetime.fromtimestamp(int(fields.get("发布日期")) / 1000, tz=SHANGHAI)
            except (TypeError, ValueError, OSError, OverflowError):
                continue
            if published.date() < cutoff:
                continue
            title = self._extract_text(fields.get("标题"))
            url = self._extract_text(fields.get("来源网站"))
            if not title or not url:
                continue
            pending.append(
                Article(
                    source=source,
                    title=title,
                    url=url,
                    published_at=published,
                    daily_date=published.date(),
                    record_status=status,
                    record_id=str(record.get("record_id") or ""),
                    meta={
                        "image": self._extract_text(fields.get("封面")),
                        "failure_reason": self._extract_text(fields.get("失败原因")),
                    },
                )
            )
        return pending

    def _url_value(self, value: str) -> Any:
        return {"text": value, "link": value} if value else None

    @staticmethod
    def _date_timestamp(value: date) -> int:
        return int(datetime.combine(value, time.min, tzinfo=SHANGHAI).timestamp() * 1000)

    def record_fields(self, article: Article) -> dict[str, Any]:
        ai = article.ai_result or {}
        fields = {
            "标题": article.title[:1000],
            "来源名称": article.source,
            "来源网站": self._url_value(article.url),
            "封面": self._url_value(str(article.meta.get("image") or "")),
            "发布日期": int(article.published_at.timestamp() * 1000),
            "日报日期": self._date_timestamp(article.daily_date or article.published_at.astimezone(SHANGHAI).date()),
            "内容主题": list(ai.get("topics") or []),
            "归档板块": str(ai.get("category") or ""),
            "记录状态": article.record_status or "已完成",
            "核心干货": str(ai.get("core_content") or "")[:100_000],
            "失败原因": str(article.meta.get("failure_reason") or "")[:1000],
        }
        return {name: value for name, value in fields.items() if name in self.field_types and value is not None}

    def write(self, articles: Iterable[Article]) -> tuple[int, int]:
        records = [{"fields": self.record_fields(article)} for article in articles]
        written = failed = 0
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/batch_create"
        for index in range(0, len(records), self.batch_size):
            batch = records[index : index + self.batch_size]
            try:
                payload = self._api("POST", path, json={"records": batch})
                written += len(payload["data"].get("records", []))
            except Exception as exc:
                print(f"[飞书] 批量写入失败（{len(batch)}条）：{exc}")
                failed += len(batch)
        return written, failed

    def update(self, articles: Iterable[Article]) -> tuple[int, int]:
        records = [
            {"record_id": article.record_id, "fields": self.record_fields(article)}
            for article in articles if article.record_id
        ]
        updated = failed = 0
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/batch_update"
        for index in range(0, len(records), self.batch_size):
            batch = records[index : index + self.batch_size]
            try:
                payload = self._api("POST", path, json={"records": batch})
                updated += len(payload["data"].get("records", []))
            except Exception as exc:
                print(f"[飞书] 批量更新失败（{len(batch)}条）：{exc}")
                failed += len(batch)
        return updated, failed

    def verify_urls(self, articles: Iterable[Article]) -> set[str]:
        urls, _ = self.record_index()
        missing: set[str] = set()
        for article in articles:
            record = urls.get(normalize_url(article.url))
            fields = (record or {}).get("fields") or {}
            status = self._extract_text(fields.get("记录状态"))
            content = self._extract_text(fields.get("核心干货"))
            if not record or status != article.record_status or (status == "已完成" and not content):
                missing.add(normalize_url(article.url))
        return missing

    def verify(self, articles: Iterable[Article]) -> set[str]:
        return self.verify_urls(articles)
