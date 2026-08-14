from __future__ import annotations

import os
import time as clock
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
        "核心干货": {"field_name": "核心干货", "type": 1},
        "来源名称": {"field_name": "来源名称", "type": 3},
        "发布日期": {"field_name": "发布日期", "type": 5, "property": {"date_formatter": "yyyy-MM-dd"}},
        "日报日期": {"field_name": "日报日期", "type": 5, "property": {"date_formatter": "yyyy-MM-dd"}},
        "记录状态": {"field_name": "记录状态", "type": 3},
    }

    def __init__(self, http: HttpClient, *, read_only: bool = False) -> None:
        self.http = http
        self.app_id = os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        self.app_token = os.environ.get("FEISHU_APP_TOKEN", "FpRzbXZQ9aTOILsDKbwceMmNn9f")
        self.table_id = os.environ.get("FEISHU_TABLE_ID", "tblQ0GsmYdWzBukz")
        if not self.app_id or not self.app_secret:
            raise RuntimeError("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET")
        self.token = ""
        self.token_expires_at = 0.0
        self.headers = {"Content-Type": "application/json"}
        self._refresh_token()
        self.field_types = self._field_types()
        if read_only:
            self._validate_required_fields()
        else:
            self.ensure_fields()

    def _tenant_token(self) -> str:
        payload = self.http.post(
            f"{self.base_url}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        ).json()
        if payload.get("code") != 0:
            raise RuntimeError(f"飞书授权失败：{payload}")
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
        return (
            status == 401
            or code in cls.token_error_codes
            or ("token" in message and any(word in message for word in ("expired", "invalid", "unauthorized")))
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
                response = self.http.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self.headers,
                    **kwargs,
                )
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
        return {
            "readable": True,
            "record_count": len(records),
            "required_fields": sorted(self.required_fields),
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
        token = ""
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"
        while True:
            params: dict[str, Any] = {"page_size": 500}
            if token:
                params["page_token"] = token
            data = self._api("GET", path, params=params)["data"]
            records.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            token = data.get("page_token", "")
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

    def _source_value(self, url: str) -> Any:
        if self.field_types.get("来源网站") == 15:
            return {"text": url, "link": url}
        return url

    def _field_value(self, name: str, value: str | list[str]) -> Any:
        if self.field_types.get(name) == 4:
            return value if isinstance(value, list) else [value]
        if isinstance(value, list):
            return "、".join(value)
        return value

    @staticmethod
    def _date_timestamp(value: date) -> int:
        return int(datetime.combine(value, time.min, tzinfo=SHANGHAI).timestamp() * 1000)

    def record_fields(self, article: Article) -> dict[str, Any]:
        ai = article.ai_result or {}
        fields = {
            "标题": article.title[:1000],
            "来源网站": self._source_value(article.url),
            "内容主题": self._field_value("内容主题", list(ai.get("topics") or [])),
            "归档板块": self._field_value("归档板块", str(ai.get("category", "社会与文化"))),
            "核心干货": str(ai.get("core_content", ""))[:100_000],
            "来源名称": article.source,
            "发布日期": int(article.published_at.timestamp() * 1000),
            "日报日期": self._date_timestamp(article.daily_date or article.published_at.astimezone(SHANGHAI).date()),
            "记录状态": article.record_status or "主稿",
        }
        return {name: value for name, value in fields.items() if name in self.field_types}

    def write(self, articles: Iterable[Article]) -> tuple[int, int]:
        records = [{"fields": self.record_fields(article)} for article in articles]
        written = 0
        failed = 0
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/batch_create"
        for index in range(0, len(records), self.batch_size):
            batch = records[index : index + self.batch_size]
            try:
                payload = self._api("POST", path, json={"records": batch})
                written += len(payload["data"].get("records", []))
            except Exception as exc:
                print(f"[飞书] 批量写入失败（{len(batch)} 条）：{exc}")
                failed += len(batch)
        return written, failed

    def update(self, articles: Iterable[Article]) -> tuple[int, int]:
        records = [
            {"record_id": article.record_id, "fields": self.record_fields(article)}
            for article in articles if article.record_id
        ]
        updated = 0
        failed = 0
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/batch_update"
        for index in range(0, len(records), self.batch_size):
            batch = records[index : index + self.batch_size]
            try:
                payload = self._api("POST", path, json={"records": batch})
                updated += len(payload["data"].get("records", []))
            except Exception as exc:
                print(f"[飞书] 批量更新失败（{len(batch)} 条）：{exc}")
                failed += len(batch)
        return updated, failed

    def upsert(self, articles: Iterable[Article]) -> tuple[int, int, int]:
        """Create unseen articles and update changed staged/final records idempotently."""
        url_index, title_index = self.record_index()
        creates: list[Article] = []
        updates: list[Article] = []
        for article in articles:
            record = url_index.get(normalize_url(article.url)) or title_index.get(normalize_title(article.title))
            if not record:
                creates.append(article)
                continue
            article.record_id = str(record.get("record_id", ""))
            fields = record.get("fields") or {}
            current_status = self._extract_text(fields.get("记录状态"))
            current_content = self._extract_text(fields.get("核心干货"))
            expected_content = str((article.ai_result or {}).get("core_content", ""))
            if (
                current_status != (article.record_status or "主稿")
                or current_content != expected_content
                or fields.get("日报日期") in (None, "")
            ):
                updates.append(article)
        written, create_failed = self.write(creates)
        updated, update_failed = self.update(updates)
        return written, updated, create_failed + update_failed

    def verify_article_states(self, articles: Iterable[Article]) -> set[str]:
        """Return URL/title keys whose record, daily date, content, or status is missing."""
        url_index, title_index = self.record_index()
        invalid: set[str] = set()
        for article in articles:
            key = normalize_url(article.url) or normalize_title(article.title)
            record = url_index.get(normalize_url(article.url)) or title_index.get(normalize_title(article.title))
            if not record:
                invalid.add(key)
                continue
            fields = record.get("fields") or {}
            if (
                self._extract_text(fields.get("记录状态")) != (article.record_status or "主稿")
                or self._extract_text(fields.get("核心干货")) != str((article.ai_result or {}).get("core_content", ""))
                or fields.get("日报日期") in (None, "")
            ):
                invalid.add(key)
        return invalid

    def backfill_daily_dates(self) -> tuple[int, int, int]:
        records = self.list_records()
        updates: list[dict[str, Any]] = []
        skipped = 0
        for record in records:
            fields = record.get("fields") or {}
            if fields.get("日报日期") not in (None, ""):
                continue
            published = fields.get("发布日期")
            try:
                published_ms = int(published)
                published_day = datetime.fromtimestamp(published_ms / 1000, tz=SHANGHAI).date()
            except (TypeError, ValueError, OverflowError, OSError):
                skipped += 1
                continue
            updates.append(
                {
                    "record_id": record.get("record_id", ""),
                    "fields": {"日报日期": self._date_timestamp(published_day)},
                }
            )

        updated = 0
        failed = 0
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/batch_update"
        for index in range(0, len(updates), self.batch_size):
            batch = [item for item in updates[index : index + self.batch_size] if item["record_id"]]
            if not batch:
                continue
            try:
                payload = self._api("POST", path, json={"records": batch})
                updated += len(payload["data"].get("records", []))
            except Exception as exc:
                print(f"[飞书] 日报日期回填失败（{len(batch)} 条）：{exc}")
                failed += len(batch)

        if updated:
            expected = {item["record_id"] for item in updates if item["record_id"]}
            actual = {
                record.get("record_id", "")
                for record in self.list_records()
                if (record.get("fields") or {}).get("日报日期") not in (None, "")
            }
            failed = max(failed, len(expected - actual))
            updated = max(0, len(expected) - len(expected - actual))
        return updated, failed, skipped

    def verify_urls(self, articles: Iterable[Article]) -> set[str]:
        expected = {normalize_url(article.url) for article in articles}
        actual, _ = self.existing_keys()
        return expected - actual

    def verify_updates(self, articles: Iterable[Article]) -> set[str]:
        expected = {article.record_id for article in articles if article.record_id}
        actual = {
            record.get("record_id", "")
            for record in self.list_records()
            if self._extract_text((record.get("fields") or {}).get("核心干货"))
            and self._extract_text((record.get("fields") or {}).get("记录状态"))
        }
        return expected - actual
