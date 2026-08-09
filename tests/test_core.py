from datetime import date

from personal_growth.feishu import FeishuBitable
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


def test_event_dedupe_keeps_longer_article():
    first = Article("甲", "公司发布新模型", "https://a.test/1", from_epoch(1786195964), body="长" * 500)
    second = Article("乙", "公司正式发布新模型", "https://b.test/2", from_epoch(1786195964), body="短" * 100)
    first.ai_result = {"event_key": "公司发布新模型"}
    second.ai_result = {"event_key": "公司发布新模型"}
    kept, duplicates = Pipeline._event_dedupe([second, first])
    assert kept == [first]
    assert duplicates == 1


def test_feishu_hyperlink_shape_without_initializing_client():
    client = object.__new__(FeishuBitable)
    client.field_types = {"来源网站": 15, "内容主题": 4}
    assert client._source_value("https://example.com") == {
        "text": "https://example.com",
        "link": "https://example.com",
    }
    assert client._field_value("内容主题", "摘要") == ["摘要"]
