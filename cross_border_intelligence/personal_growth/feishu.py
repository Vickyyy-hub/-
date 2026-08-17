from __future__ import annotations

import os
import re
import time as clock
from datetime import datetime
from typing import Any

from .http import HttpClient
from .text import normalize_title, normalize_url


def _field(name: str, field_type: int = 1) -> dict[str, Any]:
    value: dict[str, Any] = {"field_name": name, "type": field_type}
    if field_type == 5:
        value["property"] = {"date_formatter": "yyyy-MM-dd HH:mm"}
    return value


NEWS_TABLE = {
    "name": "跨境资讯",
    "fields": [
        _field("标题"), _field("来源网站", 15), _field("国家/地区"), _field("内容主题"),
        _field("归档板块"), _field("核心干货"), _field("搜索词"), _field("热度", 2),
        _field("来源名称"), _field("发布日期", 5), _field("日报日期", 5), _field("记录状态"),
    ],
}
LEGACY_TABLE_NAMES = (
    "跨境-来源配置表", "跨境-原始信号表", "跨境-国家消费画像表", "跨境-产品方向表",
)


def _date_ms(value: str | datetime | None) -> int | None:
    if value in (None, ""):
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return int(parsed.timestamp() * 1000)


class FeishuMarketBase:
    base_url = "https://open.feishu.cn/open-apis"
    token_refresh_margin_seconds = 600
    token_error_codes = {99991661, 99991663, 99991668}

    def __init__(self, http: HttpClient, *, read_only: bool = False) -> None:
        self.http = http
        self.app_id = os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        self.app_token = os.environ.get("FEISHU_APP_TOKEN", "")
        if not self.app_id or not self.app_secret or not self.app_token:
            raise RuntimeError("缺少 FEISHU_APP_ID、FEISHU_APP_SECRET 或 FEISHU_APP_TOKEN")
        self.token = ""
        self.token_expires_at = 0.0
        self._refresh_token()
        table = next((item for item in self._list_tables() if item.get("name") == NEWS_TABLE["name"]), None)
        if table is None and not read_only:
            payload = self._api("POST", f"/bitable/v1/apps/{self.app_token}/tables", json={
                "table": {"name": NEWS_TABLE["name"], "default_view_name": "全部资讯", "fields": NEWS_TABLE["fields"]}
            })
            self.table_id = str((payload.get("data") or {}).get("table_id") or "")
        else:
            self.table_id = str((table or {}).get("table_id") or "")
        self.field_issues = self._validate_fields(create=not read_only) if self.table_id else {"missing_table": NEWS_TABLE["name"]}

    def _refresh_token(self) -> None:
        payload = self.http.post(f"{self.base_url}/auth/v3/tenant_access_token/internal", json={
            "app_id": self.app_id, "app_secret": self.app_secret,
        }).json()
        if payload.get("code") != 0:
            raise RuntimeError(f"飞书授权失败：code={payload.get('code')} msg={payload.get('msg')}")
        self.token = payload["tenant_access_token"]
        self.token_expires_at = clock.monotonic() + max(1, int(payload.get("expire", 7200)))

    def _api(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(2):
            if clock.monotonic() >= self.token_expires_at - self.token_refresh_margin_seconds:
                self._refresh_token()
            response = self.http.request(method, f"{self.base_url}{path}", headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
            }, **kwargs)
            payload = response.json()
            if payload.get("code") == 0:
                return payload
            if attempt == 0 and payload.get("code") in self.token_error_codes:
                self._refresh_token()
                continue
            request_id = response.headers.get("x-tt-logid") or response.headers.get("x-request-id")
            raise RuntimeError(f"飞书API失败 code={payload.get('code')} msg={payload.get('msg')} request_id={request_id}")
        raise RuntimeError("飞书凭证刷新后仍无法调用")

    def _list_tables(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._api("GET", f"/bitable/v1/apps/{self.app_token}/tables", params=params)["data"]
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                return items
            page_token = str(data.get("page_token") or "")

    def _fields(self) -> list[dict[str, Any]]:
        return self._api("GET", f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields",
                         params={"page_size": 100})["data"].get("items", [])

    def _validate_fields(self, *, create: bool) -> dict[str, Any]:
        expected = {item["field_name"]: int(item["type"]) for item in NEWS_TABLE["fields"]}
        existing = {item["field_name"]: int(item["type"]) for item in self._fields()}
        if create:
            path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields"
            for item in NEWS_TABLE["fields"]:
                if item["field_name"] not in existing:
                    self._api("POST", path, json=item)
            existing = {item["field_name"]: int(item["type"]) for item in self._fields()}
        missing = sorted(set(expected) - set(existing))
        wrong = {name: [existing.get(name), value] for name, value in expected.items()
                 if name in existing and existing[name] != value}
        issues = {"missing": missing, "wrong": wrong} if missing or wrong else {}
        if create and issues:
            raise RuntimeError(f"跨境资讯表字段初始化失败：{issues}")
        return issues

    def status(self) -> dict[str, Any]:
        tables = {str(item.get("name")): str(item.get("table_id")) for item in self._list_tables()}
        return {
            "readable": True, "ready": bool(self.table_id) and not self.field_issues,
            "table": {"name": NEWS_TABLE["name"], "table_id": self.table_id, "fields": len(NEWS_TABLE["fields"])},
            "record_count": len(self._records()) if self.table_id else 0,
            "field_issues": self.field_issues,
            "legacy_tables": {name: tables[name] for name in LEGACY_TABLE_NAMES if name in tables},
        }

    def _records(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = ""
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"
        while True:
            params: dict[str, Any] = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self._api("GET", path, params=params)["data"]
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                return items
            page_token = str(data.get("page_token") or "")

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(FeishuMarketBase._text(item) for item in value)
        if isinstance(value, dict):
            return str(value.get("link") or value.get("text") or "")
        return str(value or "")

    @classmethod
    def _identity(cls, fields: dict[str, Any]) -> tuple[str, str]:
        title = normalize_title(cls._text(fields.get("标题")))
        raw_url = cls._text(fields.get("来源网站"))
        return title, normalize_url(raw_url) if raw_url else ""

    def upsert(self, rows: list[dict[str, Any]]) -> tuple[int, int]:
        if not rows:
            return 0, 0
        existing: dict[str, str] = {}
        for item in self._records():
            record_id = str(item.get("record_id") or "")
            title, url = self._identity(item.get("fields", {}))
            if title:
                existing[f"title:{title}"] = record_id
            if url:
                existing[f"url:{url}"] = record_id
        created = updated = 0
        base = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"
        for raw in rows:
            row = {key: value for key, value in raw.items() if value is not None}
            title, url = self._identity(row)
            if not title or not url:
                raise RuntimeError("跨境资讯存在空标题或空详情链接")
            record_id = existing.get(f"url:{url}") or existing.get(f"title:{title}")
            try:
                if record_id:
                    self._api("PUT", f"{base}/{record_id}", json={"fields": row})
                    updated += 1
                else:
                    payload = self._api("POST", base, json={"fields": row})
                    record_id = str(((payload.get("data") or {}).get("record") or {}).get("record_id") or "")
                    created += 1
            except Exception as exc:
                raise RuntimeError(f"飞书单条写入失败：{self._text(row.get('标题'))}：{exc}") from exc
            existing[f"title:{title}"] = record_id
            existing[f"url:{url}"] = record_id
        self.verify(rows)
        return created, updated

    def existing_keys(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in self._records():
            record_id = str(item.get("record_id") or "")
            title, url = self._identity(item.get("fields", {}))
            if title:
                result[f"title:{title}"] = record_id
            if url:
                result[f"url:{url}"] = record_id
        return result

    def verify(self, rows: list[dict[str, Any]]) -> bool:
        actual = {self._identity(item.get("fields", {})): item.get("fields", {}) for item in self._records()}
        for row in rows:
            fields = actual.get(self._identity(row))
            summary = self._text((fields or {}).get("核心干货"))
            if not fields or not 80 <= len(re.sub(r"\s+", "", summary)) <= 150:
                raise RuntimeError(f"飞书写后验收失败：{self._text(row.get('标题'))}")
            if not self._text(fields.get("来源网站")) or not fields.get("日报日期"):
                raise RuntimeError(f"飞书链接或日报日期回读失败：{self._text(row.get('标题'))}")
        return True

    def verify_urls(self, rows: list[dict[str, Any]]) -> None:
        self.verify(rows)

    def verify_article_states(self, rows: list[dict[str, Any]]) -> bool:
        return self.verify(rows)

    def delete_legacy_tables(self) -> dict[str, str]:
        if not self.table_id or self.field_issues or not self._records():
            raise RuntimeError("新跨境资讯表尚未通过至少一条成品资讯验收，禁止删除旧表")
        tables = {str(item.get("name")): str(item.get("table_id")) for item in self._list_tables()}
        deleted: dict[str, str] = {}
        for name in LEGACY_TABLE_NAMES:
            table_id = tables.get(name)
            if not table_id:
                continue
            self._api("DELETE", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}")
            deleted[name] = table_id
        remaining = {str(item.get("name")) for item in self._list_tables()}
        if any(name in remaining for name in deleted):
            raise RuntimeError("旧跨境表删除后回读仍存在")
        return deleted

    def reset_news_table(self) -> dict[str, Any]:
        if not self.table_id:
            return {"deleted": False, "name": NEWS_TABLE["name"], "records": 0}
        records = len(self._records())
        self._api("DELETE", f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}")
        remaining = {str(item.get("name")) for item in self._list_tables()}
        if NEWS_TABLE["name"] in remaining:
            raise RuntimeError("跨境资讯成品表删除后回读仍存在")
        return {"deleted": True, "name": NEWS_TABLE["name"], "table_id": self.table_id, "records": records}


def signal_rows(items: list[dict[str, Any]], daily_date: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        rows.append({
            "标题": item["title"], "来源网站": {"text": "查看原文", "link": item["url"]},
            "国家/地区": "、".join(item.get("countries", [])),
            "内容主题": "、".join(item.get("topics", [])) or item.get("signal_type", ""),
            "归档板块": item.get("ai_conclusion", "跨境资讯"), "核心干货": item.get("summary_zh", ""),
            "搜索词": item.get("original_keyword", ""), "热度": item.get("heat"),
            "来源名称": item.get("source_name", ""), "发布日期": _date_ms(item.get("published_at")),
            "日报日期": _date_ms(f"{daily_date}T00:00:00+08:00") if daily_date else None,
            "记录状态": "已完成",
        })
    return rows
