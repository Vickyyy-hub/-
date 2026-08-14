# WeWeRSS 登录恢复监控

每分钟只读检查 WeWeRSS SQLite 账号状态。检测到扫码恢复后，先通过本机 tRPC
刷新指定公众号，再调用 Cloudflare 的受保护恢复入口补跑最近最多 7 天。

服务器运行文件位于 `/opt/wewe-rss/recovery`，敏感配置只保存在服务器的
`recovery.env` 和 Cloudflare Secret 中，不进入仓库。

## 管理入口

固定管理地址为 `https://rss.kongye.site/dash/accounts`。后台只使用一组强
WeWeRSS AuthCode：`/dash/` 提供管理页面，`/trpc/` 由 WeWeRSS AuthCode
鉴权。不要为 `/dash/` 增加 HTTP Basic，因为两者都会占用
`Authorization` 请求头并导致后台循环提示“请求失败”。

Nginx 示例位于 `nginx-admin.inc.example`。AuthCode 不得写入仓库或运行日志。
原 HTTP Basic 用户名和密码已停用；切换后应关闭该域名的旧标签页，首次用
无痕窗口打开，避免浏览器缓存旧 Basic 凭据。RSS 随机令牌路径保持独立，裸
`/feeds` 和其他未知路径继续拒绝公网访问，WeWeRSS 容器端口只监听本机。
