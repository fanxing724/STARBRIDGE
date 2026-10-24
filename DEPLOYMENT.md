# CDN 接入与恢复

以下以 `download.xingbox.me` 为例。将 `ORIGIN_IP` 替换为自己的服务器地址；真实凭据和本机部署记录不保存在仓库中。

## 当前布局

- 本机源代码：`<本机项目目录>`。
- 服务器代码：`/opt/starbridge`；配置：`/etc/starbridge/config.json`。
- systemd：`starbridge.service`；运行用户：`starbridge`。
- HTTP 仅本机 `127.0.0.1:18080`，不修改现有 Pulse 的 80/443。
- TLS 源站示例：`ORIGIN_IP:8443`，与现有业务端口隔离。部署后应验证专属 CA 校验；这不是浏览器公认的公网边缘证书。
- 本机 SSH 隧道：`127.0.0.1:18081` → 服务器 `127.0.0.1:18080`。

## EdgeOne CDN（非 Pages Functions）

这里接入的是现有站点的自建源站 CDN，不需要创建 Pages 项目或 Edge/Node 函数。`edgeone login` 是 Makers/Pages CLI 登录，不保证有域名 DNS/CDN 管理权限；若 CLI 无相应能力，需在 EdgeOne 站点控制台操作或使用具备站点权限的官方 API。不要把 API Token 粘贴到聊天里。

1. 在 `xingbox.me` 站点中新增加速域名 `download.xingbox.me`。源站为 `ORIGIN_IP`，HTTPS，端口 `8443`，回源 Host/SNI 为 `download.xingbox.me`。
2. 独立 TLS 源站可由专属自建 CA 签发，证书需包含域名与 IP SAN。CA 公钥在本机任务目录 `origin-ca.crt`，私钥仅保留服务器 `/root/maintenance/starbridge-ca/`。在套餐支持时上传 CA 公钥启用指定 CA 校验；不上传源站私钥给客户端。公网边缘证书使用 EdgeOne 托管证书。源站证书有效期一年，需要在到期前更新。
3. 根据控制台实际给出的 CNAME 添加 `download` 记录。**不可预先编造 CNAME**；不要修改站点其他 DNS 记录。
4. 公开限额模式：所有路径禁用缓存/遵循 `no-store`，包括 `/d/*`。清理旧版 `/d/*` 缓存，不能沿用“下载文件强制缓存”规则。
5. 在回源请求头规则中**设置/覆盖** `X-StarBridge-Origin-Key`，值为服务器配置中的 `proxy_secret`。保留平台默认的 `EO-Connecting-IP`。服务器只有验证密钥后才采信这个 IP；否则按 TCP 对端计数，CDN 节点后的用户会共享额度。密钥仅通过本机受保护文件交接，不写进公开文档。
6. 透传 Range/If-Range，不启用分片回源、预取或多段合并，以免 CDN 放大源站请求或干扰限额；关闭压缩转换，保持二进制内容与 Content-Length/Content-Range 一致。
7. Docker 路径不启用 JS 挑战、交互验证码或缓存；公开模式无需登录。仅修改本加速域名的规则，保留其他站点规则。
8. 本版本签名链接是临时下载授权；如需边缘即时鉴权/私有内容控制，再独立设计缓存前鉴权，不通过忽略 Authorization 来提升命中。

官方说明：[分片回源](https://edgeone.ai/zh/document/46966)、[源站证书校验](https://edgeone.ai/document/74344)。是否有中国大陆节点、流量额度、文件大小上限及适用内容，应以账户当前套餐为准。

## 接入后的验收

- DNS 解析到控制台下发 CNAME，HTTPS 证书受信任。
- 公开模式 `/`、`/v2/` 和 `/api/limits` 无登录返回 200，页面显示限额。私用模式保留 Basic 认证。
- 下载 GitHub Release，和原文件比较 SHA256；Range 返回 206，Content-Range 与字节正确。
- 同一下载重复请求仍需回源，确认 `Cache-Control` 为 no-store、无 Age 增长/缓存 HIT；清理旧缓存后再次确认。
- 无登录执行 `docker pull download.xingbox.me/library/hello-world:latest`，确认真实客户端兼容性；必要时将客户端镜像层下载并发调低。
- 测原链接与代理下载速度，并验证同一 IP 两个下载合计不超过 2 MiB/s、第三个被限流；确认 CDN 回源头密钥验证后使用正确客户端 IP。源站成功不代表国内提速。

## 管理后台

后台地址为 `/admin`，用户名 `bridge`，首次密码来自独立配置项 `admin_password`（升级时须生成至少 24 字符随机值）。管理密码更新后以 PBKDF2 哈希保存，不再重新从配置覆盖；私用下载密码仍来自只读配置。后台设置、凭据哈希和最多 100 条管理操作记录保存在同一个 SQLite，保存设置不写 `/etc/starbridge/config.json`。

确认管理登录与写操作经过受信任 HTTPS，Cookie 包含 Secure/HttpOnly/SameSite=Strict；`/admin/*` 和静态管理资源均遵循 no-store。不要暴露配置或数据库为静态资源，也不要把后台密码、会话 Cookie 放入日志。

## 更新与回退

先将 `/opt/starbridge`、unit 和配置备份到 `/root/maintenance/starbridge-时间/`，配置备份权限 600。新代码先跑测试，再替换代码并执行 `systemctl restart starbridge`。配置不要被 `create_config.py` 覆盖：该脚本使用独占创建，已有文件会拒绝。

配额状态保存在 `/var/lib/starbridge/usage.sqlite3`，unit 的 `StateDirectory=starbridge` 创建持久化目录。公开配置必须指定可写 `quota_db`，不要将 SQLite 放在 ProtectSystem=strict 保护的 `/etc` 下。正常重启不清零日额度；备份数据时使用 SQLite backup API，避免只复制运行中的主文件而漏掉 WAL。

查看健康与资源：

```sh
systemctl status starbridge --no-pager
systemctl show starbridge -p MemoryCurrent -p CPUUsageNSec
journalctl -u starbridge -n 30 --no-pager
```

停止新增服务可执行 `systemctl disable --now starbridge`；不影响 Pulse 或 SSH。若已接 CDN，先禁用新增加速域名/移除其 DNS，避免继续回源。文件与凭据是否删除应另行确定，不自动清理用户数据。

防火墙新增规则仅允许 TCP 8443，已保存到 `/etc/nftables.conf`。原配置与运行规则备份在 `/root/maintenance/starbridge-firewall-时间/`。回退时先用 `nft -a list chain inet codex_firewall input` 查找注释 `StarBridge HTTPS origin` 对应 handle，再只删除这条规则，同时删除持久化配置中对应行；不要整体重载陈旧规则集。现有 SSH 22 和 Pulse 80/443 规则保持原状。

0.3 管理后台升级仅增加数据表，不重建 usage。代码回退时保留当前 SQLite，避免退回旧配额快照而放宽日额度；使用 SQLite backup API 保存一致快照供恢复排查。后台设置优先于配置文件默认项，旧版本代码会忽略管理设置表，采用其自身配置。
