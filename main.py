from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta

from personal_growth.adapters import ADAPTERS
from personal_growth.pipeline import Pipeline
from personal_growth.text import SHANGHAI


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="六站个人成长资讯自动化")
    parser.add_argument("--date", help="处理日期 YYYY-MM-DD；默认北京时间前一自然日")
    parser.add_argument("--sources", default="all", help="all 或逗号分隔：huxiu,sspai,jiemian,ithome,tmtpost,36kr")
    parser.add_argument("--dry-run", action="store_true", help="执行采集和AI，但不写飞书")
    parser.add_argument("--collect-only", action="store_true", help="只采集并验证正文，不调用AI、不写飞书")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target = date.fromisoformat(args.date) if args.date else datetime.now(SHANGHAI).date() - timedelta(days=1)
    sources = list(ADAPTERS) if args.sources == "all" else [item.strip() for item in args.sources.split(",") if item.strip()]
    unknown = set(sources) - set(ADAPTERS)
    if unknown:
        raise SystemExit(f"未知来源：{', '.join(sorted(unknown))}")
    report = Pipeline(sources).run(target, dry_run=args.dry_run, collect_only=args.collect_only)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if report.partial:
        print("任务部分失败：请查看 run-report.json 中各站错误。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
