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

## 内核版本

基础镜像即上游官方镜像（`ghcr.io/sagernet/sing-box`），tag 与 GitHub Release 同名。
compose 默认已指定 `SINGBOX_VERSION: "v1.15.0-alpha.12"`，换版本改这里即可：

```yaml
build:
  args:
    SINGBOX_VERSION: "v1.15.0-alpha.12"  # 或 latest / v1.14.3 ...
```

```bash
docker compose build --no-cache   # 换版本后重新构建
docker compose up -d
docker exec sing-box-sub sing-box version  # 确认内核版本
```

## 目录结构

```
Dockerfile
docker-compose.yml
docker/entrypoint.sh        # 容器启动：初始拉取 -> 后台起 sing-box -> 前台起 manager
docker/update-subs.sh       # 手动/定时触发更新（容器内执行）
manager/manager.py          # 零依赖（纯标准库）管理面板 + API
config/subscriptions.example.json
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MANAGER_PORT` | `8080` | 面板端口 |
| `MANAGER_TOKEN` | 空（不鉴权） | 设置后 API 需 `Authorization: Bearer <token>` 或 `?token=` |
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

## 注意事项

- 订阅 URL 必须直接返回 **sing-box JSON**（或其 Base64）。机场普遍给的是 Clash / V2Ray 明文，
  请自建/使用 subconverter 之类先转换：`https://sub.example.com/sub?target=singbox&url=<原订阅>&config=<规则>`。
- 改监听端口 / 证书路径 / TUN 等需重启容器，仅改节点/密码/UUID 可热重载。
- `sing-box check` 失败时接口返回 500 且不碰运行中的配置，请看 `/api/logs` 或 `docker logs`。
