#!/usr/bin/env python3
"""Detect WeWeRSS login recovery, refresh feeds, and request catch-up runs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
FAILURE_MARKERS = ("401", "429", "暂无可用读书账号")


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    database_path: Path
    wewe_env_path: Path
    state_path: Path
    lock_path: Path
    recovery_url: str
    recovery_secret: str
    wewe_base_url: str = "http://127.0.0.1:14000"
    expected_feed_ids: tuple[str, ...] = ()
    max_recovery_days: int = 7
    refresh_timeout_seconds: int = 1_200
    bootstrap_recent_login_seconds: int = 1_800
    container_name: str = "wewe-rss"

    @classmethod
    def from_env(cls) -> "Config":
        required = {
            key: os.environ.get(key, "").strip()
            for key in (
                "DATABASE_PATH",
                "WEWE_ENV_PATH",
                "STATE_PATH",
                "LOCK_PATH",
                "CLOUDFLARE_RECOVERY_URL",
                "WEWE_RECOVERY_SECRET",
            )
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise RecoveryError(f"缺少配置：{', '.join(missing)}")
        feed_ids = tuple(
            item.strip()
            for item in os.environ.get("EXPECTED_FEED_IDS", "").split(",")
            if item.strip()
        )
        return cls(
            database_path=Path(required["DATABASE_PATH"]),
            wewe_env_path=Path(required["WEWE_ENV_PATH"]),
            state_path=Path(required["STATE_PATH"]),
            lock_path=Path(required["LOCK_PATH"]),
            recovery_url=required["CLOUDFLARE_RECOVERY_URL"],
            recovery_secret=required["WEWE_RECOVERY_SECRET"],
            wewe_base_url=os.environ.get("WEWE_BASE_URL", "http://127.0.0.1:14000").rstrip("/"),
            expected_feed_ids=feed_ids,
            max_recovery_days=int(os.environ.get("MAX_RECOVERY_DAYS", "7")),
            refresh_timeout_seconds=int(os.environ.get("REFRESH_TIMEOUT_SECONDS", "1200")),
            bootstrap_recent_login_seconds=int(os.environ.get("BOOTSTRAP_RECENT_LOGIN_SECONDS", "1800")),
            container_name=os.environ.get("WEWE_CONTAINER_NAME", "wewe-rss"),
        )


def log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True), flush=True)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(state, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)


def open_database(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def account_snapshot(connection: sqlite3.Connection) -> dict:
    row = connection.execute(
        "SELECT id, status, updated_at FROM accounts ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {"id": None, "status": 0, "updated_at": 0}
    return {"id": int(row["id"]), "status": int(row["status"]), "updated_at": int(row["updated_at"])}


def feed_snapshot(connection: sqlite3.Connection, expected_ids: tuple[str, ...]) -> dict[str, int]:
    rows = connection.execute("SELECT id, sync_time FROM feeds").fetchall()
    values = {str(row["id"]): int(row["sync_time"] or 0) for row in rows}
    if expected_ids:
        missing = [feed_id for feed_id in expected_ids if feed_id not in values]
        if missing:
            raise RecoveryError(f"缺少预期公众号源：{','.join(missing)}")
        return {feed_id: values[feed_id] for feed_id in expected_ids}
    if not values:
        raise RecoveryError("未找到公众号订阅")
    return values


def parse_env_value(path: Path, name: str) -> str:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip("\"'")
    raise RecoveryError(f"{path} 中缺少 {name}")


def event_id(account: dict) -> str:
    material = f"{account['id']}:{account['updated_at']}".encode()
    digest = hashlib.sha256(material).hexdigest()[:12]
    updated = datetime.fromtimestamp(account["updated_at"] / 1000, SHANGHAI)
    return f"login-{updated:%Y%m%dT%H%M%S}-{digest}"


def recovery_dates(disabled_since: str | None, today: date, maximum: int) -> list[str]:
    if maximum < 1 or maximum > 7:
        raise RecoveryError("MAX_RECOVERY_DAYS 必须为1至7")
    if disabled_since:
        start = date.fromisoformat(disabled_since) - timedelta(days=1)
    else:
        start = today - timedelta(days=1)
    earliest = today - timedelta(days=maximum - 1)
    start = max(start, earliest)
    return [(start + timedelta(days=offset)).isoformat() for offset in range((today - start).days + 1)]


def refresh_wewe(config: Config, before: dict[str, int], started_at: datetime) -> None:
    auth_code = parse_env_value(config.wewe_env_path, "AUTH_CODE")
    request = urllib.request.Request(
        f"{config.wewe_base_url}/trpc/feed.refreshArticles",
        data=json.dumps({"json": {}}).encode(),
        headers={"Authorization": auth_code, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.refresh_timeout_seconds) as response:
            response.read()
            if response.status >= 300:
                raise RecoveryError(f"WeWeRSS刷新返回HTTP {response.status}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RecoveryError(f"WeWeRSS刷新返回HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RecoveryError(f"WeWeRSS刷新请求失败：{error.reason}") from error

    with open_database(config.database_path) as connection:
        account = account_snapshot(connection)
        after = feed_snapshot(connection, config.expected_feed_ids)
    if account["status"] != 1:
        raise RecoveryError("刷新后微信读书账号已失效")
    stale = [feed_id for feed_id, previous in before.items() if after.get(feed_id, 0) <= previous]
    if stale:
        raise RecoveryError(f"公众号同步时间未更新：{','.join(stale)}")

    since = started_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    completed = subprocess.run(
        ["docker", "logs", "--since", since, config.container_name],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    matched = [marker for marker in FAILURE_MARKERS if marker in output]
    if matched:
        raise RecoveryError(f"刷新日志出现失败标记：{','.join(matched)}")


def dispatch_recovery(config: Config, event: str, dates: list[str]) -> None:
    request = urllib.request.Request(
        config.recovery_url,
        data=json.dumps({"event_id": event, "target_dates": dates}).encode(),
        headers={
            "Authorization": f"Bearer {config.recovery_secret}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if response.status >= 300 or not payload.get("ok"):
                raise RecoveryError(f"恢复调度返回HTTP {response.status}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RecoveryError(f"恢复调度返回HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RecoveryError(f"恢复调度请求失败：{error.reason}") from error


def run_once(
    config: Config,
    *,
    now: datetime | None = None,
    refresh_fn: Callable[[Config, dict[str, int], datetime], None] = refresh_wewe,
    dispatch_fn: Callable[[Config, str, list[str]], None] = dispatch_recovery,
) -> str:
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    state = load_state(config.state_path)
    with open_database(config.database_path) as connection:
        account = account_snapshot(connection)

    pending = state.get("pending_event")
    if pending and pending.get("phase") == "refreshed":
        dispatch_fn(config, pending["id"], pending["target_dates"])
        state["last_processed_event"] = pending["id"]
        state.pop("pending_event", None)
        save_state(config.state_path, state)
        log("recovery_dispatched", event_id=pending["id"], target_dates=pending["target_dates"])
        return "dispatched"

    previous_status = state.get("last_status")
    previous_updated = int(state.get("last_seen_updated_at") or 0)
    state["last_status"] = account["status"]
    state["last_seen_updated_at"] = account["updated_at"]

    if account["status"] != 1:
        state.setdefault("disabled_since_date", current.date().isoformat())
        save_state(config.state_path, state)
        return "disabled"

    initial_recent = (
        previous_status is None
        and account["updated_at"] > 0
        and current.timestamp() - account["updated_at"] / 1000 <= config.bootstrap_recent_login_seconds
    )
    is_login = previous_status == 0 or (previous_status == 1 and account["updated_at"] > previous_updated) or initial_recent
    current_event = event_id(account)
    if not is_login or state.get("last_processed_event") == current_event:
        state.pop("disabled_since_date", None)
        save_state(config.state_path, state)
        return "no_change"

    dates = recovery_dates(state.get("disabled_since_date"), current.date(), config.max_recovery_days)
    state["pending_event"] = {"id": current_event, "phase": "detected", "target_dates": dates}
    save_state(config.state_path, state)
    started_at = datetime.now(timezone.utc)
    try:
        with open_database(config.database_path) as connection:
            before = feed_snapshot(connection, config.expected_feed_ids)
        log("login_recovered", event_id=current_event, target_dates=dates)
        refresh_fn(config, before, started_at)
        state["pending_event"]["phase"] = "refreshed"
        save_state(config.state_path, state)
        dispatch_fn(config, current_event, dates)
    except Exception as error:
        if state.get("pending_event", {}).get("phase") != "refreshed":
            state["pending_event"]["phase"] = "failed"
            state["pending_event"]["error"] = str(error)[:500]
            save_state(config.state_path, state)
        raise

    state["last_processed_event"] = current_event
    state.pop("pending_event", None)
    state.pop("disabled_since_date", None)
    save_state(config.state_path, state)
    log("recovery_dispatched", event_id=current_event, target_dates=dates)
    return "dispatched"


def main() -> int:
    config = Config.from_env()
    config.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with config.lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("monitor_already_running")
            return 0
        try:
            result = run_once(config)
            log("monitor_complete", result=result)
            return 0
        except Exception as error:
            log("monitor_failed", message=str(error))
            return 1


if __name__ == "__main__":
    sys.exit(main())
