from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from personal_growth.adapters import ADAPTERS
from personal_growth.feishu import FeishuBitable
from personal_growth.http import HttpClient
from personal_growth.pipeline import Pipeline
from personal_growth.text import SHANGHAI


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="六站个人成长资讯自动化")
    parser.add_argument("--date", help="处理日期 YYYY-MM-DD；默认北京时间前一自然日")
    parser.add_argument("--sources", default="all", help="all 或逗号分隔：huxiu,sspai,jiemian,ithome,tmtpost,36kr")
    parser.add_argument("--trigger-source", default="manual", help="运行触发来源，仅用于报告审计")
    parser.add_argument("--dry-run", action="store_true", help="执行采集和AI，但不写飞书")
    parser.add_argument("--collect-only", action="store_true", help="只采集并验证正文，不调用AI、不写飞书")
    parser.add_argument("--backfill-existing", action="store_true", help="按指定日期原地回填已有飞书记录，不新增")
    parser.add_argument("--max-articles", type=int, default=0, help="仅处理前N篇；0表示不限制，主要用于验收")
    parser.add_argument("--check-feishu", action="store_true", help="只读验证飞书权限和字段，不写入")
    parser.add_argument("--backfill-daily-dates", action="store_true", help="仅按发布日期回填飞书日报日期，不调用AI")
    parser.add_argument("--write-cached-only", action="store_true", help="只写入已有AI缓存，不继续调用模型")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.backfill_daily_dates:
        client = FeishuBitable(HttpClient())
        updated, failed, skipped = client.backfill_daily_dates()
        result = {"updated": updated, "failed": failed, "skipped_missing_date": skipped}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if failed else 0
    if args.write_cached_only:
        target = date.fromisoformat(args.date) if args.date else datetime.now(SHANGHAI).date() - timedelta(days=1)
        sources = list(ADAPTERS) if args.sources == "all" else [item.strip() for item in args.sources.split(",") if item.strip()]
        report = Pipeline(sources).write_cached(target)
        report.trigger_source = args.trigger_source
        Path("run-report.json").write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 2 if report.write_failed else 0
    if args.check_feishu:
        status = FeishuBitable(HttpClient(), read_only=True).read_only_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    target = date.fromisoformat(args.date) if args.date else datetime.now(SHANGHAI).date() - timedelta(days=1)
    sources = list(ADAPTERS) if args.sources == "all" else [item.strip() for item in args.sources.split(",") if item.strip()]
    unknown = set(sources) - set(ADAPTERS)
    if unknown:
        raise SystemExit(f"未知来源：{', '.join(sorted(unknown))}")
    if args.backfill_existing and (args.dry_run or args.collect_only):
        raise SystemExit("历史回填不能与 --dry-run 或 --collect-only 同时使用")
    report = Pipeline(sources).run(
        target,
        dry_run=args.dry_run,
        collect_only=args.collect_only,
        backfill=args.backfill_existing,
        max_articles=max(0, args.max_articles),
        trigger_source=args.trigger_source,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if report.partial:
        print("任务部分失败：请查看 run-report.json 中各站错误。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
