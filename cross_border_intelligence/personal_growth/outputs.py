from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any

from .config import output_config


def write_rows(dataset: str, rows: list[dict[str, Any]], day: date) -> dict[str, str]:
    written: dict[str, str] = {}
    json_spec = output_config("json")
    if json_spec.get("enabled", True):
        base = Path(json_spec.get("directory", "output"))
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{dataset}-{day.isoformat()}.json"
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        written["json"] = str(path)
    csv_spec = output_config("csv")
    if csv_spec.get("enabled", True):
        base = Path(csv_spec.get("directory", "output"))
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{dataset}-{day.isoformat()}.csv"
        fields = sorted({key for row in rows for key in row})
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if fields:
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: _csv_value(row.get(key)) for key in fields})
        written["csv"] = str(path)
    return written


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value
