# 部署后的分层测试

## 1. 无模型费用：服务与代理

```bash
systemctl status sorvflow --no-pager -l
cat /run/sorvflow/port
curl -i --max-time 10 "http://127.0.0.1:$(cat /run/sorvflow/port)/api/health"
curl -i --max-time 10 https://sorvflow.wlqxy.com/api/health
curl -i --max-time 10 https://sorvflow.wlqxy.com/api/auth/session
```

新账号版内网与公网 health 均应为 200，返回 `ok:true`、`accounts:true`，模型已配时 `key:true`。未登录的 session 返回 `authenticated:false`；首次创建账号前 `setup_required:true`。未登录访问 `/api/jobs` 返回 401 属于正常权限保护。

若公网 health 返回旧版 `{"error":"unauthorized"}`，或无 `accounts:true`，说明启动器/源码仍是旧版。不能只 git pull：需同步本包启动器与新代理规则。403 优先核对 `SORVFLOW_PUBLIC_URL=https://sorvflow.wlqxy.com` 是否与浏览器一致，以及代理是否保留公网 Host/Origin。502 查进程和实际端口 include。首页 200 但脚本 404 时查是否有静态缓存 location 截走 JS/CSS。

## 2. 无模型费用：账号

1. 创建管理员，打开网页强制刷新，确认出现「登录」按钮。
2. 管理员创建两个成员 `tester-a`、`tester-b`；使用两个浏览器配置文件或普通/无痕窗口分别登录。同一浏览器同域名标签页共用 Cookie，不能用来模拟不同账号。
3. 两人首次登录必须改初始密码，再访问「我的任务」。普通成员不能访问 `/api/admin/users`（403）。
4. 停用 B，B 刷新后应要求重新登录。启用后重置初始密码，旧密码和旧会话应失效，B 需用新初始密码再改密码。
5. 界面中英文切换、手机 390px 宽下确认表单、按钮可用，无横向滚动。

## 3. 少量模型费用：真实漫画链路

管理员可点击「账号管理 → 检测文本接口」。会明确发出一次文本工具调用，可能计费；成功仅证明文本 tools/function calling 通路。

实际漫画测试先用短正文和默认并发 1，例如：

> 月色下，白衣剑修沈舟站在山门前。黑衣少女林月递来一封书信。沈舟拆开信，眉头微皱。林月望向远处的山峰，说：“今晚必须出发。”两人转身走入雾中。

选择「分页漫画」，画风填“三维国漫，东方修仙，人物服装和发型保持一致”，提交一次。不要假设短正文一定只生成一张图，管线会规划人物参考图和多格/多页，按供应商计费。

```bash
journalctl -u sorvflow -f
```

生成日志可在网页查看；管理员还可查看 `/var/lib/sorvflow/web/users/<用户UUID>/logs/<任务ID>.stderr.log`。日志可能含正文或提示词，发给他人前做脱敏。

验收：任务由排队→生成→完成，结果对应输入的新正文；人物/画风正确；中文字体正常；图片、PDF/阅读器按所选格式可打开。下载 URL 应以 `/files/自己的项目编号/` 开头。401/403 的供应商错误查 key/模型权限，404 查供应商 URL/模型 ID，429 查额度/速率；不要把这些当网站登录错误。

## 4. 两人排队与隔离

使用 `SORVFLOW_WORKERS=1`、`SORVFLOW_USER_RUNNING=1`：A 与 B 各提交短项目。全站应只有一个生成进程，另一个排队。B 页面尝试打开 A 的任务 URL、漫画 URL 应为 404；B 的「我的任务」不能出现 A 的内容。

同一账号相同项目连续点击，返回同一活动任务；用不同项目填满排队额度，超出时显示上限提示。停止排队任务，刷新后仍为已取消，不能自动启动。重启测试先选可接受重新调用费用的短项目：`systemctl restart sorvflow` 后刷新，任务应仍存在，并从检查点继续。

## 5. 上线前记录

记录一篇的总耗时、图数、供应商费用、内存峰值和失败率，再决定是否把 `SORVFLOW_WORKERS` 从 1 调到 2。账号隔离测试通过不等于已经验证多人真实生图吞吐。当前仓库自动化测试和本机桌面/手机检查不会替代目标服务器上的实际验收。
