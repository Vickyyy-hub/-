# 欧洲与拉美跨境市场情报

覆盖英国、德国、法国、西班牙、巴西、墨西哥、智利和哥伦比亚。系统把政策资讯、每日需求信号、国家消费画像和周度产品方向分别处理，并写入四张关联的飞书多维表格。

## 运行内容

- `policy`：每天北京时间 08:00、12:00、16:00、20:00，采集政策、海关、产品安全和平台公告。
- `demand`：每天 09:30，采集八国 Google Trends；有官方令牌时补充 Mercado Libre。
- `weekly`：每周一 10:00 检查统计更新，并基于近 30 天至少两类信号生成产品方向。
- `profiles`：每月 1 日 11:00，使用各国官方统计刷新消费画像。
- `sources`：同步来源配置表；`all` 依次执行全部任务。

Google Trends 使用公开 Trending RSS。Google Trends Alpha API、eBay、Mercado Libre、YouTube 和 Reddit 不会通过网页绕过授权：具备官方权限后再启用对应配置。当前 Reddit 在 `agent-reach doctor` 中无可用后端，因此默认关闭。

政策与数据基础层保留原方案的 WTO、Federal Register、BIS、OFAC、英国 Trade Tariff、欧盟海关、UN Comtrade、Eurostat、ECB、IMO、洛杉矶港、Amazon、Shopify 和 Google Merchant 等来源。数据 API 首期只监控发布与版本变化，不在未配置国家、商品编码或指标时拉取无意义的全量数据。

## 飞书准备

在同一个多维表格应用中建立四张表，名称建议为：

- 来源配置表
- 原始信号表
- 国家消费画像表
- 产品方向表

程序会在 `FEISHU_APP_TOKEN` 对应的多维表格中按名称查找或创建：`跨境-来源配置表`、`跨境-原始信号表`、`跨境-国家消费画像表`、`跨境-产品方向表`。四个 Table ID 环境变量仅作为可选覆盖；正常部署无需手工填写。首次正式运行会补齐缺少的普通文本、数字和日期字段；字段类型不匹配会停止并报告，不会破坏已有记录或个人成长旧表。

飞书主键分别是 `来源ID`、`信号ID`、`国家代码`、`方向ID`。重复运行执行幂等更新，写入后重新读取验证。

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/python main.py --job demand --date 2026-08-13 --collect-only
.venv/bin/python main.py --check-feishu
.venv/bin/python main.py --setup-feishu
.venv/bin/python main.py --job sources
.venv/bin/python main.py --job all
```

`--collect-only` 不调用 AI 和飞书；`--dry-run` 可调用 AI 但不写飞书。不要用历史回填测试线上写入。

## GitHub Actions

复用现有仓库 Secrets：`ARK_API_KEY`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_APP_TOKEN`。工作流使用独立并发组，并在每次运行前后恢复和保存 SQLite 状态，以保持去重、断点和 30 天趋势窗口。

Cloudflare 在北京时间 08:17、12:17、16:17、20:17 触发政策任务，09:47 触发需求任务，12:47 检查最近7天最早漏跑日期；GitHub 原生 cron 作为备用。跨境任务错开现有个人成长任务，降低模型限流风险。

本地 JSON/CSV 输出保存在 `output/`，运行明细保存在 `run-report.json`。单个可选渠道失败只降低覆盖度；核心来源失败会将报告标为 `partial=true` 并返回退出码 2。

## 判断边界

- 国家文化只能由官方统计和多源证据支持，不能用单条社媒讨论概括。
- 每个产品方向至少需要两种信号类型支持。
- 没有销量、成本或竞争数据时统一称为“趋势方向”，不能声称高利润。
- 只保存标题、摘要、链接和分析，不保存受版权保护的完整文章正文。
