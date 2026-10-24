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
4. 默认所有路径遵循源站 `Cache-Control`，页面、API、Docker、错误响应不缓存；不要设置“全部强制缓存”。
5. `/d/*` 可缓存，完整路径必须纳入缓存键，不能忽略过期时间/签名/目标编码。遵循源站 TTL，不强制覆盖。GET/HEAD 可缓存，POST 不缓存。
6. 在确认源站 Range 测试通过后为大文件启用 Range GETs/分片回源。关闭内容压缩转换，保持二进制内容和响应头；不要通过修改后缀或切块规避套餐文件大小限制。
7. Docker 路径保持 Authorization 透传、不启用 JS 挑战、交互验证码或缓存。只对下载路径调整必要规则，保留其他站点规则。
8. 本版本签名链接是临时下载授权；如需边缘即时鉴权/私有内容控制，再独立设计缓存前鉴权，不通过忽略 Authorization 来提升命中。

官方说明：[分片回源](https://edgeone.ai/zh/document/46966)、[源站证书校验](https://edgeone.ai/document/74344)。是否有中国大陆节点、流量额度、文件大小上限及适用内容，应以账户当前套餐为准。

## 接入后的验收

- DNS 解析到控制台下发 CNAME，HTTPS 证书受信任。
- `/` 和 `/v2/` 未登录为 401；正确密码能打开页面/登录。
- 下载 GitHub Release，和原文件比较 SHA256；Range 返回 206，Content-Range 与字节正确。
- 同一签名链接重复请求，查看平台实际缓存状态头/控制台命中数据；不能仅凭下载成功认定缓存命中。
- `docker login` 后执行 `docker pull download.xingbox.me/library/hello-world:latest`，确认真实 Docker 客户端兼容性。
- 对国内客户端测原链接、首次代理、缓存命中三组速度，记录时间、文件大小与 CDN 头。服务器访问成功不代表国内加速成功。

## 更新与回退

先将 `/opt/starbridge`、unit 和配置备份到 `/root/maintenance/starbridge-时间/`，配置备份权限 600。新代码先跑测试，再替换代码并执行 `systemctl restart starbridge`。配置不要被 `create_config.py` 覆盖：该脚本使用独占创建，已有文件会拒绝。

查看健康与资源：

```sh
systemctl status starbridge --no-pager
systemctl show starbridge -p MemoryCurrent -p CPUUsageNSec
journalctl -u starbridge -n 30 --no-pager
```

停止新增服务可执行 `systemctl disable --now starbridge`；不影响 Pulse 或 SSH。若已接 CDN，先禁用新增加速域名/移除其 DNS，避免继续回源。文件与凭据是否删除应另行确定，不自动清理用户数据。

防火墙新增规则仅允许 TCP 8443，已保存到 `/etc/nftables.conf`。原配置与运行规则备份在 `/root/maintenance/starbridge-firewall-时间/`。回退时先用 `nft -a list chain inet codex_firewall input` 查找注释 `StarBridge HTTPS origin` 对应 handle，再只删除这条规则，同时删除持久化配置中对应行；不要整体重载陈旧规则集。现有 SSH 22 和 Pulse 80/443 规则保持原状。
