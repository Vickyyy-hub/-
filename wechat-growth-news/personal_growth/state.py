from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or os.environ.get("STATE_DB", ".state/news_pipeline.sqlite"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS stage_cache (
                cache_key TEXT NOT NULL,
                stage TEXT NOT NULL,
                version TEXT NOT NULL,
                result_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (cache_key, stage, version)
            );
            CREATE TABLE IF NOT EXISTS job_progress (
                job_id TEXT PRIMARY KEY,
                manifest_json TEXT NOT NULL,
                next_index INTEGER NOT NULL,
                current_article_id TEXT,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.db.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_stage(self, cache_key: str, stage: str, version: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT result_json FROM stage_cache WHERE cache_key=? AND stage=? AND version=?",
            (cache_key, stage, version),
        ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def put_stage(self, cache_key: str, stage: str, version: str, result: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO stage_cache(cache_key, stage, version, result_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cache_key, stage, version) DO UPDATE SET
                result_json=excluded.result_json,
                updated_at=excluded.updated_at
            """,
            (cache_key, stage, version, json.dumps(result, ensure_ascii=False), self._now()),
        )
        self.db.commit()

    def get_progress(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM job_progress WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def save_progress(
        self,
        job_id: str,
        manifest: list[str],
        next_index: int,
        current_article_id: str | None,
        status: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO job_progress(job_id, manifest_json, next_index, current_article_id, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                manifest_json=excluded.manifest_json,
                next_index=excluded.next_index,
                current_article_id=excluded.current_article_id,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (job_id, json.dumps(manifest), next_index, current_article_id, status, self._now()),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()
