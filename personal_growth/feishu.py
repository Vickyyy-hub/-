from __future__ import annotations

import os
import time as clock
from datetime import date, datetime, time
from typing import Any, Iterable

from .http import HttpClient
from .models import Article
from .text import SHANGHAI, clean_text, normalize_title, normalize_url


class FeishuBitable:
    base_url = "https://open.feishu.cn/open-apis"
    token_refresh_margin_seconds = 600
    batch_size = 50
    token_error_codes = {99991661, 99991663, 99991668}
    primary_title_spec = {"field_name": "标题", "type": 1, "is_primary": True}
    required_fields = {
        "核心干货": {"field_name": "核心干货", "type": 1},
        "来源名称": {"field_name": "来源名称", "type": 3},
        "发布日期": {"field_name": "发布日期", "type": 5, "property": {"date_formatter": "yyyy-MM-dd"}},
        "日报日期": {"field_name": "日报日期", "type": 5, "property": {"date_formatter": "yyyy-MM-dd"}},
        "记录状态": {"field_name": "记录状态", "type": 3},
    }

    def __init__(
        self,
        http: HttpClient,
        *,
        read_only: bool = False,
        allow_title_migration: bool = False,
    ) -> None:
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
        self.field_items = self._list_fields()
        self._refresh_field_state()
        if allow_title_migration:
            self.ensure_fields(validate_identity=False)
        elif read_only:
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

    def _list_fields(self) -> list[dict[str, Any]]:
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields"
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._api("GET", path, params=params)["data"]
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise RuntimeError("飞书字段分页缺少 page_token")
        return items

    def _refresh_field_state(self) -> None:
        self.field_types = {
            str(item["field_name"]): int(item["type"])
            for item in self.field_items
        }
        primary = [item for item in self.field_items if item.get("is_primary") is True]
        self.primary_field = primary[0] if len(primary) == 1 else None

    def refresh_fields(self) -> None:
        self.field_items = self._list_fields()
        self._refresh_field_state()

    def ensure_fields(self, *, validate_identity: bool = True) -> None:
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields"
        for name, spec in self.required_fields.items():
            if name not in self.field_types:
                self._api("POST", path, json=spec)
        self.refresh_fields()
        if validate_identity:
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
        primary = [item for item in self.field_items if item.get("is_primary") is True]
        if len(primary) != 1:
            raise RuntimeError(f"飞书必须且只能有一个主字段，当前识别到 {len(primary)} 个")
        primary_name = str(primary[0].get("field_name") or "")
        primary_type = int(primary[0].get("type") or 0)
        if primary_name != "标题" or primary_type != 1:
            raise RuntimeError(
                f"飞书首列主字段必须为文本字段‘标题’，当前为 {primary_name!r}（类型 {primary_type}）"
            )
        source_type = self.field_types.get("来源网站")
        if source_type not in {1, 15}:
            if source_type is None:
                raise RuntimeError("飞书缺少字段：['来源网站']")
            raise RuntimeError(f"飞书字段类型不匹配：{{'来源网站': ({source_type}, '文本或超链接')}}")

    def read_only_status(self) -> dict[str, Any]:
        records = self.list_records()
        return {
            "readable": True,
            "record_count": len(records),
            "required_fields": sorted(self.required_fields),
            "primary_field": {
                "field_id": str((self.primary_field or {}).get("field_id") or ""),
                "field_name": str((self.primary_field or {}).get("field_name") or ""),
                "type": int((self.primary_field or {}).get("type") or 0),
                "is_primary": bool((self.primary_field or {}).get("is_primary")),
            },
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
        title = clean_text(article.title)
        url = article.url.strip()
        if not title:
            raise ValueError(f"文章标题为空，拒绝写入：{url or '<无链接>'}")
        if not url:
            raise ValueError(f"文章来源链接为空，拒绝写入：{title}")
        self._validate_primary_title_field()
        missing_identity_fields = {"标题", "来源网站"} - set(self.field_types)
        if missing_identity_fields:
            raise RuntimeError(f"飞书缺少身份字段，拒绝静默丢弃：{sorted(missing_identity_fields)}")
        ai = article.ai_result or {}
        fields = {
            "标题": title[:1000],
            "来源网站": self._source_value(url),
            "内容主题": self._field_value("内容主题", list(ai.get("topics") or [])),
            "归档板块": self._field_value("归档板块", str(ai.get("category", "社会与文化"))),
            "核心干货": str(ai.get("core_content", ""))[:100_000],
            "来源名称": article.source,
            "发布日期": int(article.published_at.timestamp() * 1000),
            "日报日期": self._date_timestamp(article.daily_date or article.published_at.astimezone(SHANGHAI).date()),
            "记录状态": article.record_status or "主稿",
        }
        return {name: value for name, value in fields.items() if name in self.field_types}

    def _validate_primary_title_field(self) -> None:
        primary = self.primary_field or {}
        if (
            primary.get("is_primary") is not True
            or str(primary.get("field_name") or "") != "标题"
            or int(primary.get("type") or 0) != 1
        ):
            raise RuntimeError("飞书‘标题’不是首列文本主字段，拒绝读写并生成完成标记")

    def rename_field(self, field: dict[str, Any], new_name: str) -> None:
        field_id = str(field.get("field_id") or "")
        if not field_id:
            raise RuntimeError("飞书字段缺少 field_id，无法安全重命名")
        payload: dict[str, Any] = {
            "field_name": new_name,
            "type": int(field.get("type") or 0),
        }
        if field.get("property") is not None:
            payload["property"] = field["property"]
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields/{field_id}"
        self._api("PUT", path, json=payload)
        self.refresh_fields()

    def prepare_primary_title_migration(self) -> dict[str, Any]:
        """Rename the visible primary field and preserve any ordinary recovered-title field."""
        primary = self.primary_field
        if not primary:
            raise RuntimeError("未能唯一识别飞书主字段，停止标题迁移")
        ordinary_title = next(
            (
                item for item in self.field_items
                if item.get("field_name") == "标题" and item.get("is_primary") is not True
            ),
            None,
        )
        backup_name = next(
            (
                str(item.get("field_name") or "")
                for item in self.field_items
                if str(item.get("field_name") or "").startswith("标题_恢复源")
                and item.get("is_primary") is not True
            ),
            "",
        )
        if ordinary_title:
            existing_names = {str(item.get("field_name") or "") for item in self.field_items}
            backup_name = "标题_恢复源"
            suffix = 2
            while backup_name in existing_names:
                backup_name = f"标题_恢复源_{suffix}"
                suffix += 1
            self.rename_field(ordinary_title, backup_name)
            primary = self.primary_field
        if str((primary or {}).get("field_name") or "") != "标题":
            self.rename_field(primary or {}, "标题")
        self._validate_required_fields()
        return {
            "backup_field_name": backup_name,
            "primary_field_id": str((self.primary_field or {}).get("field_id") or ""),
            "primary_field_name": str((self.primary_field or {}).get("field_name") or ""),
            "primary_field_type": int((self.primary_field or {}).get("type") or 0),
        }

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
            current_title = self._extract_text(fields.get("标题"))
            current_url = self._extract_text(fields.get("来源网站"))
            current_status = self._extract_text(fields.get("记录状态"))
            current_content = self._extract_text(fields.get("核心干货"))
            expected_content = str((article.ai_result or {}).get("core_content", ""))
            if (
                clean_text(current_title) != clean_text(article.title)
                or normalize_url(current_url) != normalize_url(article.url)
                or current_status != (article.record_status or "主稿")
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
                clean_text(self._extract_text(fields.get("标题"))) != clean_text(article.title)
                or normalize_url(self._extract_text(fields.get("来源网站"))) != normalize_url(article.url)
                or self._extract_text(fields.get("记录状态")) != (article.record_status or "主稿")
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
            if self._extract_text((record.get("fields") or {}).get("标题"))
            and self._extract_text((record.get("fields") or {}).get("来源网站"))
            and self._extract_text((record.get("fields") or {}).get("核心干货"))
            and self._extract_text((record.get("fields") or {}).get("记录状态"))
        }
        return expected - actual

    def missing_title_records(self) -> list[dict[str, Any]]:
        """Return records that have a source URL but no visible title."""
        missing: list[dict[str, Any]] = []
        for record in self.list_records():
            fields = record.get("fields") or {}
            url = self._extract_text(fields.get("来源网站")).strip()
            title = self._extract_text(fields.get("标题")).strip()
            if url and not title:
                missing.append(record)
        return missing

    def repair_missing_titles(self, titles_by_url: dict[str, str]) -> dict[str, Any]:
        """Update only blank titles in place and verify the result."""
        before = self.missing_title_records()
        updates: list[dict[str, Any]] = []
        unresolved: list[dict[str, str]] = []
        for record in before:
            fields = record.get("fields") or {}
            url = self._extract_text(fields.get("来源网站")).strip()
            title = clean_text(titles_by_url.get(normalize_url(url), ""))
            record_id = str(record.get("record_id") or "")
            if not title or not record_id:
                unresolved.append({"record_id": record_id, "url": url})
                continue
            updates.append({"record_id": record_id, "fields": {"标题": title[:1000]}})

        updated = 0
        failed = 0
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/batch_update"
        for index in range(0, len(updates), self.batch_size):
            batch = updates[index : index + self.batch_size]
            try:
                payload = self._api("POST", path, json={"records": batch})
                updated += len(payload["data"].get("records", []))
            except Exception as exc:
                print(f"[飞书] 标题修复失败（{len(batch)} 条）：{exc}")
                failed += len(batch)

        remaining = self.missing_title_records()
        remaining_ids = {str(record.get("record_id") or "") for record in remaining}
        expected_ids = {item["record_id"] for item in updates}
        verification_failed = len(expected_ids & remaining_ids)
        failed = max(failed, verification_failed)
        updated = max(0, len(expected_ids) - verification_failed)
        return {
            "blank_before": len(before),
            "resolved": len(updates),
            "updated": updated,
            "failed": failed,
            "blank_after": len(remaining),
            "unresolved": unresolved,
            "ai_calls": 0,
            "created": 0,
            "deleted": 0,
        }

    def migrate_titles_to_primary(
        self,
        *,
        backup_field_name: str = "",
        titles_by_url: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Fill only blank primary titles from the preserved source or verified URL titles."""
        self._validate_primary_title_field()
        titles_by_url = titles_by_url or {}
        before_records = self.list_records()
        before_by_id = {str(item.get("record_id") or ""): item for item in before_records}
        updates: list[dict[str, Any]] = []
        update_sources: dict[str, str] = {}
        unresolved: list[dict[str, str]] = []
        for record in before_records:
            fields = record.get("fields") or {}
            record_id = str(record.get("record_id") or "")
            url = self._extract_text(fields.get("来源网站")).strip()
            primary_title = clean_text(self._extract_text(fields.get("标题")))
            if primary_title or not url or not record_id:
                continue
            backup_title = clean_text(self._extract_text(fields.get(backup_field_name))) if backup_field_name else ""
            verified_title = clean_text(titles_by_url.get(normalize_url(url), ""))
            title = backup_title or verified_title
            if not title:
                unresolved.append({"record_id": record_id, "url": url})
                continue
            updates.append({"record_id": record_id, "fields": {"标题": title[:1000]}})
            update_sources[record_id] = "backup" if backup_title else "verified_url"

        updated = 0
        api_failed = 0
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/batch_update"
        for index in range(0, len(updates), self.batch_size):
            batch = updates[index : index + self.batch_size]
            try:
                payload = self._api("POST", path, json={"records": batch})
                updated += len(payload["data"].get("records", []))
            except Exception as exc:
                print(f"[飞书] 主字段标题迁移失败（{len(batch)} 条）：{exc}")
                api_failed += len(batch)

        after_records = self.list_records()
        after_by_id = {str(item.get("record_id") or ""): item for item in after_records}
        verification_failed = 0
        for item in updates:
            record_id = item["record_id"]
            expected = clean_text(self._extract_text(item["fields"].get("标题")))
            actual = clean_text(
                self._extract_text((after_by_id.get(record_id, {}).get("fields") or {}).get("标题"))
            )
            if actual != expected:
                verification_failed += 1
        non_title_fields_changed = 0
        ignored = {"标题"}
        if backup_field_name:
            ignored.add(backup_field_name)
        for record_id, before in before_by_id.items():
            after = after_by_id.get(record_id)
            if not after:
                non_title_fields_changed += 1
                continue
            left = {key: value for key, value in (before.get("fields") or {}).items() if key not in ignored}
            right = {key: value for key, value in (after.get("fields") or {}).items() if key not in ignored}
            if left != right:
                non_title_fields_changed += 1
        blank_after = [
            record for record in after_records
            if self._extract_text((record.get("fields") or {}).get("来源网站")).strip()
            and not clean_text(self._extract_text((record.get("fields") or {}).get("标题")))
        ]
        failed = max(api_failed, verification_failed)
        return {
            "record_count_before": len(before_records),
            "record_count_after": len(after_records),
            "blank_primary_before": sum(
                1 for record in before_records
                if self._extract_text((record.get("fields") or {}).get("来源网站")).strip()
                and not clean_text(self._extract_text((record.get("fields") or {}).get("标题")))
            ),
            "copied_from_backup": sum(1 for source in update_sources.values() if source == "backup"),
            "recovered_from_verified_url": sum(1 for source in update_sources.values() if source == "verified_url"),
            "updated": max(0, len(updates) - verification_failed),
            "api_failed": api_failed,
            "verification_failed": verification_failed,
            "failed": failed,
            "blank_primary_after": len(blank_after),
            "unresolved": [
                {
                    "record_id": str(record.get("record_id") or ""),
                    "url": self._extract_text((record.get("fields") or {}).get("来源网站")).strip(),
                }
                for record in blank_after
            ],
            "non_title_fields_changed": non_title_fields_changed,
            "ai_calls": 0,
            "created": 0,
            "deleted": 0,
        }
