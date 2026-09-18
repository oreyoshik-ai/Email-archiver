@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
cd /d "%~dp0"
echo.
echo 正在打开邮件归档页面，请不要关闭本窗口。
echo 浏览器没有自动弹出时，请打开: http://127.0.0.1:8765/
echo.
python -m mail_archiver
echo.
pause
