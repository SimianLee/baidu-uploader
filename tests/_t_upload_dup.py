# -*- coding: utf-8 -*-
"""上传时的「网盘重名」处理（upload_dup）。

老李的要求（2026-10-09）：上传检测到网盘上已经有同名同后缀的文件时，
**不上传**（或删除本地那份），别稀里糊涂把人家网盘上的文件覆盖掉。

要盯住的四件事：
  一、命中判定按**完整文件名**（含后缀）、大小写不敏感；只有名字不同后缀相同
      不算同名（Book.txt 跟 Book.pdf 是两回事）
  二、skip（默认）：命中的不上传，本地那份留着
  三、delete：命中的不上传，本地那份按 after_upload 处理掉
  四、upload：命中也照传并覆盖（老行为，给「我就是要用新的替掉」留个口子）
  五、目录列不出来时**不能当成没有**：查不到的那些文件照常上传，且要如实报
      ——把「查不到」误判成「没重名」就会悄悄覆盖用户的文件
"""
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import upload_baidu as ub  # noqa: E402

PASS = FAIL = 0


def ck(cond, what, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {what}")
    else:
        FAIL += 1
        print(f"  ✗ {what}   {extra!r}")


# ---------------- 一、pathy 无关的纯函数 ----------------
class FakeApi:
    """假网盘接口：list_names 只认已经登记过的目录"""

    def __init__(self, tree, broken=()):
        self.tree = tree                 # {远程目录: [文件名]}
        self.broken = set(broken)        # 这些目录列不出来
        self.asked = []

    def list_names(self, d):
        self.asked.append(d)
        if d in self.broken:
            return set(), False
        return {n.lower() for n in self.tree.get(d, ())}, True


def items(names):
    return [(Path(n), 100) for n in names]


print("【1】策略归一化：认不出来的写法一律按最安全的 skip")
ck(ub.norm_dup("skip") == "skip", "skip → skip")
ck(ub.norm_dup("delete") == "delete", "delete → delete")
ck(ub.norm_dup("upload") == "upload", "upload → upload")
ck(ub.norm_dup("") == "skip", "空值 → skip")
ck(ub.norm_dup(None) == "skip", "None → skip")
ck(ub.norm_dup("DELETE") == "delete", "大小写不敏感")
ck(ub.norm_dup("乱写") == "skip", "★ 认不出来 → skip（不许误数据）")

print("\n【2】★ 命中判定：完整文件名 + 大小写不敏感")
api = FakeApi({"/apps/x/归档": ["Book.txt", "OTHER.PDF"]})
rmap = {Path("local/a.txt"): "/apps/x/归档/Book.txt",
        Path("local/b.txt"): "/apps/x/归档/book.TXT",     # 只差大小写 → 同名
        Path("local/c.txt"): "/apps/x/归档/Book.pdf",     # 后缀不同 → 不算同名
        Path("local/d.txt"): "/apps/x/归档/new.txt"}
it = items(["local/a.txt", "local/b.txt", "local/c.txt", "local/d.txt"])
dups, bad = ub.remote_dups(api, it, rmap)
hit = sorted(p.name for p, _ in dups)     # 只比文件名：Windows 下 Path 是反斜杠
ck(hit == ["a.txt", "b.txt"],
   "★ 完整同名与「只差大小写」都算命中，不同后缀不算", hit)
ck(bad == 0, "没有查不成的目录", bad)
ck(sorted(api.asked) == ["/apps/x/归档"],
   "★ 同一个目录只列一次（不是每文件一次）", api.asked)

print("\n【3】没有 api / 没有文件 → 一个都不命中，也不炸")
ck(ub.remote_dups(None, items(["local/a.txt"]), rmap) == ([], 0),
   "api=None 直接返回空")
ck(ub.remote_dups(api, [], rmap) == ([], 0), "空列表返回空")

print("\n【4】★ 目录列不出来：不能当成「没有重名」")
api2 = FakeApi({"/apps/x/归档": ["Book.txt"]}, broken=["/apps/x/归档"])
rmap2 = {Path("local/a.txt"): "/apps/x/归档/Book.txt",
         Path("local/e.txt"): "/apps/y/other.txt"}
dups2, bad2 = ub.remote_dups(api2, items(["local/a.txt", "local/e.txt"]), rmap2)
ck(bad2 == 1, "如实报出 1 个目录没列成", bad2)
ck([str(p) for p, _ in dups2] == [],
   "★ 查不到的那些不判重名（放过去照常上传，不悄悄覆盖）", dups2)

print("\n【5】settle_dups：只有 delete 模式才动本地文件")
ck(ub.settle_dups(items(["local/a.txt"]), "skip", {"after_upload": "trash"}) == 0,
   "skip 模式一个数都不处理")
ck(ub.settle_dups([], "delete", {"after_upload": "trash"}) == 0,
   "没有命中也不处理")
ck(ub.settle_dups(items(["local/a.txt"]), "delete", {"after_upload": "keep"}) == 0,
   "★ after_upload=keep 时即使选了 delete 也不动本地（没有答案就不猜）")
ck(ub.settle_dups(items(["local/a.txt"]), "delete", {"after_upload": "ask"}) == 0,
   "★ ask 等价于 keep：无交互环境不许替用户删")


# ---------------- 六、端到端跑一次 main() ----------------
class FakeAuth:
    def __init__(self, *a, **k):
        self.tokens = {"access_token": "fake"}
        self.token_path = None

    access_token = "fake"

    def ensure_token(self):
        pass


class FakePan:
    def __init__(self, *a, **k):
        self.made = []
        self.uploaded = {}
        self.remote = {}          # 远程目录 -> [文件名]，模拟网盘上已有的内容

    def mkdir(self, path, quiet=False):
        if path not in self.made:
            self.made.append(path)

    def list_names(self, d):
        return {n.lower() for n in self.remote.get(d, ())}, True

    def upload_file(self, local, remote, chunk):
        with open(local, "rb") as fh:
            self.uploaded[remote] = len(fh.read())
        return True


def run_upload(tmp: Path, cfg_extra=None, argv_extra=()):
    local = tmp / "local"
    (local / "子").mkdir(parents=True, exist_ok=True)
    (local / "Book.txt").write_text("a" * 50, encoding="utf-8")
    (local / "Movie.mkv").write_text("b" * 50, encoding="utf-8")
    (local / "子" / "Song.mp3").write_text("c" * 50, encoding="utf-8")
    try:
        (tmp / "data" / "upload.pid").unlink()
    except OSError:
        pass
    cfg = {"app_key": "k", "secret_key": "s", "local_dir": str(local),
           "remote_dir": "/apps/fake", "recursive": True, "workers": 2,
           "batch_size": 50, "batch_pause_sec": 0, "file_interval_sec": 0,
           "chunk_size_mb": 4, "max_file_size_mb": 4096,
           "after_upload": "keep", "upload_layout": "flat",
           "done_dir": str(tmp / "已上传"),
           "exclude_patterns": []}
    cfg.update(cfg_extra or {})
    cfgp = tmp / "config.json"
    cfgp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    pan = FakePan()
    pan.remote = {"/apps/fake": ["Book.txt"]}     # 网盘上已经有一个 Book.txt
    old_argv, old_auth, old_pan = sys.argv, ub.BaiduAuth, ub.PanApi
    ub.BaiduAuth, ub.PanApi = FakeAuth, (lambda *a, **k: pan)
    sys.argv = ["upload_baidu.py", "--config", str(cfgp), "--yes", *argv_extra]
    err, buf = None, io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ub.main()
    except BaseException as e:      # noqa: BLE001
        err = e
    finally:
        sys.argv, ub.BaiduAuth, ub.PanApi = old_argv, old_auth, old_pan
    return err, pan, buf.getvalue(), local


BASE = "/apps/fake"

print("\n【6】★ 默认 skip：网盘已有的那个不上传，其余照传")
t = Path(tempfile.mkdtemp(prefix="dup_skip_"))
err, pan, out, local = run_upload(t)
ck(err is None, "main() 正常返回", f"{type(err).__name__}: {err}" if err else "")
ck(f"{BASE}/Book.txt" not in pan.uploaded,
   "★ 重名的 Book.txt 没有再传一遍", list(pan.uploaded))
ck(f"{BASE}/Movie.mkv" in pan.uploaded and f"{BASE}/Song.mp3" in pan.uploaded,
   "没撞名的两个照常上传", list(pan.uploaded))
ck(len(pan.uploaded) == 2, "本次上传 2 个", len(pan.uploaded))
ck("网盘上已经有 1 个同名文件" in out, "★ 如实报出「有 1 个同名」", out[-300:])
ck((local / "Book.txt").exists(), "本地那份还留着（skip 不动本地）")
done_log = [l for l in (t / "data" / "uploaded_log.txt").read_text(
    encoding="utf-8").splitlines() if l] if (t / "data" / "uploaded_log.txt").exists() else []
ck(any("Book.txt" in l for l in done_log),
   "★ 跳过的也写进断点记录（下轮不再反复扫它）", done_log)

print("\n【7】upload：命中也要照传（覆盖网盘上那个）")
t2 = Path(tempfile.mkdtemp(prefix="dup_up_"))
err2, pan2, out2, _ = run_upload(t2, {"upload_dup": "upload"})
ck(err2 is None, "main() 正常返回", f"{type(err2).__name__}: {err2}" if err2 else "")
ck(f"{BASE}/Book.txt" in pan2.uploaded, "★ 重名的也照样传了", list(pan2.uploaded))
ck(len(pan2.uploaded) == 3, "三个全传", len(pan2.uploaded))

print("\n【8】★ delete：跳过上传，本地那份按 after_upload 处理掉")
t3 = Path(tempfile.mkdtemp(prefix="dup_del_"))
err3, pan3, out3, local3 = run_upload(t3, {"upload_dup": "delete",
                                           "after_upload": "move"})
ck(err3 is None, "main() 正常返回", f"{type(err3).__name__}: {err3}" if err3 else "")
ck(f"{BASE}/Book.txt" not in pan3.uploaded, "网盘重名的那个没传", list(pan3.uploaded))
ck(not (local3 / "Book.txt").exists(),
   "★ 本地那份重复的被移走了", [p.name for p in local3.iterdir()])
ck((t3 / "已上传" / "Book.txt").exists(), "落在已完成目录里",
   [p.name for p in (t3 / "已上传").iterdir()] if (t3 / "已上传").exists() else None)
ck(len(pan3.uploaded) == 2, "其余两个照常上传", len(pan3.uploaded))

print("\n【9】全是重名：一个都不传，也得说清楚为什么")


def run_onescene(tmp, cfg_extra):
    """本地只有一个文件，而它在网盘上已经有了 —— 最容易让人一头雾水的场景。"""
    local = tmp / "local"
    local.mkdir(parents=True, exist_ok=True)
    (local / "Book.txt").write_text("a" * 50, encoding="utf-8")
    try:
        (tmp / "data" / "upload.pid").unlink()
    except OSError:
        pass
    cfg = {"app_key": "k", "secret_key": "s", "local_dir": str(local),
           "remote_dir": "/apps/fake", "recursive": False, "workers": 1,
           "batch_size": 50, "batch_pause_sec": 0, "file_interval_sec": 0,
           "chunk_size_mb": 4, "max_file_size_mb": 4096,
           "after_upload": "keep", "upload_layout": "flat",
           "done_dir": str(tmp / "已上传"), "exclude_patterns": []}
    cfg.update(cfg_extra)
    cfgp = tmp / "config.json"
    cfgp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    pan = FakePan()
    pan.remote = {"/apps/fake": ["Book.txt"]}
    old_argv, old_auth, old_pan = sys.argv, ub.BaiduAuth, ub.PanApi
    ub.BaiduAuth, ub.PanApi = FakeAuth, (lambda *a, **k: pan)
    sys.argv = ["upload_baidu.py", "--config", str(cfgp), "--yes"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ub.main()
    finally:
        sys.argv, ub.BaiduAuth, ub.PanApi = old_argv, old_auth, old_pan
    return pan, buf.getvalue(), local


t5 = Path(tempfile.mkdtemp(prefix="dup_only_"))
pan5, out5, loc5 = run_onescene(t5, {"upload_dup": "skip"})
ck(not pan5.uploaded, "★ 一个都没上传", list(pan5.uploaded))
ck("全部跳过，本次没有东西要上传" in out5,
   "★ 说清楚是因为网盘上都有同名，而不是「没扫到文件」", out5[-300:])

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
