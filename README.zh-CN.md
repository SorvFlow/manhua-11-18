# sorvflow 中文漫画工作台

sorvflow 由伍六柒爱电校园维护。网站已更新品牌标识、页脚文案，并移除对外的项目与服务商跳转入口；原项目许可与版权声明保留在源码中。

本地源码来自 [phaethix/inkstone](https://github.com/phaethix/inkstone)，保留原项目 MIT 协议和上游远程地址 `upstream`。本次基于提交 `c9f3beea13a1367a96fea16bcd0a0d18b2ca0bb6`，在 `feat/zh-cn-ui` 分支添加界面汉化。

发布仓库为 [SorvFlow/manhua-11-18](https://github.com/SorvFlow/manhua-11-18)，默认分支为 `主要的`。本地远程 `origin` 指向发布仓库，`upstream` 指向上游，便于 GitHub Desktop 推送及跟进更新。

源码沿用 [MIT 许可](LICENSE)。发布仓库初始化提交中的 Apache 2.0 文本原样保存在 [LICENSE-APACHE](LICENSE-APACHE)，作为初始化文件保留；本次没有将上游源码重新授权。

## 使用

当前预览地址：<http://127.0.0.1:8765>。仅本机访问。

页面默认显示中文。右上角 **English / 中文** 可即时切换，选择会保存在浏览器中，切换不会刷新页面或清空正文。可以查看首页、作品示例、漫画创作和方案对比。

在“漫画创作”中粘贴正文，或上传 `.txt` / `.md` 文件。程序按情节规划分格与漫画页，可生成多页漫画或竖版长图，并非一页小说固定对应一页漫画。

本机预览未配置模型接口，可以查看界面、示例及输入校验；实际生成需要在部署环境配置文本与图像服务。本次未执行模型生成，也尚未接入 Dify 或飞书。

创作区说明已更新为“粘贴小说正文，生成多张漫画页或竖版长图，打造引流内容，做你增加收益的好帮手。”右上角“API 状态”区分凭据已配置、未配置完整与服务未连接；悬停可查看说明。健康检查不请求模型，不等于实际模型连通性测试。

已实现应用账号登录、管理员开通/停用账号、用户作品/任务/日志隔离、持久化队列和独立生成进程。使用与能力边界见 [多账号与并发说明](docs/deployment/multi-user.zh-CN.md)，宝塔安装与旧版升级见 [部署教程](deploy/baota/README.md)。默认全站同时生成 1 个任务，其余排队。

## 重新启动

进入本目录后执行：

```bash
uv sync --extra dev --frozen
uv run inkstone accounts create-admin --username admin
INKSTONE_UI_PORT=8765 uv run inkstone web
```

然后用浏览器访问 <http://127.0.0.1:8765>。模型配置请参阅原版 `README.md` 和 `.env.example`；请在本机配置凭据，不要写入版本库。

## 汉化范围与升级维护

- `web/locales/zh-CN.js`：中文文案，以及包含页数、项目编号等动态内容的翻译规则。
- `web/i18n.js`：界面翻译、语言切换和语言记忆。
- `web/index.html`：加载翻译、账号脚本和当前账号的任务恢复。
- `web/accounts.js` / `accounts.css`：登录、账号管理、我的任务。
- `web/app.py` / `store.py` / `queue.py` / `worker.py`：认证、数据与任务隔离、持久化队列。
- `web/server.py`：保留原有管线辅助函数及兼容测试；默认 Web 启动器已切换到账号版。
- `pyproject.toml`：确保安装打包时包含翻译文件。

翻译层只更新界面文字和占位提示，不修改表单值、接口参数、小说正文、生成图片、方案正文或原始诊断日志。未收录的新文案保留英文，便于升级后补译。

升级前先提交或备份本地修改，然后在汉化分支合并上游：

```bash
git fetch upstream
git merge upstream/main
```

合并后检查首页、上传、生成进度、错误提示、审核、导出入口和中英切换；新增文案补到语言包。如果上游重写前端，需要重新适配翻译接入点。此版本没有自动更新机制。
