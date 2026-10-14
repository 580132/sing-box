# sing-box Docker 订阅管理镜像

基于官方 `ghcr.io/sagernet/sing-box`，增加**订阅管理 + 一键热重载**面板。

## 功能

- 订阅 CRUD（名称 / URL / 启用开关），持久化到 `subscriptions.json`
- 远程拉取订阅（支持 sing-box JSON 明文 / Base64 编码的 JSON）
- 合并策略：`config.template.json` 提供 `inbounds / dns / route` 等本地基础配置，
  远程订阅只取 `outbounds` 合并进来
- 一键热重载：拉取 → `sing-box check` 校验 → 原子写 `config.json` → `kill -HUP` 热重载
  - 校验失败则保留旧配置并返回错误，不中断现有连接
- Web 面板（`:8080`）+ REST API，可选 `MANAGER_TOKEN` 鉴权
- 定时自动更新（`AUTO_UPDATE_INTERVAL`，如 `6h` / `30m`，`0` 关闭）

## 快速开始

```bash
cp config/config.template.example.json config/config.template.json
cp config/subscriptions.example.json config/subscriptions.json
# 编辑订阅地址
docker compose up -d --build
# 打开面板
xdg-open http://localhost:8080
```

点击面板右上角 **立即更新并重载** 即完成：远程加载订阅并热重载。

## 目录结构

```
Dockerfile
docker-compose.yml
docker/entrypoint.sh        # 容器启动：初始拉取 -> 后台起 sing-box -> 前台起 manager
docker/update-subs.sh       # 手动/定时触发更新（容器内执行）
manager/manager.py          # 零依赖（纯标准库）管理面板 + API
config/config.template.example.json
config/subscriptions.example.json
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MANAGER_PORT` | `8080` | 面板端口 |
| `MANAGER_TOKEN` | 空（不鉴权） | 设置后 API 需 `Authorization: Bearer <token>` 或 `?token=` |
| `CONFIG_PATH` | `/etc/sing-box/config.json` | sing-box 实际运行配置 |
| `TEMPLATE_PATH` | `/etc/sing-box/config.template.json` | 本地基础模板 |
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

## 合并策略（重要）

1. 读取 `config.template.json` 作为底座。
2. 依次拉取所有 `enabled=true` 的订阅，解析出 `outbounds[]`。
   - 内容是 `{...}` JSON → 直接用
   - 内容是 Base64 → 解码后再按 JSON 解析
   - 非 JSON（如 Clash YAML）→ 该条标记失败，提示“请用 subconverter 转成 sing-box JSON 后再订阅”
3. 去重（按 `tag`），合并到模板：
   - 模板中若有 `tag == "__SUBSCRIPTION__"` 的占位 outbound，则替换为远程 outbounds；
   - 否则直接追加到 `outbounds` 末尾。
   - 模板中的 selector（如 `proxy` / `auto`）若 `outbounds` 为空或包含占位符，会自动填入全部远程 tag，保证开箱可用。
4. 写临时文件 → `sing-box check -c tmp` → 通过则原子 `rename` 到 `config.json` → `kill -HUP $(cat /run/sing-box.pid)`。

热重载原理即 sing-box 官方 `SIGHUP` 机制：校验通过后重建实例，老连接不断，新连接走新配置。
详见 `docker/entrypoint.sh` 与 `manager/manager.py:do_reload`。

## 本地开发（不进 Docker 也可跑面板）

```bash
python3 manager/manager.py --help
CONFIG_PATH=./.tmp/config.json TEMPLATE_PATH=./config/config.template.example.json \
  SUBS_PATH=./.tmp/subs.json MANAGER_PORT=8080 python3 manager/manager.py
```

## 注意事项

- 订阅 URL 必须直接返回 **sing-box JSON**（或其 Base64）。机场普遍给的是 Clash / V2Ray 明文，
  请自建/使用 subconverter 之类先转换：`https://sub.example.com/sub?target=singbox&url=<原订阅>&config=<规则>`。
- 改监听端口 / 证书路径 / TUN 等需重启容器，仅改节点/密码/UUID 可热重载。
- `sing-box check` 失败时接口返回 500 且不碰运行中的配置，请看 `/api/logs` 或 `docker logs`。
