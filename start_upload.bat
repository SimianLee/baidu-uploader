@echo off
title Baidu Pan Uploader
rem Double-click to start. Close window = pause. Re-run = resume.
cd /d "%~dp0"
set "PYTHONPATH=%~dp0libs"
"C:\Users\UserData\python313\python.exe" -X utf8 upload_baidu.py
echo.
echo Upload process ended (finished / interrupted / error).
echo Progress is saved. Double-click this file again to resume.
pause
