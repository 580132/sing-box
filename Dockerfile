# sing-box 订阅管理镜像
# 基础镜像：官方 sing-box（已含 sing-box 二进制）
# 构建时用 --build-arg SINGBOX_VERSION=<tag> 指定内核版本，
# 如 v1.15.0-alpha.12（tag 与上游 GitHub Release 同名）；默认 latest
ARG SINGBOX_VERSION=latest
FROM ghcr.io/sagernet/sing-box:${SINGBOX_VERSION}

# manager 为纯 Python 标准库，无需 pip 依赖
RUN apk add --no-cache python3 curl ca-certificates tzdata procps && \
    mkdir -p /app/manager /app/docker /etc/sing-box /var/cache/sing-box-subs /var/log/sing-box /run

COPY manager/manager.py /app/manager/manager.py
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
COPY docker/update-subs.sh /usr/local/bin/update-subs
RUN chmod +x /app/docker/entrypoint.sh /usr/local/bin/update-subs && \
    python3 -m py_compile /app/manager/manager.py

ENV MANAGER_PORT=8080 \
    CONFIG_PATH=/etc/sing-box/config.json \
    TEMPLATE_PATH=/etc/sing-box/config.template.json \
    SUBS_PATH=/etc/sing-box/subscriptions.json \
    CACHE_DIR=/var/cache/sing-box-subs \
    SINGBOX_BIN=sing-box \
    PID_FILE=/run/sing-box.pid \
    AUTO_UPDATE_INTERVAL=0 \
    UPDATE_ON_START=true \
    ALLOW_EMPTY_OUTBOUNDS=false

EXPOSE 8080
VOLUME ["/etc/sing-box", "/var/cache/sing-box-subs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -qO- http://127.0.0.1:${MANAGER_PORT}/api/status || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
