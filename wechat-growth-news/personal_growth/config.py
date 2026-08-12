from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    path = Path(os.environ.get("PIPELINE_CONFIG", "pipeline.config.json"))
    if not path.is_file():
        raise RuntimeError(f"缺少流水线配置：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def daily_source_keys() -> list[str]:
    sources = load_config().get("sources") or {}
    return [key for key, value in sources.items() if value.get("enabled") and value.get("mode") == "daily"]


def source_config(key: str) -> dict[str, Any]:
    return dict((load_config().get("sources") or {}).get(key) or {})


def model_config() -> dict[str, Any]:
    return dict(load_config().get("model") or {})


def output_config(name: str) -> dict[str, Any]:
    return dict((load_config().get("outputs") or {}).get(name) or {})


def summary_config() -> dict[str, Any]:
    return dict(load_config().get("summary") or {})
