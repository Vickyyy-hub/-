# 公众号每日资讯

每天北京时间 08:30 处理前一自然日的简单心理、丁香医生、地球知识局、格隆和利维坦文章；09:30 为失败恢复触发，成功后自动跳过。

流程使用 WeWeRSS JSON 获取真实发布时间与原文链接，只为目标日文章请求全文。正文完整的文章先进行低 Token 分类，再生成严格三段、300–500 字的“核心干货”；广告、促销、纯图片、纯视频和信息不足内容不进入日报。不同公众号对同一事件的文章分别保留，只按规范化 URL 和标题去重。

正文或 AI 失败会在飞书保留“正文待补全”或“AI待重试”记录，最近 7 天内自动重试并原地更新，不生成占位摘要。首次运行只处理前一日，不回填历史文章。

## 环境变量

- `ARK_API_KEY`
- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_APP_TOKEN`
- `FEISHU_WECHAT_TABLE_ID`
- `WEWE_RSS_BASE_URL`

所有凭据均由 GitHub Secrets 或 Variables 注入，不写入公开仓库。

## 验证命令

```bash
python -m pytest -q
python main.py --date 2026-08-12 --collect-only
python main.py --date 2026-08-12 --dry-run --max-articles 3
python main.py --check-feishu
python main.py --date 2026-08-12
```
