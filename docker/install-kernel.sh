#!/bin/sh
# 下载并安装指定版本 sing-box 内核（musl 包，适配 alpine）。
# 构建时与运行时共用：Dockerfile 用它预装默认稳定版，entrypoint 用它按需拉取其他版本。
#
# 用法： install-kernel.sh [--resolve-tag] <version> [dest-dir]
#   --resolve-tag：只解析并打印规范化后的 tag（latest/stable 走网络解析），不下载
#   version:  v1.14.3 / 1.14.3 / v1.15.0-alpha.12 / latest（最新稳定版，走 GitHub API 解析）
#   dest-dir: 默认 /opt/sing-box/versions
# 安装到 <dest-dir>/<version>/sing-box；最终二进制路径打印到 stdout（日志走 stderr）。
# 同版本已存在且可运行则直接复用（FORCE=1 强制重下）。
# 可选环境变量：SINGBOX_ARCH（覆盖架构检测）、SINGBOX_LIBC（默认 musl）、FORCE、
#   GITHUB_PROXY（GitHub 代理/镜像前缀，如 https://ghproxy.com/，自动拼到下载地址前）。
set -eu

RESOLVE_ONLY=0
if [ "${1:-}" = "--resolve-tag" ]; then
  RESOLVE_ONLY=1
  shift
fi
VERSION="${1:?usage: install-kernel.sh [--resolve-tag] <version> [dest-dir]}"
DEST_DIR="${2:-/opt/sing-box/versions}"

# 代理前缀归一化：去掉末尾 /，使用时再拼（https://ghproxy.com + /https://github.com/...）
GH_PROXY="${GITHUB_PROXY:-}"
case "$GH_PROXY" in "" ) ;; */) GH_PROXY="${GH_PROXY%/}";; esac

# SOCKS5 代理优先于镜像站（镜像不稳时用）：
#   SOCKS5_PROXY=socks5://user:pass@127.0.0.1:1080 或 socks5h://host:1080
SOCKS_ADDR=""
case "${SOCKS5_PROXY:-}" in
  "") ;;
  socks5h://*) SOCKS_ADDR="${SOCKS5_PROXY#socks5h://}";;
  socks5://*) SOCKS_ADDR="${SOCKS5_PROXY#socks5://}";;
  *) SOCKS_ADDR="$SOCKS5_PROXY";;
esac
CURL_ARGS=""
if [ -n "$SOCKS_ADDR" ]; then
  # --socks5-hostname：域名交给代理解析，避免容器 DNS 污染
  CURL_ARGS="--socks5-hostname $SOCKS_ADDR"
  GH_PROXY=""
fi

log() { echo "[install-kernel] $*" >&2; }

do_curl() { curl -fsSL $CURL_ARGS "$@"; }

resolve_latest() {
  if [ -n "$GH_PROXY" ]; then
    # 走镜像：releases/latest 会 302 到 releases/tag/<tag>，取最终地址尾段
    EFF="$(do_curl --max-time 20 -o /dev/null -w '%{url_effective}' "$GH_PROXY/https://github.com/SagerNet/sing-box/releases/latest")"
    TAG="${EFF##*/}"
    case "$TAG" in v*|*.*) echo "$TAG"; return 0;; esac
    return 1
  fi
  do_curl --max-time 20 https://api.github.com/repos/SagerNet/sing-box/releases/latest | grep -m1 '"tag_name"' | cut -d'"' -f4
}

if [ "$VERSION" = "latest" ] || [ "$VERSION" = "stable" ]; then
  log "resolving latest stable..."
  TAG="$(resolve_latest)"
  [ -n "${TAG:-}" ] || { log "ERROR: cannot resolve latest version"; exit 1; }
  VERSION="$TAG"
fi
case "$VERSION" in v*) ;; *) VERSION="v$VERSION";; esac
VER="${VERSION#v}"

if [ "$RESOLVE_ONLY" = "1" ]; then
  echo "$VERSION"
  exit 0
fi

if [ -n "${SINGBOX_ARCH:-}" ]; then
  ARCH="$SINGBOX_ARCH"
else
  case "$(uname -m)" in
    x86_64|amd64) ARCH=amd64;;
    aarch64|arm64) ARCH=arm64;;
    i386|i686) ARCH=386;;
    armv7l) ARCH=armv7;;
    armv6l) ARCH=armv6;;
    riscv64) ARCH=riscv64;;
    s390x) ARCH=s390x;;
    ppc64le) ARCH=ppc64le;;
    *) log "ERROR: unsupported arch $(uname -m), set SINGBOX_ARCH explicitly"; exit 1;;
  esac
fi

LIBC="${SINGBOX_LIBC:-musl}"
BIN="$DEST_DIR/$VERSION/sing-box"
if [ -x "$BIN" ] && [ "${FORCE:-0}" != "1" ]; then
  if "$BIN" version >/dev/null 2>&1; then
    log "reuse cached $VERSION"
    echo "$BIN"
    exit 0
  fi
  log "cached binary broken, re-downloading"
fi

ASSET="sing-box-${VER}-linux-${ARCH}-${LIBC}.tar.gz"
URL="https://github.com/SagerNet/sing-box/releases/download/${VERSION}/${ASSET}"
if [ -n "$GH_PROXY" ]; then
  URL="$GH_PROXY/$URL"
  log "using GitHub proxy: $GH_PROXY"
fi
if [ -n "$SOCKS_ADDR" ]; then
  log "using SOCKS5 proxy: $SOCKS_ADDR"
fi
log "downloading $URL"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM
do_curl --retry 3 --max-time 300 -o "$TMP/pkg.tar.gz" "$URL"
tar -xzf "$TMP/pkg.tar.gz" -C "$TMP"
FOUND="$(find "$TMP" -name sing-box -type f | head -n 1)"
[ -n "${FOUND:-}" ] || { log "ERROR: sing-box binary not found in $ASSET"; exit 1; }
mkdir -p "$DEST_DIR/$VERSION"
cp "$FOUND" "$BIN"
chmod +x "$BIN"
"$BIN" version >&2 || { log "ERROR: downloaded binary cannot run on this system"; exit 1; }
log "installed $VERSION -> $BIN"
echo "$BIN"
