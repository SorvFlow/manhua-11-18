#!/usr/bin/env bash
# OpenCloudOS / RHEL-family or Ubuntu / Debian + BaoTa Nginx.
set -euo pipefail
umask 027
DEPLOY_SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP=/opt/sorvflow/app
if (( EUID != 0 )); then
    echo "请用 sudo bash install.sh 或 root 运行。" >&2
    exit 1
fi
if command -v dnf >/dev/null 2>&1; then
    package_manager=dnf
elif command -v yum >/dev/null 2>&1; then
    package_manager=yum
elif command -v apt-get >/dev/null 2>&1; then
    package_manager=apt-get
else
    echo "未找到 dnf、yum 或 apt-get，请先安装 git/curl/字体 等依赖。" >&2
    exit 1
fi
if [[ $package_manager == apt-get ]]; then
    apt-get update
    apt-get install -y git curl ca-certificates fontconfig fonts-noto-cjk unzip
else
    packages=(ca-certificates fontconfig)
    command -v git >/dev/null 2>&1 || packages+=(git)
    command -v curl >/dev/null 2>&1 || packages+=(curl)
    command -v unzip >/dev/null 2>&1 || packages+=(unzip)
    "$package_manager" install -y "${packages[@]}"
    if [[ -z $(fc-list :lang=zh -f '%{file}\n') ]]; then
        for font_package in google-noto-sans-cjk-fonts google-noto-sans-cjk-ttc-fonts google-noto-cjk-fonts; do
            if "$package_manager" install -y "$font_package"; then break; fi
        done
    fi
fi
for program in git curl runuser systemctl; do
    command -v "$program" >/dev/null || { echo "缺少 $program，请先安装。" >&2; exit 1; }
done
if [[ ! -x /www/server/nginx/sbin/nginx ]]; then
    echo "请先在宝塔安装 Nginx。当前模板不适用于 Apache/OpenLiteSpeed。" >&2
    exit 1
fi
if [[ -d $APP ]] && [[ -n $(ls -A "$APP") ]]; then
    echo "$APP 已有内容，本脚本只做首次安装，不覆盖已有部署。" >&2
    exit 1
fi
if [[ -e /etc/systemd/system/sorvflow.service ]]; then
    echo "已存在 sorvflow.service，请使用教程中的升级命令。" >&2
    exit 1
fi
if ! id sorvflow >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /opt/sorvflow --shell /usr/sbin/nologin sorvflow
fi
install -d -o root -g root -m 0755 /opt/sorvflow/deploy /etc/sorvflow
install -d -o sorvflow -g sorvflow -m 0750 "$APP" /opt/sorvflow/python \
    /var/lib/sorvflow/web /var/lib/sorvflow/data /var/lib/sorvflow/comic_out /var/lib/sorvflow/logs \
    /var/cache/sorvflow/uv
runuser -u sorvflow -- git clone --branch '主要的' \
    https://github.com/SorvFlow/manhua-11-18.git "$APP"
if [[ ! -x /usr/local/bin/uv ]]; then
    installer=$(mktemp)
    curl -LsSf https://astral.sh/uv/install.sh -o "$installer"
    env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh "$installer"
    rm -f "$installer"
fi
runuser -u sorvflow -- env UV_CACHE_DIR=/var/cache/sorvflow/uv \
    UV_PYTHON_INSTALL_DIR=/opt/sorvflow/python \
    /usr/local/bin/uv sync --locked --no-dev --python 3.12 --project "$APP"
for name in comic_out logs; do
    if [[ -e $APP/$name || -L $APP/$name ]]; then
        echo "$APP/$name 已存在，请检查后手动迁移，安装中止以保留数据。" >&2
        exit 1
    fi
    runuser -u sorvflow -- ln -s "/var/lib/sorvflow/$name" "$APP/$name"
done
install -o root -g root -m 0644 "$DEPLOY_SOURCE/serve.py" /opt/sorvflow/deploy/serve.py
install -o root -g root -m 0755 "$DEPLOY_SOURCE/refresh-proxy.sh" \
    /usr/local/sbin/sorvflow-refresh-proxy
if [[ ! -e /etc/sorvflow/sorvflow.env ]]; then
    install -o root -g root -m 0600 "$DEPLOY_SOURCE/sorvflow.env.example" \
        /etc/sorvflow/sorvflow.env
    if [[ -n $(fc-list :lang=zh -f '%{file}\n') ]]; then
        font_path=$(fc-list :lang=zh -f '%{file}\n' | sort -u | head -n 1)
        "$APP/.venv/bin/python" - "$font_path" <<'PY'
import sys
from pathlib import Path

path = Path("/etc/sorvflow/sorvflow.env")
lines = path.read_text().splitlines()
path.write_text("\n".join(
    f"INKSTONE_FONT_PATH={sys.argv[1]}" if line.startswith("INKSTONE_FONT_PATH=") else line
    for line in lines
) + "\n")
PY
    else
        echo "提示：尚未找到中文字体。网站可启动，生成带字漫画前请安装中文字体。" >&2
    fi
fi
install -o root -g root -m 0644 "$DEPLOY_SOURCE/sorvflow.service" \
    /etc/systemd/system/sorvflow.service
systemctl daemon-reload
systemctl enable --now sorvflow
echo "已启动。模型配置：/etc/sorvflow/sorvflow.env"
echo "实际端口：$(cat /run/sorvflow/port)；下一步按 README.md 配置宝塔网站。"

echo '公网使用前：填写 SORVFLOW_PUBLIC_URL=https://你的域名 并重启服务。'
echo '创建管理员（密码交互输入）：'
echo 'runuser -u sorvflow -- /opt/sorvflow/app/.venv/bin/python -m web.accounts create-admin --username admin --state-dir /var/lib/sorvflow/web'
