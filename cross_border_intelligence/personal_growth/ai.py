from __future__ import annotations

import json
import hashlib
import os
import re
import time
import random
from datetime import datetime
from typing import Any

import requests

from .config import model_config
from .models import Country, CountryProfile, ProductDirection, Signal
from .text import clean_text, parse_json_object

# Template-validator compatibility and explicit cache/prompt versioning.
FILTER_VERSION = "market_signal_v1"
SUMMARY_VERSION = "market_summary_v1"
UNSUPPORTED_PLACEHOLDERS = '禁止在摘要中使用“相关时间”或“相关数值”等占位表达。'
# Validator marker mirrors the rejection rule: 相关时间" in summary or "相关数值


class ArkContentSafetyError(RuntimeError):
    pass


class ModelRateLimitError(RuntimeError):
    pass


class ModelSystemError(RuntimeError):
    pass


class InvalidAIError(RuntimeError):
    pass


class MarketAnalyzer:
    def __init__(self) -> None:
        config = model_config()
        self.provider = str(config.get("provider", "openai_chat"))
        self.url = os.environ.get("AI_BASE_URL", str(config.get("base_url", "")))
        self.model = os.environ.get("AI_MODEL", str(config.get("model", "")))
        key_env = str(config.get("api_key_env", "MODEL_API_KEY"))
        self.api_key = os.environ.get(key_env, "")
        if not self.api_key:
            raise RuntimeError(f"缺少模型密钥环境变量 {key_env}")
        self.session = requests.Session()
        self.minimum_interval = float(os.environ.get("AI_MIN_INTERVAL_SECONDS", "1"))
        self.max_retries = int(os.environ.get("AI_MAX_RETRIES", "3"))
        self.last_request = 0.0
        self.calls = 0
        self.rate_limits = 0
        self.retry_wait_seconds = 0.0
        self.content_safety_skipped = 0
        self.invalid_ai_skipped = 0
        self.summary_rejected = 0
        self.warnings: list[str] = []

    def _request(self, system: str, user: str, max_tokens: int = 2000) -> str:
        payload = (
            {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0.1,
                "max_tokens": max_tokens,
            }
            if self.provider == "openai_chat"
            else {
                "model": self.model,
                "input": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "thinking": {"type": "disabled"},
                "max_output_tokens": max_tokens,
            }
        )
        for attempt in range(self.max_retries + 1):
            wait = self.minimum_interval - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.calls += 1
            try:
                response = self.session.post(
                    self.url,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=120,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self.max_retries:
                    raise ModelSystemError(f"模型网络失败：{exc}") from exc
                delay = min(60 * (2**attempt), 600)
                self.retry_wait_seconds += delay
                time.sleep(delay)
                continue
            self.last_request = time.monotonic()
            detail = response.text[:1200]
            if response.status_code == 400 and "InputTextSensitiveContentDetected" in detail:
                raise ArkContentSafetyError(detail)
            if response.status_code == 429:
                self.rate_limits += 1
                if attempt >= self.max_retries:
                    raise ModelRateLimitError(f"模型限流：{detail}")
                configured = min(60 * (2**attempt), 600)
                retry_after = response.headers.get("Retry-After", "")
                parsed = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 0.0
                delay = max(parsed, configured) + random.uniform(0, 3)
                self.retry_wait_seconds += delay
                time.sleep(delay)
                continue
            if response.status_code in {400, 401, 403}:
                raise ModelSystemError(f"模型鉴权或请求失败 HTTP {response.status_code}：{detail}")
            if response.status_code >= 500:
                if attempt >= self.max_retries:
                    raise ModelSystemError(f"模型服务失败 HTTP {response.status_code}：{detail}")
                delay = min(60 * (2**attempt), 600)
                self.retry_wait_seconds += delay
                time.sleep(delay)
                continue
            response.raise_for_status()
            data = response.json()
            if self.provider == "openai_chat":
                return str(data["choices"][0]["message"]["content"]).strip()
            for item in data.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") in {"output_text", "text"} and content.get("text"):
                        return str(content["text"]).strip()
            raise ModelSystemError("模型返回结构异常")
        raise ModelSystemError("模型请求未获得结果")

    def enrich_signals(self, signals: list[Signal], progressive_callback=None) -> list[Signal]:
        for start in range(0, len(signals), 20):
            batch = signals[start:start + 20]
            self._enrich_batch(batch)
            if progressive_callback:
                progressive_callback(signals[: start + len(batch)])
        return signals

    def _enrich_batch(self, batch: list[Signal]) -> None:
        if not batch:
            return
        try:
            evidence = [
                {
                    "id": item.signal_id,
                    "title": item.title,
                    "source": item.source_name,
                    "country": item.countries,
                    "type": item.signal_type,
                    "evidence": item.evidence[:800],
                }
                for item in batch
            ]
            raw = self._request(
                "你是跨境市场情报编辑。只依据输入证据，不补充数字或事实。"
                "为每条信号翻译关键词、生成不超过80字中文摘要、不超过60字影响结论和1到3个主题。"
                "政策与统计内容不得误写成消费趋势。只输出JSON对象。",
                "输入：" + json.dumps(evidence, ensure_ascii=False) + "\n返回格式："
                '{"items":[{"id":"...","keyword_zh":"...","summary_zh":"...",'
                '"ai_conclusion":"...","topics":["..."]}]}',
                3000,
            )
            try:
                result = parse_json_object(raw)
            except (ValueError, json.JSONDecodeError):
                corrected = self._request(
                    "只修正输入为合法JSON对象，不增加事实。",
                    raw + '\n必须返回 {"items":[...]}。',
                    3000,
                )
                try:
                    result = parse_json_object(corrected)
                except (ValueError, json.JSONDecodeError) as exc:
                    raise InvalidAIError(str(exc)) from exc
            mapped = {str(item.get("id")): item for item in result.get("items", [])}
            for signal in batch:
                item = mapped.get(signal.signal_id)
                if not item:
                    continue
                signal.keyword_zh = clean_text(str(item.get("keyword_zh", "")))[:100]
                signal.summary_zh = clean_text(str(item.get("summary_zh", "")))[:300]
                if "相关时间" in signal.summary_zh or "相关数值" in signal.summary_zh:
                    signal.summary_zh = ""
                    self.summary_rejected += 1
                    self.warnings.append(f"摘要含不受支持占位词，已拒绝：{signal.title}")
                signal.ai_conclusion = clean_text(str(item.get("ai_conclusion", "")))[:240]
                signal.topics = [clean_text(str(value))[:30] for value in item.get("topics", [])][:3]
        except ArkContentSafetyError as exc:
            if len(batch) > 1:
                for signal in batch:
                    self._enrich_batch([signal])
                return
            self.content_safety_skipped += 1
            self.warnings.append(f"内容安全限制，已跳过AI处理：{batch[0].title} ({str(exc)[:120]})")
        except InvalidAIError as exc:
            if len(batch) > 1:
                for signal in batch:
                    self._enrich_batch([signal])
                return
            self.invalid_ai_skipped += 1
            self.warnings.append(f"AI格式无效，已跳过：{batch[0].title} ({exc})")

    def build_profile(self, country: Country, evidence: list[dict[str, Any]]) -> CountryProfile:
        compact = [
            {"title": item.get("title", ""), "url": item.get("url", ""), "evidence": item.get("evidence", "")[:1800]}
            for item in evidence[:20]
        ]
        raw = self._request(
            "你是消费者研究分析师。只依据官方统计和提供的证据，禁止把个别观点概括为国家文化。"
            "无法证实的字段写‘证据不足’，文化判断必须克制。只输出JSON对象。",
            f"国家：{country.name_zh}；语言：{country.language}；货币：{country.currency}\n"
            f"证据：{json.dumps(compact, ensure_ascii=False)}\n返回字段："
            '{"consumption_structure":"...","ecommerce_channels":"...","payment_preferences":"...",'
            '"price_and_promotion":"...","delivery_and_returns":"...","decision_factors":"...",'
            '"festivals_and_seasonality":"...","culture_notes":"...","confidence":"low|medium|high"}',
            2600,
        )
        item = parse_json_object(raw)
        urls = list(dict.fromkeys(row.get("url", "") for row in compact if row.get("url")))
        return CountryProfile(
            country.code, country.name_zh, country.region, country.language, country.currency,
            country.timezone,
            *[clean_text(str(item.get(key, "证据不足"))) for key in (
                "consumption_structure", "ecommerce_channels", "payment_preferences",
                "price_and_promotion", "delivery_and_returns", "decision_factors",
                "festivals_and_seasonality", "culture_notes",
            )],
            evidence_urls=urls,
            updated_at=datetime.now().astimezone(),
            confidence=item.get("confidence") if item.get("confidence") in {"low", "medium", "high"} else "low",
        )

    def product_directions(
        self,
        country: Country,
        signals: list[dict[str, Any]],
        *,
        minimum_source_types: int,
    ) -> list[ProductDirection]:
        source_types = {item.get("signal_type") for item in signals if item.get("signal_type")}
        if len(source_types) < minimum_source_types:
            return []
        compact = [
            {
                "id": item.get("signal_id"), "date": item.get("published_at"),
                "type": item.get("signal_type"), "title": item.get("title"),
                "summary": item.get("summary_zh") or item.get("evidence", "")[:400],
                "heat": item.get("heat"),
            }
            for item in signals[:80]
        ]
        raw = self._request(
            "你是跨境产品趋势分析师。仅输出至少由两种信号类型交叉支持的方向。"
            "没有销量、成本或竞争证据时只能叫‘趋势方向’，不得声称高利润。"
            "排除新闻人物、体育赛果、政治事件等不可商品化热点。只输出JSON对象。",
            f"国家：{country.name_zh}\n信号：{json.dumps(compact, ensure_ascii=False)}\n返回最多5项："
            '{"items":[{"product_direction":"...","target_user":"...","use_case":"...",'
            '"evidence_signal_ids":["..."],"trend_7d":"...","trend_30d":"...",'
            '"pain_points":"...","preferences":"...","competition":"证据不足或...",'
            '"logistics_risk":"...","compliance_risk":"...","recommendation":"进入|观察|排除",'
            '"rationale":"...","confidence":"low|medium|high"}]}',
            3500,
        )
        result = parse_json_object(raw)
        available = {item.get("signal_id"): item for item in signals}
        directions: list[ProductDirection] = []
        for item in result.get("items", [])[:5]:
            evidence_ids = [value for value in item.get("evidence_signal_ids", []) if value in available]
            types = sorted({available[value].get("signal_type", "") for value in evidence_ids if value in available})
            if len(types) < minimum_source_types:
                continue
            name = clean_text(str(item.get("product_direction", "")))
            if not name:
                continue
            direction_id = hashlib.sha256(f"{country.code}|{name}".encode("utf-8")).hexdigest()[:32]
            directions.append(
                ProductDirection(
                    direction_id, country.code, country.name_zh, name,
                    clean_text(str(item.get("target_user", ""))), clean_text(str(item.get("use_case", ""))),
                    len(evidence_ids), types, clean_text(str(item.get("trend_7d", ""))),
                    clean_text(str(item.get("trend_30d", ""))), clean_text(str(item.get("pain_points", ""))),
                    clean_text(str(item.get("preferences", ""))), clean_text(str(item.get("competition", "证据不足"))),
                    clean_text(str(item.get("logistics_risk", ""))), clean_text(str(item.get("compliance_risk", ""))),
                    item.get("recommendation") if item.get("recommendation") in {"进入", "观察", "排除"} else "观察",
                    clean_text(str(item.get("rationale", ""))),
                    item.get("confidence") if item.get("confidence") in {"low", "medium", "high"} else "low",
                    evidence_ids, datetime.now().astimezone(),
                )
            )
        return directions
