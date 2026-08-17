from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta

from personal_growth.pipeline import MarketPipeline
from personal_growth.text import SHANGHAI


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="欧洲与拉美跨境市场情报")
    parser.add_argument("--job", choices=("auto", "policy", "demand", "all"), default="auto")
    parser.add_argument("--date", help="目标日期 YYYY-MM-DD，默认北京时间当天")
    parser.add_argument("--schedule", default=os.environ.get("GITHUB_EVENT_SCHEDULE", ""), help="GitHub cron表达式")
    parser.add_argument("--dry-run", action="store_true", help="允许AI和网络读取，但不写飞书")
    parser.add_argument("--collect-only", action="store_true", help="只采集并写本地缓存，不调用AI或飞书")
    parser.add_argument("--check-feishu", action="store_true", help="只读校验跨境资讯成品表及字段")
    parser.add_argument("--setup-feishu", action="store_true", help="按名称创建或补齐跨境资讯成品表")
    parser.add_argument("--delete-legacy-feishu", action="store_true", help="新表验收后删除四张旧跨境表")
    parser.add_argument("--trigger-source", default=os.environ.get("TRIGGER_SOURCE", "manual"))
    return parser.parse_args()


def resolve_job(job: str, schedule: str) -> str:
    if job != "auto":
        return job
    mapping = {
        "0 0,4,8,12 * * *": "policy",
        "0 1 * * *": "demand",
    }
    return mapping.get(schedule, "all")


def main() -> int:
    args = parse_args()
    pipeline = MarketPipeline()
    if args.check_feishu:
        print(json.dumps(pipeline.check_feishu(), ensure_ascii=False, indent=2))
        return 0
    if args.setup_feishu:
        print(json.dumps(pipeline.setup_feishu(), ensure_ascii=False, indent=2))
        return 0
    if args.delete_legacy_feishu:
        print(json.dumps({"deleted": pipeline.delete_legacy_feishu()}, ensure_ascii=False, indent=2))
        return 0
    target = date.fromisoformat(args.date) if args.date else datetime.now(SHANGHAI).date()
    job = resolve_job(args.job, args.schedule)
    report = pipeline.run(job, target, dry_run=args.dry_run, collect_only=args.collect_only)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 2 if report.partial else 0


if __name__ == "__main__":
    raise SystemExit(main())
