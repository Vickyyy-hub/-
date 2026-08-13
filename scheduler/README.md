# Cloudflare 独立调度器

本目录只负责定时触发 GitHub Actions，不读取文章、豆包或飞书数据。

## 定时

- `7 0 * * *`：北京时间 08:07，同时检查网站资讯和公众号资讯
- `37 0 * * *`：北京时间 08:37，同时恢复两条流水线的失败任务
- `7 1 * * *`：北京时间 09:07，最后一次恢复检查

## Cloudflare Secrets

- `GITHUB_TOKEN`：仅授权 `Vickyyy-hub/-` 且只开放 Actions Read/Write 的细粒度令牌。
- `SCHEDULER_TEST_SECRET`：保护 `POST /trigger` 的随机字符串。
- `GITHUB_TOKEN_EXPIRES_AT`：令牌到期日，格式 `YYYY-MM-DD`，用于到期前30天告警。

不要将任何真实密钥写入本目录、GitHub 日志或运行报告。

## 验证

```bash
npm test
npx wrangler deploy
curl -fsS https://<worker-domain>/health
curl -fsS -X POST https://<worker-domain>/trigger \
  -H "Authorization: Bearer <SCHEDULER_TEST_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"pipeline":"wechat","target_date":"2026-08-13"}'
```
