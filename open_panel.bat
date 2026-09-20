@echo off
title Baidu Uploader - Web Panel
cd /d "%~dp0"
set "PYTHONPATH=%~dp0libs"
"C:\Users\UserData\python313\python.exe" -X utf8 webui.py
pause
