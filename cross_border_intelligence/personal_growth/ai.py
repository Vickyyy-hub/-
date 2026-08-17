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
FILTER_VERSION = "cross_filter_v2"
SUMMARY_VERSION = "cross_summary_v4"
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

    @staticmethod
    def _visible_length(value: str) -> int:
        return len(re.sub(r"\s+", "", value))

    @staticmethod
    def _numbers(value: str) -> set[str]:
        result: set[str] = set()
        for item in re.findall(r"(?<![A-Za-z])\d[\d,.]*%?", value):
            compact = item.replace(",", "")
            suffix = "%" if compact.endswith("%") else ""
            compact = compact.removesuffix("%").rstrip(".")
            if "." in compact:
                left, right = compact.split(".", 1)
                compact = f"{int(left or '0')}.{right}"
            elif compact:
                compact = str(int(compact))
            result.add(compact + suffix)
        return result

    def enrich_signals(self, signals: list[Signal], progressive_callback=None) -> list[Signal]:
        selected: list[Signal] = []
        for start in range(0, len(signals), 12):
            batch = signals[start:start + 12]
            selected.extend(self._enrich_batch(batch))
            if progressive_callback:
                progressive_callback(selected)
        return selected

    def _enrich_batch(self, batch: list[Signal]) -> list[Signal]:
        if not batch:
            return []
        try:
            evidence = [
                {
                    "id": item.signal_id,
                    "title": item.title,
                    "source": item.source_name,
                    "country": item.countries,
                    "type": item.signal_type,
                    "keyword": item.original_keyword,
                    "evidence": item.evidence[:1600],
                }
                for item in batch
            ]
            raw = self._request(
                "你是跨境资讯主编，执行第一阶段严格筛选。只依据正文证据。仅保留与跨境贸易、"
                "海关关税、产品安全、平台规则、跨境电商或可验证消费需求直接相关的新闻。"
                "排除体育、明星、比赛、普通娱乐、政治人物热度、栏目页、API说明、统计数据库入口、"
                "没有具体事件的内容，以及无法说明对卖家/平台/消费者/供应链直接影响的内容。只输出JSON。",
                "输入：" + json.dumps(evidence, ensure_ascii=False) + "\n返回格式："
                '{"items":[{"id":"...","selected":true,"reason":"...","keyword_zh":"...",'
                '"topic":"政策合规|海关贸易|产品安全|平台动态|消费需求|供应链",'
                '"section":"欧洲市场|拉美市场|全球政策|平台与需求"}]}',
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
            selected: list[Signal] = []
            for signal in batch:
                item = mapped.get(signal.signal_id)
                if not item or item.get("selected") is not True:
                    continue
                signal.keyword_zh = clean_text(str(item.get("keyword_zh", "")))[:100]
                topic = clean_text(str(item.get("topic", "")))
                section = clean_text(str(item.get("section", "")))
                signal.topics = [topic if topic in {"政策合规", "海关贸易", "产品安全", "平台动态", "消费需求", "供应链"} else signal.signal_type]
                signal.ai_conclusion = section if section in {"欧洲市场", "拉美市场", "全球政策", "平台与需求"} else "全球政策"
                summary = self._summarize(signal)
                reason = self._summary_invalid_reason(signal, summary)
                if reason:
                    summary = self._summarize(signal, previous=summary, reason=reason)
                    reason = self._summary_invalid_reason(signal, summary)
                if reason:
                    self.summary_rejected += 1
                    self.warnings.append(f"摘要两次校验不合格，已跳过：{signal.title}（{reason}）")
                    continue
                signal.summary_zh = summary
                signal.meta["filter_version"] = FILTER_VERSION
                signal.meta["summary_version"] = SUMMARY_VERSION
                selected.append(signal)
            return selected
        except ArkContentSafetyError as exc:
            if len(batch) > 1:
                selected = []
                for signal in batch:
                    selected.extend(self._enrich_batch([signal]))
                return selected
            self.content_safety_skipped += 1
            self.warnings.append(f"内容安全限制，已跳过AI处理：{batch[0].title} ({str(exc)[:120]})")
        except InvalidAIError as exc:
            if len(batch) > 1:
                selected = []
                for signal in batch:
                    selected.extend(self._enrich_batch([signal]))
                return selected
            self.invalid_ai_skipped += 1
            self.warnings.append(f"AI格式无效，已跳过：{batch[0].title} ({exc})")
        return []

    def _summarize(self, signal: Signal, *, previous: str = "", reason: str = "") -> str:
        instruction = "请生成成品摘要。"
        if previous:
            instruction = f"上次摘要未通过校验（{reason}）：{previous}\n请针对问题重写，不能复述原结果。"
        raw = self._request(
            "你是跨境商业资讯编辑。只依据所给文章正文，写一段中文核心干货，80至150个可见字符，"
            "目标约100字，不换行。依次交代：谁在何时做了什么或发生何变化；关键政策、数据、产品或"
            "市场信息；对跨境卖家、平台、消费者或供应链的直接影响。禁止标题改写、空话、猜测、"
            "证据外数字和虚构结论。影响必须由正文明确支持；不能为了凑结构添加笼统的‘将影响跨境供应链’。"
            "正文没写日期时不要编日期。只输出JSON。",
            instruction + "\n" + json.dumps({
                "id": signal.signal_id, "title": signal.title,
                "published_at": signal.published_at.isoformat(),
                "evidence": signal.evidence[:6000],
            }, ensure_ascii=False) + '\n返回：{"summary_zh":"..."}',
            900,
        )
        try:
            return clean_text(str(parse_json_object(raw).get("summary_zh", "")))
        except (ValueError, json.JSONDecodeError):
            return ""

    def _summary_valid(self, signal: Signal, summary: str) -> bool:
        return not self._summary_invalid_reason(signal, summary)

    def _summary_invalid_reason(self, signal: Signal, summary: str) -> str:
        length = self._visible_length(summary)
        if not 80 <= length <= 150:
            return f"可见字符{length}，要求80至150"
        if "\n" in summary:
            return "必须是一段"
        if any(value in summary for value in ("相关时间", "相关数值")):
            return "包含占位表达"
        subjective = next((value for value in ("政策利好", "重大利好", "利空", "高利润", "巨大商机") if value in summary), "")
        if subjective:
            return f"包含证据外判断：{subjective}"
        evidence_numbers = self._numbers(signal.title + "\n" + signal.published_at.isoformat() + "\n" + signal.evidence)
        unsupported = self._numbers(summary) - evidence_numbers
        return f"出现正文外数字：{sorted(unsupported)}" if unsupported else ""

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
