# -*- coding: utf-8 -*-
r"""真网盘验证：清理空文件夹，百度侧会不会也有「套娃删不掉」那个毛病

本地那次是这么炸的：计划只列最外层那条，等 `os.rmdir` 去删时，父目录里还站着
几个空子目录 ⇒ 必然失败。网盘走的不是 rmdir，而是百度的 `filemanager?opera=delete`
——它删目录是**级联**的（子树里有多少东西一起带走），所以理论上不会有本地那个
毛病。理论归理论，这里拿真沙盒验一遍：

  ① 在沙盒里搭一棵临时目录树：套娃/{A/B/C, A/D}
  ② 用**网盘口径**（expand=False，只列最外层）生成空文件夹计划
  ③ 执行，然后到网盘上看 A 整棵子树是不是真的没了（级联删成立）
  ④ 无论成败都把临时树删干净（finally），不在沙盒里留垃圾

只动自己刚建的 `_空目录测试-<时间戳>` 那棵子树，碰都不碰别的东西。
没授权 / Token 失效时整个跳过。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pan_tools as pt  # noqa: E402

OK = FAIL = 0


def ck(cond, name, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def main():
    tkf, cfgf = ROOT / "token.json", ROOT / "config.json"
    if not tkf.exists():
        print("跳过：没有 token.json（先授权再跑）"); return
    tk = json.loads(tkf.read_text(encoding="utf-8-sig"))
    if not tk.get("access_token"):
        print("跳过：token.json 里没有 access_token"); return
    sandbox = "/apps/baidu_uploader"
    if cfgf.exists():
        sandbox = str(json.loads(cfgf.read_text(encoding="utf-8-sig"))
                      .get("remote_dir") or sandbox).rstrip("/")

    pan = pt.PanFiles(tk["access_token"], sandbox)
    print(f"【0】连真网盘（沙盒 {sandbox}）")
    if not pan._alive():
        print("跳过：连接不上（授权失效或网络不通）"); return
    ck(True, "连上了", None)

    base = f"{sandbox}/_空目录测试-{int(time.time())}"
    try:
        print("\n【1】搭一棵临时目录树：套娃里嵌着四个空目录")
        tree = [f"{base}/套娃/A/B/C", f"{base}/套娃/A/D", f"{base}/单独的空"]
        for d in tree:
            ck(pan.mkdir(d) is True, f"建 {d.rsplit('/', 1)[-1]}", None)
        time.sleep(1.0)     # 百度侧建目录偶有延迟，列不到就白搭了
        ent = pan.list_dir(base, recursive=True)
        # mkdir 会把中间层一起建出来：套娃、套娃/A、套娃/A/B、套娃/A/B/C、
        # 套娃/A/D、单独的空 —— 一共 6 个，全是空的
        ck(len([e for e in ent if e["isdir"]]) == 6,
           "★ 六个空目录都在网盘上（含自动建出的中间层）",
           sorted(e["path"].replace(base, "") for e in ent))

        print("\n【2】按网盘口径生成计划：只有最外层两条")
        ops, info = pt.build_empty_dir_plan(ent, base, skip=pt.BACKUP_DIR_NAME)
        rel = sorted(str(o["path"]).replace(base, "") for o in ops)
        ck(rel == ["/单独的空", "/套娃"], "★ 只列最外层（嵌套的交给级联删）", rel)
        ck(info["empty_total"] == 6 and info["nested"] == 4,
           "统计照旧：空目录共 6 / 嵌套 4", info)

        print("\n【3】★ 执行：级联删成立的话，套娃整棵树都会没")
        st = pt.apply_plan(pan, ops, ondup="skip")
        ck(st["delete"] == 2 and not st["fails"], "★ 两条都删成功了", st["fails"])
        time.sleep(1.5)
        left = pan.list_dir(base, recursive=True)
        left_rel = sorted(e["path"].replace(base, "") for e in left)
        ck(not any(x.startswith("/套娃") for x in left_rel),
           "★ 套娃 A（含 B/C、D）整棵没了——百度删目录是级联的，没有本地那个毛病",
           left_rel)
        ck(not any(x == "/单独的空" for x in left_rel), "单独的空也没了", left_rel)
        ck(left_rel == [], "★ 整个测试子树清空（级联删覆盖了每一层）", left_rel)
    finally:
        print("\n【4】收尾：把临时子树删干净")
        try:
            pan.delete_batch([base])
            time.sleep(0.5)
            gone = pan.list_dir(sandbox, recursive=False)
            ck(not any(e["path"] == base for e in gone),
               "★ 沙盒里没留下测试目录", [e["name"] for e in gone][:8])
        except Exception as e:
            ck(False, "清理失败（手工删一下即可）", str(e))


try:
    main()
except Exception as e:
    import traceback
    traceback.print_exc()
    FAIL += 1

print(f"\n{'=' * 46}\n通过 {OK} 项，失败 {FAIL} 项\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
