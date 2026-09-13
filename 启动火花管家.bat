@echo off
chcp 65001 >nul
title 火花管家 Pro
cd /d %~dp0

for /f "tokens=1,* delims==" %%a in ('findstr /b "AUTH_TOKEN=" .env') do set "TOKEN=%%b"

echo ============================================
echo   火花管家 Pro  http://127.0.0.1:8020
echo   浏览器将自动打开控制台（令牌已自动带上）
echo   请保持本窗口开启（关窗口 = 停止服务）
echo ============================================

:: 服务起来后自动打开浏览器（令牌经 URL 一次性传入，前端会立即从地址栏清除）
start "" cmd /c "timeout /t 5 /nobreak >nul & start http://127.0.0.1:8020/?token=%TOKEN%"
.venv\Scripts\python.exe app.py
pause
