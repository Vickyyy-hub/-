# Cloudflare 独立调度器

本目录只负责定时触发 GitHub Actions，不读取文章、豆包或飞书数据。

`POST /wewe-recovered` 使用独立的 `WEWE_RECOVERY_SECRET` 接收腾讯云
WeWeRSS 登录恢复事件。该入口只能强制触发公众号工作流，日期限定为最近 7 天。
一个扫码事件只创建一个 GitHub 工作流，多个日期在该工作流中按从旧到新、最大
并发 1 的方式运行，避免 GitHub 取消等待中的日期；同一登录事件会幂等跳过。

## 定时

- `7 0 * * *`：北京时间 08:07，同时检查网站资讯和公众号资讯
- `37 0 * * *`：北京时间 08:37，同时恢复两条流水线的失败任务
- `7 1 * * *`：北京时间 09:07，最后一次恢复检查
- `7 4 * * *`：北京时间 12:07，扫描网站版最近7天并恢复最早漏跑日期；有网站任务运行时跳过
- `17 0,4,8,12 * * *`：北京时间 08:17、12:17、16:17、20:17，运行跨境政策资讯
- `47 1 * * *`：北京时间 09:47，运行跨境需求信号
- `47 4 * * *`：北京时间 12:47，检查跨境任务最近7天的最早漏跑日期
- `17 2 * * 1`：每周一北京时间 10:17，生成跨境产品方向
- `17 3 1 * *`：每月1日北京时间 11:17，刷新8国消费画像

## Cloudflare Secrets

- `GITHUB_TOKEN`：仅授权 `Vickyyy-hub/-` 且只开放 Actions Read/Write 的细粒度令牌。
- `SCHEDULER_TEST_SECRET`：保护 `POST /trigger` 的随机字符串。
- `WEWE_RECOVERY_SECRET`：保护 `POST /wewe-recovered` 的独立随机字符串。
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
