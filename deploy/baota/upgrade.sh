#!/usr/bin/env bash
# Upgrade an existing BaoTa deployment while preserving keys and old work.
set -euo pipefail
umask 027
DEPLOY_SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP=/opt/sorvflow/app
PUBLIC_URL=
PROXY_FILE=
while (( $# )); do
    case "$1" in
        --public-url) PUBLIC_URL=${2:?缺少网址}; shift 2 ;;
        --proxy-file) PROXY_FILE=${2:?缺少代理文件路径}; shift 2 ;;
        *) echo "用法：bash upgrade.sh --public-url https://你的域名 --proxy-file /宝塔代理文件.conf" >&2; exit 1 ;;
    esac
done
if (( EUID != 0 )); then echo "请使用 root 运行。" >&2; exit 1; fi
if [[ -z $PROXY_FILE ]]; then
    proxy_domain=$("$APP/.venv/bin/python" - "$PUBLIC_URL" <<'PY'
import sys
from urllib.parse import urlsplit
host = urlsplit(sys.argv[1]).hostname or ''
if not host or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in host):
    raise SystemExit('请填写真实 HTTPS 域名。')
print(host)
PY
    )
    shopt -s nullglob
    candidates=(/www/server/panel/vhost/nginx/proxy/"$proxy_domain"/*.conf)
    if (( ${#candidates[@]} != 1 )); then
        echo "域名目录下不是唯一代理文件，请用 --proxy-file 明确指定，脚本未修改任何配置。" >&2
        exit 1
    fi
    PROXY_FILE=${candidates[0]}
fi
if [[ ! -d $APP/.git || ! -f /etc/sorvflow/sorvflow.env || ! -f $PROXY_FILE ]]; then
    echo "请确认现有安装及 --proxy-file 指向实际代理文件。" >&2; exit 1
fi
if [[ $PROXY_FILE != /www/server/panel/vhost/nginx/proxy/*/*.conf ]]; then
    echo "只接受该站点独立的宝塔反代 .conf，不接受主 nginx.conf。" >&2; exit 1
fi
"$APP/.venv/bin/python" - "$PUBLIC_URL" <<'PY'
import sys
from urllib.parse import urlsplit
value = urlsplit(sys.argv[1])
if value.scheme != "https" or not value.hostname or value.path or value.query or value.fragment or value.username or value.password:
    raise SystemExit('公网地址须为 https://域名，无路径、无末尾斜杠。')
PY
if [[ -n $(runuser -u sorvflow -- git -C "$APP" status --porcelain) ]]; then
    echo "源码有本地改动，请先提交或备份后再升级；脚本不会覆盖。" >&2; exit 1
fi
backup_dir="/var/backups/sorvflow/$(date +%Y%m%d-%H%M%S)"
install -d -o root -g root -m 0700 "$backup_dir"
runuser -u sorvflow -- git -C "$APP" rev-parse HEAD > "$backup_dir/source-commit"
echo "将短暂停止服务，备份账号/作品/配置并升级；备份位置：$backup_dir"
systemctl stop sorvflow
trap 'echo "升级未完成。服务保持停止，备份位于：$backup_dir。按 README.md 回滚。" >&2' ERR
tar -C / -czf "$backup_dir/state.tar.gz" var/lib/sorvflow etc/sorvflow
cp -p "$PROXY_FILE" "$backup_dir/proxy.conf"
cp -p /etc/systemd/system/sorvflow.service "$backup_dir/sorvflow.service"
cp -p /opt/sorvflow/deploy/serve.py "$backup_dir/serve.py"
cp -p /usr/local/sbin/sorvflow-refresh-proxy "$backup_dir/refresh-proxy.sh"
printf '%s\n' "$PROXY_FILE" > "$backup_dir/proxy-path"
runuser -u sorvflow -- git -C "$APP" pull --ff-only origin '主要的'
if [[ ! -f $APP/web/app.py ]]; then echo "仓库还没有账号版，请等待维护方发布。" >&2; exit 1; fi
runuser -u sorvflow -- env UV_CACHE_DIR=/var/cache/sorvflow/uv \
    UV_PYTHON_INSTALL_DIR=/opt/sorvflow/python \
    /usr/local/bin/uv sync --locked --no-dev --python 3.12 --project "$APP"
install -d -o sorvflow -g sorvflow -m 0750 /var/lib/sorvflow/web
"$APP/.venv/bin/python" - "$PUBLIC_URL" <<'PY'
import subprocess
import sys
from pathlib import Path
path = Path('/etc/sorvflow/sorvflow.env')
lines = path.read_text().splitlines()
values = {'SORVFLOW_PUBLIC_URL': sys.argv[1], 'SORVFLOW_STATE_DIR': '/var/lib/sorvflow/web',
          'SORVFLOW_WORKERS': '1', 'SORVFLOW_USER_RUNNING': '1',
          'SORVFLOW_USER_QUEUED': '5', 'SORVFLOW_SESSION_HOURS': '12'}
keys = {line.split('=',1)[0] for line in lines if '=' in line and not line.lstrip().startswith('#')}
lines = [f'SORVFLOW_PUBLIC_URL={sys.argv[1]}' if line.startswith('SORVFLOW_PUBLIC_URL=') else line for line in lines]
lines += [f'{key}={value}' for key,value in values.items() if key not in keys]
# Replace an old Latin fallback with an installed font advertised for Chinese.
fonts = subprocess.check_output(['fc-list', ':lang=zh', '-f', '%{file}\n'], text=True).splitlines()
fonts = sorted(set(fonts), key=lambda p: ('NotoSansCJK-Regular' not in p, p))
if fonts:
    lines = [f'INKSTONE_FONT_PATH={fonts[0]}' if line.startswith('INKSTONE_FONT_PATH=') and ('DejaVuSans' in line or line == 'INKSTONE_FONT_PATH=') else line for line in lines]
path.write_text('\n'.join(lines) + '\n')
PY
install -o root -g root -m 0644 "$DEPLOY_SOURCE/serve.py" /opt/sorvflow/deploy/serve.py
install -o root -g root -m 0755 "$DEPLOY_SOURCE/refresh-proxy.sh" /usr/local/sbin/sorvflow-refresh-proxy
install -o root -g root -m 0644 "$DEPLOY_SOURCE/sorvflow.service" /etc/systemd/system/sorvflow.service
install -o root -g root -m 0644 "$DEPLOY_SOURCE/baota-proxy.conf.example" "$PROXY_FILE"
systemctl daemon-reload
systemctl start sorvflow
trap - ERR
echo "升级完成。请创建初始管理员，再打开 $PUBLIC_URL。"
echo 'runuser -u sorvflow -- /opt/sorvflow/app/.venv/bin/python -m web.accounts create-admin --username admin --state-dir /var/lib/sorvflow/web'
echo "备份：$backup_dir；旧漫画仍在 /var/lib/sorvflow/comic_out，未自动归属到任何账号。"
