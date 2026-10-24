<!-- fanxing-style:start -->
<picture><source media="(prefers-color-scheme: light)" srcset="https://fanxing724.github.io/new-make/fanxing724/projects/starbridge/cover.light.svg"><img alt="StarBridge · 公开下载桥" src="https://fanxing724.github.io/new-make/fanxing724/projects/starbridge/cover.svg" width="100%"></picture>

**把公开资源接到一座更清楚的桥上。**

番星 / 星轨手记 · 项目 README 入口
<!-- fanxing-style:end -->

# 星桥 StarBridge

公开下载入口：无需账号，转发 GitHub Release、软件/驱动 HTTPS 直链，以及 Docker Hub 公共镜像。Python 3.11+ 标准库实现，无第三方运行时依赖。

## 默认限制

| 项目 | 限制 |
|---|---|
| 每 IP 同时下载 | 2 个 |
| 每 IP 合计速度 | 2 MiB/s，多个下载共享 |
| 每 IP 每日下载额度 | 5 GiB |
| 全站每日下载额度 | 30 GiB |
| 每 IP 请求频率 | 每分钟 60 次 |
| 全站同时传输 | 4 个 |

普通文件、Range 分段和 Docker 清单/镜像层共用下载额度。北京时间每日零点开启新一天额度，共享公网 IP 的用户共享额度，IPv6 按 /64 网段合并。SQLite 持久化记录，不因正常重启清零。使用按日 HMAC 标识，不在数据库中保存明文 IP。

已知大小的响应在下载前预留额度，未发送部分在连接结束时退还。未知大小的响应按分块扣减，额度耗尽时中断；客户端应校验文件哈希并按需续传。异常退出时未退还的预留额保守计入已用量。跨午夜的传输按开始下载的日期计费。额度针对下载响应正文，不等同于运营商/CDN 账单的全部带宽，页面、头部和回源开销不包含其中。

## 本地运行

```sh
python3 create_config.py /absolute/path/config.json
python3 server.py --config /absolute/path/config.json
```

默认监听 `127.0.0.1:18080`，新配置默认 `public: true`，配额数据库创建在配置文件旁。页面可查看当前额度并生成约 6 小时有效的下载链接，支持 HEAD、单段 Range 和断点续传。链接包含 Base64 编码的源地址，并非加密，不应使用带私人凭据的链接。

`create_config.py` 不覆盖已有配置，不打印密钥。配置内的 `secret`、`proxy_secret`、`admin_password` 和旧版私用密码不得提交 Git。旧配置默认保留私用鉴权；要启用公开模式，明确设置 `public: true` 和可写的持久化 `quota_db`。可在后台恢复私用；原 Basic 账号 `bridge` 继续可用。若已有后台设置，数据库保存的模式优先于配置默认项。

添加下载站时修改 `allowed_domains` 并重启；主域名覆盖其子域名。每一跳都校验白名单、拒绝内网地址并固定 DNS 解析后的连接 IP，校验 HTTPS 主机名，只允许 HTTPS 443。浏览器 Cookie、客户端 Authorization 不转发到下载源，跨主机跳转剥离上游认证。

## 管理后台

访问 `/admin`；初始用户名由 `admin_username` 指定，默认为 `bridge`，已有账号以数据库保存值为准。首次管理密码由配置中的独立 `admin_password` 初始化，随后以随机盐 PBKDF2-SHA256 哈希保存到 SQLite；在后台修改密码后，重启也会保留新密码。后台密码与旧版私用下载 Basic 密码独立，修改管理密码不会改写只读部署配置。

后台提供：

- 今日下载量、当前传输、匿名访客数、近 7 天用量和今日用量前十。
- 服务版本、运行时长、进程当前内存、可信代理请求是否收到。
- 每 IP 并发、合计速度、日额度、全站日额度与请求频率的在线修改。
- 来源域名白名单、公开/私用模式切换、暂停新下载。
- 管理密码修改、退出登录及最近管理操作记录。

设置保存到配额数据库，立即应用，不重启服务、不清空现有用量，数据库设置在下次启动时覆盖对应配置文件默认项。暂停只阻止新请求，正在传输的文件继续结束；已预留的额度仍按原请求计数。切换到私用会要求旧签名链接也通过 Basic 登录；后台会话不受公开开关或公共访问限流影响。全局 4 个传输、160 MiB 和半核的服务资源限制仍固定保留。

管理会话使用 HttpOnly、SameSite=Strict Cookie，HTTPS 下附带 Secure，最多有效 8 小时。写操作需要 CSRF 校验并拒绝跨站来源；并发修改旧版本设置返回 409，避免覆盖其他页面的更新。改密码会使其他会话失效，服务重启后需重新登录。后台及其资源禁止 CDN 缓存；不要对 `/admin/*` 启用公开下载缓存规则。

旧版本升级前需在服务器受保护配置中生成独立的 `admin_password`（至少 24 字符），不要复用可能已共享给下载用户的私用密码。`create_config.py` 会为全新部署生成独立密码。

持久化的管理数据与流量记录共用 `quota_db`，因此启动服务必须指定可写的持久化数据库路径。首次升级会增加管理数据表，不重建或删除既有用量表。

## Docker

可信 HTTPS 域名接入后，无需 `docker login`：

```sh
docker pull download.xingbox.me/library/alpine:latest
docker pull download.xingbox.me/用户名/镜像名:标签
```

官方镜像保留 `library/` 前缀。仅支持 Docker Hub 公共镜像索引、清单和层下载，不支持推送、私有镜像、GHCR 或其他 Registry。仍受 Docker Hub 出口 IP 的匿名拉取限额影响；Docker 并行层下载可能触发每 IP 并发限制，客户端需降低拉取并发或重试。

这是显式带入口域名前缀的镜像代理。暂未对所有 Docker daemon 的 `registry-mirrors` 行为做兼容性承诺。

## CDN 必要设置

**公开限额模式下必须关闭下载缓存并清理旧缓存。** 缓存命中绕过服务器，源站无法对其限速或计数。所有响应均使用 `no-store`；不要在 CDN 强制覆盖。公开模式的 CDN 收益主要来自线路中转；如需缓存与精确限额并存，需要额外实现缓存前的边缘鉴权/计量。

EdgeOne 默认传递 `EO-Connecting-IP`。服务器只有在 `X-StarBridge-Origin-Key` 与配置中的 `proxy_secret` 匹配时才信任该 IP；在 EdgeOne 回源规则中**设置/覆盖**这个自定义请求头，避免追加重复头。不要将此密钥放在网页或响应头。未验证的代理请求只按 TCP 对端 IP 计数，因此未设置回源密钥时，同一 CDN 节点后的用户会共享额度。

具体步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 运行与边界

- 全局最多 4 个传输，HTTP/TLS 共享最多 24 个连接工作线程；64 KiB 流式发送，不落盘缓存下载文件。
- 默认单响应上限 8 GiB、2 小时，套接字空闲超时 30 秒；同时受剩余日额度约束。
- systemd 独立用户运行，内存上限 160 MiB、CPU 上限半核，`StateDirectory=starbridge` 为配额数据库提供可写目录。部署时将 `quota_db` 配置为 `/var/lib/starbridge/usage.sqlite3`。
- 页面、管理后台、错误响应、下载和 Docker 均不缓存；健康检查不消耗请求频率限额。
- 不支持 Git smart-HTTP 克隆、交互式网页、登录/验证码/浏览器 Cookie 下载；未逐一适配所有驱动站。

## 校验

```sh
python3 -m unittest -v
python3 smoke.py /absolute/path/config.json
```

测试覆盖后台登录、CSRF、权限隔离、设置及密码持久化、旧会话失效、模式切换、公开/私用兼容性、持久化额度、原子预留、重启/日期切换、断流退额、合计限速、IPv6 网段、并发限制、伪造转发头、签名、跳转与 SSRF。真实 smoke 验证 GitHub Release 字节一致性、Range/HEAD，Docker Hub 清单与层的 SHA256，以及 NVIDIA 版本元数据，不安装驱动或镜像。国内速度和真实 Docker daemon 拉取仍需单独验收。
