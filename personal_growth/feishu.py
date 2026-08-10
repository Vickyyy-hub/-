from __future__ import annotations

import os
from typing import Any, Iterable

from .http import HttpClient
from .models import Article
from .text import normalize_title, normalize_url


class FeishuBitable:
    base_url = "https://open.feishu.cn/open-apis"
    required_fields = {
        "核心干货": {"field_name": "核心干货", "type": 1},
        "来源名称": {"field_name": "来源名称", "type": 3},
        "发布日期": {"field_name": "发布日期", "type": 5, "property": {"date_formatter": "yyyy-MM-dd"}},
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
        self.token = self._tenant_token()
        self.headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
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
        return payload["tenant_access_token"]

    def _api(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.http.request(method, f"{self.base_url}{path}", headers=self.headers, **kwargs).json()
        if response.get("code") != 0:
            raise RuntimeError(f"飞书API失败：{response}")
        return response

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
            "记录状态": article.record_status or "主稿",
        }
        return {name: value for name, value in fields.items() if name in self.field_types}

    def write(self, articles: Iterable[Article]) -> tuple[int, int]:
        records = [{"fields": self.record_fields(article)} for article in articles]
        written = 0
        failed = 0
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/batch_create"
        for index in range(0, len(records), 100):
            batch = records[index : index + 100]
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
        for index in range(0, len(records), 100):
            batch = records[index : index + 100]
            try:
                payload = self._api("POST", path, json={"records": batch})
                updated += len(payload["data"].get("records", []))
            except Exception as exc:
                print(f"[飞书] 批量更新失败（{len(batch)} 条）：{exc}")
                failed += len(batch)
        return updated, failed

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
