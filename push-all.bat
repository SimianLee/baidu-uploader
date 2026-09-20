@echo off
rem ============================================================
rem  push-all.bat —— 把 baidu-uploader 同时推送到三个远程仓库
rem
rem  用法：
rem    push-all.bat                  推送当前分支到 github / gitcode / gitee
rem    push-all.bat setup            只配置三个远程，不推送
rem    push-all.bat ssh              改用 SSH 地址推送（HTTPS 卡住时用）
rem    push-all.bat commit "说明"    先 add+commit 再推送
rem
rem  首次运行会自动做初始化提交（git add -A + commit），因为新仓库没有 HEAD。
rem
rem  日志：
rem    push-logs\push-<时间戳>.log   本次完整日志（git 原始输出 + 成败结论）
rem    push-logs\history.log         历次推送一行摘要（追加式）
rem    两个日志都是 UTF-8 带 BOM，记事本 / Excel / PowerShell 打开中文均不乱码
rem
rem  安全：
rem    推送前会检查 config.json / token.json / libs 是否被 git 跟踪，
rem    命中则立即中止 —— 这两个文件含 SecretKey 和授权 Token，绝不能入库。
rem
rem  说明：
rem    - 默认走 HTTPS（三平台地址由用户指定）；若某平台 HTTPS 推送卡死或
rem      报认证失败，改用 push-all.bat ssh（需本机 SSH 公钥已加到对应平台）
rem    - 脚本幂等：重复运行没副作用，远程地址与脚本不一致时自动纠正
rem    - 执行结束后会停住，提示「按任意键关闭窗口」，不会一闪而过
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

rem ---------------- 远程地址 ----------------
set "USE_SSH=0"
set "DO_COMMIT=0"

rem HTTPS（默认）
set "GITHUB_URL=https://github.com/SimianLee/baidu-uploader.git"
set "GITCODE_URL=https://gitcode.com/SimianLee/baidu-uploader.git"
set "GITEE_URL=https://gitee.com/SimianLee/baidu-uploader.git"

rem SSH 备选（https 不通时启用：gitcode 禁用密码认证、github:443 常被墙、
rem gitee HTTPS 曾出现长时间无响应）—— 由下面的 USE_SSH 开关决定

rem ---------------- 解析参数 ----------------
if /i "%~1"=="ssh"    set "USE_SSH=1"
if /i "%~1"=="commit" set "DO_COMMIT=1"
set "CMSG=%~2"
if not defined CMSG set "CMSG=update: 百度网盘分批上传工具"

if "%USE_SSH%"=="1" (
    set "GITHUB_URL=git@github.com:SimianLee/baidu-uploader.git"
    set "GITCODE_URL=git@gitcode.com:SimianLee/baidu-uploader.git"
    set "GITEE_URL=git@gitee.com:SimianLee/baidu-uploader.git"
)

rem ---------------- 0) 准备日志目录与时间戳 ----------------
if not exist "push-logs" mkdir "push-logs"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%i"
set "PLOG=push-logs\push-%STAMP%.log"
set "HIST=push-logs\history.log"
set "TMPR=push-logs\.tmp-last.log"
set "NOW=%STAMP:~0,4%-%STAMP:~4,2%-%STAMP:~6,2% %STAMP:~9,2%:%STAMP:~11,2%:%STAMP:~13,2%"

if not exist "%PLOG%" powershell -NoProfile -Command "[IO.File]::WriteAllText('%PLOG%','',(New-Object System.Text.UTF8Encoding $true))" >nul 2>&1
if not exist "%HIST%" powershell -NoProfile -Command "[IO.File]::WriteAllText('%HIST%','',(New-Object System.Text.UTF8Encoding $true))" >nul 2>&1

echo.
echo  baidu-uploader 三库推送
echo  时间: %NOW%
echo.

rem ---------------- 0.5) 安全自检：敏感文件不得入库 ----------------
git ls-files | findstr /i /c:"config.json" /c:"token.json" /c:"libs/" /c:"uploaded_log" >nul 2>&1
if not errorlevel 1 (
    echo  [中止] 检测到敏感/无关文件已被 git 跟踪，拒绝推送：
    echo.
    git ls-files | findstr /i /c:"config.json" /c:"token.json" /c:"libs/" /c:"uploaded_log"
    echo.
    echo  处理办法（以 config.json 为例）：
    echo    git rm --cached config.json
    echo  然后确认 .gitignore 里已有该文件名，再重新运行本脚本。
    echo  注意：若已推过历史，仅删除不够，还需去开放平台重置 SecretKey。
    echo.
    echo  按任意键关闭窗口...
    pause >nul
    endlocal & exit /b 9
)

rem ---------------- 1) 幂等配置三个远程 ----------------
git remote get-url github  >nul 2>&1 || git remote add github  "%GITHUB_URL%"
git remote get-url gitcode >nul 2>&1 || git remote add gitcode "%GITCODE_URL%"
git remote get-url gitee   >nul 2>&1 || git remote add gitee   "%GITEE_URL%"
git remote set-url github  "%GITHUB_URL%"  2>nul
git remote set-url gitcode "%GITCODE_URL%" 2>nul
git remote set-url gitee   "%GITEE_URL%"   2>nul

rem ---------------- 1.5) 提交处理 ----------------
git rev-parse HEAD >nul 2>&1
if errorlevel 1 (
    echo  [提示] 仓库还没有任何提交，执行初始化提交...
    set "DO_COMMIT=1"
    set "CMSG=init: 百度网盘分批上传工具（网页面板 + 分批限速 + 断点续传）"
)

if "%DO_COMMIT%"=="1" (
    git add -A
    git commit -m "%CMSG%" > "%TMPR%" 2>&1
    set "RC=!errorlevel!"
    type "%TMPR%"
    if !RC! neq 0 (
        echo  [中止] 提交失败（exit=!RC!）。
        if exist "%TMPR%" del "%TMPR%" >nul 2>&1
        echo  按任意键关闭窗口...
        pause >nul
        endlocal & exit /b !RC!
    )
    if exist "%TMPR%" del "%TMPR%" >nul 2>&1
    echo  [OK] 已提交：%CMSG%
    echo.
) else (
    git status --porcelain | findstr /r "." >nul 2>&1
    if not errorlevel 1 (
        echo  [提示] 有未提交的改动，本次推送不包含它们。
        echo        要一起提交请运行：push-all.bat commit "说明"
        echo.
    )
)

for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%b"
for /f "delims=" %%c in ('git rev-parse --short HEAD') do set "COMMIT=%%c"

rem ---------------- 2) 写日志头 ----------------
>>"%PLOG%" echo ============================================
>>"%PLOG%" echo  baidu-uploader 三库推送日志
>>"%PLOG%" echo  时间: %NOW%
>>"%PLOG%" echo  分支: %BRANCH%    提交: %COMMIT%
>>"%PLOG%" echo ============================================

echo 当前分支: %BRANCH%   提交: %COMMIT%
echo 本次日志: %PLOG%
echo 远程列表:
git remote -v
echo.

set /a FAIL=0
set /a OK=0

if /i "%~1"=="setup" (
    echo [setup] 三个远程已就绪，未执行推送。
    goto :end
)

rem ---------------- 3) 依次推送到三个远程 ----------------
for %%r in (github gitcode gitee) do (
    set "T0=!TIME:~0,8!"
    set "T0=!T0: =0!"
    echo ============================================
    echo  开始推送 %%r（分支 %BRANCH%）
    echo ============================================
    >>"%PLOG%" echo.
    >>"%PLOG%" echo ---- [%%r] 开始 !T0! ----
    git push -u %%r %BRANCH% > "!TMPR!" 2>&1
    set "RC=!errorlevel!"
    type "!TMPR!"
    type "!TMPR!" >> "%PLOG%"
    if !RC! equ 0 (
        echo  [成功] %%r 已推送。
        >>"%PLOG%" echo ---- [%%r] 结果: 成功 ----
        set "R_%%r=成功"
        set /a OK+=1
    ) else (
        echo  [失败] %%r 推送失败（exit=!RC!），详情见日志。
        >>"%PLOG%" echo ---- [%%r] 结果: 失败 exit=!RC! ----
        set "R_%%r=失败"
        set /a FAIL+=1
    )
    echo.
)
if exist "%TMPR%" del "%TMPR%" >nul 2>&1

rem ---------------- 4) 汇总 + 历史记录 ----------------
>>"%PLOG%" echo ============================================
>>"%PLOG%" echo  汇总: 成功 %OK% / 失败 %FAIL%（github=%R_github% gitcode=%R_gitcode% gitee=%R_gitee%）
>>"%PLOG%" echo ============================================

>>"%HIST%" echo [%NOW%] 分支=%BRANCH% 提交=%COMMIT% 结果: github=%R_github% gitcode=%R_gitcode% gitee=%R_gitee%（详见 %PLOG%）

echo ============================================
if %FAIL% equ 0 (
    echo  全部完成：3 个仓库都推送成功。 ^(成功 %OK%^)
) else (
    echo  推送结束：成功 %OK%，失败 %FAIL%，见上方报错。
)
echo  完整日志: %PLOG%
echo  历史记录:
powershell -NoProfile -Command "Get-Content '%HIST%' -Tail 5 -Encoding UTF8"
echo ============================================

rem ---------------- 5) 结束 ----------------
:end
echo.
if defined FAIL (
    if not "%FAIL%"=="0" (
        echo  排错提示:
        echo    1. 认证失败 / 卡住不动  -^> 改用 SSH：push-all.bat ssh
        echo       （需本机 SSH 公钥已添加到 github / gitcode / gitee 账号）
        echo    2. 提示 non-fast-forward -^> 远端已有初始化文件，先执行：
        echo       git pull --rebase gitee %BRANCH% 再重试（或确认无需保留后强推）
        echo    3. 远端仓库不存在        -^> 先到平台建名为 baidu-uploader 的空仓库
        echo    4. 脚本可重复运行，成功的库会跳过。
        echo.
    )
)
echo  按任意键关闭窗口...
pause >nul
endlocal & exit /b %FAIL%
