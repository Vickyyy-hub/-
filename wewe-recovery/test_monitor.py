import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from monitor import Config, RecoveryError, event_id, recovery_dates, run_once


SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_config(tmp_path: Path) -> Config:
    database = tmp_path / "wewe.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE accounts (id INTEGER, status INTEGER, updated_at INTEGER);
        CREATE TABLE feeds (id TEXT, sync_time INTEGER);
        INSERT INTO accounts VALUES (1, 0, 1786670000000);
        INSERT INTO feeds VALUES ('feed-a', 100), ('feed-b', 200);
    """)
    connection.commit()
    connection.close()
    env_path = tmp_path / ".env"
    env_path.write_text("AUTH_CODE=not-a-real-secret\n", encoding="utf-8")
    return Config(
        database_path=database,
        wewe_env_path=env_path,
        state_path=tmp_path / "state.json",
        lock_path=tmp_path / "monitor.lock",
        recovery_url="https://example.test/wewe-recovered",
        recovery_secret="test-secret",
        expected_feed_ids=("feed-a", "feed-b"),
        bootstrap_recent_login_seconds=1_800,
    )


def update_account(config: Config, status: int, updated_at: int) -> None:
    connection = sqlite3.connect(config.database_path)
    connection.execute("UPDATE accounts SET status = ?, updated_at = ?", (status, updated_at))
    connection.commit()
    connection.close()


def test_recovery_dates_cross_day_and_cap() -> None:
    assert recovery_dates("2026-08-13", date(2026, 8, 14), 7) == [
        "2026-08-12", "2026-08-13", "2026-08-14"
    ]
    assert recovery_dates("2026-07-01", date(2026, 8, 14), 7) == [
        "2026-08-08", "2026-08-09", "2026-08-10", "2026-08-11",
        "2026-08-12", "2026-08-13", "2026-08-14",
    ]


def test_invalid_to_enabled_refreshes_and_dispatches_once(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    now = datetime(2026, 8, 14, 11, 0, tzinfo=SHANGHAI)
    assert run_once(config, now=now) == "disabled"
    update_account(config, 1, int(now.timestamp() * 1000))
    calls = []
    refresh = lambda _config, before, _started: calls.append(("refresh", before))
    dispatch = lambda _config, event, dates: calls.append(("dispatch", event, dates))
    assert run_once(config, now=now, refresh_fn=refresh, dispatch_fn=dispatch) == "dispatched"
    assert calls[0] == ("refresh", {"feed-a": 100, "feed-b": 200})
    assert calls[1][0] == "dispatch"
    assert calls[1][2] == ["2026-08-13", "2026-08-14"]
    assert run_once(config, now=now, refresh_fn=refresh, dispatch_fn=dispatch) == "no_change"
    assert len(calls) == 2


def test_enabled_updated_at_change_is_new_event(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    now = datetime(2026, 8, 14, 11, 0, tzinfo=SHANGHAI)
    update_account(config, 1, int((now.timestamp() - 3600) * 1000))
    assert run_once(config, now=now) == "no_change"
    update_account(config, 1, int(now.timestamp() * 1000))
    calls = []
    assert run_once(
        config,
        now=now,
        refresh_fn=lambda *_: calls.append("refresh"),
        dispatch_fn=lambda *_: calls.append("dispatch"),
    ) == "dispatched"
    assert calls == ["refresh", "dispatch"]


def test_recent_initial_enabled_is_real_login_event(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    now = datetime(2026, 8, 14, 11, 0, tzinfo=SHANGHAI)
    update_account(config, 1, int((now.timestamp() - 60) * 1000))
    calls = []
    assert run_once(
        config,
        now=now,
        refresh_fn=lambda *_: calls.append("refresh"),
        dispatch_fn=lambda *_: calls.append("dispatch"),
    ) == "dispatched"
    assert calls == ["refresh", "dispatch"]


def test_refresh_failure_waits_for_next_login(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    now = datetime(2026, 8, 14, 11, 0, tzinfo=SHANGHAI)
    run_once(config, now=now)
    update_account(config, 1, int(now.timestamp() * 1000))
    with pytest.raises(RecoveryError, match="401"):
        run_once(
            config,
            now=now,
            refresh_fn=lambda *_: (_ for _ in ()).throw(RecoveryError("401")),
            dispatch_fn=lambda *_: None,
        )
    assert run_once(config, now=now, refresh_fn=lambda *_: None, dispatch_fn=lambda *_: None) == "no_change"
    state = json.loads(config.state_path.read_text(encoding="utf-8"))
    assert state["pending_event"]["phase"] == "failed"


def test_dispatch_failure_retries_without_second_refresh(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    now = datetime(2026, 8, 14, 11, 0, tzinfo=SHANGHAI)
    run_once(config, now=now)
    update_account(config, 1, int(now.timestamp() * 1000))
    refresh_calls = []
    with pytest.raises(RecoveryError, match="Cloudflare"):
        run_once(
            config,
            now=now,
            refresh_fn=lambda *_: refresh_calls.append(1),
            dispatch_fn=lambda *_: (_ for _ in ()).throw(RecoveryError("Cloudflare不可用")),
        )
    dispatch_calls = []
    assert run_once(
        config,
        now=now,
        refresh_fn=lambda *_: refresh_calls.append(1),
        dispatch_fn=lambda *_: dispatch_calls.append(1),
    ) == "dispatched"
    assert refresh_calls == [1]
    assert dispatch_calls == [1]


def test_event_id_does_not_expose_account_id() -> None:
    value = event_id({"id": 316861191, "updated_at": 1786677164517})
    assert value.startswith("login-20260814T111244-")
    assert "316861191" not in value
