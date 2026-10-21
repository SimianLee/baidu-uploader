@echo off
title Baidu Pan Uploader
rem Double-click to start. Close window = pause. Re-run = resume.
rem Code lives in core\; cd there so config / checkpoint / data follow it.
cd /d "%~dp0core"
set "PYTHONPATH=%~dp0core\libs"
"C:\Users\UserData\python313\python.exe" -X utf8 upload_baidu.py
echo.
echo Upload process ended (finished / interrupted / error).
echo Progress is saved. Double-click this file again to resume.
pause
