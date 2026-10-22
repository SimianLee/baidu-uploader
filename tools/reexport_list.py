# -*- coding: utf-8 -*-
"""用修复后的扫描逻辑重新导出归档目录的完整文件清单

旧清单（113852 条）是 bug 版本的产物：/归档/txt（约 40 万条）在深翻页时撞上
百度偶发的 errno=1，整个目录被静默丢弃。这份脚本跑的是修好的扫描，用来
产出完整清单，并顺带报告是否还有读不开的目录。
"""
import csv
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent / "core"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "libs"))

import pan_tools  # noqa: E402

COLS = ["序号", "操作", "原路径", "文件名", "后缀", "新文件名", "所在目录",
        "大小(字节)", "修改时间"]


def fmt_mtime(t):
    if not t:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(t)))
    except Exception:
        return ""


def main():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    tk = json.loads((ROOT / "token.json").read_text(encoding="utf-8-sig"))
    sandbox = str(cfg.get("remote_dir") or "/apps/baidu_uploader").rstrip("/")
    pan = pan_tools.PanFiles(tk["access_token"], sandbox)
    failures = []

    last = [0.0]

    def prog(dirs, files, cur):
        now = time.time()
        if now - last[0] >= 2:
            last[0] = now
            print(f"   已扫 {dirs} 个目录 / {files} 个文件 … {cur}",
                  flush=True)

    print(f"开始扫描 {sandbox} …", flush=True)
    t0 = time.time()
    # 串行（workers=1）跑：并发 4 路同时打百度容易触发频控 errno，
    # 诊断阶段先排除这个变量，看清每个目录到底列到多少条。
    # 用 list_dir 而不是 list_files —— 后者不接 workers，默认就上 4 路并发。
    items = pan.list_dir(sandbox, recursive=True, on_progress=prog,
                         failures=failures, workers=1)
    files = [f for f in items if not f["isdir"]]
    used = time.time() - t0
    print(f"扫描完成：{len(files)} 个文件，用时 {used:.0f}s", flush=True)

    # 失败/半截目录先报，别等写文件时崩了才说
    if failures:
        print(f"\n⚠ 有 {len(failures)} 个目录没列全：")
        for x in failures[:20]:
            print("   ", x.get("path"), "errno=", x.get("errno"),
                  x.get("msg"), "partial=", x.get("partial"))
    else:
        print("\n✓ 没有读不开的目录")

    by_dir = {}
    for f in files:
        by_dir.setdefault(f["path"].rsplit("/", 1)[0], 0)
        by_dir[f["path"].rsplit("/", 1)[0]] += 1
    print("\n各目录文件数：")
    for k, v in sorted(by_dir.items(), key=lambda x: -x[1]):
        print(f"   {v:8d}  {k}")

    # 注意：桌面在 C:\Users\UserData\Desktop，不是当前用户 freedo 的
    desk = Path(r"C:\Users\UserData\Desktop")
    if not desk.is_dir():
        desk = Path.home() / "Desktop"
    out = desk / "文件清单_apps_baidu_uploader_归档_完整.csv"
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLS)
        for i, f in enumerate(files, 1):
            path = str(f.get("path") or "")
            name = path.rsplit("/", 1)[-1]
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            w.writerow([i, "", path, name, ext, "", path.rsplit("/", 1)[0],
                        f.get("size") or 0, fmt_mtime(f.get("mtime"))])
    print(f"\n已写出：{out}")
    print(f"文件大小：{out.stat().st_size / 1024 / 1024:.1f} MB")

    if failures:
        print(f"\n⚠ 仍有 {len(failures)} 个目录读不开：")
        for x in failures[:20]:
            print("   ", x.get("path"), x.get("errno"), x.get("msg"))
    else:
        print("\n✓ 没有读不开的目录，这份清单是完整的。")


if __name__ == "__main__":
    main()
