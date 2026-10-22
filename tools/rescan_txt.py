# -*- coding: utf-8 -*-
"""补扫 /归档/txt 目录**缺的那一段**，把结果合并回桌面的完整清单。

背景：整体重扫时 txt 目录（约 40.3 万条 / 403 页）在深翻页处撞上百度间歇性
限流（每页 6-14s、约八成页报 errno=1），只列到 30.2 万就停了 —— 清单从
51.7 万掉到 41.6 万。

关键取舍：**不要整个目录重扫**。现有清单里 txt 的前 302 页（0~301000）是
完整可靠的，缺的只是 302000 往后那约 100 页。全量重扫 403 页要两个半小时，
定向补这 100 页只要几分钟，而且每读一页就落盘，中断也不会丢。

用法：
    python tools/rescan_txt.py                # 定向补齐 txt 并合并（推荐）
    python tools/rescan_txt.py --scan-only    # 只补扫、落盘，不合并
    python tools/rescan_txt.py --merge-only   # 只拿已落盘的补扫结果合并
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

# 补扫参数：撞墙就快速失败（深翻页时限流是间歇性的，原地死磕最亏），
# 页间隔放大到 1s，别催它。
pan_tools.PAGE_RETRY = 1            # 单页最多试 2 次
pan_tools.PAGE_BACKOFF_BUDGET = 30.0
PAGE_GAP = 1.0

LIMIT = 1000
TXT_DIR = "/apps/baidu_uploader/归档/txt"
# 现有清单里 txt 只列到 302000（第 0~301 页），从这儿往后补
START0 = 302000

STATE = HERE / "_out" / "txt_tail_state.json"
ROWS = HERE / "_out" / "txt_tail_rows.jsonl"
CACHE_BAK = HERE / "_out" / "csv_before_rescan_txt.csv"


def fmt_mtime(t):
    if not t:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(t)))
    except Exception:
        return ""


def load_pan():
    tk = json.loads((ROOT / "token.json").read_text(encoding="utf-8-sig"))
    return pan_tools.PanFiles(tk["access_token"], TXT_DIR)


def load_state():
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text(encoding="utf-8"))
            st.setdefault("ok", {})
            st.setdefault("fail", [])
            st.setdefault("last", False)
            return st
        except Exception:
            pass
    return {"ok": {}, "fail": [], "last": False}


def save_state(st):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def append_rows(rows, start):
    ROWS.parent.mkdir(exist_ok=True)
    with open(ROWS, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({"start": start, **r}, ensure_ascii=False)
                     + "\n")


def read_rows():
    if not ROWS.exists():
        return []
    out = []
    with open(ROWS, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def tail_scan(rounds=1):
    """从 START0 往后一页一页补，每页落盘；返回 (本次新增条数, 仍缺的页)"""
    pan = load_pan()
    st = load_state()
    t0 = time.time()
    new = 0

    for rd in range(1, rounds + 1):
        todo = [START0 + i * LIMIT
                for i in range(0, 200)
                if str(START0 + i * LIMIT) not in st["ok"]]
        # 上一轮没读到的页优先补
        if st["fail"]:
            todo = sorted(set(st["fail"])) + [x for x in todo
                                              if x not in set(st["fail"])]
        if not todo:
            print("没有待补的页了", flush=True)
            break
        print(f"第 {rd} 轮：待补 {len(todo)} 页（{todo[0]} … {todo[-1]}）",
              flush=True)
        fails = []
        for i, s in enumerate(todo, 1):
            rows, e = pan.list_page(TXT_DIR, s, LIMIT)
            if e != 0:
                fails.append(s)
                print(f"  [{i}/{len(todo)}] start={s} 失败 errno={e}",
                      flush=True)
            else:
                if rows:
                    append_rows(rows, s)
                    new += len(rows)
                st["ok"][str(s)] = len(rows)
                st["fail"] = [x for x in st["fail"] if x != s]
                save_state(st)
                tail = " ← 最后一页" if len(rows) < LIMIT else ""
                print(f"  [{i}/{len(todo)}] start={s} +{len(rows)} 条"
                      f"（累计 {new}）{tail}", flush=True)
                if len(rows) < LIMIT:
                    st["last"] = True
                    save_state(st)
                    print(f"读到不足整页 → 已到目录末尾，补扫结束", flush=True)
                    return new, fails
            time.sleep(PAGE_GAP)
        st["fail"] = fails
        save_state(st)
        if not fails:
            break
        print(f"  本轮后仍缺 {len(fails)} 页，静置 30s 再补一轮", flush=True)
        time.sleep(30)

    print(f"补扫完成：新增 {new} 条，用时 {time.time()-t0:.0f}s", flush=True)
    return new, st["fail"]


def merge():
    desk = Path(r"C:\Users\UserData\Desktop")
    if not desk.is_dir():
        desk = Path.home() / "Desktop"
    out = desk / "文件清单_apps_baidu_uploader_归档_完整.csv"
    CACHE_BAK.parent.mkdir(exist_ok=True)
    CACHE_BAK.write_bytes(out.read_bytes())
    print("原表已备份：", CACHE_BAK, flush=True)

    with open(CACHE_BAK, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        fields = list(rd.fieldnames)
        rows = list(rd)
    txt_old = [r for r in rows
               if r["所在目录"] == TXT_DIR
               or r["所在目录"].startswith(TXT_DIR + "/")]
    keep = [r for r in rows
            if not (r["所在目录"] == TXT_DIR
                    or r["所在目录"].startswith(TXT_DIR + "/"))]
    print(f"原表 {len(rows)} 条：非 txt {len(keep)} + 旧 txt {len(txt_old)}",
          flush=True)

    seen = {r["原路径"] for r in txt_old}
    added = 0
    allrows = list(keep) + list(txt_old)
    for f in read_rows():
        path = str(f.get("path") or "")
        if path in seen:
            continue
        seen.add(path)
        name = path.rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        allrows.append({"序号": "", "操作": "", "原路径": path, "文件名": name,
                        "后缀": ext, "新文件名": "",
                        "所在目录": path.rsplit("/", 1)[0],
                        "大小(字节)": f.get("size") or 0,
                        "修改时间": fmt_mtime(f.get("mtime"))})
        added += 1
    for i, r in enumerate(allrows, 1):
        r["序号"] = i
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(allrows)
    print(f"合并完成：新增 {added} 条，共 {len(allrows)} 条 → {out}", flush=True)
    return len(allrows)


def main():
    args = set(sys.argv[1:])
    if "--merge-only" not in args:
        new, fails = tail_scan(rounds=3)
        if fails:
            print(f"⚠ 仍有 {len(fails)} 页没读到：{fails[:20]}", flush=True)
            print("  清单先不合并，过一会儿再跑一次本脚本即可续补。", flush=True)
            if "--scan-only" in args:
                return
            if len(fails) > 5:
                sys.exit(2)
    if "--scan-only" in args:
        return
    merge()


if __name__ == "__main__":
    main()
