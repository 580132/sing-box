# sing-box 订阅管理镜像：alpine + 内核下载器 + 零依赖管理面板
# 构建时内置默认稳定版内核；运行时用 SINGBOX_VERSION 环境变量切换版本（自动下载，无需重建镜像）
ARG ALPINE_VERSION=3.21
FROM alpine:${ALPINE_VERSION}

# 构建时内置的默认内核版本（稳定版）；构建命令可用 --build-arg 覆盖
ARG BAKED_SINGBOX_VERSION=v1.14.3

RUN apk add --no-cache python3 curl ca-certificates tzdata procps tar gzip && \
    mkdir -p /app/manager /app/docker /etc/sing-box /var/cache/sing-box-subs /var/log/sing-box /run \
             /opt/sing-box/baked /opt/sing-box/versions

COPY docker/install-kernel.sh /app/docker/install-kernel.sh
COPY manager/manager.py /app/manager/manager.py
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
COPY docker/update-subs.sh /usr/local/bin/update-subs
RUN chmod +x /app/docker/install-kernel.sh /app/docker/entrypoint.sh /usr/local/bin/update-subs && \
    python3 -m py_compile /app/manager/manager.py && \
    /app/docker/install-kernel.sh "$BAKED_SINGBOX_VERSION" /opt/sing-box/baked && \
    echo "$BAKED_SINGBOX_VERSION" > /opt/sing-box/baked/VERSION

ENV BAKED_SINGBOX_VERSION=${BAKED_SINGBOX_VERSION} \
    SINGBOX_VERSION="" \
    GITHUB_PROXY="" \
    KERNEL_DIR=/opt/sing-box/versions \
    MANAGER_PORT=8080 \
    CONFIG_PATH=/etc/sing-box/config.json \
    SUBS_PATH=/etc/sing-box/subscriptions.json \
    CACHE_DIR=/var/cache/sing-box-subs \
    SINGBOX_BIN=sing-box \
    PID_FILE=/run/sing-box.pid \
    AUTO_UPDATE_INTERVAL=0 \
    UPDATE_ON_START=true \
    ALLOW_EMPTY_OUTBOUNDS=false

EXPOSE 8080
VOLUME ["/etc/sing-box", "/var/cache/sing-box-subs", "/opt/sing-box/versions"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -qO- http://127.0.0.1:${MANAGER_PORT}/api/status || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
