# 开发与安装

目前以 Windows 为主要验证环境。Python 3.10+、Node.js 18+；登录凭据使用 Windows
凭据管理器，桌面打包依赖 Windows / Tk / PyInstaller。

以下命令以仓库根目录为起点，不依赖固定盘符。

## 安装

```powershell
python -m venv backend/.venv
.\backend\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
cd frontend
npm ci
npm run build
cd ..
```

`requirements.txt` 是后端直接依赖；`requirements-dev.txt` 增加 pytest 和 httpx。
`requirements.lock` 保留既有 Windows 环境解析快照，不能当作跨平台依赖声明。
前端使用 `package-lock.json` 与 `npm ci` 固定解析结果。

不需要 `.env` 即可运行演示。若需覆盖设置，可参考 `backend/.env.example`；
配置前缀为 `RADAR_`，默认只监听回环地址。真实入口显式设置真实模式与授权门。

## 可选真实登录组件

在仓库根目录运行：

```powershell
.\backend\.venv\Scripts\python.exe -m pip install -r backend/requirements-auth.txt
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location).Path '.runtime/playwright'
$env:GPPT_CONFIG_DIR = Join-Path (Get-Location).Path '.runtime/gppt-empty'
.\backend\.venv\Scripts\python.exe -m playwright install chromium
```

`requirements-auth.lock` 保留经过核对的 Windows wheel 版本与哈希；基础安装不要求
这些组件。登录 worker 将 gppt 配置和浏览器路径约束到仓库 `.runtime`，不会自动下载
缺失的浏览器。`install-playwright` 的 Windows `EX_OK` 兼容处理只在 worker 内生效。

安装完成后，可先做离线就绪检查：

```powershell
.\backend\.venv\Scripts\python.exe scripts/check_readiness.py
```

它检查依赖、前端构建、浏览器和端口，不读取登录凭据，也不访问 Pixiv。

## 运行

```powershell
# 首次离线初始化；再次运行可用 CMD 入口复用演示库
.\backend\.venv\Scripts\python.exe scripts/run_local_preview.py --seed
# 真实模式：用户在场完成登录，页面按钮触发实际请求
.\backend\.venv\Scripts\python.exe scripts/run_real_app.py --enable-real
# 可选每 30 分钟低频发现，默认关闭
.\backend\.venv\Scripts\python.exe scripts/run_real_app.py --enable-real --schedule-minutes 30 --tag ウマ娘
```

定时范围 15–1440 分钟，先等待完整周期；忙碌、未连接、暂停或冷却时跳过。
端口被占用时退出，可用 `--port` 指定其他端口。

前后端分开开发时，在两个终端运行：

```powershell
# 终端一：后端默认 demo 模式，下面显式指定隔离开发库
$env:RADAR_DB_PATH = Join-Path (Get-Location).Path 'data/dev/radar.db'
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 终端二（从仓库根开始）：Vite 已代理 /api
cd frontend
npm run dev
```

API 文档为 `http://127.0.0.1:8000/docs`，开发页面为 `http://127.0.0.1:5173`。

## 验证

完整后端套件包含 gppt/Chromium 的离线能力自检，因此先完成上方可选组件安装，
并构建前端。测试不会执行真实登录。当前测试环境以 Windows 为准。

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
cd ../frontend
npm test
npm exec -- vitest run --config vitest.codex-review.config.ts
npm run typecheck
npm run build
cd ..
.\backend\.venv\Scripts\python.exe scripts/demo_smoke_test.py
.\backend\.venv\Scripts\python.exe scripts/login_ready_smoke.py
```

正式测试和组合 smoke 使用 Mock / fake transport、隔离数据库与内存凭据存储，
其通过不代表真实 Pixiv 验收完成。smoke 默认要求临时目录尚不存在，并在结束后清理。

可选浏览器回归需要上方 Playwright 与 Chromium；它只操作本地演示：

```powershell
.\backend\.venv\Scripts\python.exe scripts/ui_login_ready_check.py
```

真实账号操作由用户现场完成，按 [UAT 模板](USER_UAT_RECORD_TEMPLATE.md)记录结果。

## Windows EXE 打包

需要已构建的 `frontend/dist`、真实登录组件和 `.runtime/playwright` 下的 Chromium。
另在项目虚拟环境安装 PyInstaller 6，然后运行：

```powershell
.\backend\.venv\Scripts\python.exe -m pip install 'pyinstaller>=6,<7'
.\backend\.venv\Scripts\python.exe scripts/build_release.py --help
.\backend\.venv\Scripts\python.exe scripts/build_release.py
```

产物在 `release/`，该目录整体不进入 Git。启动器为
`scripts/desktop_launcher.py`，打包配置与用户说明在 `packaging/`。
分发前检查包内没有个人数据、凭据或 `.env`，并实际验证解压运行与登录 worker。

## 提交内容

提交源码、正式测试、测试固定样本、锁文件、打包配置与文档。
`.gitignore` 排除 `data/`、`.runtime/`、`.venv/`、`node_modules/`、`frontend/dist/`、
`release/`、本机 Kilo 配置、`.env`、缓存与历史验收产物。
根目录 CMD 通过 `.gitattributes` 使用 CRLF，其余文本默认 LF。
