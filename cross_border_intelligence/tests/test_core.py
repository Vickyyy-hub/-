from __future__ import annotations

from datetime import date, datetime

from main import resolve_job
from personal_growth.ai import MarketAnalyzer
from personal_growth.collectors import Collector, signal_id
from personal_growth.config import countries, sources
from personal_growth.feishu import FeishuMarketBase, NEWS_TABLE, signal_rows
from personal_growth.models import Signal
from personal_growth.pipeline import MarketPipeline
from personal_growth.store import MarketStore
from personal_growth.text import SHANGHAI


ARTICLE = "<html><article><h1>News</h1><p>" + ("Verified cross-border trade evidence and market impact. " * 20) + "</p></article></html>"


class Response:
    def __init__(self, *, content: bytes = b"", text: str = "") -> None:
        self.content = content
        self.text = text or content.decode(errors="ignore")


def test_eight_country_mapping_is_complete():
    values = countries()
    assert set(values) == {"GB", "DE", "FR", "ES", "BR", "MX", "CL", "CO"}


def test_only_active_news_cadences_are_used():
    assert sources(cadence="policy_4x")
    trends = next(item for item in sources(cadence="demand_daily") if item.key == "google_trends")
    assert trends.countries == ("GB", "DE", "FR", "ES", "BR", "MX", "CL", "CO")


def test_signal_id_is_stable():
    assert signal_id("source", "https://example.com/a", " A title ") == signal_id(
        "source", "https://example.com/a", "A title"
    )


def test_article_body_requires_300_visible_characters_and_uses_reader_fallback():
    class Http:
        @staticmethod
        def get(url):
            if url.startswith("https://r.jina.ai/"):
                return Response(text="Markdown Content:\n" + ("Reader evidence. " * 30))
            return Response(text="<article>short</article>")

    body, fallback = Collector(Http(), countries())._article_body("https://example.com/detail")
    assert fallback is True
    assert len(body.replace(" ", "")) >= 300


def test_article_body_rejects_unverifiable_page():
    class Http:
        @staticmethod
        def get(url):
            return Response(text="short")

    assert Collector(Http(), countries())._article_body("https://example.com/detail")[0] == ""


def test_article_detail_reads_real_published_time():
    class Http:
        @staticmethod
        def get(url):
            return Response(text="<meta property='article:published_time' content='2026-08-11T09:30:00+02:00'>" + ARTICLE)

    body, fallback, published = Collector(Http(), countries())._article_detail("https://example.com/detail")
    assert body and fallback is False
    assert published is not None and published.date() == date(2026, 8, 11)


def test_google_trends_uses_associated_news_detail():
    xml = b"""<?xml version='1.0'?><rss xmlns:ht='https://trends.google.com/trending/rss'><channel>
    <item><title>air fryer</title><link>https://trends.google.com/topic</link>
    <pubDate>Thu, 13 Aug 2026 02:00:00 +0000</pubDate><ht:approx_traffic>20,000+</ht:approx_traffic>
    <ht:news_item><ht:news_item_title>Retailer launches new appliance range</ht:news_item_title>
    <ht:news_item_url>https://example.com/article</ht:news_item_url><ht:news_item_source>Example News</ht:news_item_source></ht:news_item>
    </item></channel></rss>"""

    class Http:
        @staticmethod
        def get(url):
            return Response(content=xml) if "trends.google" in url else Response(text=ARTICLE)

    spec = next(item for item in sources(cadence="demand_daily") if item.key == "google_trends")
    single = type(spec)(**{**spec.__dict__, "countries": ("GB",)})
    items, warnings = Collector(Http(), countries()).collect_google_trends(single, date(2026, 8, 13))
    assert warnings == []
    assert len(items) == 1
    assert items[0].title == "Retailer launches new appliance range"
    assert items[0].url == "https://example.com/article"
    assert items[0].source_name == "Example News"
    assert items[0].original_keyword == "air fryer"
    assert items[0].heat == 20000


def test_summary_validation_checks_length_and_evidence_numbers():
    analyzer = object.__new__(MarketAnalyzer)
    signal = Signal("s", "x", "X", "政策", "Policy update", "https://example.com/a",
                    datetime.now(SHANGHAI), ["DE"], evidence="The tariff changes from 10% to 8% for sellers.")
    valid = "德国主管部门公布关税调整方案，将相关商品税率由10%降至8%，并说明新规适用于跨境进口业务。卖家需要重新核算申报成本、更新商品定价及清关资料，避免使用旧税率造成补税或通关延误。"
    assert analyzer._summary_valid(signal, valid)
    assert not analyzer._summary_valid(signal, "太短")
    assert not analyzer._summary_valid(signal, valid.replace("8%", "7%"))


def test_feishu_schema_and_clickable_link_shape():
    fields = {item["field_name"]: item["type"] for item in NEWS_TABLE["fields"]}
    assert fields["来源网站"] == 15
    now = datetime.now(SHANGHAI)
    signal = Signal("s", "x", "X", "政策", "Title", "https://example.com/detail", now, ["DE"])
    signal.summary_zh = "德国主管部门发布跨境商品监管更新，明确新的申报要求、适用范围和执行时间。卖家需要及时检查商品资料与合规文件，并同步调整清关流程，避免因信息不完整导致货物延误或额外处理成本。"
    row = signal_rows([signal.to_dict()], "2026-08-17")[0]
    assert row["来源网站"] == {"text": "查看原文", "link": "https://example.com/detail"}


def test_feishu_identity_deduplicates_title_and_detail_url():
    title, url = FeishuMarketBase._identity({"标题": "A News!", "来源网站": {"text": "查看原文", "link": "https://example.com/a?utm=x"}})
    assert title == "anews"
    assert url == "https://example.com/a"


def test_sqlite_dedup(tmp_path):
    item = Signal("sig1", "x", "X", "政策", "Title", "https://example.com/a", datetime.now(SHANGHAI), ["GB"])
    store = MarketStore(str(tmp_path / "state.sqlite"))
    try:
        assert store.upsert_signals([item]) == (1, 0)
        assert store.upsert_signals([item]) == (0, 1)
    finally:
        store.close()


def test_completed_demand_job_adds_zero(tmp_path, monkeypatch):
    state_path = tmp_path / "state.sqlite"
    monkeypatch.setenv("STATE_DB", str(state_path))
    store = MarketStore(str(state_path))
    try:
        store.mark_complete("v2:demand:2026-08-17", {"completion_marker": True})
    finally:
        store.close()
    report = MarketPipeline().run("demand", date(2026, 8, 17), report_path=tmp_path / "report.json")
    assert report.written == 0 and report.updated == 0 and report.completion_marker


def test_cron_job_routing():
    assert resolve_job("auto", "0 0,4,8,12 * * *") == "policy"
    assert resolve_job("auto", "0 1 * * *") == "demand"
    assert resolve_job("policy", "") == "policy"
