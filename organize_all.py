# -*- coding: utf-8 -*-
r"""
organize_all.py —— 整理归档「循环执行」版
=========================================================================

为什么需要它：面板上单次执行上限是 2000 条。一个 9783 个文件的目录按后缀整理，
面板点一次只搬走 2000 个，剩下 7783 个得再点四次「重新生成 → 执行」。这个脚本
就是替你把这件事连着干完：

    每轮：重新扫描 → 生成移动计划 → 执行前 limit 条 → 打印统计
    直到：源目录下再没有可移动的文件

用法（在项目目录下）：

    # 先干跑，只看会移动哪些、分几个文件夹
    python -X utf8 organize_all.py --path "/apps/baidu_uploader/79、【14000本】最流行网络小说txt大合集1" --by ext --dest /apps/baidu_uploader/归档 --dry-run

    # 确认无误后真跑
    python -X utf8 organize_all.py --path "..." --by ext --dest /apps/baidu_uploader/归档

    # 归档目标里已经有同名文件时，加 --ondup overwrite（否则它们搬不走，见下）

关于重名：默认策略是 skip —— 撞到同名就跳过。但被跳过的是**源目录里那个文件**，
它会留在原地，下一轮重新扫描又扫到、又撞名、又被跳过，永远搬不完。脚本每轮
会把这个数字报出来；要连这些一起搬就加 --ondup overwrite，撞到的旧文件会先
挪进「_覆盖备份/时间戳/」再覆盖，能找回。

关于并发：脚本默认**拒绝**在上传任务运行时执行。实测过——上传和整理同时操作
网盘（同一个 access_token），百度会整批整批地回假错误（-9 / 111），一次 2000 条
的移动里 1800 条被报成失败，虽然实际都生效了，但看着像白干。想强行跑加 --force。
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "libs"))

import pan_tools  # noqa: E402
import proclock   # noqa: E402

EXEC_LIMIT = 2000          # 与面板一致：单轮最多提交多少条


def dup_skipped(fails) -> int:
    """统计因「归档处已有同名」而没搬走的条数（百度 errno=-8）。

    正常情况不该出现：skip 策略下 build_organize_plan 会给撞名的自动编号
    `A (2).txt`，两边数据都保住。真还报 -8，说明撞的那个名字**没被计划看见**
    ——典型是归档目录不在扫描范围内、而我们也没去列它。所以这里的文案要指
    向「换 overwrite」而不是干等下一轮。
    """
    return sum(1 for f in (fails or []) if f.get("errno") == -8)


def load_pan():
    tk = {}
    p = HERE / "token.json"
    if p.exists():
        tk = json.loads(p.read_text(encoding="utf-8-sig"))
    if not tk.get("access_token"):
        print("[错误] 还没授权：先在面板上完成百度账号授权（token.json 里没有 access_token）")
        sys.exit(1)
    cfg = {}
    c = HERE / "config.json"
    if c.exists():
        cfg = json.loads(c.read_text(encoding="utf-8-sig"))
    sandbox = str(cfg.get("remote_dir") or "/apps/baidu_uploader").rstrip("/")
    return pan_tools.PanFiles(tk["access_token"], sandbox), sandbox


def upload_running(pidfile=None):
    """上传任务在跑吗？读 PID 锁文件后**必须确认那个进程真的还活着**。

    进程被强杀（蓝屏/关机/任务管理器结束进程）时跑不到 atexit 清理，
    upload.pid 会一直留着。曾经的实现只读文件不看存活，结果把早就没了的
    任务报成「正在运行」，把脚本自己挡在门外 —— 实测就撞上一次：
    文件里写着 PID 24456，进程列表里根本没这个进程。
    """
    f = Path(pidfile) if pidfile else (HERE / "data" / "upload.pid")
    return proclock.read_pidfile(f)


def main():
    ap = argparse.ArgumentParser(
        description="整理归档 · 循环执行（自动跑完单次 2000 条的上限）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", required=True, help="要整理的网盘目录（沙盒内）")
    ap.add_argument("--by", default="ext", choices=("ext", "category", "date"),
                    help="整理方式：ext 按后缀 / category 按大类 / date 按修改月份")
    ap.add_argument("--dest", required=True, help="归档目标目录，会在此之下按分类建子目录")
    ap.add_argument("--ondup", default="skip", choices=("skip", "overwrite"),
                    help="撞到同名文件时：skip 跳过（默认，撞名的会留在源目录搬不走）"
                         " / overwrite 先把旧文件备份到 _覆盖备份/ 再覆盖")
    ap.add_argument("--limit", type=int, default=EXEC_LIMIT,
                    help=f"每轮最多执行多少条（默认 {EXEC_LIMIT}，与面板一致）")
    ap.add_argument("--rounds", type=int, default=0,
                    help="最多跑几轮，0 = 跑到没有可移动的文件为止")
    ap.add_argument("--dry-run", action="store_true", help="只扫描和统计，不动网盘")
    ap.add_argument("--force", action="store_true",
                    help="上传任务正在跑时也强行执行（不推荐，容易触发百度假失败）")
    args = ap.parse_args()

    pan, sandbox = load_pan()
    src = pan.check_path(args.path)
    dest = pan.check_path(args.dest)

    # 目标在源里面 → 自己搬自己，会无限循环。宁可现在拒绝，也别让它跑起来
    if dest == src or dest.startswith(src.rstrip("/") + "/"):
        print(f"[错误] 归档目标不能放在被整理的目录里面：\n  {dest}\n  在 {src} 之内")
        sys.exit(1)

    pid = upload_running()
    if pid and not args.dry_run and not args.force:
        print(f"[错误] 检测到上传任务正在运行（PID {pid}）。")
        print("       两个任务同时操作网盘会用同一个 access_token 猛敲百度，")
        print("       实测会整批整批地回假错误（-9 / 111），看着像大面积失败。")
        print("       建议等上传结束后再整理；确实要同时跑就加 --force。")
        sys.exit(1)

    print("=" * 62)
    print(f"整理目录：{src}")
    print(f"归档到  ：{dest}")
    print(f"整理方式：{args.by}　重名策略：{args.ondup}　每轮上限：{args.limit}")
    if pid:
        print(f"⚠ 上传任务正在运行（PID {pid}），建议错开")
    if args.dry_run:
        print("模式    ：干跑（不会改动网盘）")
    print("=" * 62)

    total_move = total_verified = 0
    all_fails = []
    rnd = 0
    while True:
        rnd += 1
        if args.rounds and rnd > args.rounds:
            print(f"\n[停止] 已跑满 {args.rounds} 轮")
            break

        print(f"\n---------- 第 {rnd} 轮：扫描 ----------")
        t0 = time.time()
        files = pan.list_files(src, recursive=True,
                               on_progress=lambda d, f, cur: print(
                                   f"\r  已遍历 {d} 个目录 / {f} 个文件…", end=""))
        print(f"\r  扫到 {len(files)} 个文件（{time.time() - t0:.0f} 秒）")

        if not files:
            print("  源目录下已经没有文件了，整理完成")
            break

        # 撞名检测的底子是「扫描范围内的实况」，而归档目录常常不在扫描范围里
        # （--path 多半就是个子目录）。不补这一层的话，归档处已有的同名文件
        # 完全看不见 → 不编号 → 每轮都撞 -8，永远搬不完。这里照面板的做法：
        # 先用扫描结果兜底，再把这份计划真正要用到的目标目录单独列一遍。
        occupied = {}
        for f in files:
            fd, _, fn = f["path"].rpartition("/")
            occupied.setdefault(fd, set()).add(fn)
        ops = pan_tools.build_organize_plan(files, args.by, dest, sandbox,
                                            occupied, ondup=args.ondup)
        if pan_tools.occupy_dests(pan, ops, occupied):
            ops = pan_tools.build_organize_plan(files, args.by, dest, sandbox,
                                                occupied, ondup=args.ondup)
        buckets = {}
        for o in ops:
            buckets[o["dest"]] = buckets.get(o["dest"], 0) + 1
        print(f"  待移动 {len(ops)} 个文件 → {len(buckets)} 个文件夹")
        for d, n in sorted(buckets.items(), key=lambda x: -x[1])[:8]:
            print(f"      {n:>6} 个 → {d.rsplit('/', 1)[-1]}")
        if len(buckets) > 8:
            print(f"      …另有 {len(buckets) - 8} 个文件夹")

        batch = ops[:args.limit]
        if args.dry_run:
            print(f"  [干跑] 本轮将移动 {len(batch)} 个"
                  f"（还剩 {len(ops) - len(batch)} 个留给下一轮）")
            break

        print(f"  本轮执行 {len(batch)} 条…")
        t1 = time.time()
        stat = pan_tools.apply_plan(
            pan, batch, ondup=args.ondup,
            on_progress=lambda s: print(
                f"\r  {s.get('phase', '')}：{s.get('done', 0)}/{s.get('total', 0)}"
                f"　失败 {s.get('fails', 0)}", end=""))
        print()
        v = stat.get("verified") or 0
        total_move += stat["move"]
        total_verified += v
        all_fails += stat["fails"]
        print(f"  本轮：移动 {stat['move']}"
              + (f"（其中 {v} 条百度报了错误码，核对确认已生效）" if v else "")
              + f"，未生效 {len(stat['fails'])}，用时 {time.time() - t1:.0f} 秒")
        if stat.get("stop_reason"):
            print(f"  ⚠ 中途停止：{stat['stop_reason']}")
            print(f"  已执行的部分是有效的，稍后重跑本脚本会从剩下的继续")
            break
        dup = dup_skipped(stat["fails"])
        if dup:
            print(f"  ⚠ {dup} 个文件因「归档处已有同名」没搬成，仍留在源目录")
            print(f"     跳过策略下撞名的本该自动编成 A (2).txt 照搬过去，")
            print(f"     还撞 -8 说明那个同名没被计划看见（归档目录在扫描范围外等）。")
            print(f"     要连这些一起搬，加参数：--ondup overwrite")
            print(f"     （覆盖前旧文件会先挪进 {sandbox}/_覆盖备份/时间戳/，可找回）")
        if not stat["move"] and stat["fails"]:
            # 一条都没动：再循环下去只是重复报错，不如停下来让人看原因
            print("  本轮没有任何文件被移动，停止。失败原因见下：")
            for f in stat["fails"][:10]:
                print(f"      {f.get('path', '?')}　{f.get('msg', '')}")
            break

    print("\n" + "=" * 62)
    print(f"合计：移动 {total_move} 个"
          + (f"（其中 {total_verified} 个是百度报错、核对后确认已生效的）" if total_verified else ""))
    if all_fails:
        print(f"未生效 {len(all_fails)} 个，前 10 条：")
        for f in all_fails[:10]:
            print(f"    {f.get('path', '?')}　{f.get('msg', '')}")
    dup_all = dup_skipped(all_fails)
    if dup_all:
        print()
        print(f"⚠ 其中 {dup_all} 个是「归档处已有同名」被跳过的，还留在源目录里。")
        print(f"  想连这些一起搬：--ondup overwrite（旧文件先备份，可找回）")
    if args.dry_run:
        print("（干跑模式，网盘没有任何改动）")
    print("=" * 62)


if __name__ == "__main__":
    main()
