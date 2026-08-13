# 个人成长每日资讯

Cloudflare 独立调度器位于 `scheduler/`，每日北京时间 08:07、08:37、09:07 触发网站版工作流。GitHub 原生定时和飞书自动化仅作为备用触发。

每天北京时间 08:07、08:37 由飞书触发，08:22、09:22 由 GitHub 备用触发，处理前一自然日的虎嗅、少数派、界面新闻、钛媒体和36氪正式文章；已成功则跳过。IT之家不进入每日任务，仅保留手动历史诊断入口。

## 自动流程

1. 五站独立翻页，直到越过目标日期边界。
2. 排除快讯、付费稿、促销、广告等非目标内容，并读取完整正文。
3. 本地从全文各章节提取证据；豆包先低 Token 初筛，仅对入选稿生成 300–500 字三段式“核心干货”。AI顺序约一半为虎嗅，其他四站轮询，但筛选标准保持一致。
4. 按链接、标题和同一事件去重，以证据质量选择主稿；有独家事实或分析增量的稿件保留为增量稿。
5. 写入飞书后重新读取验证。

豆包固定单线程并至少间隔 4 秒；429 会记录原始响应、退避并保存 SQLite 断点。初筛和摘要分别按正文哈希缓存，失败重启不会重复调用已经完成的阶段。分析未全部完成时不会写入残缺日报。

36氪的发布日期来自其列表接口，正文使用 Jina Reader；绝不使用 Jina 返回的发布日期。

## GitHub 配置

仓库 Secrets：

- `ARK_API_KEY`
- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`

可选变量：`ARK_MODEL`，默认 `doubao-seed-2-0-mini-260215`。

## 本地命令

```bash
python -m pip install -r requirements.txt
pytest -q
python main.py --date 2026-08-08 --collect-only
python main.py --date 2026-08-08 --dry-run
python main.py --date 2026-08-08 --dry-run --max-articles 20
python main.py --date 2026-08-08
python main.py --date 2026-08-08 --backfill-existing
```

历史回填只更新指定日期已存在的飞书记录，不新建、不删除；未匹配记录保持原样并进入报告。

退出码 `2` 表示至少一个来源或正文未完整处理；其他来源仍会继续运行并写入，详细信息见 `run-report.json`。
