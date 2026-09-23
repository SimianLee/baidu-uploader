# -*- coding: utf-8 -*-
r"""Windows 路径工具：长路径（MAX_PATH）安全访问。

为什么需要这个模块：路径超过 260 字符时，所有老式 Win32 API 都直接报
「系统找不到指定的路径」(ERROR_PATH_NOT_FOUND = 3)。要命的不是这个报错，而是
Python 的 ``Path.is_file() / exists()`` 会把异常吞掉、返回 False —— 长路径文件
在**扫描阶段**就被当成「这不是个文件」静默跳过：不报错、不进统计、界面上什么
都不显示。

实例：某目录磁盘上实有 1142 个文件，上传工具只看到 1135 个，剩下 7 个（路径
264~286 字符）从来没被上传过，而界面一直说「扫描到的文件此前全都上传成功过了」。

加 ``\\?\`` 前缀是唯一不用改注册表、不需要管理员权限的解法（长度上限提到约
32767）。本模块把这件事收在一处，上传、改名等所有碰本地文件的工具统一用。
"""
import os
import stat as _stat
from pathlib import Path

LONG_PATH_MIN = 240          # 超过这个长度就上前缀（留点余量，别卡在 260 边缘）
LP_PREFIX = "\\\\?\\"
LP_UNC = "\\\\?\\UNC\\"


def long_path(p) -> str:
    r"""把路径转成可安全访问的形式：Windows 下必要时加 \\?\ 前缀。

    非 Windows、或路径不长时原样返回，调用方不必写分支。前缀要求绝对路径、
    反斜杠分隔、且不做 . 与 .. 归一化 —— Path 对象 str() 出来的形式正好满足。
    """
    s = str(p)
    if os.name != "nt" or s.startswith(LP_PREFIX) or len(s) < LONG_PATH_MIN:
        return s
    if s.startswith("\\\\"):          # UNC：\\server\share -> \\?\UNC\server\share
        return LP_UNC + s[2:]
    if not os.path.isabs(s):
        s = os.path.abspath(s)
    return LP_PREFIX + s.replace("/", "\\")


def stat_any(p):
    """stat 路径：先试原生，失败再试长路径。都读不到就返回 None（不抛异常）。

    判定「是不是文件/目录」「多大」一律走这里，别用 Path.is_file() —— 它会把
    长路径的异常吞成 False，让文件凭空消失。
    """
    try:
        return os.stat(p)
    except OSError:
        pass
    lp = long_path(p)
    if lp != str(p):
        try:
            return os.stat(lp)
        except OSError:
            pass
    return None


def exists_any(p) -> bool:
    """存在性判断（长路径安全）。"""
    return stat_any(p) is not None


def is_dir_any(p) -> bool:
    """是不是目录（长路径安全）。"""
    st = stat_any(p)
    return st is not None and _stat.S_ISDIR(st.st_mode)


def open_any(p, mode="rb", **kw):
    """open 本地文件（长路径安全）。上传读文件、写完删文件都该走这里。"""
    lp = long_path(p)
    if lp == str(p):
        return open(p, mode, **kw)
    try:
        return open(lp, mode, **kw)
    except OSError:
        return open(p, mode, **kw)    # 前缀被个别重定向盘拒绝时退回原生


def move_any(src, dst):
    """移动/改名（长路径安全）。"""
    import shutil
    return shutil.move(long_path(src), long_path(dst))


def rename_any(src, dst):
    """改名（长路径安全）。撞名在 Windows 上会抛 FileExistsError，不会覆盖。"""
    return os.rename(long_path(src), long_path(dst))


def rel_display(p, root) -> str:
    """相对 root 的展示名（拿不到相对路径时退回全路径）"""
    try:
        return str(Path(p).relative_to(root))
    except Exception:
        return str(p)


def entry_is_dir(e, full: Path) -> bool:
    """这条目录项是不是目录（长路径安全）。

    DirEntry.is_dir() 正常情况下用的是枚举时缓存下来的属性，不额外发系统调用，
    所以哪怕完整路径超 260 也判得准；万一缓存不可用会退化成 stat（那就会失败），
    所以再兜一层 stat_any。
    """
    try:
        return e.is_dir(follow_symlinks=False)
    except OSError:
        st = stat_any(full)
        return st is not None and _stat.S_ISDIR(st.st_mode)


def iter_entries(root, recursive=True):
    """列出 root 下的所有条目（文件 + 子目录），按路径排序。

    刻意不用 Path.rglob() 也不用 os.walk()：它们向下走子目录时用的是原生路径，
    一旦某层路径超过 260，scandir 当场失败、**整棵子树被静默跳过**（连文件名都
    列不出来）。这里自己用 os.scandir(long_path(dir)) 一层层走，路径多长都进得去。
    """
    root = Path(root)
    out, stack = [], [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(long_path(d)) as it:
                entries = list(it)
        except OSError:
            continue                 # 目录读不开：与 os.walk 的默认行为一致，跳过
        for e in entries:
            p = d / e.name
            out.append(p)
            if recursive and entry_is_dir(e, p):
                stack.append(p)
    out.sort()
    return out
