from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from .models import Country, SourceSpec


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    path = Path(os.environ.get("PIPELINE_CONFIG", "pipeline.config.json"))
    if not path.is_file():
        raise RuntimeError(f"缺少流水线配置：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def countries() -> dict[str, Country]:
    return {key: Country(code=key, **value) for key, value in load_config()["countries"].items()}


def sources(*, cadence: str | None = None) -> list[SourceSpec]:
    result: list[SourceSpec] = []
    for key, value in load_config()["sources"].items():
        spec = SourceSpec(
            key=key,
            name=value["name"],
            url=value["url"],
            kind=value["kind"],
            cadence=value["cadence"],
            signal_type=value["signal_type"],
            countries=tuple(value.get("countries") or ()),
            enabled=bool(value.get("enabled", True)),
            optional=bool(value.get("optional", False)),
            body_selector=value.get("body_selector", "article, main"),
            notes=value.get("notes", ""),
            auth_env=value.get("auth_env", ""),
            params=dict(value.get("params") or {}),
        )
        if spec.enabled and (cadence is None or spec.cadence == cadence):
            result.append(spec)
    return result


def model_config() -> dict[str, Any]:
    return dict(load_config().get("model") or {})


def output_config(name: str) -> dict[str, Any]:
    return dict((load_config().get("outputs") or {}).get(name) or {})


def feishu_config() -> dict[str, Any]:
    return dict((load_config().get("outputs") or {}).get("feishu") or {})


def product_config() -> dict[str, Any]:
    return dict(load_config().get("product_directions") or {})
