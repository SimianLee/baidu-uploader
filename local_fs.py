# -*- coding: utf-8 -*-
r"""本地磁盘版「网盘接口」：让网盘整理那一套也能整理 Win10 本地文件夹。

为什么不是另写一套整理逻辑：批量改名 / 整理归档 / 合并重复 / 批量删除的
**计划构建与执行流程**本来就跟存储介质无关——它们只认「一批 entry（path/
name/isdir/size/mtime）」和「rename / move / delete 三种动作」。网盘侧能跑通，
是因为 `pan_tools.PanFiles` 提供了这些动作；这里给本地磁盘实现一份同样接口的
`LocalFiles`，`pan_tools.apply_plan` 就能原样复用到本地，撞名检测、备份区、
分批进度那些老逻辑一个字都不用改。

两条必须写死的 Windows 特性：

1. **路径统一成正斜杠**。pan_tools 里全是按 `/` 拼字符串（rpartition("/")、
   startswith(root + "/")），本地路径进来前必须先 `D:\书\a.txt → D:/书/a.txt`。
   Windows 的 os/shutil 吃正斜杠，所以执行时不用再转回去。
2. **文件名大小写不敏感**。NTFS 上 `A.txt` 和 `a.txt` 是同一个文件，撞名判断
   必须按 `os.path.normcase` 比，否则「只改大小写」会被自己撞到——那正是
   pan_tools._find_conflicts 里特意排掉的坑，本地一样要排。

长路径（>260 字符）一律走 `winpath.long_path`：不然目录读不开会被 scandir
静默跳过，文件在扫描阶段就凭空消失。
"""
import os
import shutil
import stat

import winpath

# 每处理多少条汇报一次进度。逐条回调在十万条量级下会把时间全花在回调上
_REPORT_EVERY = 50

# NTFS 文件属性位：只读。它跟「权限」是两码事——只读文件删不掉，os.remove 直接
# 甩 [WinError 5] 拒绝访问，跟没权限、被占用长得一模一样
_ATTR_READONLY = 0x0001


def posix(p) -> str:
    r"""本地路径 → 正斜杠形式。`D:\书\` → `D:/`；末尾的斜杠只在不是盘符根时去掉"""
    s = str(p).strip().replace("\\", "/")
    # `D:/` 这种盘符根必须留着斜杠：削掉就成了 `D:`，拼出来的路径全错
    if len(s) > 3:
        s = s.rstrip("/")
    return s


def join(parent: str, name: str) -> str:
    """拼路径：`D:/` + `a.txt` → `D:/a.txt`（不会拼出 `D://a.txt`）"""
    if not parent:
        return name
    return parent.rstrip("/") + "/" + name


def normcase(p: str) -> str:
    """本地比较用的规范化形式（Windows 上大小写不敏感）"""
    return os.path.normcase(posix(p))


def same_path(a: str, b: str) -> bool:
    """两条路径是不是同一个东西（含只差大小写的情况）"""
    return normcase(a) == normcase(b)


class LocalFiles:
    """本地目录的「网盘接口」实现：方法签名与 pan_tools.PanFiles 对齐"""

    def __init__(self, root: str):
        # sandbox 这个名字沿用网盘侧：所有操作都被 validate_plan 限制在它里面，
        # 本地就是用户选的那个文件夹（含子目录），出界一律拒绝
        self.sandbox = posix(root)
        self.last_stop_reason = ""
        # 这一趟为了删掉而去掉了多少个文件的只读属性（执行完报给用户知道）
        self.readonly_cleared = 0

    # --------------------------------------------------------------- 扫描
    def _scan(self, path, recursive, on_progress=None, should_stop=None,
              failures=None):
        from pan_tools import ScanCancelled      # 与网盘扫描同一个「被叫停」异常
        root = posix(path or self.sandbox)
        out, stack = [], [root]
        dirs = files = 0
        while stack:
            d = stack.pop()
            if should_stop and should_stop():
                raise ScanCancelled(dirs, files)
            try:
                with os.scandir(winpath.long_path(d)) as it:
                    ents = list(it)
            except OSError as e:
                # 读不开（权限/被占用）：记下来继续，不能因为一个目录废掉整次扫描，
                # 更不能装作「里面没东西」——那会让空文件夹判定把有内容的目录删掉
                if failures is not None:
                    failures.append({"path": d, "msg": f"目录打不开：{e}"})
                continue
            for e in ents:
                full = join(d, e.name)
                try:
                    isdir = winpath.entry_is_dir(e, winpath.Path(full))
                except OSError:
                    isdir = False
                st = None
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    st = winpath.stat_any(full)     # 长路径兜底
                if isdir:
                    dirs += 1
                else:
                    files += 1
                out.append({"path": full, "name": e.name, "isdir": isdir,
                            "size": int(st.st_size) if st and not isdir else 0,
                            "mtime": int(st.st_mtime) if st else 0})
                if recursive and isdir:
                    stack.append(full)
            if on_progress:
                on_progress(dirs, files, d)
        return out

    def list_dir(self, path: str, recursive: bool = False, limit: int = 1000,
                 on_progress=None, should_stop=None, failures=None):
        """列出目录下条目（文件 + 子目录）。limit 是网盘分页用的，本地忽略"""
        return self._scan(path, recursive, on_progress, should_stop, failures)

    def list_files(self, path: str, recursive: bool = True, on_progress=None,
                   should_stop=None, failures=None):
        return [e for e in self._scan(path, recursive, on_progress,
                                      should_stop, failures) if not e["isdir"]]

    def list_names(self, path: str):
        """该层现有文件名集合（小写），撞名检测用。列不开就返回空集"""
        try:
            with os.scandir(winpath.long_path(posix(path))) as it:
                return {e.name.lower() for e in it}
        except OSError:
            return set()

    def check_path(self, p: str) -> str:
        return posix(p)

    # --------------------------------------------------------------- 目录
    def mkdir(self, path: str) -> bool:
        try:
            os.makedirs(winpath.long_path(posix(path)), exist_ok=True)
            return True
        except OSError as e:
            self.last_stop_reason = f"建目录失败：{e}"
            return False

    # --------------------------------------------------------------- 改名
    def rename_batch(self, ops, ondup="skip", on_progress=None):
        """ops: [{path, newname}] —— 同目录内改名"""
        ok, fails, done = 0, [], 0
        for o in ops:
            src = posix(o["path"])
            parent = src.rpartition("/")[0]
            dst = join(parent, o["newname"])
            try:
                if not same_path(src, dst) and winpath.exists_any(dst):
                    # Windows 上 os.rename 到已存在的目标会直接抛异常，
                    # 这里先判一次好给出人能看懂的原因
                    fails.append({"path": src, "msg": "目标已存在（重名），没改名"})
                else:
                    winpath.rename_any(src, dst)
                    ok += 1
            except OSError as e:
                fails.append({"path": src, "msg": f"改名失败：{e}"})
            done += 1
            if on_progress and done % _REPORT_EVERY == 0:
                on_progress(_REPORT_EVERY, 0)
        if on_progress and done % _REPORT_EVERY:
            on_progress(done % _REPORT_EVERY, 0)
        return ok, fails

    # --------------------------------------------------------------- 移动
    def move_batch(self, ops, ondup="skip", on_progress=None):
        """ops: [{path, dest, newname?}] —— dest 是目标**目录**"""
        ok, fails, done = 0, [], 0
        for o in ops:
            src = posix(o["path"])
            dest = posix(o["dest"])
            name = o.get("newname") or src.rpartition("/")[2]
            dst = join(dest, name)
            try:
                if not same_path(src, dst) and winpath.exists_any(dst):
                    fails.append({"path": src, "msg": f"目标已有同名文件（重名），没搬：{dst}"})
                else:
                    os.makedirs(winpath.long_path(dest), exist_ok=True)
                    # shutil.move：跨盘也能搬（os.rename 跨盘会失败）
                    winpath.move_any(src, dst)
                    ok += 1
            except OSError as e:
                fails.append({"path": src, "msg": f"移动失败：{e}"})
            done += 1
            if on_progress and done % _REPORT_EVERY == 0:
                on_progress(_REPORT_EVERY, 0)
        if on_progress and done % _REPORT_EVERY:
            on_progress(done % _REPORT_EVERY, 0)
        return ok, fails

    # --------------------------------------------------------------- 删除
    def _rmdir_tree(self, p: str):
        r"""整棵子树里一个文件都没有 ⇒ 自下而上删干净。

        给「计划里只有外层那一条」兜的底：正常流程的空文件夹计划会把嵌套的每层
        都列出来（expand=True），删到父目录时子目录已经没了；但**旧预览**、手工
        清单里可能只有外层那条，那时 rmdir 会因为「里面还有 N 项」整批失败。

        住手条件一律从严（只要有一丝看不清，就不删）：
          · 子树里出现任何一个**文件** → 不删（非空目录本来就不该由这个功能删）
          · 子是**联接/符号链接**（junction）→ 不删：它可能指向别的盘，删链接没意义
          · 任何一层 **scandir 读不开** → 不删
          · 中途 rmdir 失败 → 立刻停，已经删掉的那几层如实算成功
        """
        _REPARSE = 0x0400
        order, stack = [], [p]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(winpath.long_path(d)) as it:
                    ents = list(it)
            except OSError as e:
                return False, f"目录读不开，没删：{e}"
            order.append(d)
            for e in ents:
                sub = join(d, e.name)
                try:
                    isdir = winpath.entry_is_dir(e, winpath.Path(sub))
                except OSError:
                    isdir = False
                if not isdir:
                    return False, f"里面还有文件 {e.name}，没敢删"
                try:
                    attr = os.lstat(winpath.long_path(sub)).st_file_attributes
                except OSError:
                    return False, f"子目录 {e.name} 看不清，没敢删"
                if attr & _REPARSE:
                    return False, f"里面有目录联接 {e.name}，没敢删（可能指向别的地方）"
                stack.append(sub)
        for d in reversed(order):       # 自下而上：先干叶子，父目录自然变空
            try:
                os.rmdir(winpath.long_path(d))
            except OSError as e:
                return False, f"删除失败：{e}"
        return True, ""

    def _unlink(self, p: str):
        """删一个文件。成功返回 None，失败返回给人看的一句话

        ★ 只读文件必须先摘掉只读属性：NTFS 上 os.remove 对只读文件直接甩
        [WinError 5]「拒绝访问」，而它跟「没权限」「被别的程序占着」长得一模
        一样——不处理的话用户只看到一句拒绝访问，压根想不到是文件带只读属性
        （实测：下载工具/解压出来的文件里有一批就带这个属性，23 万个里撞到
        两千多个）。用户让删，等于连「只读」这道保险一起撤了，所以直接摘。

        摘了仍然删不掉就把属性**恢复回去**：宁可留着原样，也不白改人家的文件。
        """
        lp = winpath.long_path(p)
        try:
            os.remove(lp)
            return None
        except OSError as e:
            if getattr(e, "winerror", None) not in (5, 13):
                return del_fail_msg(p, e)
            if not is_readonly(p):
                return del_fail_msg(p, e)
            if not _set_readonly(p, False):
                return del_fail_msg(p, e)
            try:
                os.remove(lp)
                self.readonly_cleared += 1
                return None
            except OSError as e2:
                _set_readonly(p, True)
                return del_fail_msg(p, e2)

    def delete_batch(self, paths, on_progress=None):
        """删文件 / 删**空**目录。

        目录不是简单 rmdir 就完事：空文件夹之间常常互相套着（A 里只有 B、C 两个
        空目录），这时候挨个 rmdir 会因为「里面还有 N 项」整批失败。所以：
          ① 本批内的目录按**深度倒序**处理，先干深的，轮到父目录时它已经空了；
          ② 万一还是非空（旧预览里只有外层那条），走 _rmdir_tree：确认整棵子树
             一个文件都没有才自上而下删，里面有文件就一条不动。
        绝不 rmtree —— 那等于把说好不碰的文件连根拔掉。
        """
        ok, fails, done = 0, [], 0
        todo = list(paths or [])
        dirs, others = [], []
        for raw in todo:
            (dirs if winpath.is_dir_any(posix(raw)) else others).append(raw)
        dirs.sort(key=lambda x: -posix(x).count("/"))    # 深层在前
        for raw in dirs + others:
            p = posix(raw)
            try:
                if winpath.is_dir_any(p):
                    left = None
                    try:
                        with os.scandir(winpath.long_path(p)) as it:
                            left = [x.name for x in it]
                    except OSError as e:
                        fails.append({"path": p, "msg": f"目录读不开，没删：{e}"})
                        done += 1
                        continue
                    if not left:
                        os.rmdir(winpath.long_path(p))
                        ok += 1
                    else:
                        good, msg = self._rmdir_tree(p)
                        if good:
                            ok += 1
                        else:
                            fails.append({"path": p, "msg": msg})
                elif winpath.exists_any(p):
                    msg = self._unlink(p)
                    if msg:
                        fails.append({"path": p, "msg": msg})
                    else:
                        ok += 1
                else:
                    fails.append({"path": p, "msg": "文件不存在（可能已经被删过）"})
            except OSError as e:
                fails.append({"path": p, "msg": del_fail_msg(p, e)})
            done += 1
            if on_progress and done % _REPORT_EVERY == 0:
                on_progress(_REPORT_EVERY, 0)
        if on_progress and done % _REPORT_EVERY:
            on_progress(done % _REPORT_EVERY, 0)
        return ok, fails


def file_attrs(p: str) -> int:
    """文件属性位。取不到（不存在 / 读不了）返回 -1"""
    try:
        return os.stat(winpath.long_path(posix(p))).st_file_attributes
    except (OSError, AttributeError):
        return -1


def is_readonly(p: str) -> bool:
    return bool(file_attrs(p) & _ATTR_READONLY)


def _set_readonly(p: str, on: bool) -> bool:
    """加/去只读属性。Windows 的 os.chmod 只认读写位，够用"""
    try:
        os.chmod(winpath.long_path(posix(p)),
                 stat.S_IREAD if on else (stat.S_IREAD | stat.S_IWRITE))
        return True
    except OSError:
        return False


def del_fail_msg(p: str, e) -> str:
    """把 Windows 那几句没头没尾的错翻成人话，并说清下一步该怎么办"""
    we = getattr(e, "winerror", None)
    if we == 32:
        return (f"文件正被别的程序打开着，删不掉：{e}——关掉看图/编辑/播放器再试")
    if we in (5, 13):
        return (f"拒绝访问，删不掉：{e}——已试过去掉只读属性；还不行多半是被别的"
                f"程序锁着（杀毒/索引/网盘客户端都可能占着）")
    return f"删除失败：{e}"


def list_drives():
    """Windows 盘符列表（作为本地浏览的最顶层）。非 Windows 返回根目录"""
    if os.name != "nt":
        return ["/"]
    import string
    out = []
    for ch in string.ascii_uppercase:
        root = f"{ch}:/"
        try:
            if winpath.is_dir_any(root):
                out.append(root)
        except OSError:
            continue
    return out
