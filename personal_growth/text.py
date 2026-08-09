from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, time
from difflib import SequenceMatcher
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

SHANGHAI = ZoneInfo("Asia/Shanghai")

TITLE_EXCLUDES = (
    "广告",
    "推广",
    "招商",
    "加盟",
    "优惠券",
    "领券",
    "到手价",
    "探新低",
    "官方狂促",
    "限时秒杀",
    "应用限免",
    "运势",
    "星座",
    "会员通知",
    "网站公告",
)


def target_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time.min, tzinfo=SHANGHAI),
        datetime.combine(day, time.max, tzinfo=SHANGHAI),
    )


def from_epoch(value: int | float | str) -> datetime:
    number = float(value)
    if number > 10_000_000_000:
        number /= 1000
    return datetime.fromtimestamp(number, tz=SHANGHAI)


def parse_datetime(value: str) -> datetime:
    value = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def html_to_text(fragment: str, selector: str | None = None) -> str:
    soup = BeautifulSoup(fragment or "", "lxml")
    node = soup.select_one(selector) if selector else soup
    if node is None:
        return ""
    for unwanted in node.select(
        "script,style,noscript,svg,form,nav,footer,.comment,.comments,.recommend,.related,.copyright"
    ):
        unwanted.decompose()
    text = node.get_text("\n", strip=True)
    return clean_text(text)


def clean_text(text: str) -> str:
    text = html.unescape(text or "").replace("\u3000", " ").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    result: list[str] = []
    previous = ""
    for line in lines:
        if not line or line == previous:
            continue
        if line in {"点赞", "评论", "收藏", "分享", "举报", "加载中"}:
            continue
        result.append(line)
        previous = line
    return "\n".join(result).strip()


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+", "/", parts.path).rstrip("/")
    return urlunsplit(("https", host, path, "", ""))


def normalize_title(title: str) -> str:
    title = re.sub(r"[-_|｜].{0,12}(虎嗅|少数派|界面新闻|IT之家|钛媒体|36氪).*$", "", title)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", title.lower())


def is_obvious_exclusion(title: str) -> str:
    compact = title.replace(" ", "")
    for keyword in TITLE_EXCLUDES:
        if keyword in compact:
            return f"标题命中排除词：{keyword}"
    return ""


def split_long_text(text: str, max_chars: int = 14_000) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in text.split("\n"):
        if len(paragraph) > max_chars:
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.extend(paragraph[i : i + max_chars] for i in range(0, len(paragraph), max_chars))
            continue
        if current and size + len(paragraph) + 1 > max_chars:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def parse_json_object(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            text = match.group(0)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("model output is not a JSON object")
    return value


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()
