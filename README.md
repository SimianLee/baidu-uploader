# 百度网盘分批上传工具（非会员友好版）

Win10 命令行 + 网页可视化面板：把本地目录的文件**分批**上传到百度网盘指定目录，专门针对非会员的上传数量限制做了批处理；超过大小上限的文件自动跳过防失败；上传成功的文件可**手动确认**后移动到"已完成"目录或送入回收站。

## 目录结构

```
baidu_uploader/
├── README.md              # 本说明
├── requirements.txt       # 依赖（requests / send2trash）
├── .gitignore             # 已排除密钥、Token、依赖、日志
├── config.example.json    # 配置模板（复制成 config.json 后填写）
├── upload_baidu.py        # 上传主程序（命令行入口）
├── webui.py               # 网页控制面板后端（只监听 127.0.0.1）
├── webui.html             # 网页控制面板页面
├── open_panel.bat         # 双击打开控制面板
├── start_upload.bat       # 双击直接命令行全量上传
├── libs/                  # 依赖安装目录（gitignore 排除，不入库）
├── config.json            # 你的配置（gitignore 排除，含密钥）
└── token.json             # 授权 Token（gitignore 排除，切勿外传）
```

> ⚠️ **安全提醒**：`config.json` 里有你的 SecretKey，`token.json` 里有百度账号授权凭证。这两个文件已在 `.gitignore` 中排除，**请勿手动 add 或提交到公开仓库**。

## 一、前置准备（一次性，约 10 分钟）

本工具走**百度网盘开放平台**官方 API（`pan.baidu.com/union`），需要免费注册个应用拿密钥：

1. 打开 https://pan.baidu.com/union/main/all ，用你的百度账号登录，申请成为**个人开发者**（免费）。
2. 控制台里「创建应用」：
   - 应用类型选 **个人应用**（个人自用足够）
   - 记下 **AppKey** 和 **SecretKey**
3. 把 `config.example.json` 复制一份改名为 `config.json`，填入这两个值。

> 个人应用有官方免费的 API 调用配额，日常备份文件完全够用。若某天提示配额/频控（errno=31034），把 `batch_pause_sec` 调大即可。

## 二、安装

```bat
pip install -r requirements.txt
```

（Python 3.8+，Win10 自带的终端 cmd/PowerShell 都能跑）

## 三、使用

### 1. 首次登录（二选一）

**方式 A：授权码模式（推荐，不限时）**

浏览器打开下面网址（AppKey 替换成自己的），登录并确认授权，页面会显示一串授权码：

```
https://openapi.baidu.com/oauth/2.0/authorize?response_type=code&client_id=你的AppKey&redirect_uri=oob&scope=basic,netdisk
```

```bat
python upload_baidu.py --login-code
```

按提示粘贴授权码即可。授权码约 10 分钟有效、只能用一次。

**方式 B：设备码扫码（限时 5 分钟）**

```bat
python upload_baidu.py --login
```

按提示打开 https://openapi.baidu.com/device ，输入终端显示的验证码，手机扫码确认。超时或失败就换方式 A。

两种方式 Token 都存在 `token.json`，之后自动续期，无需重复登录。

### 2. 先干跑看看计划（不上传，强烈建议第一次先跑这个）

```bat
python upload_baidu.py --dry-run
```

输出：哪些文件上传、哪些因超大被跳过、分成几批、每批多少个。

### 3. 正式上传

```bat
python upload_baidu.py
```

### 4. 其他参数

| 参数 | 作用 |
|---|---|
| `--batch-size 30` | 临时改每批文件数（默认取配置里的 500） |
| `--limit 5` | 本次只传前 5 个（小规模试水） |
| `--yes` | 跳过手动确认（`after_upload` 为 ask 时视作保留原处） |
| `--config xx.json` | 指定别的配置文件 |

## 四、配置项说明（config.json）

| 配置项 | 推荐值 | 说明 |
|---|---|---|
| `app_key` / `secret_key` | - | 开放平台应用密钥，必填 |
| `local_dir` | - | 本地待上传目录，必填 |
| `remote_dir` | `/apps/baidu_uploader` | 网盘目标目录（不存在会自动创建，可改成如 `/我的备份`） |
| `batch_size` | 500 | **每批文件数**——非会员限制的核心对策，海量小文件用 500；被限制就调小到 50~100 |
| `batch_pause_sec` | 5 | 批与批之间暂停秒数，模拟"手动分次上传" |
| `file_interval_sec` | 0.2 | 单个文件间隔，降低频控风险 |
| `max_file_size_mb` | 4096 | **大文件上限**，超过的直接跳过并列出清单，防止传一半失败浪费时间 |
| `chunk_size_mb` | 4 | 分片大小，保持 4（官方标准）别改 |
| `recursive` | true | 是否递归子目录（远程保持同样目录结构） |
| `done_dir` | 空 | 上传成功后文件的归档目录；留空则自动取「本地目录同级/已上传」 |
| `after_upload` | keep | 成功后处理：`ask` 每次手动确认（仅命令行）/ `move` 移动 / `trash` 进回收站 / `keep` 不动 |
| `exclude_patterns` | 常见垃圾文件 | 通配符排除规则 |

> `batch_size: 500 / batch_pause_sec: 5 / file_interval_sec: 0.2` 这组参数是在 9.9 万个小文件、21GB 的真实场景下实测调优出来的，一般无需修改。

## 五、"手动确认"是怎么工作的

默认 `after_upload: "ask"`：所有批次传完后，终端列出成功清单，问你：

```
如何处理这些本地文件？
  m = 移动到已完成目录 (done_dir)
  t = 放入回收站（可找回）
  k = 保留在原处不动
请选择 [m/t/k]:
```

- 选 **m**：移动到 `done_dir`，重名文件自动加 `(1)`、`(2)` 后缀，绝不覆盖；
- 选 **t**：用 `send2trash` 送入 **Windows 回收站**，后悔了还能还原；
- 选 **k**：什么都不动。

想全自动就把 `after_upload` 改成 `move` 或 `trash`。

## 六、可靠性设计

- **断点续传**：每成功一个文件立刻写入 `uploaded_log.json`，中途中断（Ctrl+C / 断网 / 蓝屏 😉）后重跑自动跳过已成功的；
- **秒传**：网盘已有相同内容（MD5 分片一致）时直接秒完成，不耗流量；
- **分片重试**：4MB 分片上传失败自动重试 3 次（指数退避）；
- **失败重跑**：失败的文件下次运行自动重试；
- **Token 自动刷新**：过期自动用 refresh_token 续期。

## 七、常见错误码

| errno | 含义 | 处理 |
|---|---|---|
| -6 / 111 | Token 无效/过期 | 重新 `--login`（自动刷新失败时） |
| 31034 | 命中频控 | 调大 `batch_pause_sec` 和 `file_interval_sec` |
| 31061 | 网盘已有同名同内容文件 | 无害，已按覆盖策略处理 |
| 31064 | 文件违规被拒 | 换文件或改文件名 |

## 八、典型工作流（比如备份照片目录）

```bat
python upload_baidu.py --login
python upload_baidu.py --dry-run
python upload_baidu.py --limit 3        :: 先传 3 个试试水
python upload_baidu.py                  :: 确认没问题，全量开跑
```

## 九、海量小文件实测备注（本次 9.9 万个文件调优经验）

- **`return_type` 语义**：precreate 返回 `return_type=2` 才是秒传；`return_type=1` 表示需要按顶层 `block_list` 指定的分片序号真实上传（实测确认，2026-09-20）
- **目录创建缓存**：同一远程目录只请求一次 mkdir，9.9 万文件不会重复建目录
- **断点记录**：`uploaded_log.txt` 追加写（一行一个路径），海量文件下不会越写越慢
- **频控退避**：遇 errno=31034 自动退避 30s/60s 重试
- **推荐参数**（海量小文件）：`batch_size: 500, batch_pause_sec: 5, file_interval_sec: 0.2`
- **长时间任务**：双击 `start_upload.bat` 开跑，关闭窗口即暂停，再次双击断点续传；蓝屏/断网/重启都不丢进度

## 十、网页控制面板（推荐入口）

**双击 `open_panel.bat`**，浏览器自动打开 `http://127.0.0.1:8765/` 控制面板，可视化完成：

- **参数设置**：AppKey/SecretKey、本地目录、网盘目录、每批数量、批间暂停、文件间隔、大小上限
- **上传范围**：仅当前目录文件 / 含所有子目录
- **上传成功后处理**：保留原处 / 移动到指定目录 / 送入回收站（三选一）
- **百度授权**：一键打开授权页 → 粘贴授权码 → 验证登录
- **上传控制**：干跑预览、开始（可设试传数量）、停止
- **实时进度**：进度条 + 成功/失败统计 + 当前文件 + 滚动日志（每 2.5 秒刷新）

说明：
- 面板只监听本机 127.0.0.1，外部无法访问
- **关闭面板/页面不影响正在进行的上传**，上传是独立后台进程
- 面板临时实例（如由 AI 会话代起的）超时会退出，日常用 `open_panel.bat` 自己起即可
- 不想用面板的话，命令行方式照旧：`start_upload.bat` 或 `python upload_baidu.py`

## 十一、命令行入口

`start_upload.bat`：双击即按 `config.json` 全量上传；跑完停在窗口等确认。

## 十二、上传到 Git

项目已配好 `.gitignore`，密钥、Token、依赖、日志都不会入库。

```bat
cd baidu_uploader
git init
git add .
git commit -m "init: 百度网盘分批上传工具"
git remote add origin 你的仓库地址
git push -u origin main
```

**push 之前先自查**，确认敏感文件没被带进去：

```bat
git status
```

正常情况下 `git status` 里只会出现这些文件，`config.json`、`token.json`、`libs/`、`uploaded_log.txt`、`upload.log`、`progress.json` 一个都不该有：

```
.gitignore  README.md  requirements.txt  config.example.json
upload_baidu.py  webui.py  webui.html  open_panel.bat  start_upload.bat
```

> 万一之前误提交过密钥：`立刻去开放平台重置 SecretKey`，再按 git 历史清理流程处理（单纯删文件不足以抹掉历史记录）。

**别人克隆后怎么跑起来**：

```bat
pip install -r requirements.txt
copy config.example.json config.json
open_panel.bat            :: 用面板填写密钥和目录最省事
```

## 十三、一键推送到三个远程仓库（push-all.bat）

本项目同时托管在三个平台，双击 `push-all.bat` 一次推完：

| 平台 | 地址 | 协议 |
|---|---|---|
| GitHub | https://github.com/SimianLee/baidu-uploader.git | HTTPS |
| GitCode | git@gitcode.com:SimianLee/baidu-uploader.git | **SSH** |
| Gitee | https://gitee.com/SimianLee/baidu-uploader.git | HTTPS |

> GitCode 之所以单独走 SSH：该平台已移除密码认证，HTTPS 推送必失败（见下）。

### 用法

| 命令 | 作用 |
|---|---|
| `push-all.bat` | 推送当前分支到三个库（首次运行会自动做初始化提交） |
| `push-all.bat setup` | 只配置三个远程地址，不推送 |
| `push-all.bat ssh` | 改用 SSH 地址推送（HTTPS 不通时用） |
| `push-all.bat commit "说明"` | 先 `git add -A` + commit 再推送 |

### 它做了什么

1. **推送前安全自检**：若 `config.json` / `token.json` / `libs/` / `uploaded_log.txt` 已被 git 跟踪，立即中止并给出 `git rm --cached` 的解法——防止密钥泄漏到公开仓库
2. **幂等配置远程**：三个远程不存在就添加、地址不对就纠正，重复运行无副作用
3. **依次推送并汇总**：每个库的原始输出实时显示，结束给出成功/失败统计
4. **写日志**：`push-logs/push-<时间戳>.log`（完整）+ `push-logs/history.log`（历次一行摘要，UTF-8 带 BOM，记事本打开不乱码）

### GitCode 为什么要走 SSH（2026-09-20 实测）

HTTPS 推送会直接失败：

```
remote: <CH.00905401> HTTP Basic: Access denied.
remote: The password-based authentication of Git has been removed.
        Please use your personal access token instead of the password.
fatal: Authentication failed for 'https://gitcode.com/...'
```

原因不是账号密码输错，而是 **GitCode 已经彻底移除密码认证**，HTTPS 只剩两条路（私人令牌 / SSH）。脚本默认选了 SSH，因此双击即可一次推完三个库。

若哪天报 `Permission denied (publickey)`，说明 `~/.ssh/id_rsa.pub` 的公钥没加到 GitCode 账号，去「个人设置 → SSH 公钥」粘贴一次即可（本机密钥已存在：id_rsa / id_rsa.pub）。

> 脚本可重复运行：已推送成功的库会显示 `Everything up-to-date` 直接跳过，只补推落后的那个。
