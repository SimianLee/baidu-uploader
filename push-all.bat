@echo off
rem ============================================================
rem  push-all.bat —— 把 baidu-uploader 同时推送到三个远程仓库
rem
rem  用法：
rem    push-all.bat                  推送当前分支到 github / gitcode / gitee
rem    push-all.bat setup            只配置三个远程，不推送
rem    push-all.bat ssh              全部改用 SSH 地址推送
rem    push-all.bat commit "说明"    先 add+commit 再推送
rem
rem  首次运行会自动做初始化提交（git add -A + commit），因为新仓库没有 HEAD。
rem
rem  自动回退：某个库用 HTTPS 推送失败时，会自动换 SSH 地址再试一次。
rem  （github:443 常被代理挡成 502，gitcode 已移除密码认证只能 SSH，
rem    所以这两家最终都会走 SSH —— 需本机 SSH 公钥已添加到对应平台）
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
rem    - 脚本幂等：重复运行没副作用，远程地址与脚本不一致时自动纠正
rem    - 执行结束后会停住，提示「按任意键关闭窗口」，不会一闪而过
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

rem 首次连接 SSH 主机时自动接受 host key（但仍然拒绝密钥变更的主机）
set "GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new"

rem ---------------- 远程地址 ----------------
set "USE_SSH=0"
set "DO_COMMIT=0"

rem github
set "HTTPS_github=https://github.com/SimianLee/baidu-uploader.git"
set "SSH_github=git@github.com:SimianLee/baidu-uploader.git"
rem gitee
set "HTTPS_gitee=https://gitee.com/SimianLee/baidu-uploader.git"
set "SSH_gitee=git@gitee.com:SimianLee/baidu-uploader.git"
rem gitcode：该平台已移除密码认证，HTTPS 推送必报 HTTP Basic: Access denied，
rem 因此首选地址直接填 SSH（2026-09-20 实测）
set "HTTPS_gitcode=git@gitcode.com:SimianLee/baidu-uploader.git"
set "SSH_gitcode=git@gitcode.com:SimianLee/baidu-uploader.git"

set "URL_github=%HTTPS_github%"
set "URL_gitcode=%HTTPS_gitcode%"
set "URL_gitee=%HTTPS_gitee%"

if /i "%~1"=="ssh" set "USE_SSH=1"
if "%USE_SSH%"=="1" (
    set "URL_github=%SSH_github%"
    set "URL_gitcode=%SSH_gitcode%"
    set "URL_gitee=%SSH_gitee%"
)

if /i "%~1"=="commit" set "DO_COMMIT=1"
set "CMSG=%~2"
if not defined CMSG set "CMSG=update: 百度网盘分批上传工具"

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
git remote get-url github  >nul 2>&1 || git remote add github  "%URL_github%"
git remote get-url gitcode >nul 2>&1 || git remote add gitcode "%URL_gitcode%"
git remote get-url gitee   >nul 2>&1 || git remote add gitee   "%URL_gitee%"
git remote set-url github  "%URL_github%"  2>nul
git remote set-url gitcode "%URL_gitcode%" 2>nul
git remote set-url gitee   "%URL_gitee%"   2>nul

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

rem ---------------- 3) 依次推送（失败自动回退 SSH） ----------------
for %%r in (github gitcode gitee) do (
    set "T0=!TIME:~0,8!"
    set "T0=!T0: =0!"
    echo ============================================
    echo  开始推送 %%r（分支 %BRANCH%）
    echo ============================================
    >>"%PLOG%" echo.
    >>"%PLOG%" echo ---- [%%r] 开始 !T0! ----
    call :push_one %%r
    rem HTTPS 失败则换 SSH 地址再试一次
    if !RC! neq 0 (
        if not "!URL_%%r!"=="!SSH_%%r!" (
            echo  [重试] %%r 改用 SSH 地址再试一次...
            >>"%PLOG%" echo ---- [%%r] HTTPS 失败，改用 SSH 重试 ----
            git remote set-url %%r "!SSH_%%r!" 2>nul
            call :push_one %%r
        )
    )
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
    rem 恢复首选地址，保持脚本幂等
    git remote set-url %%r "!URL_%%r!" 2>nul
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
goto :end

rem ---------------- 子程序：推送单个远程，结果放 RC ----------------
:push_one
git push -u %1 %BRANCH% > "%TMPR%" 2>&1
set "RC=!errorlevel!"
type "%TMPR%"
type "%TMPR%" >> "%PLOG%"
exit /b 0

rem ---------------- 5) 结束 ----------------
:end
echo.
if defined FAIL (
    if not "%FAIL%"=="0" (
        echo  排错提示:
        echo    1. Permission denied ^(publickey^) -^> SSH 公钥没加到该平台，
        echo       把 ~/.ssh/id_rsa.pub 的内容粘贴到平台「SSH 公钥」设置里
        echo    2. github 报 502 / Empty reply -^> 代理挡住了 HTTPS，脚本已自动
        echo       回退 SSH；仍失败就直接：push-all.bat ssh
        echo    3. gitcode 报 HTTP Basic: Access denied -^> 该平台已移除密码认证，
        echo       只能 SSH 或用私人令牌 PAT（脚本默认已用 SSH）
        echo    4. non-fast-forward -^> 远端已有初始化文件，先
        echo       git pull --rebase gitee %BRANCH% 再重试
        echo    5. 脚本可重复运行，成功的库会跳过。
        echo.
    )
)
echo  按任意键关闭窗口...
pause >nul
endlocal & exit /b %FAIL%
