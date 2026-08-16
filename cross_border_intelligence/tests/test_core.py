from __future__ import annotations

from datetime import date, datetime

from personal_growth.collectors import Collector, signal_id
from personal_growth.config import countries, load_config, sources
from personal_growth.feishu import FeishuMarketBase, direction_rows, profile_rows, signal_rows, source_rows
from personal_growth.models import CountryProfile, ProductDirection, Signal
from personal_growth.pipeline import MarketPipeline
from personal_growth.store import MarketStore
from personal_growth.text import SHANGHAI
from main import resolve_job


def test_eight_country_mapping_is_complete():
    values = countries()
    assert set(values) == {"GB", "DE", "FR", "ES", "BR", "MX", "CL", "CO"}
    assert values["BR"].language == "葡萄牙语"
    assert values["DE"].currency == "EUR"
    assert values["MX"].timezone == "America/Mexico_City"


def test_required_source_groups_exist():
    enabled = sources()
    cadences = {item.cadence for item in enabled}
    assert {"policy_4x", "demand_daily", "data_weekly", "profile_monthly"} <= cadences
    assert next(item for item in enabled if item.key == "google_trends").countries == (
        "GB", "DE", "FR", "ES", "BR", "MX", "CL", "CO"
    )


def test_signal_id_is_stable():
    assert signal_id("source", "https://example.com/a", " A title ") == signal_id(
        "source", "https://example.com/a", "A title"
    )


def test_google_trends_parsing_and_date_boundary():
    xml = b"""<?xml version='1.0'?><rss xmlns:ht='https://trends.google.com/trending/rss'><channel>
    <item><title>air fryer</title><link>https://example.com/a</link>
    <pubDate>Thu, 13 Aug 2026 02:00:00 +0000</pubDate><ht:approx_traffic>20,000+</ht:approx_traffic></item>
    <item><title>old topic</title><link>https://example.com/b</link>
    <pubDate>Wed, 12 Aug 2026 02:00:00 +0000</pubDate></item></channel></rss>"""

    class Response:
        content = xml

    class Http:
        @staticmethod
        def get(url):
            return Response()

    spec = next(item for item in sources(cadence="demand_daily") if item.key == "google_trends")
    single = type(spec)(**{**spec.__dict__, "countries": ("GB",)})
    items, warnings = Collector(Http(), countries()).collect_google_trends(single, date(2026, 8, 13))
    assert warnings == []
    assert [item.title for item in items] == ["air fryer"]
    assert items[0].heat == 20000


def test_google_trends_id_ignores_volatile_landing_url():
    def collect(link):
        xml = f"""<?xml version='1.0'?><rss xmlns:ht='https://trends.google.com/trending/rss'><channel>
        <item><title>air fryer</title><link>{link}</link><pubDate>Thu, 13 Aug 2026 02:00:00 +0000</pubDate></item>
        </channel></rss>""".encode()

        class Response:
            content = xml

        class Http:
            @staticmethod
            def get(url):
                return Response()

        spec = next(item for item in sources(cadence="demand_daily") if item.key == "google_trends")
        single = type(spec)(**{**spec.__dict__, "countries": ("GB",)})
        return Collector(Http(), countries()).collect_google_trends(single, date(2026, 8, 13))[0][0]

    first = collect("https://trends.google.com/a?volatile=1")
    second = collect("https://trends.google.com/b?volatile=2")
    assert first.signal_id == second.signal_id
    assert first.url != second.url


def test_federal_register_repeats_agency_query_parameter():
    captured = {}

    class Response:
        @staticmethod
        def json():
            return {"results": []}

    class Http:
        @staticmethod
        def get(url, params):
            captured["params"] = params
            return Response()

    spec = next(item for item in sources(cadence="policy_4x") if item.key == "federal_register")
    Collector(Http(), countries()).collect_federal_register(spec, date(2026, 8, 13))
    agency_params = [value for key, value in captured["params"] if key == "conditions[agencies][]"]
    assert agency_params == spec.params["agencies"]
    assert "u-s-customs-and-border-protection" in agency_params


def test_reference_uses_reader_fallback_when_direct_page_is_blocked():
    class Response:
        text = "Markdown Content:\n# Official statistics\n" + ("Consumer expenditure evidence. " * 20)

    class Http:
        @staticmethod
        def get(url):
            if not url.startswith("https://r.jina.ai/"):
                raise RuntimeError("blocked")
            return Response()

    spec = next(item for item in sources(cadence="profile_monthly") if item.key == "ibge_profile")
    items, warnings = Collector(Http(), countries()).collect_reference(spec, date(2026, 8, 13))
    assert warnings == []
    assert items[0].meta["reader_fallback"] is True


def test_sqlite_dedup_and_country_window(tmp_path):
    item = Signal(
        "sig1", "trends", "Trends", "搜索趋势", "air fryer", "https://example.com", datetime.now(SHANGHAI), ["GB"]
    )
    store = MarketStore(str(tmp_path / "state.sqlite"))
    try:
        assert store.upsert_signals([item]) == (1, 0)
        assert store.upsert_signals([item]) == (0, 1)
        item.summary_zh = "已分析摘要"
        assert store.upsert_signals([item]) == (0, 1)
        assert store.load_signals_by_ids(["sig1"])["sig1"]["summary_zh"] == "已分析摘要"
        assert len(store.load_signals(datetime(2020, 1, 1, tzinfo=SHANGHAI), "GB")) == 1
        assert store.load_signals(datetime(2020, 1, 1, tzinfo=SHANGHAI), "DE") == []
    finally:
        store.close()


def test_sqlite_profile_cache(tmp_path):
    now = datetime.now(SHANGHAI)
    profile = CountryProfile("GB", "英国", "欧洲", "英语", "GBP", "Europe/London", updated_at=now)
    store = MarketStore(str(tmp_path / "state.sqlite"))
    try:
        store.upsert_profiles([profile])
        assert store.load_profiles()["GB"]["country_name"] == "英国"
    finally:
        store.close()


def test_completed_daily_job_is_not_collected_or_written_again(tmp_path, monkeypatch):
    state_path = tmp_path / "state.sqlite"
    monkeypatch.setenv("STATE_DB", str(state_path))
    target = date(2026, 8, 16)
    store = MarketStore(str(state_path))
    try:
        store.mark_complete("demand:2026-08-16", {"completion_marker": True})
    finally:
        store.close()
    report = MarketPipeline().run("demand", target, report_path=tmp_path / "report.json")
    assert report.written == 0
    assert report.updated == 0
    assert report.completion_marker is True
    assert report.final_output_verified is True
    assert "跳过重复采集" in report.warnings[0]


def test_feishu_row_shapes_and_dates():
    now = datetime.now(SHANGHAI)
    signal = Signal("s", "x", "X", "政策", "Title", "https://example.com", now, ["DE"])
    assert signal_rows([signal.to_dict()])[0]["信号ID"] == "s"
    profile = CountryProfile("DE", "德国", "欧洲", "德语", "EUR", "Europe/Berlin", updated_at=now)
    assert profile_rows([profile.to_dict()])[0]["国家代码"] == "DE"
    direction = ProductDirection(
        "d", "DE", "德国", "厨房收纳趋势方向", "家庭", "小户型", 2,
        ["搜索趋势", "电商趋势"], "上升", "稳定", "空间不足", "耐用", "证据不足",
        "低", "中", "观察", "两类信号支持", "medium", ["s1", "s2"], now,
    )
    row = direction_rows([direction.to_dict()])[0]
    assert row["建议"] == "观察"
    assert row["证据数量"] == 2


def test_feishu_signal_fallback_does_not_merge_countries_or_shared_trend_urls():
    base = object.__new__(FeishuMarketBase)
    base._records = lambda _table: [
        {"record_id": "gb-air", "fields": {"信号ID": "old-gb", "国家": "GB", "来源名称": "Google Trends 每日热搜", "信号类型": "搜索趋势", "原文标题": "air fryer", "原词": "air fryer", "原文链接": "https://trends.google.com/trending"}},
        {"record_id": "de-air", "fields": {"信号ID": "old-de", "国家": "DE", "来源名称": "Google Trends 每日热搜", "信号类型": "搜索趋势", "原文标题": "air fryer", "原词": "air fryer", "原文链接": "https://trends.google.com/trending"}},
    ]
    existing = base.existing_keys("signals")
    assert len(existing["titles"]) == 2
    assert existing["urls"] == {}


def test_source_table_includes_disabled_optional_sources():
    items = [{"key": key, **value} for key, value in load_config()["sources"].items()]
    rows = source_rows(items)
    reddit = next(row for row in rows if row["来源ID"] == "reddit")
    assert reddit["启用状态"] == "停用"
    assert "无可用后端" in reddit["备注"]


def test_cron_job_routing():
    assert resolve_job("auto", "0 0,4,8,12 * * *") == "policy"
    assert resolve_job("auto", "30 1 * * *") == "demand"
    assert resolve_job("auto", "0 2 * * 1") == "weekly"
    assert resolve_job("auto", "0 3 1 * *") == "profiles"
    assert resolve_job("sources", "") == "sources"
