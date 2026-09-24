# -*- coding: utf-8 -*-
r"""proclock.py —— 上传进程锁（upload.pid）的判活与读写

为什么单独一个模块：判活这段逻辑原本在 webui.py 和 organize_all.py 里各写
了一份。同一件事写两遍，就一定会在其中一份上出错。

真实的坑（实测过）：Windows 上「能不能 OpenProcess 到这个 PID」**不等于**
「这个进程还活着」。只要还有任何一个句柄指向进程对象，对象就继续存在 ——
最典型的就是父进程里的 subprocess.Popen：面板启动了上传子进程，Popen 对象
一直挂在 _state["proc"] 上不释放，子进程退出后 OpenProcess 照样成功。
于是刚跑完（或被强杀）的上传被误判成「还在运行」，面板从此拒绝启动新上传，
必须重启面板才能解开。

实测数据（Windows 10，同一个刚 wait() 返回的子进程）：

    OpenProcess            -> OK      ← 骗人的
    GetExitCodeProcess     -> 0       ← 真实退出码
    活着时 GetExitCodeProcess -> 259  （STILL_ACTIVE）

所以判活必须再问一次 GetExitCodeProcess，只认 259。

锁文件由 upload_baidu.acquire_pidfile 写入、靠 atexit 在正常退出时删除；
被强杀（蓝屏 / 关机 / 任务管理器）时删不掉，会残留 —— 残留本身无害，
只要判活可靠，「残留」和「真在跑」就能分得清。
"""
import os
import subprocess

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259          # GetExitCodeProcess 的「进程还在跑」退出码


def pid_alive(pid: int) -> bool:
    """PID 是否真的还在运行。

    注意别改成"OpenProcess 成功就是活着" —— 父进程持有句柄时，已退出的
    子进程依然能 OpenProcess 成功（见模块开头实测记录）。
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        try:
            # 只要能查询退出码的权限即可，不要 PROCESS_ALL_ACCESS：
            # 别的东西（比如高权限进程）不该因为权限不足被误判成"不存在"
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                k.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)      # 信号 0：只做存在性检查，不真的发信号
        return True
    except Exception:
        return False


def read_pidfile(path) -> int:
    """读锁文件，返回其中**确实还活着**的 PID；文件不存在 / 内容坏了 / 进程
    已退出都返回 0。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            pid = int(f.read().strip() or 0)
    except Exception:
        return 0
    return pid if pid_alive(pid) else 0


def write_pidfile(path, pid: int = None) -> bool:
    """把 PID 写进锁文件（默认写自己）"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(pid if pid is not None else os.getpid()))
        return True
    except Exception:
        return False


def kill_pid(pid: int) -> bool:
    """按 PID 结束进程（Windows 用 taskkill，带子进程一起收）"""
    try:
        if os.name == "nt":
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=20)
            return r.returncode == 0
        os.kill(int(pid), 15)
        return True
    except Exception:
        return False
