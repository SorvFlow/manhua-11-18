#!/usr/bin/env bash
set -euo pipefail
NGINX=/www/server/nginx/sbin/nginx
CONF=/www/server/panel/vhost/nginx/sorvflow-backend.inc
if (( EUID != 0 )); then
    echo "需要 root 更新宝塔反向代理配置。" >&2
    exit 1
fi
IFS= read -r port < /run/sorvflow/port
if [[ ! $port =~ ^[1-9][0-9]{3,4}$ ]] || (( port < 1024 || port > 65535 )); then
    echo "实际端口文件不合法。" >&2
    exit 1
fi
temporary=$(mktemp "${CONF}.new.XXXXXX")
backup=$(mktemp "${CONF}.old.XXXXXX")
had_old=0
trap 'rm -f "$temporary" "$backup"' EXIT
if [[ -f $CONF ]]; then
    cp -p "$CONF" "$backup"
    had_old=1
fi
rollback() {
    if (( had_old )); then cp -p "$backup" "$CONF"; else rm -f "$CONF"; fi
}
printf 'set $sorvflow_backend 127.0.0.1:%s;\n' "$port" > "$temporary"
chown root:root "$temporary"
chmod 0644 "$temporary"
mv -f "$temporary" "$CONF"
if ! "$NGINX" -t; then
    rollback
    echo "Nginx 配置检查失败，已恢复原端口配置。" >&2
    exit 1
fi
if ! "$NGINX" -s reload; then
    rollback
    echo "Nginx 重载失败，已恢复原端口配置，请检查宝塔 Nginx 状态。" >&2
    exit 1
fi
echo "宝塔 Nginx 后端端口已更新为 $port"
