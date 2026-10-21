@echo off
title Baidu Uploader - Web Panel
rem 代码全收在 core\ 下。cd 进去是为了让相对路径以 core 为基准——配置、
rem 断点和 data 目录跟着 core 走，整目录挪位置也不会散架。
cd /d "%~dp0core"
set "PYTHONPATH=%~dp0core\libs"
"C:\Users\UserData\python313\python.exe" -X utf8 webui.py
pause
