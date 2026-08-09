from __future__ import annotations

import os
from typing import Any

from .http import HttpClient
from .models import Article
from .text import parse_json_object, split_long_text

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


class ArkAnalyzer:
    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.api_key = os.environ.get("ARK_API_KEY", "")
        self.model = os.environ.get("ARK_MODEL", "doubao-seed-2-0-mini-260215")
        self.url = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        if not self.api_key:
            raise RuntimeError("缺少 ARK_API_KEY")

    def _chat(self, system: str, user: str) -> dict[str, Any]:
        response = self.http.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        ).json()
        if response.get("error"):
            raise RuntimeError(f"豆包接口失败：{response['error']}")
        try:
            text = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"豆包返回结构异常：{response}") from exc
        return parse_json_object(text)

    def _summarize_chunk(self, article: Article, chunk: str, index: int, total: int) -> dict[str, Any]:
        return self._chat(
            "你是严谨的新闻事实提取器。只依据正文提取，不补充外部知识。必须返回JSON对象。",
            (
                f"文章：{article.title}\n这是正文第{index}/{total}段。"
                "提取本段的核心事实、数字、参与方、变化和潜在影响。"
                '返回：{"facts":["..."],"impacts":["..."]}。\n\n'
                f"{chunk}"
            ),
        )

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
                "你是个人成长资讯编辑。按广泛重要性、信息增量、社会影响和可行动性筛选。"
                "排除广告、品牌软文、商品推广、招商加盟、鸡汤情感、哲理随笔、星座运势、"
                "无实质信息的娱乐八卦、公告通知。只依据给定全文或全文分段事实，不虚构。"
                "必须返回JSON对象。"
            ),
            (
                f"来源：{article.source}\n标题：{article.title}\n发布日期：{article.published_at.isoformat()}\n"
                f"原摘要：{article.summary}\n\n{evidence}\n\n"
                "判断是否值得收入个人成长资讯库。content_topic必须是精炼核心事实＋价值/影响判断，"
                "不是标题改写；event_key用12到30字描述事件主体与核心变化，用于跨站去重；"
                f"category只能从这些值选择：{categories}。"
                '返回：{"valuable":true,"reason":"...","content_topic":"...",'
                '"category":"...","event_key":"..."}'
            ),
        )
        required = {"valuable", "reason", "content_topic", "category", "event_key"}
        if not required.issubset(result):
            raise ValueError(f"豆包结果缺少字段：{required - set(result)}")
        if result["category"] not in CATEGORIES:
            result["category"] = "社会与文化"
        result["valuable"] = bool(result["valuable"])
        return result
