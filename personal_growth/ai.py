from __future__ import annotations

import os
from typing import Any

import requests

from .http import HttpClient
from .models import Article
from .text import clean_text, parse_json_object, split_long_text

CATEGORIES = (
    "科技与AI",
    "商业与公司",
    "财经与投资",
    "宏观与政策",
    "职业与效率",
    "消费与生活",
    "社会与文化",
    "全球与出海",
)
SCORE_KEYS = ("importance", "information_gain", "social_impact", "actionability", "evidence_density")


class ArkAnalyzer:
    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.api_key = os.environ.get("ARK_API_KEY", "")
        self.model = os.environ.get("ARK_MODEL", "doubao-seed-2-0-mini-260215")
        self.url = "https://ark.cn-beijing.volces.com/api/v3/responses"
        if not self.api_key:
            raise RuntimeError("缺少 ARK_API_KEY")

    def _chat(self, system: str, user: str) -> dict[str, Any]:
        try:
            response = self.http.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "input": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "thinking": {"type": "disabled"},
                },
            ).json()
        except requests.HTTPError as exc:
            detail = exc.response.text[:1200] if exc.response is not None else ""
            raise RuntimeError(f"豆包HTTP失败：{exc}；响应：{detail}") from exc
        if response.get("error"):
            raise RuntimeError(f"豆包接口失败：{response['error']}")
        try:
            text = next(
                content["text"]
                for item in response["output"]
                if item.get("type") == "message"
                for content in item.get("content", [])
                if content.get("type") in {"output_text", "text"} and content.get("text")
            )
        except (KeyError, StopIteration, TypeError) as exc:
            raise RuntimeError(f"豆包返回结构异常：{response}") from exc
        return parse_json_object(text)

    def _summarize_chunk(self, article: Article, chunk: str, index: int, total: int) -> dict[str, Any]:
        return self._chat(
            "你是严谨的新闻事实提取器。只依据正文提取，不补充外部知识。必须返回JSON对象。",
            (
                f"文章：{article.title}\n这是正文第{index}/{total}段。"
                "提取本段的核心事实、关键数字、时间、主体、变化、论据和有证据的独特观点。"
                '返回：{"facts":["..."],"numbers":["..."],"impacts":["..."],"unique_points":["..."]}。\n\n'
                f"{chunk}"
            ),
        )

    @staticmethod
    def _strings(value: Any, minimum: int = 0, maximum: int = 99) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("结构化字段不是数组")
        items = [clean_text(str(item)) for item in value if clean_text(str(item))]
        if not minimum <= len(items) <= maximum:
            raise ValueError(f"数组条数应为{minimum}–{maximum}，实际{len(items)}")
        return items

    @staticmethod
    def render_core_content(result: dict[str, Any]) -> str:
        facts = result["core_facts"]
        numbers = result["key_numbers"]
        actions = result["actions"]
        parts = ["核心事实：" + " ".join(f"{i + 1}. {fact}" for i, fact in enumerate(facts))]
        if numbers:
            parts.append("关键数字：" + "；".join(numbers))
        parts.append("影响判断：" + result["impact"])
        parts.append("可行动启示：" + " ".join(f"{i + 1}. {item}" for i, item in enumerate(actions)))
        return "\n".join(parts)

    @classmethod
    def validate_result(cls, result: dict[str, Any]) -> dict[str, Any]:
        required = {
            "candidate", "exclusion_reason", "core_facts", "key_numbers", "impact", "actions",
            "topics", "category", "event_key", "unique_points", "scores",
        }
        if not required.issubset(result):
            raise ValueError(f"豆包结果缺少字段：{required - set(result)}")
        result["core_facts"] = cls._strings(result["core_facts"], 3, 6)
        result["key_numbers"] = cls._strings(result["key_numbers"], 0, 12)
        result["actions"] = cls._strings(result["actions"], 1, 3)
        result["topics"] = cls._strings(result["topics"], 1, 3)
        result["unique_points"] = cls._strings(result["unique_points"], 0, 6)
        result["impact"] = clean_text(str(result["impact"]))
        result["event_key"] = clean_text(str(result["event_key"]))
        if len(result["impact"]) < 35 or len(result["event_key"]) < 8:
            raise ValueError("影响判断或事件标识过短")
        scores = result["scores"]
        if not isinstance(scores, dict):
            raise ValueError("scores不是对象")
        normalized_scores: dict[str, int] = {}
        for key in SCORE_KEYS:
            score = int(scores.get(key, -1))
            if not 0 <= score <= 5:
                raise ValueError(f"评分{key}超出0–5")
            normalized_scores[key] = score
        result["scores"] = normalized_scores
        result["score_total"] = sum(normalized_scores.values())
        result["category"] = result["category"] if result["category"] in CATEGORIES else "社会与文化"
        result["candidate"] = bool(result["candidate"])
        result["valuable"] = bool(
            result["candidate"]
            and result["score_total"] >= 15
            and normalized_scores["evidence_density"] >= 3
            and normalized_scores["information_gain"] >= 2
        )
        result["core_content"] = cls.render_core_content(result)
        compact_length = len("".join(result["core_content"].split()))
        if result["valuable"] and not 300 <= compact_length <= 500:
            raise ValueError(f"核心干货长度不合格：{compact_length}字")
        return result

    def analyze(self, article: Article) -> dict[str, Any]:
        chunks = split_long_text(article.body)
        if len(chunks) == 1:
            evidence = chunks[0]
        else:
            summaries = [self._summarize_chunk(article, chunk, i + 1, len(chunks)) for i, chunk in enumerate(chunks)]
            evidence = "全文分段事实提取：\n" + "\n".join(str(item) for item in summaries)
        categories = "、".join(CATEGORIES)
        result = self._chat(
            (
                "你是严谨的个人成长资讯主编，只依据给定正文。广告、品牌软文、商品推广、招商加盟、"
                "快讯、鸡汤、无实质信息娱乐稿应排除。对深度分析、独家事实、关键数据和可行动洞察应保留，"
                "不因来源或篇幅偏置。不得虚构数字；原文无关键数字时key_numbers返回空数组。必须返回JSON对象。"
            ),
            (
                f"来源：{article.source}\n标题：{article.title}\n发布日期：{article.published_at.isoformat()}\n"
                f"原摘要：{article.summary}\n\n{evidence}\n\n"
                "生成可直接入库的结构化编辑结果。core_facts为3–6条具体事实，必须包含主体、动作、时间或变化；"
                "impact说明二阶影响；actions为1–3条可执行启示；四部分组合后的中文核心干货目标300–500字。"
                "topics为1–3个短标签；event_key用12–30字描述主体与核心变化；unique_points列出相较同类报道"
                "可能构成增量的独家事实、关键数据或有证据的分析角度。五项评分均为0–5整数。"
                f"category只能从这些值选择：{categories}。"
                '返回：{"candidate":true,"exclusion_reason":"","core_facts":["..."],'
                '"key_numbers":["..."],"impact":"...","actions":["..."],"topics":["..."],'
                '"category":"...","event_key":"...","unique_points":["..."],'
                '"scores":{"importance":0,"information_gain":0,"social_impact":0,'
                '"actionability":0,"evidence_density":0}}'
            ),
        )
        return self.validate_result(result)
