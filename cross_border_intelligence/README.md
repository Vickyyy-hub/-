# 跨境资讯成品流水线

覆盖英国、德国、法国、西班牙、巴西、墨西哥、智利和哥伦比亚，并补充全球贸易、海关、产品安全及平台公告。系统只向飞书写入有真实详情页、正文证据和合格中文摘要的成品资讯。

## 成品标准

- 原文必须是文章详情页，不接受 RSS 地址、栏目首页、API 文档或统计数据库入口。
- 直接读取受阻时使用 Jina Reader；正文不足 300 个可见字符直接跳过。
- Google Trends 仅作为发现入口，保存关联新闻的标题、媒体、详情链接、真实发布日期和正文。
- AI 先筛选跨境相关性，再生成一段 80–150 字、目标约 100 字的中文摘要。
- 摘要中的时间、实体和数字必须来自正文或文章元数据；一次重写仍不合格就不入库。
- 体育、明星、比赛、普通娱乐及无法说明商业影响的热点不入库。

## 飞书表

程序在 `FEISHU_APP_TOKEN` 对应的多维表格中按名称创建或查找 `跨境资讯`，字段为标题、可点击来源网站、国家/地区、内容主题、归档板块、核心干货、搜索词、热度、来源名称、发布日期、日报日期和记录状态。

按详情 URL 和规范化标题幂等更新。创建后会回读验证链接、日报日期及摘要长度。只有新表至少一条成品通过验收，`--delete-legacy-feishu` 才允许删除四张旧跨境表；个人成长资讯表不在删除名单中。

## 运行

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest -q
python3 main.py --job demand --date 2026-08-17 --collect-only
python3 main.py --job demand --date 2026-08-17 --dry-run
python3 main.py --check-feishu
python3 main.py --setup-feishu
python3 main.py --job all --date 2026-08-17
python3 main.py --delete-legacy-feishu
```

`--collect-only` 不调用 AI 或飞书；`--dry-run` 调用 AI 但不写飞书。JSON/CSV 成品保存在 `output/news-YYYY-MM-DD.*`，采集检查保存在 `output/collected-YYYY-MM-DD.*`。

## 调度

Cloudflare 是唯一调度入口：北京时间 08:17、12:17、16:17、20:17 运行政策任务，09:17 运行需求任务；12:17 同时检查需求任务漏跑。Cloudflare 调用 GitHub Actions，GitHub 工作流不配置原生 cron。
