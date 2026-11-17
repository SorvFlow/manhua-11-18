# sorvflow 宝塔部署与升级

适用于 OpenCloudOS（dnf/yum）、Ubuntu/Debian（apt-get）与宝塔 Nginx。登录、账号、作品权限和生成队列由应用管理；宝塔负责域名、HTTPS 与反向代理。使用模型 API 可先从 4 核 8 GB 起步，此规格不含本地模型推理资源。

## 已安装旧版：直接升级

你的网站 `https://sorvflow.wlqxy.com` 已经反代，内网健康检查正常，公网旧版返回 401。现有代理的 `Host 127.0.0.1` 未带端口，不能满足旧版的本机访问校验。**本包与账号版源码配套升级**，保留公网 Host，由应用验证账号和公网 Origin。

1. 下载最新 `sorvflow-baota.zip`，在宝塔「文件」上传并解压到 `/tmp`，得到 `/tmp/sorvflow-baota/upgrade.sh`。替换旧安装包目录里的脚本即可，不要覆盖 `/etc/sorvflow/sorvflow.env`。
2. 等现有生成任务结束，root 终端执行：

```bash
cd /tmp/sorvflow-baota && bash upgrade.sh --public-url https://sorvflow.wlqxy.com
```

脚本会识别该域名目录下唯一的反代 `.conf`；有多个时会停止，此时追加 `--proxy-file /www/server/panel/vhost/nginx/proxy/sorvflow.wlqxy.com/实际文件名.conf`。它会停止服务，备份 `/var/lib/sorvflow`、环境文件、启动器、服务和旧代理到 `/var/backups/sorvflow/时间戳/`，拉取 `主要的` 分支、同步依赖、更新启动器和反代，再启动。源码有未提交修改会先停止，防止覆盖。

保留模型 URL、key、模型 ID；补充账号设置；若字体仍指向 DejaVuSans，会改用系统已有的中文字体。旧漫画留在 `/var/lib/sorvflow/comic_out`，不自动分配给任何用户；新的作品按用户保存，防止误把旧作品公开给新账号。

3. 创建管理员，密码会交互输入、不回显：

```bash
runuser -u sorvflow -- /opt/sorvflow/app/.venv/bin/python \
  -m web.accounts create-admin --username admin --state-dir /var/lib/sorvflow/web
```

4. 浏览器强制刷新网站，点击右上角「登录」。管理员在「账号管理」创建成员。成员首次登录必须修改初始密码。不要把模型 key 当作登录密码。
5. 按 [TESTING.md](TESTING.md) 验收。未访问你的服务器终端，脚本未在该 OpenCloudOS 主机实际执行；已有公网 401 已通过只读请求确认。

## 首次安装

1. 宝塔安装 Nginx，域名 DNS 指向服务器；安全组只需开放 80/443。
2. 上传并解压最新部署包到 `/tmp`，root 执行：

```bash
cd /tmp/sorvflow-baota && bash install.sh
```

脚本检测 dnf/yum/apt-get，安装 git/curl/中文字体、uv/Python 3.12、依赖和 sorvflow 专用用户。已有源码或服务会停止，已有部署用上面的升级脚本。不要再用宝塔 Python 项目管理器启动第二份 Web 服务。

3. 编辑 `/etc/sorvflow/sorvflow.env`。按示例填写模型 URL、key、模型 ID，并设置：

```dotenv
SORVFLOW_PUBLIC_URL=https://你的域名
SORVFLOW_STATE_DIR=/var/lib/sorvflow/web
SORVFLOW_WORKERS=1
SORVFLOW_USER_RUNNING=1
SORVFLOW_USER_QUEUED=5
SORVFLOW_SESSION_HOURS=12
INKSTONE_IMAGE_CONCURRENCY=1
```

`SORVFLOW_PUBLIC_URL` 必须与浏览器 HTTPS 地址一致，不带路径或末尾 `/`。环境文件 root:root 600，勿上传 Git。中文字体可用 `fc-list :lang=zh -f '%{file}\n'` 查找；如 `/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc`，按实际存在的路径填写。

文本接口需支持 tools/function calling。图片兼容层目前请求 `/images/generations`，参考图经 `extra_body.image` 提交，供应商需兼容；不兼容时需要修改适配器。`key:true` 只说明配置存在，不能证明模型调用成功。

4. 宝塔添加纯静态站点，根目录用空目录 `/www/wwwroot/你的域名`，不要指向源码或作品目录。申请证书、启用强制 HTTPS。
5. 在该站点 `server { ... }` 内使用 `baota-proxy.conf.example` 内容。保留 listen/server_name、证书和 ACME 配置，只保留一份根路径代理；删除旧 `location /`、图片/JS/CSS 静态缓存规则及 PHP 引用。已有宝塔独立代理文件时，用模板替换那一份，避免在主配置里再加同路径代理。
6. 创建管理员（命令同上），执行：

```bash
systemctl restart sorvflow
/www/server/nginx/sbin/nginx -t
/www/server/nginx/sbin/nginx -s reload
```

## 自动端口探测

启动器依次绑定 8765–8815，遇到已占用端口继续下一个，保持选中 socket 到 Uvicorn 接管，避免探测与监听之间的抢占。实际端口保存在 `/run/sorvflow/port`。服务就绪后自动写入 `/www/server/panel/vhost/nginx/sorvflow-backend.inc`，检查 Nginx 配置并重载。公网无需开放这些内网端口。

扩大范围：修改 `/etc/systemd/system/sorvflow.service` 的 `--port-start`/`--port-end`，再 `systemctl daemon-reload`、重启服务。不要用多个 Web 进程共用一个账号数据库；增加生成并发用 `SORVFLOW_WORKERS`。

## 常用命令

```bash
systemctl status sorvflow --no-pager -l
journalctl -u sorvflow -n 80 --no-pager
cat /run/sorvflow/port
curl -fsS "http://127.0.0.1:$(cat /run/sorvflow/port)/api/health"
curl -i https://sorvflow.wlqxy.com/api/health
```

重置密码（会退出该账号的所有会话，下次登录需修改初始密码）：

```bash
runuser -u sorvflow -- /opt/sorvflow/app/.venv/bin/python \
  -m web.accounts reset-password --username admin --state-dir /var/lib/sorvflow/web
```

后续代码更新也可重新运行 `upgrade.sh`，会再次备份并保留已有账号/数据。

## 备份与回滚

完整备份包括 `/var/lib/sorvflow`（账号数据库、任务、用户作品）、`/etc/sorvflow`（含密钥，限制备份读取权限）、服务、部署脚本与站点代理配置。复制 SQLite 文件前先停止服务，确保 WAL 一起保存。推荐将加密备份保存到另一台机器。

升级失败，服务保持停止并显示备份位置。先修复日志中的问题再重跑；需要回滚时，用实际备份路径替换下例。回滚前再保存升级后新产生的数据，恢复旧快照会覆盖同名文件：

```bash
systemctl stop sorvflow
BACKUP=/var/backups/sorvflow/实际时间戳
OLD_COMMIT=$(cat "$BACKUP/source-commit")
runuser -u sorvflow -- git -C /opt/sorvflow/app switch --detach "$OLD_COMMIT"
runuser -u sorvflow -- env UV_CACHE_DIR=/var/cache/sorvflow/uv \
  UV_PYTHON_INSTALL_DIR=/opt/sorvflow/python \
  /usr/local/bin/uv sync --locked --no-dev --python 3.12 --project /opt/sorvflow/app
tar -C / -xzf "$BACKUP/state.tar.gz"
cp -p "$BACKUP/serve.py" /opt/sorvflow/deploy/serve.py
cp -p "$BACKUP/sorvflow.service" /etc/systemd/system/sorvflow.service
cp -p "$BACKUP/refresh-proxy.sh" /usr/local/sbin/sorvflow-refresh-proxy
PROXY_FILE=$(cat "$BACKUP/proxy-path")
cp -p "$BACKUP/proxy.conf" "$PROXY_FILE"
systemctl daemon-reload
systemctl start sorvflow
```

旧版回滚会恢复旧访问校验，不能保留新版反代规则单独使用。新版完整能力及边界见 [MULTI_USER.md](MULTI_USER.md)。
