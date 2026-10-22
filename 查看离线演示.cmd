@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Pixiv 小说雷达（离线演示 / Mock）
set "DEMODB=%~dp0data\local-preview\radar.db"
set "SEEDFLAG="
if not exist "%DEMODB%" (
  echo 首次运行：正在初始化离线演示数据（Mock，不连接 Pixiv）...
  set "SEEDFLAG=--seed"
) else (
  echo 使用已有离线演示数据，不会重新初始化。
)
echo ============================================
echo   Pixiv 小说雷达 - 离线演示（Mock 数据）
echo ============================================
echo 数据位置：%DEMODB%
echo 关闭本窗口即停止服务。
echo.
"%~dp0backend\.venv\Scripts\python.exe" "%~dp0scripts\run_local_preview.py" --db-path "%DEMODB%" %SEEDFLAG% %*
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" (
  echo.
  echo 启动失败（退出码 %EXITCODE%）。请查看上方提示，或运行：
  echo   backend\.venv\Scripts\python.exe scripts\check_readiness.py --no-browser-required
  pause
)
exit /b %EXITCODE%
