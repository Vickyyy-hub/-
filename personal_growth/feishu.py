from __future__ import annotations

import os
from typing import Any, Iterable

from .http import HttpClient
from .models import Article
from .text import normalize_title, normalize_url


class FeishuBitable:
    base_url = "https://open.feishu.cn/open-apis"

    def __init__(self, http: HttpClient) -> None:
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

    @staticmethod
    def _extract_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return str(value.get("link") or value.get("text") or "")
        if isinstance(value, list):
            return " ".join(FeishuBitable._extract_text(item) for item in value)
        return ""

    def existing_keys(self) -> tuple[set[str], set[str]]:
        urls: set[str] = set()
        titles: set[str] = set()
        token = ""
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"
        while True:
            params = {"page_size": 500}
            if token:
                params["page_token"] = token
            data = self._api("GET", path, params=params)["data"]
            for record in data.get("items", []):
                fields = record.get("fields") or {}
                title = self._extract_text(fields.get("标题"))
                url = self._extract_text(fields.get("来源网站"))
                if title:
                    titles.add(normalize_title(title))
                if url:
                    urls.add(normalize_url(url))
            if not data.get("has_more"):
                break
            token = data.get("page_token", "")
        return urls, titles

    def _source_value(self, url: str) -> Any:
        if self.field_types.get("来源网站") == 15:
            return {"text": url, "link": url}
        return url

    def record_fields(self, article: Article) -> dict[str, Any]:
        ai = article.ai_result or {}
        return {
            "标题": article.title[:1000],
            "来源网站": self._source_value(article.url),
            "内容主题": str(ai.get("content_topic", ""))[:5000],
            "归档板块": str(ai.get("category", "社会与文化")),
        }

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
            except Exception:
                failed += len(batch)
        return written, failed

    def verify_urls(self, articles: Iterable[Article]) -> set[str]:
        expected = {normalize_url(article.url) for article in articles}
        actual, _ = self.existing_keys()
        return expected - actual
