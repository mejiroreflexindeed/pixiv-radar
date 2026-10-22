# 脚本分类

保持脚本路径稳定，避免破坏 CMD、打包配置与回归用例中的调用。
在仓库根用 `backend/.venv/Scripts/python.exe scripts/<脚本名>` 运行，参数以 `--help` 为准。

| 分类 | 脚本 | 用途 |
| --- | --- | --- |
| 启动 | `run_local_preview.py` | 单服务离线预览，显式初始化演示库 |
| 启动 | `run_real_app.py` | 真实模式，需 `--enable-real`；可选低频调度 |
| 启动 | `desktop_launcher.py` | Windows GUI 启动面板与冻结 worker 调度 |
| 启动 | `launch_support.py` | 端口探测、服务启动与页面打开辅助 |
| 就绪 | `check_readiness.py` | 离线检查依赖、构建、浏览器和端口 |
| 数据 | `backup_formal.py` | 正式数据库只读备份，供根目录 CMD 调用 |
| 数据 | `backup_restore.py` | 显式备份/恢复命令，恢复写新目录 |
| 认证兼容 | `store_pixiv_token.py` | 高级手动凭据导入；普通登录用页面入口 |
| 离线样本 | `seed_e_demo.py`、`seed_demo_phase_c.py`、`seed_discovery_demo.py` | 当前/阶段演示与发现样本生成 |
| 离线样本 | `seed_feedback_events_fixture.py`、`run_broad_scan.py` | 反馈样本与 Mock 广域扫描 |
| 集成验证 | `demo_smoke_test.py` | 发现、分组、反馈和备份恢复闭环 |
| 集成验证 | `login_ready_smoke.py` | fake 授权/发现/正文/恢复，走生产调用链 |
| 浏览器验证 | `ui_login_ready_check.py` | 当前页面流程、多视口与图表交互 |
| 阶段浏览器回归 | `ui_demo_check.py`、`ui_e_check.py`、`ui_f_feedback_check.py`、`ui_h0_preview_check.py`、`ui_h0_feedback_flow_check.py` | 早期页面/反馈回归工具，部分要求先启动对应本地预览 |
| 真实试验 | `run_real_smoke.py` | 用户在场且已有凭据时才运行的低频真实试验 |
| 打包 | `build_release.py` | 使用 `packaging/` 配置装配 EXE 包与 ZIP |

新环境建议按[开发说明](../docs/DEVELOPMENT.md)安装，优先使用当前组合 smoke 与
`ui_login_ready_check.py`。Mock/fake 通过不代表真实上游行为已验证。
