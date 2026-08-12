import time
from datetime import date

import requests

from personal_growth.feishu import FeishuBitable
from personal_growth.ai import (
    FILTER_STAGE,
    FILTER_VERSION,
    SUMMARY_STAGE,
    SUMMARY_VERSION,
    ArkAPIError,
    ArkAnalyzer,
    ArkRateLimitError,
)
from personal_growth.evidence import effective_chars, evidence_packets
from personal_growth.models import Article, RunReport, SourceResult
from personal_growth.pipeline import Pipeline
from personal_growth.state import StateStore
from personal_growth.text import (
    SHANGHAI,
    from_epoch,
    is_obvious_exclusion,
    normalize_title,
    normalize_url,
    parse_datetime,
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


def test_parse_datetime_accepts_dotnet_seven_digit_fraction():
    parsed = parse_datetime("2026-08-10T18:39:18.6830000+08:00")
    assert parsed.isoformat() == "2026-08-10T18:39:18.683000+08:00"


def test_normalization_and_obvious_exclusions():
    assert normalize_url("http://www.ithome.com/0/1/2.htm?x=1") == "https://ithome.com/0/1/2.htm"
    assert normalize_title("测试标题 - IT之家") == "测试标题"
    assert "排除词" in is_obvious_exclusion("某商品官方狂促：到手价9元")


def test_json_parser_and_chunker():
    assert parse_json_object('```json\n{"valuable": true}\n```')["valuable"] is True
    chunks = split_long_text("甲" * 100 + "\n" + "乙" * 100, max_chars=120)
    assert "".join(chunks).replace("\n", "") == "甲" * 100 + "乙" * 100
    assert all(len(chunk) <= 120 for chunk in chunks)


def test_filter_invalid_json_is_corrected_once(monkeypatch, tmp_path):
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.sqlite"))
    analyzer = ArkAnalyzer()
    responses = iter([
        '{"status":"入选"',
        '{"status":"淘汰","reason":"普通快讯","event_key":"测试事件",'
        '"topics":["商业"],"category":"商业与公司","unique_points":[],'
        '"scores":{"importance":1,"information_gain":1,"social_impact":1,'
        '"actionability":1,"evidence_density":1}}',
    ])
    monkeypatch.setattr(analyzer, "_request", lambda *args, **kwargs: next(responses))
    article = Article("虎嗅", "测试文章", "https://example.com/invalid-once", from_epoch(1786195964))
    state = StateStore()
    try:
        result = analyzer.analyze(article, "初筛证据", "摘要证据", "invalid-once", state)
    finally:
        state.close()
    assert result["valuable"] is False
    assert result.get("invalid_ai") is not True


def test_filter_twice_invalid_is_cached_as_single_article_skip(monkeypatch, tmp_path):
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.sqlite"))
    analyzer = ArkAnalyzer()
    calls = []

    def invalid_request(*args, **kwargs):
        calls.append(1)
        return '{"status":"入选"'

    monkeypatch.setattr(analyzer, "_request", invalid_request)
    article = Article("虎嗅", "测试文章", "https://example.com/invalid-twice", from_epoch(1786195964))
    state = StateStore()
    try:
        first = analyzer.analyze(article, "初筛证据", "摘要证据", "invalid-twice", state)
        second = analyzer.analyze(article, "初筛证据", "摘要证据", "invalid-twice", state)
    finally:
        state.close()
    assert first["invalid_ai"] is True
    assert first["reason"] == "AI格式错误跳过"
    assert second["invalid_ai"] is True
    assert len(calls) == 2


def test_invalid_ai_article_does_not_interrupt_following_articles(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.sqlite"))
    articles = [
        Article("虎嗅", "坏格式", "https://example.com/bad", from_epoch(1786195965), body="正文" * 500),
        Article("虎嗅", "正常文章", "https://example.com/good", from_epoch(1786195964), body="正文" * 500),
    ]

    class FakeAnalyzer:
        model = "test-model"

        def __init__(self):
            self.filter_calls = 0
            self.summary_calls = 0
            self.cache_hits = 0
            self.rate_limit_count = 0
            self.retry_wait_seconds = 0.0

        def analyze(self, article, filter_evidence, summary_evidence, cache_key, state):
            if article.title == "坏格式":
                return {
                    "status": "淘汰", "valuable": False, "reason": "AI格式错误跳过",
                    "invalid_ai": True, "invalid_ai_error": "JSON缺少逗号",
                }
            return {
                "status": "入选", "valuable": True, "reason": "有价值",
                "event_key": article.title, "topics": ["商业"], "category": "商业与公司",
                "unique_points": ["新增事实"], "score_total": 16,
                "scores": {"importance": 3, "information_gain": 4, "social_impact": 3,
                           "actionability": 3, "evidence_density": 3},
                "core_content": "甲" * 105 + "\n\n" + "乙" * 105 + "\n\n" + "丙" * 105,
                "summary_chars": 315,
            }

    monkeypatch.setattr("personal_growth.pipeline.ArkAnalyzer", FakeAnalyzer)
    pipeline = object.__new__(Pipeline)
    pipeline.deadline = time.monotonic() + 60
    report = RunReport("2026-08-11", sources={"huxiu": SourceResult("虎嗅")})
    analyzed, complete = pipeline.analyze(articles, report, job_id="invalid-continues")
    assert complete is True
    assert [article.title for article in analyzed] == ["正常文章"]
    assert report.invalid_ai_skipped == 1
    assert report.sources["huxiu"].invalid_ai_skipped == 1
    assert report.sources["huxiu"].invalid_ai_items[0]["url"] == "https://example.com/bad"


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


def test_manual_limit_samples_sources_round_robin():
    items = [
        Article(source, f"{source}-{index}", f"https://example.com/{source}/{index}", from_epoch(1786195964 + index))
        for source in ("虎嗅", "少数派", "界面新闻")
        for index in range(5)
    ]
    sampled = Pipeline._sample_articles(items, 6)
    assert len(sampled) == 6
    assert {item.source for item in sampled} == {"虎嗅", "少数派", "界面新闻"}
    assert all(sum(item.source == source for item in sampled) == 2 for source in {"虎嗅", "少数派", "界面新闻"})


def test_daily_analysis_order_round_robins_all_six_sources():
    sources = Pipeline.ANALYSIS_SOURCE_ORDER
    items = [
        Article(source, f"{source}-{index}", f"https://example.com/{source}/{index}", from_epoch(1786195964 + index))
        for source in reversed(sources)
        for index in range(3)
    ]
    ordered = Pipeline._analysis_order(items)
    assert [item.source for item in ordered[:6]] == list(sources)
    assert all(sum(item.source == source for item in ordered[:12]) == 2 for source in sources)
    for source in sources:
        published = [item.published_at for item in ordered if item.source == source]
        assert published == sorted(published, reverse=True)


def test_interrupted_analysis_resumes_from_same_round_robin_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.sqlite"))
    sources = Pipeline.ANALYSIS_SOURCE_ORDER
    articles = [
        Article(source, f"{source}-{index}", f"https://example.com/{source}/{index}", from_epoch(1786195964 + index), body="正文" * 500)
        for source in sources
        for index in range(3)
    ]
    created = []

    class FakeAnalyzer:
        model = "test-model"

        def __init__(self):
            self.filter_calls = 0
            self.summary_calls = 0
            self.cache_hits = 0
            self.rate_limit_count = 0
            self.retry_wait_seconds = 0.0
            self.calls = []
            created.append(self)

        def analyze(self, article, filter_evidence, summary_evidence, cache_key, state):
            self.calls.append(article.source)
            if len(created) == 1 and len(self.calls) == 13:
                raise ArkAPIError("模拟中断")
            result = {
                "status": "入选", "valuable": True, "reason": "有增量",
                "event_key": article.title, "topics": ["商业"], "category": "商业与公司",
                "unique_points": [article.title], "score_total": 16,
                "scores": {"importance": 3, "information_gain": 4, "social_impact": 3, "actionability": 3, "evidence_density": 3},
            }
            summary = {"status": "入选", "core_content": "甲" * 105 + "\n\n" + "乙" * 105 + "\n\n" + "丙" * 105, "summary_chars": 315}
            state.put_stage(cache_key, FILTER_STAGE, f"{self.model}:{FILTER_VERSION}", result)
            state.put_stage(cache_key, SUMMARY_STAGE, f"{self.model}:{SUMMARY_VERSION}", summary)
            return {**result, **summary}

    monkeypatch.setattr("personal_growth.pipeline.ArkAnalyzer", FakeAnalyzer)
    pipeline = object.__new__(Pipeline)
    pipeline.deadline = time.monotonic() + 60

    first = RunReport("2026-08-11")
    first.sources = {source: SourceResult(source) for source in sources}
    analyzed, complete = pipeline.analyze(articles, first, job_id="daily-six")
    assert complete is False
    assert len(analyzed) == 12
    assert all(result.ai_processed == 2 for result in first.sources.values())
    assert all(result.ai_pending == 1 for result in first.sources.values())

    second = RunReport("2026-08-11")
    second.sources = {source: SourceResult(source) for source in sources}
    analyzed, complete = pipeline.analyze(articles, second, job_id="daily-six")
    assert complete is True
    assert len(analyzed) == 18
    assert len(created[1].calls) == 6
    assert all(result.ai_processed == 3 for result in second.sources.values())
    assert all(result.ai_pending == 0 for result in second.sources.values())
    assert all(result.cache_hits == 4 for result in second.sources.values())


def test_evidence_packets_cover_sections_and_stay_bounded():
    article = Article("虎嗅", "公司调整欧洲业务", "https://example.com/1", from_epoch(1786195964))
    article.body = "\n".join(
        f"第{i}部分，公司在2026年调整欧洲市场业务，收入增长{i}%，这将影响供应链和企业决策。"
        for i in range(1, 80)
    )
    filter_evidence, summary_evidence = evidence_packets(article)
    assert 900 <= len(filter_evidence) <= 1200
    assert 1600 <= len(summary_evidence) <= 2400
    assert "E001" in filter_evidence
    assert "第79部分" in summary_evidence


def test_filter_validation_and_deep_article_protection():
    raw = '{"status":"入选","reason":"有独家数据","event_key":"公司调整欧洲业务",' \
          '"topics":["跨境"],"category":"全球与出海","unique_points":["独家数据"],' \
          '"scores":{"importance":3,"information_gain":4,"social_impact":2,"actionability":2,"evidence_density":3}}'
    result = ArkAnalyzer._validate_filter(raw)
    assert result["valuable"] is True
    assert result["score_total"] == 14


def test_summary_requires_three_paragraphs_and_300_to_500_chars():
    summary = "甲" * 105 + "\n\n" + "乙" * 105 + "\n\n" + "丙" * 105
    assert effective_chars(summary) == 315
    assert ArkAnalyzer.validate_summary(summary, "原文没有数字") == summary
    try:
        ArkAnalyzer.validate_summary("甲" * 299, "原文")
    except ValueError as exc:
        assert "字数不合格" in str(exc)
    else:
        raise AssertionError("短摘要应被拒绝")


def test_summary_safely_fits_small_overflow_without_second_call():
    summary = "甲" * 220 + "\n\n" + "乙" * 210 + "\n\n" + "丙" * 210
    fitted = ArkAnalyzer.validate_summary(summary, "原文没有数字")
    assert 300 <= effective_chars(fitted) <= 500
    assert len(fitted.split("\n\n")) == 3
    assert fitted.endswith("。")


def test_summary_scrubs_numbers_not_present_in_evidence():
    summary = "甲" * 100 + "10日发布。" + "\n\n" + "乙" * 105 + "\n\n" + "丙" * 105
    fitted = ArkAnalyzer.validate_summary(summary, "原文没有日期数字")
    assert "10日" not in fitted
    assert "相关时间" in fitted


def test_state_cache_persists_stages_and_progress(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite"))
    store.put_stage("hash", "filter", "v1", {"status": "淘汰"})
    store.save_progress("job", ["hash"], 0, "a1", "waiting_retry")
    store.close()
    reopened = StateStore(str(tmp_path / "state.sqlite"))
    assert reopened.get_stage("hash", "filter", "v1") == {"status": "淘汰"}
    assert reopened.get_progress("job")["current_article_id"] == "a1"
    reopened.close()


def test_report_exposes_summary_rejections():
    report = RunReport("2026-08-09", trigger_source="feishu", summary_rejected=2)
    assert report.to_dict()["summary_rejected"] == 2
    assert report.to_dict()["trigger_source"] == "feishu"


def test_report_exposes_per_source_ai_progress():
    report = RunReport("2026-08-11")
    report.sources = {
        "huxiu": SourceResult("虎嗅", ai_processed=3, ai_pending=2, cache_hits=4),
    }
    source = report.to_dict()["sources"]["huxiu"]
    assert source["ai_processed"] == 3
    assert source["ai_pending"] == 2
    assert source["cache_hits"] == 4


def test_cached_only_report_ignores_expected_source_gaps():
    report = RunReport("2026-08-10", cached_only=True, truncated_by_user=True)
    report.sources = {"sspai": SourceResult("少数派", body_failed=1, errors=["正文为空"])}
    assert report.partial is False
    report.write_failed = 1
    assert report.partial is True


def test_ark_429_exposes_original_error(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test")
    monkeypatch.setenv("ARK_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("ARK_MAX_429_RETRIES", "0")
    analyzer = ArkAnalyzer()

    class Response:
        status_code = 429
        text = '{"error":{"code":"RateLimitExceeded","message":"TPM exceeded"}}'
        headers = {"Retry-After": "60"}

    monkeypatch.setattr(analyzer.session, "post", lambda *args, **kwargs: Response())
    try:
        analyzer._request("system", "user", 20, "filter")
    except ArkRateLimitError as exc:
        assert "TPM exceeded" in exc.detail
        assert exc.retry_after == 60
    else:
        raise AssertionError("429必须抛出可观测错误")


def test_account_limit_429_does_not_retry(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test")
    monkeypatch.setenv("ARK_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("ARK_MAX_429_RETRIES", "4")
    analyzer = ArkAnalyzer()
    calls = 0

    class Response:
        status_code = 429
        text = '{"error":{"code":"SetLimitExceeded","message":"safe mode limit"}}'
        headers = {}

    def fake_post(*args, **kwargs):
        nonlocal calls
        calls += 1
        return Response()

    monkeypatch.setattr(analyzer.session, "post", fake_post)
    try:
        analyzer._request("system", "user", 20, "filter")
    except ArkRateLimitError:
        pass
    else:
        raise AssertionError("账户级限额必须中止")
    assert calls == 1


def test_analyzer_uses_stage_cache_without_api_call(monkeypatch, tmp_path):
    monkeypatch.setenv("ARK_API_KEY", "test")
    monkeypatch.setenv("ARK_MIN_INTERVAL_SECONDS", "0")
    analyzer = ArkAnalyzer()
    store = StateStore(str(tmp_path / "state.sqlite"))
    article = Article("虎嗅", "公司调整业务", "https://example.com/2", from_epoch(1786195964))
    filter_result = {
        "status": "入选", "valuable": True, "reason": "有增量", "event_key": "公司调整业务",
        "topics": ["商业"], "category": "商业与公司", "unique_points": ["独家变化"],
        "score_total": 16,
        "scores": {"importance": 3, "information_gain": 4, "social_impact": 3, "actionability": 3, "evidence_density": 3},
    }
    summary = "甲" * 105 + "\n\n" + "乙" * 105 + "\n\n" + "丙" * 105
    store.put_stage("hash", "filter", f"{analyzer.model}:filter_v2", filter_result)
    store.put_stage(
        "hash", "summary", f"{analyzer.model}:summary_v4",
        {"status": "入选", "core_content": summary, "summary_chars": 315},
    )
    monkeypatch.setattr(analyzer, "_request", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应调用API")))
    result = analyzer.analyze(article, "证据", "E001 证据", "hash", store)
    assert result["valuable"] is True
    assert analyzer.cache_hits == 2
    store.close()


def test_feishu_hyperlink_shape_without_initializing_client():
    client = object.__new__(FeishuBitable)
    client.field_types = {"来源网站": 15, "内容主题": 4}
    assert client._source_value("https://example.com") == {
        "text": "https://example.com",
        "link": "https://example.com",
    }
    assert client._field_value("内容主题", "摘要") == ["摘要"]


def test_feishu_record_contains_daily_date():
    client = object.__new__(FeishuBitable)
    client.field_types = {
        "标题": 1,
        "来源网站": 15,
        "发布日期": 5,
        "日报日期": 5,
    }
    article = Article("虎嗅", "测试", "https://example.com/3", from_epoch(1786195964))
    article.daily_date = date(2026, 8, 10)
    fields = client.record_fields(article)
    assert fields["日报日期"] == FeishuBitable._date_timestamp(date(2026, 8, 10))
    assert fields["发布日期"] == int(article.published_at.timestamp() * 1000)


def test_daily_date_backfill_uses_published_date_without_ai(monkeypatch):
    client = object.__new__(FeishuBitable)
    client.app_token = "app"
    client.table_id = "table"
    published = FeishuBitable._date_timestamp(date(2026, 8, 8))
    before = [
        {"record_id": "r1", "fields": {"发布日期": published}},
        {"record_id": "r2", "fields": {"发布日期": None}},
    ]
    after = [
        {"record_id": "r1", "fields": {"发布日期": published, "日报日期": published}},
        {"record_id": "r2", "fields": {"发布日期": None}},
    ]
    reads = iter([before, after])
    monkeypatch.setattr(client, "list_records", lambda: next(reads))
    calls = []
    monkeypatch.setattr(client, "_api", lambda method, path, **kwargs: calls.append(kwargs["json"]) or {"data": {"records": [{"record_id": "r1"}]}})
    updated, failed, skipped = client.backfill_daily_dates()
    assert (updated, failed, skipped) == (1, 0, 1)
    assert calls[0]["records"][0]["fields"]["日报日期"] == published


def test_feishu_read_only_mode_never_creates_fields(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setattr(FeishuBitable, "_tenant_token", lambda self: "token")
    field_types = {name: spec["type"] for name, spec in FeishuBitable.required_fields.items()}
    monkeypatch.setattr(FeishuBitable, "_field_types", lambda self: field_types)
    monkeypatch.setattr(
        FeishuBitable,
        "ensure_fields",
        lambda self: (_ for _ in ()).throw(AssertionError("只读模式不得创建字段")),
    )
    client = FeishuBitable(object(), read_only=True)
    assert client.field_types == field_types


def test_feishu_refreshes_token_before_expiry(monkeypatch):
    client = object.__new__(FeishuBitable)
    client.http = object()
    client.base_url = "https://open.feishu.cn/open-apis"
    client.headers = {"Authorization": "Bearer old"}
    client.token = "old"
    client.token_expires_at = 100
    refreshed = []

    def refresh():
        refreshed.append(1)
        client.token = "new"
        client.headers["Authorization"] = "Bearer new"
        client.token_expires_at = 10_000

    class Response:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {"code": 0, "data": {}}

    client._refresh_token = refresh
    client.http = type("Http", (), {"request": staticmethod(lambda *args, **kwargs: Response())})()
    monkeypatch.setattr("personal_growth.feishu.clock.monotonic", lambda: 1_000)
    assert client._api("GET", "/test")["code"] == 0
    assert refreshed == [1]
    assert client.headers["Authorization"] == "Bearer new"


def test_feishu_refreshes_and_retries_once_on_expired_token(monkeypatch):
    client = object.__new__(FeishuBitable)
    client.base_url = "https://open.feishu.cn/open-apis"
    client.headers = {"Authorization": "Bearer old"}
    client.token = "old"
    client.token_expires_at = 10_000
    refreshed = []
    calls = []

    class Response:
        headers = {"x-tt-logid": "log-1"}

        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload
            self.text = str(payload)

        def json(self):
            return self.payload

    expired = Response(400, {"code": 99991663, "msg": "tenant_access_token expired"})
    success = Response(200, {"code": 0, "data": {"ok": True}})

    def request(*args, **kwargs):
        calls.append(kwargs["headers"]["Authorization"])
        if len(calls) == 1:
            error = requests.HTTPError("400")
            error.response = expired
            raise error
        return success

    def refresh():
        refreshed.append(1)
        client.token = "new"
        client.headers["Authorization"] = "Bearer new"
        client.token_expires_at = 20_000

    client.http = type("Http", (), {"request": staticmethod(request)})()
    client._refresh_token = refresh
    monkeypatch.setattr("personal_growth.feishu.clock.monotonic", lambda: 1_000)
    assert client._api("POST", "/test", json={})["data"]["ok"] is True
    assert refreshed == [1]
    assert calls == ["Bearer old", "Bearer new"]


def test_feishu_write_uses_small_independent_batches(monkeypatch):
    client = object.__new__(FeishuBitable)
    client.app_token = "app"
    client.table_id = "table"
    client.field_types = {"标题": 1}
    calls = []
    monkeypatch.setattr(
        client,
        "_api",
        lambda method, path, **kwargs: calls.append(kwargs["json"]["records"])
        or {"data": {"records": kwargs["json"]["records"]}},
    )
    articles = [
        Article("虎嗅", f"文章{index}", f"https://example.com/{index}", from_epoch(1786195964 + index))
        for index in range(123)
    ]
    written, failed = client.write(articles)
    assert (written, failed) == (123, 0)
    assert [len(batch) for batch in calls] == [50, 50, 23]
