from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta

from personal_growth.adapters import ADAPTERS
from personal_growth.config import daily_source_keys
from personal_growth.feishu import FeishuBitable
from personal_growth.http import HttpClient
from personal_growth.pipeline import Pipeline
from personal_growth.text import SHANGHAI


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="公众号每日资讯自动化")
    parser.add_argument("--date", help="处理日期 YYYY-MM-DD；默认北京时间前一自然日")
    parser.add_argument("--sources", default="all", help="all或逗号分隔的公众号来源键")
    parser.add_argument("--trigger-source", default="manual", help="运行触发来源")
    parser.add_argument("--dry-run", action="store_true", help="执行采集和AI，但不写飞书")
    parser.add_argument("--collect-only", action="store_true", help="只采集并验证正文")
    parser.add_argument("--max-articles", type=int, default=0, help="最多处理文章数；0表示不限制")
    parser.add_argument("--check-feishu", action="store_true", help="只读验证飞书权限和字段")
    parser.add_argument("--bootstrap-feishu", action="store_true", help="幂等创建公众号每日资讯数据表")
    return parser.parse_args()


def resolve_sources(value: str) -> list[str]:
    return daily_source_keys() if value == "all" else [item.strip() for item in value.split(",") if item.strip()]


def main() -> int:
    args = parse_args()
    if args.bootstrap_feishu:
        result = FeishuBitable.bootstrap_table(HttpClient(), "公众号每日资讯")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.check_feishu:
        status = FeishuBitable(HttpClient(), read_only=True).read_only_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    target = date.fromisoformat(args.date) if args.date else datetime.now(SHANGHAI).date() - timedelta(days=1)
    sources = resolve_sources(args.sources)
    unknown = set(sources) - set(ADAPTERS)
    if unknown:
        raise SystemExit(f"未知来源：{', '.join(sorted(unknown))}")
    report = Pipeline(sources).run(
        target,
        dry_run=args.dry_run,
        collect_only=args.collect_only,
        max_articles=max(0, args.max_articles),
        trigger_source=args.trigger_source,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if report.partial:
        print("任务部分失败：已保留失败记录并将在7天内自动重试。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
