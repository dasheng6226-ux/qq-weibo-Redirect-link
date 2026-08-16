@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 "qq_wb2(2)(4).py"
if errorlevel 1 (
  echo.
  echo 启动失败。请把本窗口中的错误文字或 startup_error.log 发给我。
  pause
)
