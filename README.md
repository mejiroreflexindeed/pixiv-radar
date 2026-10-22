# Pixiv 小说雷达

一个本地运行的 Pixiv 小说发现工具，主要用于追踪小众标签下容易被热度排序淹没的作品。
输入标签后低频采集指标，保留作品快照、增长信号和个人反馈，帮助你整理值得继续看的小说。

技术栈：FastAPI + React / TypeScript + SQLite。WebUI 只监听 `127.0.0.1`。

> 当前为开发测试版本。离线演示和 fake 集成测试可独立运行；真实登录与上游行为仍需
> 用户在场验证。完整状态见[当前交接](docs/CURRENT_IMPLEMENTATION_HANDOFF.md)。

## 功能

- 按标签执行精确标签、部分标签、关键词三次低频只读搜索。
- 候选按“小众新发现 / 稳定追踪 / 热门参考 / 待观察”分组，并解释出现原因。
- 保存指标快照，依据增长率、EWMA 和动态门限追踪冷启动、广域与追尾状态。
- 作品列表支持筛选、排序、分页，提供历史曲线、雷达图和热力图。
- 标记好文、排除和撤销反馈，反馈保存在本地。
- 对进入 `track` 的作品按篇抓取并缓存正文特征，显示本地 TF-IDF 文本相似结果。
- 支持手动备份与恢复到新目录，以及默认关闭的进程内定时发现。
- Windows 受监督浏览器登录与可选 EXE 启动面板。

结构分、增长信号和文本相似度用于辅助筛选，不代表文学质量。页面与 API 不返回正文原文。

## 从源码启动（Windows）

需要 Python 3.10+、Node.js 18+ 和 Git。以下命令在 PowerShell 中执行。

```powershell
git clone https://github.com/mejiroreflexindeed/pixiv-radar.git
cd pixiv-radar
python -m venv backend/.venv
.\backend\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
cd frontend
npm ci
npm run build
cd ..
```

安装完成后，双击 `查看离线演示.cmd`，或执行：

```powershell
.\backend\.venv\Scripts\python.exe scripts/run_local_preview.py --seed
```

浏览器打开 `http://127.0.0.1:8000`。演示使用 Mock 数据，无需 Pixiv 账号。
`--seed` 只用于首次初始化；已有演示库时可直接双击演示入口。

真实登录需要额外安装受监督浏览器组件，见[开发与安装说明](docs/DEVELOPMENT.md#可选真实登录组件)。
准备完成后双击 `启动 Pixiv 小说雷达.cmd`，在页面上登录并点击“运行发现/扫描”。
账号、密码和验证码由你本人在 Pixiv 官方页面输入，应用不保存密码。

## 使用和数据

| 用途 | 入口 |
| --- | --- |
| 离线演示 | `查看离线演示.cmd` |
| 真实模式 | `启动 Pixiv 小说雷达.cmd` |
| 备份正式数据 | `备份我的数据.cmd` |

根目录的 CMD 入口使用已经安装好的 `backend/.venv` 和 `frontend/dist`。
关闭对应命令行窗口或按 `Ctrl+C` 停止；EXE 面板通过“停止并退出”结束。

正式库为 `data/state/radar.db`，演示库为 `data/local-preview/radar.db`。
登录凭据保存在 Windows 凭据管理器，不在数据库备份中。
操作步骤与恢复命令见[用户指南](docs/USER_TEST_GUIDE.md)。

每轮真实发现只做同标签三次低频只读搜索，不执行收藏、关注、评论或发布。
遇到 401/403/429、挑战页面或异常响应时停止相关操作，不连续重试。

## 源码分类

```text
backend/
  app/           API、认证、发现、评分、正文分析和存储模块
  tests/         后端离线单元/集成测试与固定样本
frontend/
  src/           页面、组件、图表、API 客户端与正式测试
  review/        交互与恢复行为的独立回归用例
docs/            安装开发、架构、用户指南和当前状态
scripts/         启动、数据工具、演示/验收和发布脚本
packaging/       PyInstaller 配置与分发包使用说明
```

[文档索引](docs/README.md) · [脚本分类](scripts/README.md) · [架构说明](docs/ARCHITECTURE.md)

仓库只收录源码、测试与文档。数据库、凭据、本机配置、依赖目录、浏览器运行时、
前端构建和发布 ZIP 均由 `.gitignore` 排除。本机历史测试包未上传为 GitHub Release。

## 开发验证

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
cd ../frontend
npm test
npm exec -- vitest run --config vitest.codex-review.config.ts
npm run typecheck
npm run build
```

集成烟测与浏览器检查见[开发说明](docs/DEVELOPMENT.md#验证)。

## 第三方来源与许可

第三方代码来源及原许可证见 [LICENSE-NOTICE.md](LICENSE-NOTICE.md)。
本项目尚未单独指定项目许可证；第三方 MIT 声明仅适用于对应来源。
