from datetime import date

from personal_growth.feishu import FeishuBitable
from personal_growth.ai import ArkAnalyzer, ArkRateLimitError
from personal_growth.evidence import effective_chars, evidence_packets
from personal_growth.models import Article
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
    from personal_growth.models import RunReport

    report = RunReport("2026-08-09", summary_rejected=2)
    assert report.to_dict()["summary_rejected"] == 2


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
