# -*- coding: utf-8 -*-
r"""
verify_remote.py —— 上传结果核对工具

用法（上传跑完后执行）：
    set PYTHONPATH=<项目目录>\libs
    python verify_remote.py                 # 核对默认远程目录
    python verify_remote.py --sample 30     # 差异样本显示 30 条
    python verify_remote.py --remote /apps/baidu_uploader

做什么：
  1. 递归列出网盘目标目录下所有文件（数量 + 总字节）
  2. 读取本地断点记录 uploaded_log.txt（本地认为已上传成功的清单）
  3. 两边按相对路径比对，找出「网盘缺」和「网盘多」的文件
  4. 大小不一致的也会被揪出来（可能是传一半损坏）

注意：核对本身只读不写，不会动网盘和本地文件。
"""
import argparse
import json
import sys
import time
from pathlib import Path

import requests

PROJ = Path(__file__).resolve().parent
TOKEN_FILE = PROJ / "token.json"
CONFIG = PROJ / "config.json"
DONE_LOG = PROJ / "uploaded_log.txt"

LIST_URL = "https://pan.baidu.com/rest/2.0/xpan/file?method=list"


def load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}PB"


def get_token() -> str:
    tk = load_json(TOKEN_FILE, {})
    if not tk.get("access_token"):
        print(f"[错误] 没有 access_token，请先授权（{TOKEN_FILE}）")
        sys.exit(1)
    return tk["access_token"]


def list_dir(token: str, remote_dir: str, start: int = 0, limit: int = 1000):
    """列出单层目录内容（百度 list 接口不递归）"""
    for attempt in range(3):
        try:
            r = requests.get(LIST_URL, params={
                "access_token": token, "dir": remote_dir,
                "start": start, "limit": limit, "order": "name",
            }, timeout=30).json()
        except Exception as e:
            print(f"    [重试{attempt}] 网络异常: {e}")
            time.sleep(3)
            continue
        errno = r.get("errno", 0)
        if errno == 0:
            return r.get("list", [])
        if errno in (31034, 42000):        # 频控：退避重试
            print(f"    [频控] errno={errno}，退避 20s...")
            time.sleep(20)
            continue
        print(f"    [错误] 列目录失败: errno={errno} {r}")
        return []
    return []


def walk_remote(token: str, root: str, ui):
    """递归遍历网盘目录树，返回 {相对路径: size}"""
    found = {}
    stack = [root]
    while stack:
        d = stack.pop()
        start = 0
        while True:
            items = list_dir(token, d, start=start)
            if not items:
                break
            for it in items:
                rel = it["path"][len(root):].lstrip("/")
                if it.get("isdir"):
                    if it.get("server_filename", "").startswith("."):
                        continue
                    stack.append(it["path"])
                else:
                    found[rel] = it.get("size", 0)
            ui()
            if len(items) < 1000:
                break
            start += 1000
    return found


def main():
    ap = argparse.ArgumentParser(description="核对网盘上传结果 vs 本地断点记录")
    ap.add_argument("--remote", help="网盘目标目录，默认取 config.json 的 remote_dir")
    ap.add_argument("--local", help="本地源目录，默认取 config.json 的 local_dir")
    ap.add_argument("--sample", type=int, default=20, help="差异样本显示条数")
    args = ap.parse_args()

    cfg = load_json(CONFIG, {})
    base_remote = cfg.get("remote_dir", "/apps/baidu_uploader").rstrip("/")
    remote = (args.remote or base_remote).rstrip("/")
    local_dir = Path(args.local or cfg.get("local_dir", "."))
    token = get_token()

    # 只核对子目录时，把远程路径前缀剥掉，才能和本地相对路径对齐
    prefix = ""
    if remote.startswith(base_remote + "/"):
        prefix = remote[len(base_remote) + 1:] + "/"

    print("=" * 62)
    print("上传结果核对（只读，不改动任何文件）")
    print("=" * 62)
    print(f"网盘目录: {remote}")
    print(f"本地目录: {local_dir}")

    # ---- 1. 遍历网盘 ----
    print("\n[1/3] 正在递归列出网盘文件...")
    n_api = [0]
    t0 = time.time()
    remote_files = walk_remote(token, remote.rstrip("/"),
                               lambda: n_api.__setitem__(0, n_api[0] + 1))
    remote_size = sum(remote_files.values())
    print(f"      ✔ {len(remote_files):,} 个文件，总 {human_size(remote_size)}"
          f"（{n_api[0]} 次接口调用，耗时 {time.time()-t0:.0f}s）")

    # ---- 2. 读本地断点记录 ----
    print("\n[2/3] 读取本地断点记录...")
    done = {}
    if DONE_LOG.exists():
        with open(DONE_LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.strip()
                if not p:
                    continue
                try:
                    rel = Path(p).relative_to(local_dir)
                except ValueError:
                    rel = Path(p).name
                except Exception:
                    continue
                done[str(rel).replace("\\", "/")] = None
    if prefix:   # 只核对某个子目录时，记录的比对范围也跟着收窄
        done = {k[len(prefix):]: v for k, v in done.items() if k.startswith(prefix)}
    scope = f"（范围：{prefix or '整个上传目录'}）"
    print(f"      ✔ 记录 {len(done):,} 条 {scope}")

    # ---- 3. 比对 ----
    print("\n[3/3] 比对中...")
    done_keys = set(done.keys())
    remote_keys = set(remote_files.keys())

    missing = sorted(done_keys - remote_keys)      # 本地记录传了，网盘没有
    extra = sorted(remote_keys - done_keys)        # 网盘有，本地没记录

    print("\n" + "=" * 62)
    print("核对结论")
    print("=" * 62)
    print(f"网盘实际文件数   : {len(remote_keys):>10,}")
    print(f"本地成功记录数   : {len(done_keys):>10,}")
    print(f"两边一致         : {len(done_keys & remote_keys):>10,}")
    print(f"网盘缺失(需补传) : {len(missing):>10,}")
    print(f"网盘多出(未记录) : {len(extra):>10,}")

    if missing:
        print(f"\n>>> 网盘缺失样本（最多 {args.sample} 条）：")
        for k in missing[:args.sample]:
            fp = local_dir / k
            try:
                sz = human_size(fp.stat().st_size)
            except Exception:
                sz = "?"
            print(f"    {sz:>10}  {k}")
        print(f"\n    提示：重跑 upload_baidu.py 会自动跳过断点记录里的文件，")
        print(f"          要补传这 {len(missing)} 个，需先从 {DONE_LOG.name} 删掉对应行。")

    if extra:
        print(f"\n>>> 网盘多出样本（最多 {args.sample} 条，通常是往轮次遗留/已删除源文件）：")
        for k in extra[:args.sample]:
            print(f"    {human_size(remote_files[k]):>10}  {k}")

    if not missing and not extra:
        print("\n✔ 完全一致：本地记录与网盘实际内容一一对应，可以放心清理本地文件。")

    # 大小核对（只对双方都有的做抽查，全部比代价高）
    same = sorted(done_keys & remote_keys)
    mismatch = []
    for k in same:
        fp = local_dir / k
        try:
            if fp.exists() and fp.stat().st_size != remote_files[k]:
                mismatch.append((k, fp.stat().st_size, remote_files[k]))
        except Exception:
            pass
    if mismatch:
        print(f"\n>>> 大小不一致 {len(mismatch)} 个（源文件还在本地，可比对）：")
        for k, a, b in mismatch[:args.sample]:
            print(f"    本地 {human_size(a):>10} vs 网盘 {human_size(b):>10}   {k}")
    elif same:
        print(f"\n✔ 大小抽查：{len(same):,} 个本地仍存在的文件大小全部一致。")


if __name__ == "__main__":
    main()
