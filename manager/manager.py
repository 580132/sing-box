#!/usr/bin/env python3
"""sing-box 订阅管理面板（纯标准库，零第三方依赖）.

环境变量见 README。提供 Web UI + REST API：
  GET  /                          面板
  GET  /api/status
  GET  /api/subscriptions
  POST /api/subscriptions
  PUT  /api/subscriptions/{id}
  DELETE /api/subscriptions/{id}
  POST /api/subscriptions/{id}/test
  POST /api/reload                 拉取并热重载
  POST /api/reload-only            仅 SIGHUP
  GET  /api/logs
"""
import argparse
import base64
import binascii
import copy
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------- 配置 ----------------

def env(key, default):
    return os.environ.get(key, default)

CONFIG_PATH = env("CONFIG_PATH", "/etc/sing-box/config.json")
TEMPLATE_PATH = env("TEMPLATE_PATH", "/etc/sing-box/config.template.json")
SUBS_PATH = env("SUBS_PATH", "/etc/sing-box/subscriptions.json")
CACHE_DIR = env("CACHE_DIR", "/var/cache/sing-box-subs")
SINGBOX_BIN = env("SINGBOX_BIN", "sing-box")
PID_FILE = env("PID_FILE", "/run/sing-box.pid")
MANAGER_PORT = int(env("MANAGER_PORT", "8080"))
MANAGER_TOKEN = env("MANAGER_TOKEN", "")
AUTO_UPDATE_INTERVAL = env("AUTO_UPDATE_INTERVAL", "0")
ALLOW_EMPTY_OUTBOUNDS = env("ALLOW_EMPTY_OUTBOUNDS", "false").lower() in ("1", "true", "yes")
PLACEHOLDER_TAG = "__SUBSCRIPTION__"


def last_update_path():
    return os.path.join(env("CACHE_DIR", CACHE_DIR), "last_update.json")

_lock = threading.Lock()
_logs = []
_logs_lock = threading.Lock()
MAX_LOGS = 500


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with _logs_lock:
        _logs.append(line)
        if len(_logs) > MAX_LOGS:
            del _logs[: len(_logs) - MAX_LOGS]


def utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def parse_interval(s):
    """'6h'/'30m'/'90s'/'1d'/纯数字秒 -> 秒；'0'/空 -> 0（关闭）"""
    s = (s or "").strip().lower()
    if s in ("", "0", "off", "false", "never"):
        return 0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", s)
    if not m:
        raise ValueError(f"invalid interval: {s!r}")
    val, unit = float(m.group(1)), m.group(2)
    return int(val * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit])


# ---------------- 订阅持久化 ----------------

def load_subs():
    if not os.path.exists(SUBS_PATH):
        return []
    with open(SUBS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    return []


def save_subs(subs):
    os.makedirs(os.path.dirname(os.path.abspath(SUBS_PATH)), exist_ok=True)
    tmp = SUBS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(subs, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SUBS_PATH)


def new_id():
    return f"s{int(time.time() * 1000) % 100000000:08d}"


def save_last_update(payload):
    try:
        cache = env("CACHE_DIR", CACHE_DIR)
        os.makedirs(cache, exist_ok=True)
        with open(os.path.join(cache, "last_update.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        log(f"WARN: save last_update failed: {e}")


def load_last_update():
    try:
        with open(os.path.join(env("CACHE_DIR", CACHE_DIR), "last_update.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


# ---------------- 订阅拉取与解析 ----------------

def fetch_url(url, timeout=30):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "sing-box-sub-manager/1.0 (curl-like)",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.geturl()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"URL error: {e.reason}") from e


def try_parse_subscription_config(text):
    """把订阅文本解析为 (outbounds_list, info_dict, raw_obj)。

    支持 JSON 明文 / Base64(JSON)。raw_obj 为解析出的原始 JSON 对象，
    调用方可据此判断是否为完整配置（见 is_full_config）。
    """
    s = text.strip()
    if not s:
        raise ValueError("订阅内容为空")
    candidates = [s]
    # 尝试整体 base64 解码（去掉空白后）
    compact = re.sub(r"\s+", "", s)
    if not s.lstrip().startswith("{") and not s.lstrip().startswith("["):
        try:
            padded = compact + "=" * (-len(compact) % 4)
            decoded = base64.b64decode(padded, validate=False).decode("utf-8")
            candidates.append(decoded.strip())
        except (binascii.Error, UnicodeDecodeError, ValueError):
            pass
        try:
            padded = compact + "=" * (-len(compact) % 4)
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
            if decoded.strip() not in candidates:
                candidates.append(decoded.strip())
        except (binascii.Error, UnicodeDecodeError, ValueError):
            pass
    last_err = None
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError as e:
            last_err = e
            continue
        outbounds, info = extract_outbounds(obj)
        return outbounds, info, obj
    #  heuristic: 看起来像 clash yaml？
    head = s[:200].lower()
    if "proxies:" in head or "proxy-groups:" in head or "port:" in head:
        raise ValueError("内容疑似 Clash YAML，不是 sing-box JSON。请先用 subconverter 转成 target=singbox 的订阅")
    raise ValueError(f"无法解析为 sing-box JSON（{last_err}）。订阅必须直接返回 sing-box JSON 或其 Base64")


def extract_outbounds(obj):
    if isinstance(obj, list):
        return obj, {"mode": "list", "count": len(obj)}
    if isinstance(obj, dict):
        if isinstance(obj.get("outbounds"), list):
            return obj["outbounds"], {"mode": "full-config", "count": len(obj["outbounds"])}
        if "type" in obj and "tag" in obj:
            return [obj], {"mode": "single-outbound", "count": 1}
    raise ValueError("JSON 中未找到 outbounds 数组，也不是单个 outbound 对象")


def is_full_config(obj):
    """判断订阅是否为完整 sing-box 配置（而不仅是 outbounds 片段）。

    完整配置：含 outbounds 数组，且同时含 inbounds 或 route。
    仅含 {"outbounds": [...]} 的属于节点片段，走模板合并流程。
    """
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("outbounds"), list)
        and ("inbounds" in obj or "route" in obj)
    )


def fetch_subscription(sub, timeout=30):
    url = sub["url"]
    raw, final_url = fetch_url(url, timeout=timeout)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, f"{sub['id']}.raw"), "wb") as f:
        f.write(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"订阅非 UTF-8 文本：{e}") from e
    outbounds, info, obj = try_parse_subscription_config(text)
    if not isinstance(outbounds, list):
        raise ValueError("解析出的 outbounds 不是数组")
    return outbounds, {"final_url": final_url, "bytes": len(raw), **info}, obj


# ---------------- 合并与重载 ----------------

def load_template():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def merge_config(template, all_outbounds):
    """模板为底座，远程 outbounds 按 tag 去重后合并。"""
    cfg = copy.deepcopy(template)
    # 去重（后出现的覆盖先出现的）
    dedup = {}
    order = []
    for ob in all_outbounds:
        if not isinstance(ob, dict) or "tag" not in ob:
            continue
        tag = str(ob["tag"])
        if tag not in dedup:
            order.append(tag)
        dedup[tag] = ob
    remote = [dedup[t] for t in order]
    remote_tags = [str(ob["tag"]) for ob in remote]

    cfg.setdefault("outbounds", [])
    existing = cfg["outbounds"]
    # 有占位则替换，否则追加
    replaced = False
    merged = []
    for ob in existing:
        if isinstance(ob, dict) and ob.get("tag") == PLACEHOLDER_TAG:
            merged.extend(remote)
            replaced = True
        else:
            merged.append(ob)
    if not replaced:
        # 避免重复 tag：模板里已有的 tag 不重复追加
        existing_tags = {str(ob.get("tag")) for ob in merged if isinstance(ob, dict)}
        merged.extend([ob for ob in remote if str(ob["tag"]) not in existing_tags])
    cfg["outbounds"] = merged

    # 自动填充 selector/urltest 中对占位符的引用
    for ob in cfg["outbounds"]:
        if not isinstance(ob, dict):
            continue
        if ob.get("type") in ("selector", "urltest"):
            lst = ob.get("outbounds") or []
            if PLACEHOLDER_TAG in lst:
                ob["outbounds"] = [t for t in lst if t != PLACEHOLDER_TAG] + remote_tags
            elif not lst and remote_tags:
                # 空 selector 自动填入，保證開箱可用（但保留 direct 等本地出站）
                pass
    # 模板示例中的 auto 组初始只有占位符，上一步已处理；若 selector 仍为空则填入 remote
    for ob in cfg["outbounds"]:
        if isinstance(ob, dict) and ob.get("type") in ("selector", "urltest") and not ob.get("outbounds"):
            if ob.get("tag") in ("proxy", "auto"):
                ob["outbounds"] = remote_tags + [t for t in ("direct",) if t]
    return cfg, remote_tags


def singbox_check(path):
    r = subprocess.run([SINGBOX_BIN, "check", "-c", path],
                       capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def find_singbox_pid():
    # 1) pid 文件
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:  # noqa: BLE001
        pass
    # 2) pgrep 兜底
    try:
        r = subprocess.run(["pgrep", "-f", "sing-box run"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            try:
                return int(line.strip())
            except ValueError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return None


def send_hup():
    pid = find_singbox_pid()
    if not pid:
        return False, "未找到 sing-box 进程（可能未启动），配置已写入，待下次启动生效"
    try:
        os.kill(pid, signal.SIGHUP)
        return True, f"SIGHUP 已发送至 pid={pid}"
    except ProcessLookupError:
        return False, f"pid={pid} 不存在，配置已写入"
    except PermissionError as e:
        return False, f"无权限发送 SIGHUP：{e}"


def do_update_and_reload(fetch=True, send_hup_signal=True):
    """核心流程：拉取 -> 合并 -> check -> 原子写 -> SIGHUP。返回结果 dict。"""
    with _lock:
        started = utcnow_iso()
        if not fetch:
            code, output = singbox_check(CONFIG_PATH)
            if code != 0:
                return {"ok": False, "reloaded": False, "fetch": False,
                        "message": "当前 config.json 校验失败，未重载", "check_output": output[-4000:]}
            ok, msg = send_hup() if send_hup_signal else (False, "跳过 SIGHUP（--update-once 模式）")
            return {"ok": True, "reloaded": ok, "fetch": False, "message": msg,
                    "check_output": output[-4000:]}

        subs = load_subs()
        enabled = [s for s in subs if s.get("enabled", True)]
        if not enabled:
            return {"ok": False, "reloaded": False, "message": "没有启用的订阅，请先添加并启用"}
        try:
            template = load_template()
        except Exception as e:  # noqa: BLE001
            template = None
            log(f"WARN: 读取模板失败 {TEMPLATE_PATH}: {e}（若订阅为完整配置则可忽略）")

        all_obs, details, full_configs = [], [], []
        for s in enabled:
            try:
                obs, info, obj = fetch_subscription(s)
                all_obs.extend(obs)
                if is_full_config(obj):
                    full_configs.append(obj)
                s["last_status"] = "ok"
                s["last_error"] = ""
                s["updated_at"] = utcnow_iso()
                details.append({"id": s["id"], "name": s.get("name"), "ok": True,
                                "count": len(obs), "bytes": info.get("bytes"), "mode": info.get("mode"),
                                "full_config": is_full_config(obj)})
                log(f"订阅 OK: {s.get('name')} ({s['id']}) nodes={len(obs)} bytes={info.get('bytes')} full={is_full_config(obj)}")
            except Exception as e:  # noqa: BLE001
                err = str(e)
                s["last_status"] = "error"
                s["last_error"] = err[:500]
                s["updated_at"] = utcnow_iso()
                details.append({"id": s["id"], "name": s.get("name"), "ok": False, "error": err[:500]})
                log(f"订阅 FAIL: {s.get('name')} ({s['id']}): {err}")
        save_subs(subs)

        if not all_obs and not ALLOW_EMPTY_OUTBOUNDS:
            save_last_update({"at": started, "ok": False, "details": details,
                              "message": "全部订阅拉取失败，已拒绝重载以保护运行中的配置"})
            return {"ok": False, "reloaded": False, "details": details,
                    "message": "全部订阅拉取失败，已保留旧配置（置 ALLOW_EMPTY_OUTBOUNDS=true 可强制）"}

        if full_configs and all_obs:
            if len(full_configs) == 1 and len([d for d in details if d.get("ok")]) == 1:
                # 唯一启用的订阅即完整配置：直接采用，不合并模板
                cfg = copy.deepcopy(full_configs[0])
                tags = [str(o["tag"]) for o in cfg.get("outbounds", []) if isinstance(o, dict) and "tag" in o]
                apply_mode = "direct"
                log(f"订阅为完整配置，直接采用（含 inbounds/route，共 {len(tags)} 个 outbounds）")
            else:
                # 多订阅且含完整配置：以第一份完整配置为底座，其余订阅的节点合并进来
                base = copy.deepcopy(full_configs[0])
                rest = [o for o in all_obs
                        if str(o.get("tag")) not in {str(x.get("tag")) for x in base.get("outbounds", []) if isinstance(x, dict)}]
                cfg, tags = merge_config(base, rest)
                apply_mode = "full-base-merge"
                log(f"多订阅混合：以第一份完整配置为底座，合并其余 {len(rest)} 个节点")
        else:
            if template is None:
                return {"ok": False, "reloaded": False, "details": details,
                        "message": f"订阅仅为节点片段，但读取模板失败 {TEMPLATE_PATH}，无法合并"}
            cfg, tags = merge_config(template, all_obs)
            apply_mode = "template-merge"
        os.makedirs(os.path.dirname(os.path.abspath(CONFIG_PATH)) or ".", exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        code, output = singbox_check(tmp)
        if code != 0:
            try:
                os.remove(tmp)
            except OSError:  # noqa: BLE001
                pass
            save_last_update({"at": started, "ok": False, "details": details,
                              "message": "sing-box check 失败，未替换配置", "check_output": output[-4000:]})
            return {"ok": False, "reloaded": False, "details": details,
                    "message": "新配置校验失败，已保留旧配置", "check_output": output[-4000:]}
        os.replace(tmp, CONFIG_PATH)
        log(f"配置已更新并通过校验：outbounds={len(tags)}")

        if send_hup_signal:
            ok, msg = send_hup()
        else:
            ok, msg = False, "配置已写入（--update-once，不发送 SIGHUP）"
        result = {"ok": True, "reloaded": ok, "fetch": True, "message": msg,
                  "mode": apply_mode, "tags": tags, "count": len(tags), "details": details,
                  "check_output": output[-4000:]}
        save_last_update({"at": started, **{k: v for k, v in result.items() if k != "check_output"}})
        return result


def get_status():
    subs = load_subs()
    pid = find_singbox_pid()
    try:
        st = os.stat(CONFIG_PATH)
        cfg_info = {"exists": True, "mtime": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(),
                    "size": st.st_size}
    except OSError:
        cfg_info = {"exists": False}
    return {
        "singbox": {"running": pid is not None, "pid": pid, "bin": SINGBOX_BIN},
        "config": cfg_info,
        "subscriptions": {"total": len(subs), "enabled": sum(1 for s in subs if s.get("enabled", True))},
        "last_update": load_last_update(),
        "allow_empty": ALLOW_EMPTY_OUTBOUNDS,
        "time": utcnow_iso(),
    }


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "sing-box-sub-manager/1.0"

    def log_message(self, fmt, *args):  # 安静，统一走 log()
        pass

    # -- helpers --
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def authorized(self):
        if not MANAGER_TOKEN:
            return True
        auth = self.headers.get("Authorization") or ""
        if auth == f"Bearer {MANAGER_TOKEN}":
            return True
        qs = parse_qs(urlparse(self.path).query)
        if qs.get("token", [""])[0] == MANAGER_TOKEN:
            return True
        # UI 把 token 放 localStorage 并通过 header 传；兼容 query
        return False

    # -- routing --
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self.send_html(UI_HTML)
        if path == "/api/status":
            if not self.authorized():
                return self.send_json({"error": "unauthorized"}, 401)
            return self.send_json(get_status())
        if path == "/api/subscriptions":
            if not self.authorized():
                return self.send_json({"error": "unauthorized"}, 401)
            return self.send_json(load_subs())
        if path == "/api/logs":
            if not self.authorized():
                return self.send_json({"error": "unauthorized"}, 401)
            qs = parse_qs(urlparse(self.path).query)
            try:
                n = max(1, min(500, int(qs.get("n", ["200"])[0])))
            except ValueError:
                n = 200
            with _logs_lock:
                tail = _logs[-n:]
            return self.send_json({"logs": tail})
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/subscriptions":
            if not self.authorized():
                return self.send_json({"error": "unauthorized"}, 401)
            try:
                body = self.read_json()
            except json.JSONDecodeError:
                return self.send_json({"error": "invalid json"}, 400)
            name = (body.get("name") or "").strip() or "未命名"
            url = (body.get("url") or "").strip()
            if not url or not url.lower().startswith(("http://", "https://")):
                return self.send_json({"error": "url 必须以 http(s):// 开头"}, 400)
            with _lock:
                subs = load_subs()
                sub = {"id": new_id(), "name": name, "url": url,
                       "enabled": bool(body.get("enabled", True)),
                       "updated_at": "", "last_status": "", "last_error": ""}
                subs.append(sub)
                save_subs(subs)
            log(f"新增订阅: {name} {url}")
            return self.send_json(sub)
        if path.startswith("/api/subscriptions/") and path.endswith("/test"):
            if not self.authorized():
                return self.send_json({"error": "unauthorized"}, 401)
            sid = path[len("/api/subscriptions/"): -len("/test")].strip("/")
            subs = load_subs()
            sub = next((s for s in subs if s["id"] == sid), None)
            if not sub:
                return self.send_json({"error": "not found"}, 404)
            try:
                obs, info, obj = fetch_subscription(sub)
                preview = [str(o.get("tag")) for o in obs[:20] if isinstance(o, dict)]
                return self.send_json({"ok": True, "count": len(obs), "bytes": info.get("bytes"),
                                        "mode": info.get("mode"), "full_config": is_full_config(obj),
                                        "preview": preview})
            except Exception as e:  # noqa: BLE001
                return self.send_json({"ok": False, "error": str(e)[:1000]}, 502)
        if path == "/api/reload":
            if not self.authorized():
                return self.send_json({"error": "unauthorized"}, 401)
            try:
                body = self.read_json()
            except json.JSONDecodeError:
                body = {}
            fetch = bool(body.get("fetch", True))
            try:
                result = do_update_and_reload(fetch=fetch, send_hup_signal=True)
            except Exception as e:  # noqa: BLE001
                log(f"RELOAD EXCEPTION: {e}\n{traceback.format_exc()}")
                return self.send_json({"ok": False, "message": f"内部错误: {e}"}, 500)
            return self.send_json(result, 200 if result.get("ok") else 500)
        if path == "/api/reload-only":
            if not self.authorized():
                return self.send_json({"error": "unauthorized"}, 401)
            try:
                result = do_update_and_reload(fetch=False, send_hup_signal=True)
            except Exception as e:  # noqa: BLE001
                return self.send_json({"ok": False, "message": f"内部错误: {e}"}, 500)
            return self.send_json(result, 200 if result.get("ok") else 500)
        return self.send_json({"error": "not found"}, 404)

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith("/api/subscriptions/"):
            if not self.authorized():
                return self.send_json({"error": "unauthorized"}, 401)
            sid = path[len("/api/subscriptions/"):].strip("/")
            try:
                body = self.read_json()
            except json.JSONDecodeError:
                return self.send_json({"error": "invalid json"}, 400)
            with _lock:
                subs = load_subs()
                sub = next((s for s in subs if s["id"] == sid), None)
                if not sub:
                    return self.send_json({"error": "not found"}, 404)
                if "name" in body:
                    sub["name"] = (body["name"] or "").strip() or sub["name"]
                if "url" in body:
                    url = (body["url"] or "").strip()
                    if not url.lower().startswith(("http://", "https://")):
                        return self.send_json({"error": "url 非法"}, 400)
                    sub["url"] = url
                if "enabled" in body:
                    sub["enabled"] = bool(body["enabled"])
                save_subs(subs)
            return self.send_json(sub)
        return self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/subscriptions/"):
            if not self.authorized():
                return self.send_json({"error": "unauthorized"}, 401)
            sid = path[len("/api/subscriptions/"):].strip("/")
            with _lock:
                subs = load_subs()
                kept = [s for s in subs if s["id"] != sid]
                if len(kept) == len(subs):
                    return self.send_json({"error": "not found"}, 404)
                save_subs(kept)
            log(f"删除订阅: {sid}")
            return self.send_json({"ok": True})
        return self.send_json({"error": "not found"}, 404)


# ---------------- Web UI ----------------

UI_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>sing-box 订阅管理</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#0f1420;color:#e8ecf4}
.wrap{max-width:960px;margin:0 auto;padding:20px}
.card{background:#1a2233;border:1px solid #2a3650;border-radius:12px;padding:16px;margin-bottom:16px}
h1{font-size:20px;margin:0 0 4px}small{color:#8fa0bd}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
button{background:#3b82f6;border:0;color:#fff;padding:9px 16px;border-radius:8px;cursor:pointer;font-size:14px}
button.ghost{background:#26314a}button.danger{background:#7f1d1d}button:disabled{opacity:.5;cursor:wait}
input,select{width:100%;padding:9px 10px;border-radius:8px;border:1px solid #2a3650;background:#0f172a;color:#e8ecf4}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:10px 8px;border-bottom:1px solid #26314a;text-align:left;vertical-align:top}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:12px}.ok{background:#065f46}.err{background:#7f1d1d}.mut{background:#334155}
.url{color:#8fa0bd;word-break:break-all;font-size:12px}pre{background:#0b1120;padding:12px;border-radius:8px;overflow:auto;max-height:300px;font-size:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:640px){.grid{grid-template-columns:1fr}}
label{font-size:13px;color:#8fa0bd}
</style></head><body><div class="wrap">
<div class="card"><div class="row" style="justify-content:space-between">
<div><h1>📡 sing-box 订阅管理</h1><small>远程拉取订阅 → 校验 → SIGHUP 热重载，老连接不断</small></div>
<div class="row"><button id="btnReload">⚡ 立即更新并重载</button><button class="ghost" id="btnReloadOnly">仅重载</button></div>
</div><div id="status" style="margin-top:10px"><small>加载中…</small></div></div>
<div class="card"><h1 style="font-size:16px">➕ 添加 / 编辑订阅</h1>
<div class="grid" style="margin-top:10px">
<div><label>名称</label><input id="fName" placeholder="如：机场A sing-box"/></div>
<div><label>订阅 URL（须返回 sing-box JSON 或其 Base64）</label><input id="fUrl" placeholder="https://...?target=singbox"/></div>
</div>
<div class="row" style="margin-top:10px">
<label style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="fEnabled" checked style="width:auto"/> 启用</label>
<button id="btnSave">保存</button><button class="ghost" id="btnCancel" style="display:none">取消编辑</button>
</div><small>Clash/V2Ray 明文订阅请先用 subconverter 转成 target=singbox。</small></div>
<div class="card"><h1 style="font-size:16px">📋 订阅列表</h1><div id="list" style="margin-top:8px">加载中…</div></div>
<div class="card"><h1 style="font-size:16px">🧾 日志</h1><div class="row" style="margin:8px 0"><button class="ghost" id="btnLogs">刷新日志</button></div><pre id="logs">-</pre></div>
</div>
<script>
let editing=null;
const token=()=>localStorage.getItem('sb_token')||'';
function headers(h={}){const t=token();if(t)h['Authorization']='Bearer '+t;h['Content-Type']='application/json';return h;}
async function api(p,o={}){o.headers=headers(o.headers);const r=await fetch(p,o);
if(r.status===401){const t=prompt('请输入 MANAGER_TOKEN:');if(t!==null){localStorage.setItem('sb_token',t);return api(p,o);}throw new Error('未授权');}
const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.message||j.error||('HTTP '+r.status));return j;}
async function refresh(){try{const s=await api('/api/status');
document.getElementById('status').innerHTML=`<small>sing-box: ${s.singbox.running?('运行中 pid='+s.singbox.pid):'<b style=color:#f87171>未运行</b>'} · 配置: ${s.config.exists?('✅ '+s.config.size+'B @ '+s.config.mtime):'❌ 不存在'} · 订阅: ${s.subscriptions.enabled}/${s.subscriptions.total} 启用 · 上次: ${s.last_update.at||'-'} ${s.last_update.message||''}</small>`;
const subs=await api('/api/subscriptions');render(subs);}catch(e){document.getElementById('status').innerHTML='<small style=color:#f87171>'+e.message+'</small>';}}
function render(subs){const el=document.getElementById('list');
if(!subs.length){el.innerHTML='<small>暂无订阅，请先添加。</small>';return;}
el.innerHTML='<table><tr><th>名称/URL</th><th>状态</th><th>操作</th></tr>'+subs.map(s=>`<tr>
<td><b>${esc(s.name)}</b> ${s.enabled?'':'<span class=badge mut>停用</span>'}<div class=url>${esc(s.url)}</div><div class=url>${s.updated_at?('更新:'+esc(s.updated_at)+' '+(s.last_status||'')):'从未更新'} ${s.last_error?('<span style=color:#f87171>'+esc(s.last_error.slice(0,120))+'</span>'):''}</div></td>
<td>${s.last_status==='ok'?'<span class=badge ok>OK</span>':s.last_status==='error'?'<span class=badge err>失败</span>':'<span class=badge mut>-</span>'}</td>
<td><div class=row>
<button class=ghost onclick="toggleSub('${s.id}',${s.enabled?0:1})">${s.enabled?'停用':'启用'}</button>
<button class=ghost onclick="testSub('${s.id}')">测试</button>
<button class=ghost onclick="editSub('${s.id}')">编辑</button>
<button class=danger onclick="delSub('${s.id}')">删除</button></div></td></tr>`).join('')+'</table>';
window._subs=Object.fromEntries(subs.map(s=>[s.id,s]));}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function toggleSub(id,en){await api('/api/subscriptions/'+id,{method:'PUT',body:JSON.stringify({enabled:!!en})});refresh();}
async function testSub(id){try{const r=await api('/api/subscriptions/'+id+'/test',{method:'POST'});alert('连通 OK：'+r.count+' 个 outbounds\\n'+(r.preview||[]).join(', '));}catch(e){alert('测试失败：'+e.message);}}
function editSub(id){const s=window._subs[id];editing=id;fName.value=s.name;fUrl.value=s.url;fEnabled.checked=!!s.enabled;btnCancel.style.display='';}
async function delSub(id){if(!confirm('确定删除该订阅？'))return;await api('/api/subscriptions/'+id,{method:'DELETE'});refresh();}
btnSave.onclick=async()=>{const body={name:fName.value,url:fUrl.value,enabled:fEnabled.checked};
if(!body.url)return alert('请填写 URL');
try{if(editing){await api('/api/subscriptions/'+editing,{method:'PUT',body:JSON.stringify(body)});}else{await api('/api/subscriptions',{method:'POST',body:JSON.stringify(body)});}
editing=null;fName.value=fUrl.value='';fEnabled.checked=true;btnCancel.style.display='none';refresh();}catch(e){alert(e.message);}};
btnCancel.onclick=()=>{editing=null;fName.value=fUrl.value='';btnCancel.style.display='none';};
btnReload.onclick=async()=>{btnReload.disabled=true;btnReload.textContent='更新中…';
try{const r=await api('/api/reload',{method:'POST',body:JSON.stringify({fetch:true})});
alert((r.reloaded?'✅ 热重载成功：':'✅ 配置已更新（'+r.message+'）\\n')+r.message+'\\n节点数:'+(r.count??'-'));refresh();loadLogs();}catch(e){alert('❌ '+e.message);}finally{btnReload.disabled=false;btnReload.textContent='⚡ 立即更新并重载';}};
btnReloadOnly.onclick=async()=>{try{const r=await api('/api/reload-only',{method:'POST'});alert(r.message);refresh();}catch(e){alert('❌ '+e.message);}};
async function loadLogs(){try{const j=await api('/api/logs?n=200');logs.textContent=j.logs.join('\\n')||'(空)';}catch(e){logs.textContent=e.message;}}
btnLogs.onclick=loadLogs;refresh();loadLogs();setInterval(refresh,15000);
</script></body></html>"""


# ---------------- main ----------------

def auto_update_loop():
    try:
        interval = parse_interval(AUTO_UPDATE_INTERVAL)
    except ValueError as e:
        log(f"WARN: AUTO_UPDATE_INTERVAL 非法，已关闭定时更新：{e}")
        return
    if interval <= 0:
        return
    log(f"定时更新已启用：每 {AUTO_UPDATE_INTERVAL} 拉取并热重载")
    while True:
        time.sleep(interval)
        try:
            r = do_update_and_reload(fetch=True, send_hup_signal=True)
            log(f"定时更新结果: ok={r.get('ok')} reloaded={r.get('reloaded')} msg={r.get('message')}")
        except Exception as e:  # noqa: BLE001
            log(f"定时更新异常: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-once", action="store_true", help="只拉取并写配置（供 entrypoint 启动前调用），不发 SIGHUP")
    ap.add_argument("--port", type=int, default=MANAGER_PORT)
    args = ap.parse_args()

    os.makedirs(CACHE_DIR, exist_ok=True)

    if args.update_once:
        r = do_update_and_reload(fetch=True, send_hup_signal=False)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        sys.exit(0 if r.get("ok") else 1)

    threading.Thread(target=auto_update_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    log(f"manager listening on :{args.port}  config={CONFIG_PATH} template={TEMPLATE_PATH}")

    def _term(signum, frame):  # noqa: ARG001
        log("收到退出信号，关闭 manager")
        # manager 是 PID1(exec) 时，顺手带走 sing-box
        pid = find_singbox_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
