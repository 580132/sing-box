# sing-box Docker 订阅管理镜像

基于官方 `ghcr.io/sagernet/sing-box`，增加**订阅管理 + 一键热重载**面板。

## 功能

- 订阅 CRUD（名称 / URL / 启用开关），持久化到 `subscriptions.json`
- 远程拉取订阅（支持 sing-box 完整配置 JSON 明文 / Base64 编码）
- **完整配置直接采用**：订阅含 `inbounds`/`route` 时原样写入，不合并模板
- 多订阅混合：以第一份完整配置为底座，其余订阅的节点合并进来
- 一键热重载：拉取 → `sing-box check` 校验 → 原子写 `config.json` → `kill -HUP` 热重载
  - 校验失败则保留旧配置并返回错误，不中断现有连接
- Web 面板（`:8080`）+ REST API，可选 `MANAGER_TOKEN` 鉴权
- 定时自动更新（`AUTO_UPDATE_INTERVAL`，如 `6h` / `30m`，`0` 关闭）

## 快速开始

```bash
cp config/subscriptions.example.json config/subscriptions.json
# 编辑订阅地址（vim config/subscriptions.json）
docker compose up -d --build
# 打开面板
xdg-open http://localhost:8080
```

> compose 用的是 `./config:/etc/sing-box` 整个目录挂载，不要改成单文件挂载
>（如 `.../config.json:/etc/sing-box/config.json`）：宿主文件不存在时 docker 会建出
> 同名目录，启动即报 `not a directory`。目录挂载无此问题，`config.json` 由容器
> 启动时拉取订阅自动生成。

点击面板右上角 **立即更新并重载** 即完成：远程加载订阅并热重载。

## 内核版本（无需重建镜像）

镜像基于 alpine，构建时内置默认**稳定版**内核；运行时切换版本自动从上游 Release
下载（musl 包），缓存在 `sing-box-kernel` volume 里。**版本控制权二选一、互斥：**

- compose 里写了 `SINGBOX_VERSION` → 以 compose 为准，面板只显示不给换；
  且本地已有更新版本（面板钉住/缓存）时默认不降级下载，
  `ALLOW_KERNEL_DOWNGRADE=1` 可强制降级；
- 注释掉它 → 归面板管：面板「🔧 内核版本」卡片点「检查更新」看最新稳定/预览版，
  一键切换（下载→新二进制 check→重启 sing-box→失败自动回滚旧版），成功后自动钉住，
  容器重建仍用该版本；「恢复内置版」解除钉住。

```yaml
environment:
  # SINGBOX_VERSION: "v1.15.0-alpha.12"  # 放开即 compose 接管
```

上游查询走 GitHub API（半小时缓存、失败沿用旧值），限流时可设 `GITHUB_TOKEN` 提额度。

构建时换内置版本（一般不需要）：`docker compose build --build-arg BAKED_SINGBOX_VERSION=v1.14.3`。

国内服务器直连 GitHub Release 慢/超时，加 `GITHUB_PROXY`（代理前缀拼到下载地址前）：

```yaml
environment:
  SINGBOX_VERSION: "v1.15.0-alpha.12"
  GITHUB_PROXY: "https://ghproxy.com/"   # 换成你可用的代理
```

> 另外 curl（内核下载）与面板（订阅拉取）都自动识别小写的 `http_proxy`/`https_proxy`/
> `no_proxy` 环境变量，有自建代理也可直接配那几个。

## 目录结构

```
Dockerfile
docker-compose.yml
docker/entrypoint.sh        # 容器启动：解析内核版本 -> 初始拉取 -> 后台起 sing-box -> 前台起 manager
docker/install-kernel.sh      # 内核下载器（构建与运行共用）：按版本从上游 Release 拉 musl 包
docker/update-subs.sh       # 手动/定时触发更新（容器内执行）
manager/manager.py          # 零依赖（纯标准库）管理面板 + API
config/subscriptions.example.json
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MANAGER_PORT` | `8080` | 面板端口 |
| `MANAGER_TOKEN` | 空（不鉴权） | 设置后 API 需 `Authorization: Bearer <token>` 或 `?token=` |
| `SINGBOX_VERSION` | 空（=面板接管） | 写死则 compose 接管版本，面板只读；注释掉则归面板切换+钉住 |
| `ALLOW_KERNEL_DOWNGRADE` | 空（不降级） | 置 `1` 才允许按 compose 低版本降级；默认本地有更新版时跳过下载 |
| `GITHUB_TOKEN` | 空 | GitHub API token，面板查最新版限流时提额度 |
| `GITHUB_PROXY` | 空（直连） | GitHub 代理前缀，如 `https://ghproxy.com/`，拼到 Release 下载地址前（含 `latest` 解析） |
| `TUN_EXCLUDE` | 空 | 逗号分隔的 CIDR，自动并入 tun 入站 `route_exclude_address`（防 `auto_route` 吞内网 DNS） |
| `KERNEL_DIR` | `/opt/sing-box/versions` | 运行时下载的内核存放目录（建议挂 volume 持久化） |
| `CONFIG_PATH` | `/etc/sing-box/config.json` | sing-box 实际运行配置 |
| `TEMPLATE_PATH` | `/etc/sing-box/config.template.json` | 本地基础模板，**仅节点片段模式需要**，完整配置订阅可不提供 |
| `SUBS_PATH` | `/etc/sing-box/subscriptions.json` | 订阅列表持久化 |
| `CACHE_DIR` | `/var/cache/sing-box-subs` | 订阅原始内容缓存 |
| `SINGBOX_BIN` | `sing-box` | sing-box 二进制路径 |
| `PID_FILE` | `/run/sing-box.pid` | sing-box pid 文件（entrypoint 写入） |
| `AUTO_UPDATE_INTERVAL` | `0` | 自动更新间隔，如 `6h`/`30m`，`0` 关闭 |
| `UPDATE_ON_START` | `true` | 启动时先拉取一次再起 sing-box |
| `ALLOW_EMPTY_OUTBOUNDS` | `false` | 全部订阅失败时是否仍用空 outbounds 重载（默认拒绝，保证安全） |

## API

```
GET  /                          Web 面板
GET  /api/status                运行状态、配置 mtime、上次更新结果
GET  /api/subscriptions         订阅列表
POST /api/subscriptions         {name, url, enabled} 新增
PUT  /api/subscriptions/{id}    {name, url, enabled} 更新
DELETE /api/subscriptions/{id}  删除
POST /api/subscriptions/{id}/test  只拉取该条做连通性测试，不写配置
POST /api/reload                {fetch=true} 拉取全量订阅并热重载（默认）
POST /api/reload-only           不拉取，仅 SIGHUP 重载当前 config.json
GET  /api/kernel[?refresh=1]       内核信息：当前/内置/钉住/控制权/已缓存/最新稳定/最新预览
POST /api/kernel/update           {version} 下载并切换内核（失败回滚），成功自动钉住
POST /api/kernel/restore          切回内置版并解除钉住
GET  /api/logs?n=200            manager 日志尾部（排障用）
```

鉴权（仅当设置 `MANAGER_TOKEN` 时）：

```bash
curl -H "Authorization: Bearer $MANAGER_TOKEN" http://localhost:8080/api/status
```

## 配置应用策略（重要）

1. 依次拉取所有 `enabled=true` 的订阅（JSON 明文 / Base64，失败的单条标记 `last_error`，不影响其他条）。
2. **完整配置**（含 `outbounds` 且含 `inbounds` 或 `route`）：
   - 仅一份启用的完整配置 → **原样直接采用**，不做任何合并；
   - 多份 → 以第一份完整配置为底座，其余订阅的 `outbounds` 按 `tag` 去重合并进来。
3. **节点片段**（`outbounds` 数组 / 单个 outbound / 仅 `{"outbounds": [...]}`）：
   需要 `TEMPLATE_PATH` 模板做底座（模板提供 `inbounds/dns/route`），片段按 `tag` 去重后合并；
   模板中 `tag == "__SUBSCRIPTION__"` 的占位 outbound 会被替换，`selector`/`urltest` 的占位引用会自动填充。
   不用此模式可不提供模板。
4. 写临时文件 → `sing-box check -c tmp` → 通过则原子 `rename` 到 `config.json` → `kill -HUP $(cat /run/sing-box.pid)`。

热重载原理即 sing-box 官方 `SIGHUP` 机制：校验通过后重建实例，老连接不断，新连接走新配置。
详见 `docker/entrypoint.sh` 与 `manager/manager.py:do_reload`。

## 本地开发（不进 Docker 也可跑面板）

```bash
python3 manager/manager.py --help
CONFIG_PATH=./.tmp/config.json \
  SUBS_PATH=./.tmp/subs.json MANAGER_PORT=8080 python3 manager/manager.py
```

## TUN 模式

订阅是 TUN 入站时，compose 已配好必需项（`cap_add: NET_ADMIN` + `/dev/net/tun`
设备映射），`network_mode: host` 下照样需要这两项。宿主机检查：

```bash
ls -l /dev/net/tun || sudo modprobe tun   # 没有就加载内核模块
# 做网关给别的设备用时，再开宿主转发：
# sudo sysctl -w net.ipv4.ip_forward=1
```

> 不用 TUN 的话把 compose 里 `cap_add`/`devices` 两节删掉即可。

订阅是远端完整配置、不方便手改时，用 `TUN_EXCLUDE`（逗号分隔 CIDR）让面板在每次
更新订阅时自动并入 tun 入站的 `route_exclude_address`，防止 `auto_route` 吞掉内网
DNS/网关（典型症状：启动时 `lookup ... deadline exceeded` 接着 FATAL）：

```yaml
TUN_EXCLUDE: "192.168.1.0/24"
```

## 注意事项

- 订阅 URL 必须直接返回 **sing-box JSON**（或其 Base64）。机场普遍给的是 Clash / V2Ray 明文，
  请自建/使用 subconverter 之类先转换：`https://sub.example.com/sub?target=singbox&url=<原订阅>&config=<规则>`。
- 改监听端口 / 证书路径 / TUN 等需重启容器，仅改节点/密码/UUID 可热重载。
- `sing-box check` 失败时接口返回 500 且不碰运行中的配置，请看 `/api/logs` 或 `docker logs`。
