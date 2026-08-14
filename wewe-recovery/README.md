# WeWeRSS 登录恢复监控

每分钟只读检查 WeWeRSS SQLite 账号状态。检测到扫码恢复后，先通过本机 tRPC
刷新指定公众号，再调用 Cloudflare 的受保护恢复入口补跑最近最多 7 天。

服务器运行文件位于 `/opt/wewe-rss/recovery`，敏感配置只保存在服务器的
`recovery.env` 和 Cloudflare Secret 中，不进入仓库。

## 管理入口

固定管理地址为 `https://rss.kongye.site/dash/accounts`。`/dash/` 使用独立
HTTP Basic 密码保护，进入页面后仍需 WeWeRSS AuthCode；`/trpc/` 保留
WeWeRSS 自身鉴权，避免两个鉴权层争用 `Authorization` 请求头。

Nginx 示例位于 `nginx-admin.inc.example`。服务器仅保存 Basic 密码哈希，
真实密码和 AuthCode 不得写入仓库或运行日志。RSS 随机令牌路径保持独立，
裸 `/feeds` 和其他未知路径继续拒绝公网访问。
