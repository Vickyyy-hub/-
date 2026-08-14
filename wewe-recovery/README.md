# WeWeRSS 登录恢复监控

每分钟只读检查 WeWeRSS SQLite 账号状态。检测到扫码恢复后，先通过本机 tRPC
刷新指定公众号，再调用 Cloudflare 的受保护恢复入口补跑最近最多 7 天。

服务器运行文件位于 `/opt/wewe-rss/recovery`，敏感配置只保存在服务器的
`recovery.env` 和 Cloudflare Secret 中，不进入仓库。
