from datetime import date

from personal_growth.feishu import FeishuBitable
from personal_growth.ai import ArkAnalyzer
from personal_growth.models import Article
from personal_growth.pipeline import Pipeline
from personal_growth.text import (
    SHANGHAI,
    from_epoch,
    is_obvious_exclusion,
    normalize_title,
    normalize_url,
    parse_json_object,
    split_long_text,
    target_bounds,
)


def test_target_bounds_are_shanghai_natural_day():
    start, end = target_bounds(date(2026, 8, 8))
    assert start.isoformat() == "2026-08-08T00:00:00+08:00"
    assert end.date() == date(2026, 8, 8)
    assert end.hour == 23 and end.minute == 59


def test_epoch_milliseconds_and_seconds_match():
    assert from_epoch(1786195964) == from_epoch(1786195964000)
    assert from_epoch(1786195964).tzinfo == SHANGHAI


def test_normalization_and_obvious_exclusions():
    assert normalize_url("http://www.ithome.com/0/1/2.htm?x=1") == "https://ithome.com/0/1/2.htm"
    assert normalize_title("测试标题 - IT之家") == "测试标题"
    assert "排除词" in is_obvious_exclusion("某商品官方狂促：到手价9元")


def test_json_parser_and_chunker():
    assert parse_json_object('```json\n{"valuable": true}\n```')["valuable"] is True
    chunks = split_long_text("甲" * 100 + "\n" + "乙" * 100, max_chars=120)
    assert "".join(chunks).replace("\n", "") == "甲" * 100 + "乙" * 100
    assert all(len(chunk) <= 120 for chunk in chunks)


def _ai(score: int, unique: str) -> dict:
    return {
        "event_key": "公司发布新模型",
        "score_total": score,
        "scores": {"information_gain": 4, "evidence_density": 4},
        "core_facts": ["事实一", "事实二", "事实三"],
        "unique_points": [unique],
        "valuable": True,
    }


def test_event_group_uses_quality_and_keeps_real_increment():
    first = Article("甲", "公司发布新模型", "https://a.test/1", from_epoch(1786195964), body="长" * 500)
    second = Article("乙", "公司正式发布新模型", "https://b.test/2", from_epoch(1786195964), body="短" * 100)
    first.ai_result = _ai(16, "同一组事实")
    second.ai_result = _ai(22, "新增独家数据")
    kept, duplicates = Pipeline._assign_statuses([first, second])
    assert kept[0] == second
    assert second.record_status == "主稿"
    assert first.record_status == "增量稿"
    assert duplicates == 0


def test_ai_validation_rejects_short_core_content():
    result = {
        "candidate": True,
        "exclusion_reason": "",
        "core_facts": ["短事实一", "短事实二", "短事实三"],
        "key_numbers": [],
        "impact": "这是一段长度足够通过单字段校验的影响判断，但全部结构组合后依然明显不足三百字，因此必须触发整体长度校验并要求重新生成。",
        "actions": ["采取行动"],
        "topics": ["人工智能"],
        "category": "科技与AI",
        "event_key": "公司发布新模型带来变化",
        "unique_points": [],
        "scores": {
            "importance": 4, "information_gain": 4, "social_impact": 3,
            "actionability": 3, "evidence_density": 4,
        },
    }
    try:
        ArkAnalyzer.validate_result(result)
    except ValueError as exc:
        assert "长度不合格" in str(exc)
    else:
        raise AssertionError("短摘要应被拒绝")


def test_feishu_hyperlink_shape_without_initializing_client():
    client = object.__new__(FeishuBitable)
    client.field_types = {"来源网站": 15, "内容主题": 4}
    assert client._source_value("https://example.com") == {
        "text": "https://example.com",
        "link": "https://example.com",
    }
    assert client._field_value("内容主题", "摘要") == ["摘要"]
