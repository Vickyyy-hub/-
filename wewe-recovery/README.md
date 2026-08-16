# WeWeRSS 登录恢复监控

每分钟只读检查 WeWeRSS SQLite 账号状态。账号连续两次显示启用后才认定扫码
成功，再通过本机 tRPC 刷新指定公众号，并调用 Cloudflare 的受保护恢复入口
串行补跑最近最多 7 天。短暂启用后立即失效的令牌不会启动 AI 任务。

服务器运行文件位于 `/opt/wewe-rss/recovery`，敏感配置只保存在服务器的
`recovery.env` 和 Cloudflare Secret 中，不进入仓库。

## 管理入口

固定管理地址为 `https://rss.kongye.site/dash/accounts`。后台只使用一组强
WeWeRSS AuthCode：`/dash/` 提供管理页面，`/trpc/` 由 WeWeRSS AuthCode
鉴权。不要为 `/dash/` 增加 HTTP Basic，因为两者都会占用
`Authorization` 请求头并导致后台循环提示“请求失败”。

容器继续使用本机 `SERVER_ORIGIN_URL`，Nginx 只在 `/dash/` 的 HTML 响应中
将该地址替换为 `https://rss.kongye.site`。这样浏览器会通过同域名访问
`/trpc/`，同时不会改变带随机令牌的 RSS/JSON 地址。

Nginx 示例位于 `nginx-admin.inc.example`。AuthCode 不得写入仓库或运行日志。
原 HTTP Basic 用户名和密码已停用；切换后应关闭该域名的旧标签页，首次用
无痕窗口打开，避免浏览器缓存旧 Basic 凭据。RSS 随机令牌路径保持独立，裸
`/feeds` 和其他未知路径继续拒绝公网访问，WeWeRSS 容器端口只监听本机。

若账号列表或二维码提示 `Load failed`，先检查页面源码中的
`window.__WEWE_RSS_SERVER_ORIGIN_URL__`；公网响应必须为
`https://rss.kongye.site`，不能是 `http://127.0.0.1:14000`。
