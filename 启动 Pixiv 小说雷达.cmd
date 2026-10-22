@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Pixiv 小说雷达（真实模式）
echo ============================================
echo   Pixiv 小说雷达 - 真实模式
echo ============================================
echo 数据位置：%~dp0data\state\radar.db
echo 登录与发现只在你点击网页按钮后触发；
echo 关闭本窗口即停止服务。
echo.
"%~dp0backend\.venv\Scripts\python.exe" "%~dp0scripts\run_real_app.py" --enable-real %*
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" (
  echo.
  echo 启动失败（退出码 %EXITCODE%）。请查看上方提示，或运行：
  echo   backend\.venv\Scripts\python.exe scripts\check_readiness.py
  pause
)
exit /b %EXITCODE%
