from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import CountryProfile, Signal


class MarketStore:
    def __init__(self, path: str | None = None) -> None:
        db_path = Path(path or os.environ.get("STATE_DB", ".state/market_intelligence.sqlite"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(db_path)
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY,
                published_at TEXT NOT NULL,
                countries_json TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS progress (
                job_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS completions (
                job_key TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS profiles (
                country_code TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )"""
        )
        self.connection.commit()

    def upsert_signals(self, signals: list[Signal]) -> tuple[int, int]:
        inserted = duplicates = 0
        for signal in signals:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO signals VALUES (?, ?, ?, ?, ?)",
                (
                    signal.signal_id,
                    signal.published_at.isoformat(),
                    json.dumps(signal.countries, ensure_ascii=False),
                    signal.signal_type,
                    json.dumps(signal.to_dict(), ensure_ascii=False),
                ),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                duplicates += 1
        self.connection.commit()
        return inserted, duplicates

    def save_progress(self, job_key: str, payload: dict) -> None:
        now = datetime.now().astimezone().isoformat()
        self.connection.execute(
            "INSERT OR REPLACE INTO progress VALUES (?, ?, ?)",
            (job_key, json.dumps(payload, ensure_ascii=False), now),
        )
        self.connection.commit()

    def load_progress(self, job_key: str) -> dict:
        row = self.connection.execute(
            "SELECT payload_json FROM progress WHERE job_key = ?", (job_key,)
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def mark_complete(self, job_key: str, payload: dict) -> None:
        now = datetime.now().astimezone().isoformat()
        self.connection.execute(
            "INSERT OR REPLACE INTO completions VALUES (?, ?, ?)",
            (job_key, now, json.dumps(payload, ensure_ascii=False)),
        )
        self.connection.execute("DELETE FROM progress WHERE job_key = ?", (job_key,))
        self.connection.commit()

    def is_complete(self, job_key: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM completions WHERE job_key = ?", (job_key,)
        ).fetchone() is not None

    def load_signals(self, since: datetime, country_code: str | None = None) -> list[dict]:
        rows = self.connection.execute(
            "SELECT payload_json FROM signals WHERE published_at >= ? ORDER BY published_at DESC",
            (since.isoformat(),),
        ).fetchall()
        result = [json.loads(row[0]) for row in rows]
        if country_code:
            result = [item for item in result if country_code in item.get("countries", [])]
        return result

    def upsert_profiles(self, profiles: list[CountryProfile]) -> None:
        for profile in profiles:
            updated = profile.updated_at or datetime.now().astimezone()
            self.connection.execute(
                "INSERT OR REPLACE INTO profiles VALUES (?, ?, ?)",
                (profile.country_code, updated.isoformat(), json.dumps(profile.to_dict(), ensure_ascii=False)),
            )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
