# 星桥 StarBridge

自用下载入口：生成临时下载链接，转发 GitHub Release、软件/驱动 HTTPS 直链，以及 Docker Hub 公共镜像。Python 3.11+ 标准库实现，无运行时依赖。

## 运行

```sh
python3 create_config.py /absolute/path/config.json
python3 server.py --config /absolute/path/config.json
```

默认监听 `127.0.0.1:18080`。浏览器 Basic 用户名为 `bridge`，随机密码在配置文件 `password` 字段。配置文件包含密码与签名密钥，禁止提交 Git 或放入静态目录。

粘贴文件直链后生成约 6 小时有效链接，链接可直接下载、支持单段 Range/HEAD。同一文件在 5 分钟内复用相同链接，以便 CDN 命中缓存。持有签名链接的人可下载；不要分享带有私人临时凭据的源地址。源 URL 在链接内以 Base64 编码，并非加密。

添加新来源时修改 `allowed_domains` 并重启服务；主域名覆盖其子域名。每次跳转都检查域名、解析公网地址并固定连接 IP，HTTPS 证书按原始主机名验证。只允许 HTTPS 443，不转发浏览器 Cookie 或客户端 Authorization。跨站跳转剥离上游 Authorization。

## Docker

接入可信 HTTPS 域名后：

```sh
docker login download.xingbox.me -u bridge
docker pull download.xingbox.me/library/alpine:latest
docker pull download.xingbox.me/用户名/镜像名:标签
```

密码在提示符中输入，不写进命令参数。支持 Docker Hub 公共镜像的索引、清单和层下载，只读，不支持私有镜像、推送、GHCR 或其他 Registry。服务端取得匿名上游令牌，不向客户端暴露上游认证跳转。仍受 Docker Hub 出口 IP 的匿名限额影响。

这是显式带域名前缀的镜像代理，**不能直接作为无认证的 `registry-mirrors` 替换项**。原生 Docker daemon 对镜像站认证行为有区别，首版采用 `docker login` + 显式入口地址。

## 部署边界

- 最多 4 个传输，HTTP/TLS 共享最多 24 个连接工作线程，64 KiB 分块流式发送。
- 不落盘缓存下载文件，也不在内存中装载整个文件。默认单次响应上限 8 GiB、2 小时，空闲套接字超时 30 秒。无 Content-Length 时超限会中断连接；使用文件自身哈希验证完整性。
- 下载链接响应允许 CDN 缓存最多 1 小时，且不超过链接剩余有效期。CDN 缓存可能暂时保留已撤销的链接；换密钥后如需立即撤销，要同时清理 CDN 缓存。
- 页面/API/健康检查/Docker 默认 `no-store`，Docker 走 CDN 时首版仅利用中转线路，不宣称镜像缓存收益。不要对所有路径强制缓存，否则会破坏鉴权。
- 没有用户流量计费、总流量配额和带宽整形；并发限制不等于流量限制。适合自用，CDN/服务器设置用量提醒后再扩大使用。
- 不支持 Git smart-HTTP 克隆、交互式网页、登录/验证码/浏览器 Cookie 下载，未逐一适配所有驱动站。

`starbridge.service` 独立用户运行，内存上限 160 MiB、CPU 上限半核，日志不记录请求 URL、密码或上游错误正文。可选 TLS 源站与本机 HTTP 共享服务，不占用现有 80/443。

## 校验

```sh
python3 -m unittest -v
python3 smoke.py /absolute/path/config.json
```

单元/HTTP 测试验证签名、鉴权、内网目标拦截、跳转安全、断点续传、并发和流中断。真实 smoke 下载 GitHub Release 校验文件并逐字节比较，验证 Range/HEAD；获取 hello-world 索引、linux/amd64 清单及层，校验 SHA256；下载 NVIDIA 公开版本元数据。不会自动安装驱动或镜像。

## HTTPS/CDN 与维护

具体接入步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)。本机通过 SSH 隧道先验收，CDN 接入后必须另外验证 HTTPS、缓存命中、大文件 Range 和实际 Docker 客户端拉取。源站测试通过不能代替国内提速实测。
