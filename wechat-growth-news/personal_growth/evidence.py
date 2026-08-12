from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from .models import Article
from .text import clean_text, normalize_title, normalize_url

FILTER_MIN = 900
FILTER_MAX = 1200
SUMMARY_MIN = 1600
SUMMARY_MAX = 2400
EVIDENCE_VERSION = "evidence_v1"

NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?(?:%|％|万|亿|元|美元|欧元|日元|年|月|日|天|家|人|件|台)?")
CHANGE_RE = re.compile(r"增长|下降|发布|推出|收购|融资|监管|禁止|开放|调整|裁员|上调|下调|新增|减少")
IMPACT_RE = re.compile(r"因此|导致|影响|意味着|相比|但|同时|预计|可能|风险|机会")
ENTITY_RE = re.compile(r"公司|集团|平台|政府|委员会|部门|银行|机构|国家|市场|行业|企业|品牌")


def content_hash(article: Article) -> str:
    payload = "\n".join(
        (normalize_title(article.title), normalize_url(article.url), clean_text(article.body))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def effective_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def extract_numbers(text: str) -> set[str]:
    return {item.replace("％", "%").replace(",", "") for item in NUMBER_RE.findall(text or "")}


def _sentences(body: str) -> list[tuple[int, int, str]]:
    sections = [section.strip() for section in re.split(r"\n{1,}", body) if section.strip()]
    output: list[tuple[int, int, str]] = []
    for section_index, section in enumerate(sections):
        parts = [item.strip() for item in re.split(r"(?<=[。！？；])", section) if item.strip()]
        if not parts:
            parts = [section]
        for position, sentence in enumerate(parts):
            if 12 <= len(sentence) <= 600:
                output.append((section_index, position, sentence))
    return output


def _title_terms(title: str) -> set[str]:
    compact = normalize_title(title)
    terms = set(re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,6}", compact))
    return {term for term in terms if len(term) >= 2}


def _score(sentence: str, title_terms: set[str], position: int) -> int:
    score = sum(3 for term in title_terms if term in sentence)
    score += 3 if NUMBER_RE.search(sentence) else 0
    score += 2 if CHANGE_RE.search(sentence) else 0
    score += 2 if ENTITY_RE.search(sentence) else 0
    score += 2 if position < 3 else 0
    score += 1 if IMPACT_RE.search(sentence) else 0
    return score


def extract_evidence(article: Article, minimum: int, maximum: int) -> str:
    sentences = _sentences(clean_text(article.body))
    if not sentences:
        return ""
    title_terms = _title_terms(article.title)
    ranked = [
        {
            "section": section,
            "position": position,
            "text": sentence,
            "score": _score(sentence, title_terms, position),
        }
        for section, position, sentence in sentences
    ]
    by_section: dict[int, list[dict]] = defaultdict(list)
    for item in ranked:
        by_section[item["section"]].append(item)

    selected: list[dict] = []
    for items in by_section.values():
        selected.append(max(items, key=lambda value: (value["score"], -value["position"])))
    if len(selected) > 2:
        average = max(20, sum(len(item["text"]) for item in selected) // len(selected))
        capacity = max(2, maximum // (average + 6))
        if len(selected) > capacity:
            indexes = {
                round(position * (len(selected) - 1) / (capacity - 1))
                for position in range(capacity)
            }
            selected = [item for index, item in enumerate(selected) if index in indexes]
    selected_ids = {(item["section"], item["position"]) for item in selected}
    if sum(len(value["text"]) for value in selected) < minimum:
        for item in sorted(ranked, key=lambda value: value["score"], reverse=True):
            identity = (item["section"], item["position"])
            if identity in selected_ids:
                continue
            selected.append(item)
            selected_ids.add(identity)
            if sum(len(value["text"]) for value in selected) >= minimum:
                break

    selected.sort(key=lambda value: (value["section"], value["position"]))
    lines: list[str] = []
    size = 0
    seen: set[str] = set()
    for item in selected:
        sentence = item["text"]
        normalized = re.sub(r"\W+", "", sentence)
        if normalized in seen:
            continue
        line = f"E{len(lines) + 1:03d} {sentence}"
        if lines and size + len(line) + 1 > maximum:
            continue
        lines.append(line)
        seen.add(normalized)
        size += len(line) + 1
    return "\n".join(lines)


def evidence_packets(article: Article) -> tuple[str, str]:
    return (
        extract_evidence(article, FILTER_MIN, FILTER_MAX),
        extract_evidence(article, SUMMARY_MIN, SUMMARY_MAX),
    )
