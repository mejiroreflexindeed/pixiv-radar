@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Pixiv 小说雷达 - 备份我的数据
echo ============================================
echo   只读备份正式数据库到 data\backups
echo ============================================
echo 本操作由你手动运行，只读取 data\state\radar.db；
echo.
echo 不会修改、迁移或替换原库；备份不含账号登录信息。
echo.
"%~dp0backend\.venv\Scripts\python.exe" "%~dp0scripts\backup_formal.py" %*
set EXITCODE=%ERRORLEVEL%
echo.
if "%EXITCODE%"=="0" (
  echo 备份完成：成功路径见上方 target 字段。
) else (
  echo 备份失败（退出码 %EXITCODE%）：请查看上方 code 字段。
  echo.
  echo 常见原因：尚未运行过真实模式（源库不存在）、目标已存在、路径越界。
)
pause
exit /b %EXITCODE%
