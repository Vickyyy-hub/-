from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Iterable

from .config import output_config
from .models import Article


FIELDS = (
    "标题", "来源名称", "来源网站", "封面", "发布日期", "日报日期",
    "内容主题", "归档板块", "记录状态", "核心干货", "失败原因",
)


def article_row(article: Article) -> dict[str, str]:
    ai = article.ai_result or {}
    return {
        "标题": article.title,
        "来源名称": article.source,
        "来源网站": article.url,
        "封面": str(article.meta.get("image") or ""),
        "发布日期": article.published_at.isoformat(),
        "日报日期": str(article.daily_date or article.published_at.date()),
        "内容主题": "、".join(ai.get("topics") or []),
        "归档板块": str(ai.get("category") or ""),
        "记录状态": article.record_status,
        "核心干货": str(ai.get("core_content") or ""),
        "失败原因": str(article.meta.get("failure_reason") or ""),
    }


def _path(spec: dict, day: date) -> Path:
    path = Path(str(spec.get("path", "output/selected-{date}.json")).format(date=day.isoformat()))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_local_outputs(articles: Iterable[Article], day: date) -> dict[str, str]:
    rows = [article_row(article) for article in articles]
    written: dict[str, str] = {}
    json_spec = output_config("json")
    if json_spec.get("enabled"):
        path = _path(json_spec, day)
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        written["json"] = str(path)
    csv_spec = output_config("csv")
    if csv_spec.get("enabled"):
        path = _path(csv_spec, day)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        written["csv"] = str(path)
    return written
