from __future__ import annotations

import os
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


TABLES: dict[str, dict[str, Any]] = {
    "sources": {
        "name": "跨境-来源配置表", "env": "FEISHU_SOURCE_TABLE_ID", "key": "来源ID",
        "fields": [_field(name) for name in ("来源ID", "来源名称", "国家", "采集类型", "信号类型", "频率", "官方入口", "鉴权变量", "启用状态", "备注")],
    },
    "signals": {
        "name": "跨境-原始信号表", "env": "FEISHU_SIGNAL_TABLE_ID", "key": "信号ID",
        "fields": [_field("信号ID"), _field("国家"), _field("信号类型"), _field("原文标题"), _field("原词"), _field("中文含义"), _field("中文摘要"), _field("AI结论"), _field("主题"), _field("热度", 2), _field("来源名称"), _field("原文链接"), _field("发布时间", 5), _field("日报日期", 5), _field("记录状态"), _field("置信度")],
    },
    "profiles": {
        "name": "跨境-国家消费画像表", "env": "FEISHU_PROFILE_TABLE_ID", "key": "国家代码",
        "fields": [_field("国家代码"), _field("国家"), _field("地区"), _field("语言"), _field("货币"), _field("时区"), _field("消费结构"), _field("电商渠道"), _field("支付偏好"), _field("价格与促销"), _field("配送与退货"), _field("决策因素"), _field("节日与季节性"), _field("文化注意事项"), _field("证据链接"), _field("更新时间", 5), _field("置信度")],
    },
    "directions": {
        "name": "跨境-产品方向表", "env": "FEISHU_DIRECTION_TABLE_ID", "key": "方向ID",
        "fields": [_field("方向ID"), _field("国家"), _field("产品方向"), _field("目标用户"), _field("使用场景"), _field("证据数量", 2), _field("信号类型"), _field("7日趋势"), _field("30日趋势"), _field("用户痛点"), _field("产品偏好"), _field("竞争热度"), _field("物流风险"), _field("合规风险"), _field("建议"), _field("判断依据"), _field("置信度"), _field("证据信号ID"), _field("生成时间", 5)],
    },
}


def _date_ms(value: str | datetime | None) -> int | None:
    if value is None or value == "":
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
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
        self.read_only = read_only
        self.table_ids: dict[str, str] = {}
        self.missing_tables: list[str] = []
        self.field_issues: dict[str, dict[str, Any]] = {}
        self._resolve_tables(create=not read_only)
        self._validate_or_create_fields(create=not read_only)

    def _refresh_token(self) -> None:
        payload = self.http.post(f"{self.base_url}/auth/v3/tenant_access_token/internal", json={"app_id": self.app_id, "app_secret": self.app_secret}).json()
        if payload.get("code") != 0:
            raise RuntimeError(f"飞书授权失败：code={payload.get('code')} msg={payload.get('msg')}")
        self.token = payload["tenant_access_token"]
        self.token_expires_at = clock.monotonic() + max(1, int(payload.get("expire", 7200)))

    def _ensure_token(self) -> None:
        if clock.monotonic() >= self.token_expires_at - self.token_refresh_margin_seconds:
            self._refresh_token()

    def _api(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(2):
            self._ensure_token()
            response = self.http.request(method, f"{self.base_url}{path}", headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}, **kwargs)
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
        path = f"/bitable/v1/apps/{self.app_token}/tables"
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._api("GET", path, params=params)["data"]
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                return items
            page_token = str(data.get("page_token") or "")

    def _resolve_tables(self, *, create: bool) -> None:
        existing = {str(item.get("name")): str(item.get("table_id")) for item in self._list_tables()}
        path = f"/bitable/v1/apps/{self.app_token}/tables"
        for key, spec in TABLES.items():
            table_id = os.environ.get(spec["env"], "") or existing.get(spec["name"], "")
            if not table_id and create:
                payload = self._api("POST", path, json={"table": {"name": spec["name"], "default_view_name": "全部记录", "fields": spec["fields"]}})
                table_id = str((payload.get("data") or {}).get("table_id") or "")
            if table_id:
                self.table_ids[key] = table_id
            else:
                self.missing_tables.append(spec["name"])

    def _field_list(self, table_id: str) -> list[dict[str, Any]]:
        path = f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields"
        return self._api("GET", path, params={"page_size": 100})["data"].get("items", [])

    def _validate_or_create_fields(self, *, create: bool) -> None:
        for table_name, table_id in self.table_ids.items():
            spec = TABLES[table_name]
            expected = {item["field_name"]: int(item["type"]) for item in spec["fields"]}
            existing = {item["field_name"]: int(item["type"]) for item in self._field_list(table_id)}
            path = f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields"
            if create:
                for item in spec["fields"]:
                    if item["field_name"] not in existing:
                        self._api("POST", path, json=item)
                existing = {item["field_name"]: int(item["type"]) for item in self._field_list(table_id)}
            missing = sorted(set(expected) - set(existing))
            wrong = {name: (existing.get(name), value) for name, value in expected.items() if name in existing and existing[name] != value}
            if missing or wrong:
                self.field_issues[table_name] = {"missing": missing, "wrong": wrong}
        if create and (self.missing_tables or self.field_issues):
            raise RuntimeError(f"飞书四表初始化失败 missing_tables={self.missing_tables} field_issues={self.field_issues}")

    def status(self) -> dict[str, Any]:
        return {"readable": True, "ready": not self.missing_tables and not self.field_issues, "missing_tables": self.missing_tables, "field_issues": self.field_issues, "tables": {key: {"name": TABLES[key]["name"], "table_id": value, "fields": len(TABLES[key]["fields"])} for key, value in self.table_ids.items()}}

    def _records(self, table_name: str) -> list[dict[str, Any]]:
        table_id = self.table_ids[table_name]
        path = f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records"
        items: list[dict[str, Any]] = []
        page_token = ""
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
            return str(value.get("text") or value.get("link") or "")
        return str(value or "")

    @classmethod
    def _signal_identity(cls, fields: dict[str, Any]) -> tuple[str, str]:
        scope = "|".join((
            cls._text(fields.get("国家")).strip().casefold(),
            cls._text(fields.get("来源名称")).strip().casefold(),
            cls._text(fields.get("信号类型")).strip().casefold(),
        ))
        title = normalize_title(cls._text(fields.get("原文标题")))
        title_key = f"{scope}|{title}" if title else ""
        # Google Trends 等关键词记录会共享落地页，不能只按 URL 合并不同关键词。
        url = cls._text(fields.get("原文链接"))
        url_key = "" if cls._text(fields.get("原词")) else (f"{scope}|{normalize_url(url)}" if url else "")
        return title_key, url_key

    def existing_keys(self, table_name: str) -> dict[str, dict[str, str]]:
        spec = TABLES[table_name]
        result: dict[str, dict[str, str]] = {"keys": {}, "titles": {}, "urls": {}}
        for item in self._records(table_name):
            fields = item.get("fields", {})
            record_id = str(item.get("record_id") or "")
            key = self._text(fields.get(spec["key"]))
            if key:
                result["keys"][key] = record_id
            if table_name == "signals":
                title_key, url_key = self._signal_identity(fields)
                if title_key:
                    result["titles"][title_key] = record_id
                if url_key:
                    result["urls"][url_key] = record_id
        return result

    def upsert(self, table_name: str, rows: list[dict[str, Any]]) -> tuple[int, int]:
        if not rows:
            return 0, 0
        spec = TABLES[table_name]
        existing = self.existing_keys(table_name)
        base_path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_ids[table_name]}/records"
        created = updated = 0
        for raw_row in rows:
            row = {name: value for name, value in raw_row.items() if value is not None}
            key = self._text(row.get(spec["key"]))
            if not key:
                raise RuntimeError(f"{spec['name']}存在空主键")
            record_id = existing["keys"].get(key)
            if table_name == "signals" and not record_id:
                title_key, url_key = self._signal_identity(row)
                record_id = existing["titles"].get(title_key) or (existing["urls"].get(url_key) if url_key else None)
            if record_id:
                self._api("PUT", f"{base_path}/{record_id}", json={"fields": row})
                updated += 1
            else:
                payload = self._api("POST", base_path, json={"fields": row})
                record_id = str(((payload.get("data") or {}).get("record") or {}).get("record_id") or "")
                created += 1
            existing["keys"][key] = str(record_id or "")
            if table_name == "signals":
                title_key, url_key = self._signal_identity(row)
                if title_key:
                    existing["titles"][title_key] = str(record_id or "")
                if url_key:
                    existing["urls"][url_key] = str(record_id or "")
        self.verify_urls(table_name, rows)
        return created, updated

    def verify_urls(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        spec = TABLES[table_name]
        by_key = {self._text(item.get("fields", {}).get(spec["key"])): item for item in self._records(table_name)}
        missing = [self._text(row.get(spec["key"])) for row in rows if self._text(row.get(spec["key"])) not in by_key]
        if missing:
            raise RuntimeError(f"飞书写后验证失败：{spec['name']}缺少{missing[:5]}")

    def verify_article_states(self, table_name: str, rows: list[dict[str, Any]]) -> bool:
        self.verify_urls(table_name, rows)
        if table_name != "signals":
            return True
        expected = {self._text(row.get("信号ID")): row for row in rows}
        actual = {self._text(item.get("fields", {}).get("信号ID")): item.get("fields", {}) for item in self._records(table_name)}
        return all(key in actual and self._text(actual[key].get("记录状态")) == self._text(row.get("记录状态")) and actual[key].get("日报日期") not in (None, "") for key, row in expected.items())


def source_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"来源ID": item["key"], "来源名称": item["name"], "国家": "、".join(item.get("countries", [])), "采集类型": item["kind"], "信号类型": item["signal_type"], "频率": item["cadence"], "官方入口": item["url"], "鉴权变量": item.get("auth_env", ""), "启用状态": "启用" if item.get("enabled", True) else "停用", "备注": item.get("notes", "")} for item in items]


def signal_rows(items: list[dict[str, Any]], daily_date: str | None = None) -> list[dict[str, Any]]:
    return [{"信号ID": item["signal_id"], "国家": "、".join(item.get("countries", [])), "信号类型": item["signal_type"], "原文标题": item["title"], "原词": item.get("original_keyword", ""), "中文含义": item.get("keyword_zh", ""), "中文摘要": item.get("summary_zh", ""), "AI结论": item.get("ai_conclusion", ""), "主题": "、".join(item.get("topics", [])), "热度": item.get("heat"), "来源名称": item["source_name"], "原文链接": item["url"], "发布时间": _date_ms(item["published_at"]), "日报日期": _date_ms(f"{daily_date}T00:00:00+08:00") if daily_date else None, "记录状态": "已完成", "置信度": item.get("confidence", "")} for item in items]


def profile_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapping = {"country_code": "国家代码", "country_name": "国家", "region": "地区", "language": "语言", "currency": "货币", "timezone": "时区", "consumption_structure": "消费结构", "ecommerce_channels": "电商渠道", "payment_preferences": "支付偏好", "price_and_promotion": "价格与促销", "delivery_and_returns": "配送与退货", "decision_factors": "决策因素", "festivals_and_seasonality": "节日与季节性", "culture_notes": "文化注意事项", "confidence": "置信度"}
    rows = []
    for item in items:
        row = {target: item.get(source, "") for source, target in mapping.items()}
        row["证据链接"] = "\n".join(item.get("evidence_urls", []))
        row["更新时间"] = _date_ms(item.get("updated_at"))
        rows.append(row)
    return rows


def direction_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"方向ID": item["direction_id"], "国家": item["country_name"], "产品方向": item["product_direction"], "目标用户": item["target_user"], "使用场景": item["use_case"], "证据数量": item["evidence_count"], "信号类型": "、".join(item["source_types"]), "7日趋势": item["trend_7d"], "30日趋势": item["trend_30d"], "用户痛点": item["pain_points"], "产品偏好": item["preferences"], "竞争热度": item["competition"], "物流风险": item["logistics_risk"], "合规风险": item["compliance_risk"], "建议": item["recommendation"], "判断依据": item["rationale"], "置信度": item["confidence"], "证据信号ID": "、".join(item["evidence_signal_ids"]), "生成时间": _date_ms(item["generated_at"])} for item in items]
