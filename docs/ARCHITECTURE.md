# 架构与源码导航

本地单用户应用由浏览器 WebUI、FastAPI 服务和 SQLite 组成。构建后的前端可以由
同一个 FastAPI 进程提供；开发时使用 Vite 的 `/api` 代理。

## 后端模块

| 路径（`backend/app/` 下） | 职责 |
| --- | --- |
| `main.py` | 应用组装、迁移和调度/发现线程生命周期 |
| `api/routes/` | 健康状态、认证、发现、目标、仪表盘、正文、事件和反馈 API |
| `auth/` | 授权状态机、受监督浏览器 provider、隔离 worker 及运行时路径 |
| `core/` | 设置、时间、日志脱敏、固定凭据项与惰性凭据存储 |
| `pixiv/` | 上游适配器、异常分类、共享请求门和真实运行时 |
| `discovery/` | 查询预算、候选合并/分组/排序、发现服务和单运行编排 |
| `domain/` | 广域扫描、扫描协调与指标写入调用链 |
| `scoring/` | 指标、EWMA/门限参数、cold_start/broad/track 状态机纯函数 |
| `content/` | 正文轻量分析、缓存调用链与字符 n-gram TF-IDF 相似检索 |
| `scheduler/` | 默认关闭的进程内定时发现 |
| `storage/` | SQLite 连接、v1–v6 迁移、repository、备份与恢复 |

## 主要调用流

1. 页面显式启动发现，`DiscoveryRunner` 保证同一时间最多一个运行。
2. `DiscoveryService` 根据预算调用适配器，合并作品与来源记录。
3. 元数据进入指标快照、评分和状态机，在短事务中持久化。
4. API 从本地数据库提供候选、作品、曲线和事件；反馈另行持久化并影响候选解释。
5. 用户选择 `track` 目标并点击分析时，正文服务读取缓存或抓取单篇、计算特征，
   返回特征和本地相似结果；网络请求和计算不持有 SQLite 写事务。

演示使用固定 Mock 适配器。真实模式由 `run_real_app.py --enable-real` 开启；
初始化和普通状态读取不自动登录或扫描。认证、发现和正文共用请求门，控制并发、
间隔、暂停和限流冷却。401/403/429 与上游挑战映射为受控错误。

## 发现和评分

真实默认计划只对同一标签做三次搜索；通用预算上限为 8 请求、每请求 30 条、
每轮 200 个唯一候选。精确参数和冻结行为以 `discovery/` 与正式测试为准。

评分参数在 `scoring/params.py`：曝光门槛、最少观察次数、平滑先验、EWMA、
稳健门限和晋级/降级连续次数。`unknown` 不清空晋降级计数，避免缺失数据制造状态跳转。
这些信号描述增长和筛选条件，不推断文学质量。

## 前端

`frontend/src/App.tsx` 管理目标选择、列表和刷新；`components/AppShell.tsx`
组织发现、我的作品、运行与设置视图。`api.ts` 定义请求和类型，`radarMapping.ts`
定义确定性坐标映射，`echartsSetup.ts` 管理图表加载。

组件测试在 `src/`，额外的并发、恢复和交互回归在 `frontend/review/`，使用独立
Vitest 配置运行。源码中的旧阶段契约名为历史注释；当前实现与测试是保留的行为基线。

## 本地文件

| 位置 | 内容 | Git |
| --- | --- | --- |
| `data/state/radar.db` | 正式数据库 | 忽略 |
| `data/local-preview/radar.db` | 当前离线演示库 | 忽略 |
| `data/backups/` | 用户手动备份 | 忽略 |
| `.runtime/` | Chromium、gppt 隔离配置与运行时缓存 | 忽略 |
| Windows 凭据管理器 | 固定项目项中的 refresh token | 不在仓库内 |
| `frontend/dist/`、`release/` | 构建和发布产物 | 忽略 |
| `backend/tests/fixtures/` | 人工固定测试样本 | 提交 |

备份校验 schema 和完整性；恢复只写入显式指定且不存在的新目录。
部署范围是本机回环服务，尚未提供多用户或公网服务方案。
