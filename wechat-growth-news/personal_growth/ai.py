from __future__ import annotations

import json
import os
import random
import re
import time
from typing import Any

import requests

from .evidence import effective_chars, extract_numbers
from .config import model_config, summary_config
from .models import Article
from .state import StateStore
from .text import SHANGHAI, clean_text, parse_json_object

CATEGORIES = ("心理与关系", "健康与生活", "社会与地理", "商业与趋势", "文化与思想")
FILTER_VERSION = "filter_wechat_v1"
SUMMARY_VERSION = "summary_wechat_v3"
FILTER_STAGE = "filter"
SUMMARY_STAGE = "summary"
FILTER_CORRECTION_MAX_TOKENS = 260


class ArkRateLimitError(RuntimeError):
    def __init__(self, detail: str, retry_after: float | None = None) -> None:
        self.detail = detail
        self.retry_after = retry_after
        super().__init__(f"豆包429限流：{detail}")


class ArkAPIError(RuntimeError):
    pass


class ArkAnalyzer:
    def __init__(self) -> None:
        config = model_config()
        self.provider = str(config.get("provider", "volcengine_responses"))
        key_env = str(config.get("api_key_env", "ARK_API_KEY"))
        self.api_key = os.environ.get(key_env, "")
        self.model = os.environ.get("AI_MODEL", str(config.get("model", "doubao-seed-2-0-mini-260215")))
        self.url = os.environ.get("AI_BASE_URL", str(config.get("base_url", "https://ark.cn-beijing.volces.com/api/v3/responses")))
        if not self.api_key:
            raise RuntimeError(f"缺少模型密钥环境变量 {key_env}")
        self.session = requests.Session()  # Deliberately no urllib3 status retry for Ark.
        self.minimum_interval = float(os.environ.get("ARK_MIN_INTERVAL_SECONDS", "4"))
        self.max_429_retries = int(os.environ.get("ARK_MAX_429_RETRIES", "4"))
        self.circuit_limit = int(os.environ.get("ARK_429_CIRCUIT_LIMIT", "3"))
        self.backoff = [float(item) for item in os.environ.get("ARK_BACKOFF_SECONDS", "60,120,300,600").split(",")]
        self.timeout = int(os.environ.get("ARK_TIMEOUT", "90"))
        self.last_request_at = 0.0
        self.consecutive_429 = 0
        self.filter_calls = 0
        self.summary_calls = 0
        self.rate_limit_count = 0
        self.retry_wait_seconds = 0.0
        self.cache_hits = 0

    def _wait_for_slot(self) -> None:
        remaining = self.minimum_interval - (time.monotonic() - self.last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    @staticmethod
    def _output_text(payload: dict[str, Any]) -> str:
        try:
            return next(
                content["text"]
                for item in payload["output"]
                if item.get("type") == "message"
                for content in item.get("content", [])
                if content.get("type") in {"output_text", "text"} and content.get("text")
            ).strip()
        except (KeyError, StopIteration, TypeError) as exc:
            raise ArkAPIError(f"豆包返回结构异常：{str(payload)[:1200]}") from exc

    @staticmethod
    def _chat_output_text(payload: dict[str, Any]) -> str:
        try:
            return str(payload["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ArkAPIError(f"兼容模型返回结构异常：{str(payload)[:1200]}") from exc

    def _request(self, system: str, user: str, max_output_tokens: int, stage: str) -> str:
        for attempt in range(self.max_429_retries + 1):
            self._wait_for_slot()
            if stage == FILTER_STAGE:
                self.filter_calls += 1
            else:
                self.summary_calls += 1
            try:
                response = self.session.post(
                    self.url,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=(
                        {
                            "model": self.model,
                            "messages": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": user},
                            ],
                            "max_tokens": max_output_tokens,
                            "temperature": 0.1,
                        }
                        if self.provider == "openai_chat"
                        else {
                            "model": self.model,
                            "input": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": user},
                            ],
                            "thinking": {"type": "disabled"},
                            "max_output_tokens": max_output_tokens,
                        }
                    ),
                    timeout=self.timeout,
                )
                self.last_request_at = time.monotonic()
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self.max_429_retries:
                    raise ArkAPIError(f"豆包网络失败：{exc}") from exc
                wait = min(self.backoff[min(attempt, len(self.backoff) - 1)], 600)
                self.retry_wait_seconds += wait
                time.sleep(wait)
                continue

            detail = response.text[:1600]
            if response.status_code == 429:
                self.rate_limit_count += 1
                self.consecutive_429 += 1
                retry_header = response.headers.get("Retry-After", "")
                retry_after = float(retry_header) if retry_header.replace(".", "", 1).isdigit() else None
                # A model-level safety quota pauses service until the account setting
                # is changed; waiting and retrying cannot recover it.
                if "SetLimitExceeded" in detail:
                    raise ArkRateLimitError(detail, retry_after)
                if attempt >= self.max_429_retries or self.consecutive_429 >= self.circuit_limit:
                    raise ArkRateLimitError(detail, retry_after)
                configured = self.backoff[min(attempt, len(self.backoff) - 1)]
                wait = min(max(retry_after or 0, configured) + random.uniform(0, 3), 600)
                self.retry_wait_seconds += wait
                time.sleep(wait)
                continue
            if response.status_code in {400, 401, 403}:
                raise ArkAPIError(f"豆包不可重试错误 HTTP {response.status_code}：{detail}")
            if response.status_code >= 500:
                if attempt >= self.max_429_retries:
                    raise ArkAPIError(f"豆包服务错误 HTTP {response.status_code}：{detail}")
                wait = min(self.backoff[min(attempt, len(self.backoff) - 1)], 600)
                self.retry_wait_seconds += wait
                time.sleep(wait)
                continue
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise ArkAPIError(f"豆包返回非JSON：{detail}") from exc
            if payload.get("error"):
                raise ArkAPIError(f"豆包接口错误：{payload['error']}")
            self.consecutive_429 = 0
            return self._chat_output_text(payload) if self.provider == "openai_chat" else self._output_text(payload)
        raise ArkAPIError("豆包请求未获得结果")

    @staticmethod
    def _filter_prompts(article: Article, evidence: str) -> tuple[str, str]:
        categories = "、".join(CATEGORIES)
        system = (
            "你是公众号内容质量检查与分类器，只依据提供的证据判断。只淘汰广告、促销、纯导购、"
            "纯图片或视频、正文不完整、无实质信息和明显重复内容。心理、健康、地理、社会、商业、财经、"
            "文化、艺术与思想类的有效文章都应入选，不因题材普通、不是新闻或缺少数据而淘汰。"
            "只输出一行合法JSON，不要Markdown或解释。五项评分为0到5整数。"
        )
        user = (
            f"标题：{article.title}\n来源：{article.source}\n发布日期：{article.published_at.isoformat()}\n\n"
            f"正文证据：\n{evidence}\n\n"
            f"category只能是：{categories}。reason不超过30字，event_key为12到30字，topics为1到3个短标签，"
            "unique_points为0到3条有证据的信息增量。返回："
            '{"status":"入选或淘汰","reason":"...","event_key":"...","topics":["..."],'
            '"category":"...","unique_points":["..."],"scores":{"importance":0,'
            '"information_gain":0,"social_impact":0,"actionability":0,"evidence_density":0}}'
        )
        return system, user

    @staticmethod
    def _validate_filter(raw: str) -> dict[str, Any]:
        result = parse_json_object(raw)
        if result.get("status") not in {"入选", "淘汰"}:
            raise ValueError("初筛status非法")
        scores = result.get("scores")
        if not isinstance(scores, dict):
            raise ValueError("初筛scores缺失")
        keys = ("importance", "information_gain", "social_impact", "actionability", "evidence_density")
        normalized: dict[str, int] = {}
        for key in keys:
            value = int(scores.get(key, -1))
            if not 0 <= value <= 5:
                raise ValueError(f"初筛评分{key}非法")
            normalized[key] = value
        result["scores"] = normalized
        result["score_total"] = sum(normalized.values())
        result["reason"] = clean_text(str(result.get("reason", "")))[:30]
        result["event_key"] = clean_text(str(result.get("event_key", "")))[:40]
        result["topics"] = [clean_text(str(item))[:20] for item in (result.get("topics") or [])][:3]
        result["unique_points"] = [clean_text(str(item)) for item in (result.get("unique_points") or [])][:3]
        if result.get("category") not in CATEGORIES:
            result["category"] = "文化与思想"
        result["valuable"] = result["status"] == "入选"
        if not result["valuable"]:
            result["status"] = "淘汰"
        return result

    @staticmethod
    def _summary_prompts(article: Article, evidence: str) -> tuple[str, str]:
        config = summary_config()
        minimum = int(config.get("minimum_chars", 300))
        maximum = int(config.get("maximum_chars", 500))
        target_minimum = int(config.get("target_minimum_chars", 340))
        target_maximum = int(config.get("target_maximum_chars", 420))
        system = (
            "你是严谨的个人成长资讯编辑，只使用提供的证据，不得补充外部事实、数字或因果。"
            "输出恰好三段连续正文，不要标题、标签、项目符号、JSON、开场白或结束语。"
            f"总长度严格{minimum}到{maximum}个可见字符，目标{target_minimum}到{target_maximum}字。"
            "第一段提炼文章主旨、对象、时间和关键事实；第二段解释论证、原因、机制、数据或重要观点；"
            "第三段总结对个人认知、健康、决策、行动或理解世界的可迁移启示。"
            "不得强行商业化或强行动建议；原文没有数字就不编造，禁止空话和标题改写。"
            "禁止使用‘相关时间’‘相关数值’等占位词；证据不足时只输出：证据不足，无法生成可靠摘要"
        )
        user = (
            f"标题：{article.title}\n来源：{article.source}\n发布日期：{article.published_at.isoformat()}\n\n"
            f"编号原文证据：\n{evidence}\n\n请生成{minimum}到{maximum}字个人成长干货摘要。"
        )
        return system, user

    @staticmethod
    def _clean_summary(raw: str) -> str:
        text = raw.strip()
        text = re.sub(r"^```(?:text)?\s*|\s*```$", "", text, flags=re.S)
        return text.strip()

    @classmethod
    def _fit_summary_length(cls, raw: str) -> str:
        summary = cls._clean_summary(raw)
        size = effective_chars(summary)
        paragraphs = [item.strip() for item in re.split(r"\n+", summary) if item.strip()]
        if size <= 500 or len(paragraphs) != 3:
            return summary
        lengths = [effective_chars(item) for item in paragraphs]
        budgets = [min(length, 100) for length in lengths]
        remaining = 480 - sum(budgets)
        while remaining > 0:
            candidates = [index for index, length in enumerate(lengths) if budgets[index] < length]
            if not candidates:
                break
            for index in candidates:
                if remaining <= 0:
                    break
                addition = min(lengths[index] - budgets[index], max(1, remaining // len(candidates)))
                budgets[index] += addition
                remaining -= addition

        fitted: list[str] = []
        for paragraph, budget in zip(paragraphs, budgets):
            if effective_chars(paragraph) <= budget:
                fitted.append(paragraph)
                continue
            visible = 0
            chars: list[str] = []
            for char in paragraph:
                if not char.isspace():
                    if visible >= budget - 1:
                        break
                    visible += 1
                chars.append(char)
            fitted.append("".join(chars).rstrip("，；：、。！？ ") + "。")
        return "\n\n".join(fitted)

    @classmethod
    def validate_summary(cls, raw: str, evidence: str) -> str:
        config = summary_config()
        minimum = int(config.get("minimum_chars", 300))
        maximum = int(config.get("maximum_chars", 500))
        summary = cls._clean_summary(raw)
        summary = cls._fit_summary_length(summary)
        if summary == "证据不足，无法生成可靠摘要":
            raise ValueError("证据不足")
        if "相关时间" in summary or "相关数值" in summary:
            raise ValueError("摘要包含占位词")
        size = effective_chars(summary)
        if not minimum <= size <= maximum:
            raise ValueError(f"摘要字数不合格：{size}")
        paragraphs = [item.strip() for item in re.split(r"\n+", summary) if item.strip()]
        if len(paragraphs) != 3:
            raise ValueError(f"摘要必须恰好三段，实际{len(paragraphs)}段")
        invented = extract_numbers(summary) - extract_numbers(evidence)
        if invented:
            raise ValueError(f"摘要出现证据外数字：{sorted(invented)}")
        return summary

    @staticmethod
    def _validation_evidence(article: Article) -> str:
        published = article.published_at.astimezone(SHANGHAI)
        published_zh = f"{published.year}年{published.month}月{published.day}日"
        return f"{article.title}\n{published.isoformat()}\n{published_zh}\n{article.body}"

    def analyze(
        self,
        article: Article,
        filter_evidence: str,
        summary_evidence: str,
        cache_key: str,
        state: StateStore,
    ) -> dict[str, Any]:
        filter_result = state.get_stage(cache_key, FILTER_STAGE, f"{self.model}:{FILTER_VERSION}")
        if filter_result is None:
            system, user = self._filter_prompts(article, filter_evidence)
            raw_filter = self._request(system, user, 260, FILTER_STAGE)
            try:
                filter_result = self._validate_filter(raw_filter)
            except (ValueError, TypeError, KeyError) as first_error:
                correction = (
                    f"上一版初筛结果无法解析：{first_error}。\n"
                    f"上一版：\n{raw_filter}\n\n"
                    "请保持原判断，只修正为提示词要求的一行合法JSON；不要Markdown或解释。"
                )
                corrected = self._request(
                    system,
                    correction,
                    FILTER_CORRECTION_MAX_TOKENS,
                    FILTER_STAGE,
                )
                try:
                    filter_result = self._validate_filter(corrected)
                except (ValueError, TypeError, KeyError) as second_error:
                    filter_result = {
                        "status": "淘汰",
                        "valuable": False,
                        "reason": "AI格式错误跳过",
                        "invalid_ai": True,
                        "invalid_ai_error": clean_text(str(second_error))[:200],
                    }
            if not filter_result.get("invalid_ai"):
                state.put_stage(cache_key, FILTER_STAGE, f"{self.model}:{FILTER_VERSION}", filter_result)
        else:
            self.cache_hits += 1
        if not filter_result.get("valuable"):
            return filter_result

        summary_result = state.get_stage(cache_key, SUMMARY_STAGE, f"{self.model}:{SUMMARY_VERSION}")
        if summary_result is None:
            system, user = self._summary_prompts(article, summary_evidence)
            raw = self._request(system, user, 900, SUMMARY_STAGE)
            validation_evidence = self._validation_evidence(article)
            try:
                summary = self.validate_summary(raw, validation_evidence)
            except ValueError as first_error:
                if str(first_error) == "证据不足":
                    summary_result = {"status": "淘汰", "reason": "摘要证据不足"}
                    return {**filter_result, **summary_result, "valuable": False}
                correction = (
                    f"上一版摘要未通过校验：{first_error}。\n上一版：\n{raw}\n\n"
                    f"原证据：\n{summary_evidence}\n\n严格重写为三段、340到420字，绝对不超过480字，只输出正文。"
                    "删除证据中不存在的数字，禁止用‘相关时间’或‘相关数值’代替。"
                )
                corrected = self._request(system, correction, 900, SUMMARY_STAGE)
                try:
                    summary = self.validate_summary(corrected, validation_evidence)
                except ValueError as second_error:
                    summary_result = {"status": "淘汰", "reason": f"摘要校验失败：{second_error}"[:80]}
                    return {**filter_result, **summary_result, "valuable": False}
            summary_result = {"status": "入选", "core_content": summary, "summary_chars": effective_chars(summary)}
            state.put_stage(cache_key, SUMMARY_STAGE, f"{self.model}:{SUMMARY_VERSION}", summary_result)
        else:
            self.cache_hits += 1

        result = {**filter_result, **summary_result}
        result["valuable"] = result.get("status") == "入选" and bool(result.get("core_content"))
        result["core_facts"] = [line.split(" ", 1)[-1] for line in summary_evidence.splitlines()[:3]]
        result["key_numbers"] = sorted(extract_numbers(result.get("core_content", "")))
        return result
