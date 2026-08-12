from __future__ import annotations

from datetime import date, datetime

import pytest

from personal_growth.adapters import ADAPTERS, ExcludedArticle
from personal_growth.adapters_custom.simple_psychology import SimplePsychologyAdapter
from personal_growth.ai import ArkAnalyzer
from personal_growth.evidence import content_hash, effective_chars, evidence_packets
from personal_growth.feishu import FeishuBitable
from personal_growth.models import Article, RunReport, SourceResult
from personal_growth.outputs import article_row
from personal_growth.pipeline import Pipeline
from personal_growth.state import StateStore
from personal_growth.text import SHANGHAI, normalize_title, normalize_url, parse_datetime, target_bounds


class Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeHttp:
    def __init__(self, metadata, fulltext=None):
        self.metadata = metadata
        self.fulltext = fulltext or metadata
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs.get("params") or {}))
        params = kwargs.get("params") or {}
        return Response(self.fulltext if params.get("mode") == "fulltext" else self.metadata)


def item(title, url, stamp, content="", image="https://img.test/a.jpg"):
    return {"title": title, "url": url, "date_modified": stamp, "content_html": content, "image": image}


def article(title="标题", url="https://mp.weixin.qq.com/s/a", source="简单心理"):
    return Article(source, title, url, datetime(2026, 8, 12, 9, tzinfo=SHANGHAI), body="正文" * 500)


def test_all_five_adapters_are_registered():
    assert {"simple_psychology", "dxy", "earth_knowledge", "gelong", "leviathan"} <= set(ADAPTERS)


def test_shanghai_day_and_wewe_timestamp_boundaries():
    start, end = target_bounds(date(2026, 8, 12))
    assert start.isoformat() == "2026-08-12T00:00:00+08:00"
    assert end.date() == start.date()
    assert parse_datetime("2026-08-11T16:00:00.000Z") == start
    assert parse_datetime("2026-08-12T15:59:59.000Z").date() == start.date()


def test_wewe_discovery_uses_date_modified_and_crosses_boundary(monkeypatch):
    monkeypatch.setenv("WEWE_RSS_BASE_URL", "https://rss.test/token")
    payload = {
        "items": [
            item("今日文章", "https://mp.weixin.qq.com/s/today", "2026-08-12T03:00:00.000Z"),
            item("昨日文章", "https://mp.weixin.qq.com/s/old", "2026-08-11T03:00:00.000Z"),
        ]
    }
    adapter = SimplePsychologyAdapter(FakeHttp(payload))
    result = adapter.discover(date(2026, 8, 12))
    assert result.scanned == 2
    assert result.discovered == 1
    assert result.coverage_complete is True
    assert result.articles[0].meta["feed_position"] == 0


def test_wewe_fulltext_is_cleaned_and_empty_is_failure(monkeypatch):
    monkeypatch.setenv("WEWE_RSS_BASE_URL", "https://rss.test/token")
    metadata = {"items": [item("文章", "https://mp.weixin.qq.com/s/a", "2026-08-12T03:00:00.000Z")]}
    fulltext = {"items": [item("文章", "https://mp.weixin.qq.com/s/a", "2026-08-12T03:00:00.000Z", "<style>x</style><p>有效正文</p>")]}
    adapter = SimplePsychologyAdapter(FakeHttp(metadata, fulltext))
    found = adapter.discover(date(2026, 8, 12)).articles[0]
    assert adapter.fetch_body(found) == "有效正文"

    blocked = SimplePsychologyAdapter(FakeHttp(metadata, {"items": [item("文章", found.url, "2026-08-12T03:00:00.000Z", "<style>x</style>")]}))
    blocked_found = blocked.discover(date(2026, 8, 12)).articles[0]
    with pytest.raises(RuntimeError, match="全文为空"):
        blocked.fetch_body(blocked_found)


def test_wewe_feed_at_limit_without_old_boundary_is_incomplete(monkeypatch):
    monkeypatch.setenv("WEWE_RSS_BASE_URL", "https://rss.test/token")
    payload = {"items": [item(str(i), f"https://mp.weixin.qq.com/s/{i}", "2026-08-12T03:00:00.000Z") for i in range(3)]}
    adapter = SimplePsychologyAdapter(FakeHttp(payload))
    adapter.feed_limit = 3
    result = adapter.discover(date(2026, 8, 12))
    assert result.coverage_complete is False
    assert result.errors


def test_pipeline_dedupes_by_either_url_or_title_but_keeps_distinct_articles():
    first = article("同标题", "https://mp.weixin.qq.com/s/a")
    same_url = article("另一个标题", "https://mp.weixin.qq.com/s/a")
    same_title = article("同标题", "https://mp.weixin.qq.com/s/b")
    distinct = article("不同文章", "https://mp.weixin.qq.com/s/c", "丁香医生")
    kept = Pipeline._dedupe([first, same_url, same_title, distinct])
    assert len(kept) == 2
    assert distinct in kept


def test_filter_keeps_valid_low_score_article_and_uses_fixed_categories():
    raw = '{"status":"入选","reason":"有效文章","event_key":"心理机制分析","topics":["心理"],"category":"心理与关系","unique_points":[],"scores":{"importance":1,"information_gain":1,"social_impact":1,"actionability":1,"evidence_density":1}}'
    result = ArkAnalyzer._validate_filter(raw)
    assert result["valuable"] is True
    assert result["category"] == "心理与关系"


def test_summary_requires_three_paragraphs_and_evidence_numbers():
    summary = "甲" * 105 + "\n\n" + "乙" * 105 + "\n\n" + "丙" * 105
    assert ArkAnalyzer.validate_summary(summary, "没有数字") == summary
    with pytest.raises(ValueError, match="证据外数字"):
        ArkAnalyzer.validate_summary("甲" * 100 + "10日" + "\n\n" + "乙" * 105 + "\n\n" + "丙" * 105, "没有数字")


def test_evidence_and_cache_identity_include_body():
    value = article()
    value.body = "\n".join(f"第{i}部分提供了可验证的事实、原因、影响和行动启示。" * 3 for i in range(80))
    filter_packet, summary_packet = evidence_packets(value)
    assert 900 <= len(filter_packet) <= 1200
    assert 1600 <= len(summary_packet) <= 2400
    before = content_hash(value)
    value.body += "变化"
    assert content_hash(value) != before


def test_state_cache_and_progress_are_persistent(tmp_path):
    path = tmp_path / "state.sqlite"
    first = StateStore(str(path))
    first.put_stage("k", "filter", "v", {"status": "入选"})
    first.save_progress("job", ["k"], 1, None, "complete")
    first.close()
    second = StateStore(str(path))
    assert second.get_stage("k", "filter", "v")["status"] == "入选"
    assert second.get_progress("job")["next_index"] == 1
    second.close()


def test_output_row_preserves_failure_and_cover():
    value = article()
    value.daily_date = date(2026, 8, 12)
    value.record_status = "正文待补全"
    value.meta = {"image": "https://img.test/a.jpg", "failure_reason": "微信验证"}
    row = article_row(value)
    assert row["封面"].startswith("https://")
    assert row["失败原因"] == "微信验证"


def test_feishu_record_fields_for_completed_and_pending():
    client = object.__new__(FeishuBitable)
    client.field_types = {name: spec["type"] for name, spec in FeishuBitable.required_fields.items()}
    value = article()
    value.daily_date = date(2026, 8, 12)
    value.record_status = "已完成"
    value.ai_result = {"topics": ["心理"], "category": "心理与关系", "core_content": "摘要"}
    fields = client.record_fields(value)
    assert fields["来源网站"]["link"] == value.url
    assert fields["记录状态"] == "已完成"
    assert fields["核心干货"] == "摘要"


def test_feishu_pending_records_respect_source_and_cutoff(monkeypatch):
    client = object.__new__(FeishuBitable)
    published = int(datetime(2026, 8, 10, 9, tzinfo=SHANGHAI).timestamp() * 1000)
    monkeypatch.setattr(client, "list_records", lambda: [
        {"record_id": "r1", "fields": {"标题": "待补", "来源名称": "简单心理", "来源网站": {"link": "https://mp.weixin.qq.com/s/a"}, "发布日期": published, "记录状态": "正文待补全"}},
        {"record_id": "r2", "fields": {"标题": "完成", "来源名称": "简单心理", "来源网站": {"link": "https://mp.weixin.qq.com/s/b"}, "发布日期": published, "记录状态": "已完成"}},
    ])
    pending = client.pending_articles(date(2026, 8, 7), {"简单心理"})
    assert [item.record_id for item in pending] == ["r1"]


def test_feishu_writes_in_small_batches(monkeypatch):
    client = object.__new__(FeishuBitable)
    client.app_token = "app"
    client.table_id = "table"
    client.field_types = {"标题": 1}
    calls = []
    monkeypatch.setattr(client, "_api", lambda method, path, **kwargs: calls.append(kwargs["json"]["records"]) or {"data": {"records": kwargs["json"]["records"]}})
    written, failed = client.write([article(str(i), f"https://mp.weixin.qq.com/s/{i}") for i in range(123)])
    assert (written, failed) == (123, 0)
    assert [len(batch) for batch in calls] == [50, 50, 23]


def test_run_report_is_partial_for_body_failure():
    report = RunReport("2026-08-12", sources={"simple_psychology": SourceResult("简单心理", coverage_complete=True, body_failed=1)})
    assert report.partial is True


def test_normalization_is_stable():
    assert normalize_url("http://www.example.com/a/?x=1") == "https://example.com/a"
    assert normalize_title("同一 标题！") == "同一标题"
