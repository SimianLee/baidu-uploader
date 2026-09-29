# -*- coding: utf-8 -*-
r"""
pan_tools.py —— 百度网盘（应用沙盒）文件操作库
=============================================

定位：给「分批上传工具」补上网盘的**整理 / 批量改名 / 批量删除**能力。

为什么只做沙盒：百度开放平台 2026-06-03 之后新建的应用，filemanager 等接口
只能操作 `/apps/<应用名>/` 沙盒目录，做不了全盘；全盘整理需要走 alist（NAS）。
所以这里所有操作都**强制校验路径必须落在沙盒根目录内**，越界直接拒绝。

能力：
  list_dir(path, recursive)        递归列目录
  mkdir(path)                      建目录（自动建父目录）
  rename_batch(ops)                批量改名（filemanager rename）
  move_batch(ops)                  批量移动
  delete_batch(paths)              批量删除（危险，调用方必须二次确认）

以及「计划」构建（先出计划、预览、再执行，绝不直接改）：
  build_rename_plan(files, mode, params, occupied)  → (ops, unchanged, auto)
  build_organize_plan(files, by, dest)
  build_delete_plan(files, filters)
  build_empty_dir_plan(entries, root, skip)   找出空目录（递归为空的）

改名五模式：replace（查找替换）/ affix（加前后缀）/ serial（序号）
          / regex（正则捕获组）/ clean（去广告与括号）/ title（只保留书名）

命名规则（rename_one / title_name）也被 local_rename.py 复用来改本地文件名——
两边必须是同一套规则，否则「先改名再上传」和「上传后在网盘改名」结果会不一样。

依赖：requests；Token 复用 upload_baidu.py 的 BaiduAuth（token.json）。
"""

import re
import time
import unicodedata
from pathlib import PurePosixPath

import requests

FILEMANAGER = "https://pan.baidu.com/rest/2.0/xpan/file?method=filemanager"
LIST_URL = "https://pan.baidu.com/rest/2.0/xpan/file?method=list"
CREATE_URL = "https://pan.baidu.com/rest/2.0/xpan/file?method=create"

# 百度 filemanager 单次最多提交多少个（留余量，官方上限 1000）
BATCH_LIMIT = 100

# 递归扫描列目录的并发线程数。列目录是纯只读，2T 网盘几千个目录串行列要好
# 几分钟，正是「生成预览慢」的主因。4 路对百度频控友好（_get 里本来就有
# 31034 的 20s 退避重试兜底）。
LIST_WORKERS = 4

# 整批报错的条目要复核几遍、每遍之间等多久。百度的目录索引有延迟：刚搬完
# 立刻列目录，源处可能还挂着旧名字，第一遍核对会把「已经搬走了」误判成
# 「没生效」（实测 1612 条全被误判）。所以隔几秒再核一遍，连续两遍都判
# 没生效才算真失败。详见 _verify_uncertain。
VERIFY_ROUNDS = 2
VERIFY_WAIT = 4.0

# 这些错误码**不能只看响应就判失败**——它们都能用网盘上的事实证伪。
# 真事：一次 2000 条的整理归档，界面报「移动 388 / 失败 1612」，失败里
# 1300 条 errno=-9「文件不存在」、200 条 111、100 条 31033、12 条 -8。
# 事后把 1612 条全查了一遍：1366 条**早就躺在新目录里了**，其中 -9 那 1300 条
# 里有 1267 条是搬走了的——百度把成功报成了失败。而这些错码是 info 里**逐条**
# 给的（不是整批空 info），以前只有整批报错才标 uncertain，于是这 1612 条
# 一条都没进复核，全被当成真失败。所以凡是下面这些码，一律先记「存疑」，
# 执行完由 _verify_uncertain 到网盘上核对真假。
_VERIFIABLE_ERRNOS = {-7, -8, -9, 12, 111, 31033, 31034, 31061, 31066}


class PanError(Exception):
    pass


class ScanCancelled(Exception):
    """扫描被用户主动停止（只读操作，没有对网盘做任何改动）

    dirs/files 记录中断时已经走了多远，面板据此回显「已停止（扫了 N 个目录）」。"""

    def __init__(self, dirs: int = 0, files: int = 0):
        self.dirs = dirs
        self.files = files
        super().__init__(f"已停止扫描（已遍历 {dirs} 个目录 / 找到 {files} 个文件）")


class PanFiles:
    """百度网盘沙盒内的文件操作封装"""

    def __init__(self, access_token: str, sandbox: str, timeout: int = 30):
        self.token = access_token
        self.sandbox = sandbox.rstrip("/")     # 例如 /apps/baidu_uploader
        self.timeout = timeout
        # 最近一次批量提交为什么提前停了（连接失效时填），供调用方如实告知用户
        self.last_stop_reason = ""

    # ---------- 安全校验 ----------
    def check_path(self, path: str) -> str:
        """所有待操作路径必须落在沙盒内，越界直接抛异常（防误删整盘）"""
        p = (path or "").strip()
        if not p.startswith("/"):
            p = "/" + p
        if p != self.sandbox and not p.startswith(self.sandbox + "/"):
            raise PanError(f"路径越界，只允许操作沙盒目录 {self.sandbox} 内的文件：{path}")
        return p

    # ---------- 基础请求 ----------
    def _get(self, url, params, retry=2):
        params = {**params, "access_token": self.token}
        for i in range(retry + 1):
            try:
                r = requests.get(url, params=params, timeout=self.timeout).json()
            except Exception as e:
                if i >= retry:
                    raise PanError(f"网络异常: {e}")
                time.sleep(3)
                continue
            errno = r.get("errno", 0)
            if errno == 31034 and i < retry:
                time.sleep(20)
                continue
            return r
        return r

    def _post(self, url, params, data=None, retry=2):
        params = {**params, "access_token": self.token}
        for i in range(retry + 1):
            try:
                r = requests.post(url, params=params, data=data,
                                  timeout=self.timeout).json()
            except Exception as e:
                if i >= retry:
                    raise PanError(f"网络异常: {e}")
                time.sleep(3)
                continue
            errno = r.get("errno", 0)
            if errno == 31034 and i < retry:
                time.sleep(20)
                continue
            return r
        return r

    # ---------- 列目录 ----------
    def list_dir(self, path: str, recursive: bool = False, limit: int = 1000,
                 on_progress=None, should_stop=None, failures=None,
                 workers=None):
        """列出目录内容；recursive=True 时递归全部子目录。
        返回 [{path, name, size, isdir, mtime}]

        on_progress(dirs_done, files_seen, current_dir) 每列完一个目录回调一次。
        目录总数事先未知，只能报「已处理多少」，面板据此显示扫描进度。

        should_stop() 返回真值时立刻抛 ScanCancelled 中断扫描（用户点了「停止」）。
        检查点放在「每层目录开始前」和「同层分页拉取前」——后者保证一个几万条的
        大目录在翻页途中也能被刹住，而不是必须读完这一层。

        failures: 传一个 list 进来即开启**容错模式**。子目录列不开时不再中断整次扫描，
                  而是把 {"path", "errno", "msg"} 追加进去后跳过，继续扫别的。
                  为什么需要：实测百度会返回这种脏条目——父目录的列表里有它
                  （isdir=1），拿它自己的 path 去列却稳定回 -9；试过 trim 尾部空白、
                  全角空格换半角、去掉全角空格等变体全部无效（见 _probe_ls2.py：
                  69 个子目录里就 1 个这样）。几千个目录的大扫描里只要摊上一个，
                  整次预览就全废了，而它跟其余 99.9% 的目录毫无关系。
                  调用方拿到的是同一个 list 对象（可变），不需要接返回值。
                  注意：**起点目录**列不开时无论是否容错都照样抛错——那说明路径或
                  沙盒配置本身有问题，必须让用户当场看见，而不是给个空结果。

        workers: 递归扫描的并发线程数（默认 LIST_WORKERS=4）。列目录是纯只读，
                  2T 网盘几千个目录串行列要好几分钟，是「生成预览慢」的主因；
                  4 路并发对百度频控也友好（_get 里本来就有 31034 的退避重试）。
                  workers<=1 时走原来的串行路径，行为分毫不变。
        """
        root = self.check_path(path)
        w = LIST_WORKERS if workers is None else workers
        if not recursive or w <= 1:
            return self._list_seq(root, recursive, limit, on_progress,
                                  should_stop, failures)
        return self._list_conc(root, w, limit, on_progress,
                               should_stop, failures)

    def _list_one(self, d, limit, should_stop):
        """列单个目录（含翻页）。返回 (rows, 子目录列表, errno)。
        errno != 0 时 rows/subs 为空；should_stop 命中时抛 ScanCancelled。"""
        rows, subs = [], []
        start = 0
        while True:
            if should_stop and should_stop():
                raise ScanCancelled(0, 0)
            r = self._get(LIST_URL, {"dir": d, "start": start,
                                     "limit": limit, "order": "name"})
            e = r.get("errno", 0)
            if e != 0:
                return [], [], e
            items = r.get("list", [])
            for it in items:
                rec = {
                    "path": it.get("path", ""),
                    "name": it.get("server_filename", ""),
                    "size": it.get("size", 0) or 0,
                    "isdir": bool(it.get("isdir")),
                    "mtime": it.get("server_mtime") or it.get("local_mtime") or 0,
                }
                rows.append(rec)
                if rec["isdir"]:
                    subs.append(rec["path"])
            if len(items) < limit:
                return rows, subs, 0
            start += limit

    def _list_seq(self, root, recursive, limit, on_progress,
                  should_stop, failures):
        """串行递归扫描（workers<=1 / 非递归时用），行为与老版本一致"""
        out, stack, dirs_done, files_seen = [], [root], 0, 0
        while stack:
            d = stack.pop()
            rows, subs, e = self._list_one(d, limit, should_stop)
            if e != 0:
                # 这几种是全局性的（授权坏了 / 被限流），跳过没有任何意义：
                # 后面每个目录都会同样失败，此时静默跳过只会给出一个假结果
                if (failures is not None and d != root
                        and e not in _FATAL_LIST_ERRNOS):
                    if len(failures) < 500:     # 兜个上限，别让响应体爆炸
                        failures.append({"path": d, "errno": e,
                                         "msg": errno_text(e)})
                    dirs_done += 1
                    if on_progress:
                        try:
                            on_progress(dirs_done, files_seen, d)
                        except Exception:
                            pass
                    continue
                raise PanError(list_dir_error(d, e))
            out.extend(rows)
            stack.extend(subs)
            files_seen += sum(1 for r in rows if not r["isdir"])
            dirs_done += 1
            if on_progress:
                try:
                    on_progress(dirs_done, files_seen, d)
                except Exception:
                    pass        # 进度回调只是锦上添花，出错绝不能中断扫描
            if not recursive:
                break
        if should_stop and should_stop():       # 收尾前最后一道检查
            raise ScanCancelled(dirs_done, files_seen)
        return out

    def _list_conc(self, root, workers, limit, on_progress,
                   should_stop, failures):
        """并发递归扫描：N 个线程从共享待办栈里抢目录列。

        线程安全要点：out/failures 的 append、计数器累加在 GIL 下是原子的；
        待办栈和结束判定必须拿条件变量——「队列空了且没有在途的目录」才算扫完。
        任何线程遇到致命错误（起点列不开 / 授权坏 / 被停止）就把错误记下并
        清空待办，其他线程在下一个检查点退出，主线程统一重抛。
        """
        import threading

        out = []
        cv = threading.Condition()
        todo = [root]
        inflight = [0]          # 正在列的目录数
        dirs_done = [0]
        files_seen = [0]
        first_err = [None]      # 第一个致命错误（PanError / ScanCancelled）

        def worker():
            while True:
                with cv:
                    while (not todo and inflight[0] > 0
                           and first_err[0] is None
                           and not (should_stop and should_stop())):
                        cv.wait(0.25)
                    if first_err[0] is not None:
                        return
                    if should_stop and should_stop():
                        if first_err[0] is None:
                            first_err[0] = ScanCancelled(dirs_done[0],
                                                         files_seen[0])
                        return
                    if not todo:
                        return              # 没活干也没人在干 → 扫完了
                    d = todo.pop()
                    inflight[0] += 1
                    cv.notify_all()
                try:
                    try:
                        rows, subs, e = self._list_one(d, limit, should_stop)
                    except ScanCancelled:
                        with cv:
                            if first_err[0] is None:
                                first_err[0] = ScanCancelled(dirs_done[0],
                                                             files_seen[0])
                            cv.notify_all()
                        return
                    if e != 0:
                        if (failures is not None and d != root
                                and e not in _FATAL_LIST_ERRNOS):
                            if len(failures) < 500:
                                failures.append({"path": d, "errno": e,
                                                 "msg": errno_text(e)})
                            with cv:
                                dirs_done[0] += 1
                                cv.notify_all()
                            if on_progress:
                                try:
                                    on_progress(dirs_done[0], files_seen[0], d)
                                except Exception:
                                    pass
                        else:
                            with cv:
                                if first_err[0] is None:
                                    first_err[0] = PanError(list_dir_error(d, e))
                                cv.notify_all()
                            return
                    else:
                        out.extend(rows)
                        with cv:
                            todo.extend(subs)
                            dirs_done[0] += 1
                            files_seen[0] += sum(1 for r in rows
                                                 if not r["isdir"])
                            cv.notify_all()
                        if on_progress:
                            try:
                                on_progress(dirs_done[0], files_seen[0], d)
                            except Exception:
                                pass
                finally:
                    with cv:
                        inflight[0] -= 1
                        cv.notify_all()

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(max(1, workers))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if first_err[0] is not None:
            raise first_err[0]
        if should_stop and should_stop():
            raise ScanCancelled(dirs_done[0], files_seen[0])
        return out

    def list_files(self, path: str, recursive: bool = True, on_progress=None,
                   should_stop=None, failures=None):
        """只要文件，不要目录（failures 的语义见 list_dir）"""
        return [f for f in self.list_dir(path, recursive=recursive,
                                         on_progress=on_progress,
                                         should_stop=should_stop,
                                         failures=failures)
                if not f["isdir"]]

    # ---------- 建目录 ----------
    def mkdir(self, path: str) -> bool:
        p = self.check_path(path)
        r = self._post(CREATE_URL, {}, data={"path": p, "isdir": 1, "rtype": 0})
        return r.get("errno", 0) in (0, -8, 12)     # -8/12 = 已存在

    # ---------- filemanager 批量操作 ----------
    def _alive(self) -> bool:
        """轻量健康检查：token / 连接还好使吗。

        只在「整批整批地报错」时用来分辨两种完全不同的情况：
          · 连接好使 → 是百度的**假失败**（操作其实生效了），该批记下来事后核对
          · 连接也坏 → 是真的断了（token 过期等），后面再提交也是白跑，必须停
        """
        try:
            r = self._get(LIST_URL, {"dir": self.sandbox, "start": 0, "limit": 1})
        except Exception:
            return False
        return r.get("errno", 0) == 0

    def _filemanager(self, opera: str, filelist, ondup: str = "skip",
                     on_progress=None):
        """提交一批操作，返回 (成功数, 失败明细列表)

        filelist 超过 BATCH_LIMIT 会自动拆成多批提交，每批发一次请求。
        每批处理完调一次 on_progress(本批条数, 本批失败条数)：
        一次几百条要等好几分钟，没有进度反馈会让人以为卡死了。
        失败条数必须由这里给出——调用方要等本方法返回后才拿得到 fails，
        等它统计的话进度里的失败数会滞后一批。

        **整批报错 ≠ 整批失败**：百度 filemanager 在高并发（比如同时还有上传任务
        在跑）、大批量时会整批回一个错误码，而 info 是空的；实测一次 2000 条的
        批量移动里，18 批分别回了 -9 和 111，但源目录里那 2000 个文件一个不剩、
        全都到了目标目录。所以这里的处理是：
          · 该批标 uncertain（结果未知），不直接算失败——由 apply_plan 执行完
            以后按磁盘事实核对
          · 如果是连接级错误码（-6/111/31034）就先探一下连接：真断了立刻停，
            剩下的批次如实标「未执行」，不做无谓的重复提交
        """
        import json as _json
        ok, fails = 0, []
        link_ok = None          # None=还没探过；探过且正常就不再重复探
        stop_reason = ""
        for i in range(0, len(filelist), BATCH_LIMIT):
            chunk = filelist[i:i + BATCH_LIMIT]
            fails_before = len(fails)
            r = self._post(FILEMANAGER, {"opera": opera, "async": "0"}, data={
                "filelist": _json.dumps(chunk, ensure_ascii=False),
                "ondup": ondup,
            })
            errno = r.get("errno", 0)
            info = [x for x in (r.get("info") or []) if isinstance(x, dict)]
            if errno == 0 or info:
                # 同步模式下 info 里是逐条结果。errno=0 表示这批全成功；
                # errno=12 是「部分失败」，逐条结果同样在 info 里——必须按条统计，
                # 否则成功的也会被算成失败，真正的原因码（-8 撞名 / -9 不存在）也丢了。
                judged = set()
                for it_ in info:
                    judged.add(it_.get("path"))
                    ec = it_.get("errno", 0)
                    if ec != 0:
                        rec = {"path": it_.get("path", "?"), "errno": ec,
                               "msg": errno_text(ec)}
                        # 逐条给的错误码同样可能是假的（实测 1300 条 -9 里
                        # 1267 条其实已经搬走）。可证伪的码先标存疑，事后核对
                        if ec in _VERIFIABLE_ERRNOS:
                            rec["uncertain"] = True
                        fails.append(rec)
                if errno != 0:      # info 没覆盖到的条目，结论只能看外层 errno
                    for it in chunk:
                        if it.get("path") not in judged:
                            fails.append({"path": it.get("path"), "errno": errno,
                                          "msg": errno_text(errno),
                                          "uncertain": True})
                ok += len(chunk) - (len(fails) - fails_before)
            else:
                # 请求级错误：info 是空的，这批到底动没动，只有网盘自己知道。
                # 连接级错误码先验证连接，别在真断线时把剩下几十批白跑一遍
                if errno in _FATAL_LIST_ERRNOS:
                    if link_ok is None:
                        link_ok = self._alive()
                    if not link_ok:
                        stop_reason = f"连接已失效（{errno_text(errno)}）"
                        for it in filelist[i:]:
                            fails.append({
                                "path": it.get("path") if isinstance(it, dict) else it,
                                "errno": None, "not_run": True,
                                "msg": f"未执行（{stop_reason}）"})
                        break
                for it in chunk:
                    fails.append({"path": it.get("path") if isinstance(it, dict) else it,
                                  "errno": errno, "msg": errno_text(errno),
                                  "uncertain": True})
            if on_progress:
                try:
                    on_progress(len(chunk), len(fails) - fails_before)
                except Exception:
                    pass        # 进度回调只是锦上添花，出错绝不能中断执行
        if stop_reason:
            self.last_stop_reason = stop_reason
        return ok, fails

    def rename_batch(self, ops, ondup="skip", on_progress=None):
        """ops: [{path, newname}]"""
        clean = []
        for o in ops:
            p = self.check_path(o["path"])
            clean.append({"path": p, "newname": o["newname"]})
        return self._filemanager("rename", clean, ondup=ondup,
                                 on_progress=on_progress)

    def move_batch(self, ops, ondup="skip", on_progress=None):
        """ops: [{path, dest, newname?}]  dest 为目标**目录**；newname 可选——
        搬过去的同时改名，一步到位（实测百度 filemanager move 支持 newname，
        见 2026-09-28 试验场实验）。撞名自动编号靠它把「改名+移动」合成一条，
        十万条量级下操作数直接砍半。"""
        clean = []
        for o in ops:
            p = self.check_path(o["path"])
            it = {"path": p, "dest": self.check_path(o["dest"])}
            nn = o.get("newname")
            if nn and nn != p.rsplit("/", 1)[-1]:
                it["newname"] = nn
            clean.append(it)
        return self._filemanager("move", clean, ondup=ondup,
                                 on_progress=on_progress)

    def delete_batch(self, paths, on_progress=None):
        clean = [{"path": self.check_path(p)} for p in paths]
        return self._filemanager("delete", clean, on_progress=on_progress)


def errno_text(errno):
    # 逐条明细里只报简短原因；「百度常假报」这类解释统一放在执行结果提示里，
    # 每条明细都带一遍会啰嗦到看不清
    return {
        0: "成功", -6: "Token 无效", -7: "文件或目录名非法", -8: "文件已存在",
        -9: "文件不存在", -10: "云盘容量不足", -11: "文件超长", -12: "文件名非法",
        2: "参数错误", 12: "批量操作部分失败", 111: "Token 过期",
        31034: "命中频控", 31061: "文件不存在", 31066: "文件不存在或无权限",
    }.get(errno, f"错误码 {errno}")


# 列目录时「跳过没意义、必须当场报错」的几种错误：授权类是全局性的（后面每个
# 目录都会同样失败），31034 是限流（_get 里已经退避重试过，还失败说明真被限住了，
# 继续扫下去只会满屏失败、给出一个假的「扫完了」）
_FATAL_LIST_ERRNOS = {-6, 111, 31034}


def list_dir_error(path: str, errno) -> str:
    """把「列目录失败」翻成人话。

    之前是直接把原始响应 JSON 甩出去（errno=-9 {'errno': -9, 'request_id': ...}），
    用户看到一串方块字码点，既不知道出了什么事，也不知道该怎么办。
    """
    why = {
        -6: "授权已失效，请先重新授权",
        111: "授权已过期，请先重新授权",
        31034: "请求太频繁，被百度限流了，歇几分钟再试",
        -9: "目录不存在，可能已被移动或改名",
        31061: "目录不存在，可能已被移动或改名",
        31066: "目录不存在，或没有访问权限",
        -7: "目录名不合法",
    }.get(errno, errno_text(errno))
    return f"列目录失败（{why}）：{path}"


# ===========================================================================
# 改名：五种模式
# ===========================================================================

# 常见下载站/资源站的广告尾巴（clean 模式用）
_AD_PATTERNS = [
    # 1. 方括号/圆括号里带网址的整块广告：[www.xxx.com]、[xxx.com整理]
    r"[\[\(【][^\]\)】]{0,40}(www\.|http|https|\.com|\.net|\.cn|\.org)[^\]\)】]{0,40}[\]\)】]",
    # 2. 裸网址 / 域名（含 http://、www. 前缀，或纯域名）
    r"[\(（]?\s*(?:https?://)?(?:www\.)?[\w\-]+\.(?:com|cn|net|org|io|me|top|xyz)(?:\.[a-z]{2,3})?(?:/[\w\-./?%&=]*)?\s*[\)）]?",
    # 3. 书名号/方括号里的资源站套话：【免费下载】、【xxx首发】
    r"[【\[][^】\]]{0,60}(?:首发|独家|整理|收集|电子书|txt下载|免费下载|全集|完结)[^】\]]{0,60}[】\]]",
    # 4. 网址后面拖着的中文尾巴：——收集整理 / -整理 / —首发
    r"[—–\-_]{1,2}\s*(?:收集整理|整理收集|收集|整理|首发|独家|出品|制作|发布)",
    # 5. 纯长串域名（无分隔符时兜底）
    r"[a-zA-Z0-9]{6,}\.(?:com|cn|net|org)",
]


def split_name(name: str):
    """拆成 (主名, 扩展名)，点开头或无后缀时扩展名为空"""
    if "." not in name[1:]:
        return name, ""
    base, ext = name.rsplit(".", 1)
    if not ext or len(ext) > 8:      # 最后一段太长不当作扩展名
        return name, ""
    return base, ext


def clean_name(name: str) -> str:
    """clean 模式：去广告、去网址、合并多余空格与标点"""
    base, ext = split_name(name)
    s = base
    for pat in _AD_PATTERNS:
        s = re.sub(pat, "", s, flags=re.I)
    # 去掉残留的空括号
    s = re.sub(r"[（(\[【]\s*[）)\]】]", "", s)
    # 全角括号包裹的纯英文站点名
    s = re.sub(r"[（(]\s*[A-Za-z0-9\-_.]{3,}\s*[）)]", "", s)
    # 收尾：合并空格、去掉行尾分隔符
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = re.sub(r"^[\s\-_—–、,，.。]+|[\s\-_—–、,，.。]+$", "", s)
    # 不做 NFKC 归一化：它会把全角括号「（）」改成半角，中文名里很难看。
    # 只把全角数字/字母统一成半角（这类差异不影响观感，反而利于排序）。
    s = "".join(chr(ord(c) - 0xFEE0) if "０" <= c <= "９" or "Ａ" <= c <= "Ｚ"
                or "ａ" <= c <= "ｚ" else c for c in s)
    if not s:                        # 全被清掉了就保留原名，避免出现空文件名
        return name
    return f"{s}.{ext}" if ext else s


# ---------------------------------------------------------------------------
# title 模式：「只保留书名」（整理小说名用）
#
# 规则是**按信号强度级联**的，命中即停，都没命中就原样返回——宁可不动，也别乱改。
# 依据是本地 6 万个小说文件名的实际形态统计：
#   完整书名号《…》6.4% | 残缺右书名号 …》1.0% | 作者标记 3.6%
#   前导方括号标签 1.7% | 空格-空格 作者 0.5% | 其余本来就不脏
# ---------------------------------------------------------------------------

# 括号里只要出现这些词，**整组**都是标注。
# 必须整组丢：只删关键词会把「(全+二番外)」掏成「(全+二)」，比不删更难看。
_TITLE_TAIL_KW = re.compile(
    r"[（(\[【][^（()）\[\]【】]{0,32}"
    r"(?:全本|全集|完结|番外|出书版|网络版|校对版|精校版|完整版|无删减|修订版|"
    r"TXT|txt|电子书|上部|下部|第[一二三四五六七八九十百\d]{1,3}[部卷册篇]|"
    r"[\d一二三四五六七八九十百]{1,3}[部卷册集篇])"
    r"[^（()）\[\]【】]{0,32}[）)\]】]")

# 括号里就一个单字卷次标记：【全】、（上）、（下）
# 单独一支而不是并进上面：并进去的话「（李上校）」这种会被「含上字」误伤
_TITLE_TAIL_SINGLE = re.compile(r"[（(\[【]\s*[上下全]\s*[）)\]】]")

# 括号里只有一个「卷次 / 册数 / 区间」：(1-7)、（3）、(12)、（第5卷）
#
# 单个数字那一支必须 (?<!\s)——**前面有空格就不动**。因为撞名自动编号生成的就是
# 「同名 (2)」，若把它也当卷次清掉，第二次扫描会再把「同名 (2)」改回「同名」，
# 名字被反复搅动、永不收敛（实测踩过：改名后重扫仍有 2 条待改）。
_TITLE_TAIL_VOL = re.compile(
    r"(?<!\s)[（(\[【]\s*(?:第)?[\d一二三四五六七八九十百千]{1,4}"
    r"(?:[\-~～—–至,，、][\d一二三四五六七八九十百千]{1,4})*"
    r"\s*(?:[部卷册集篇])?\s*[）)\]】]")

# 收尾的裸标注：全TXT格式电子书_003 / 全本 / 完结 / +番外
_TITLE_TAIL_BARE = re.compile(
    r"(?:全?\s*TXT\s*格式\s*电子书|全本\s*TXT|TXT\s*格式|电子书|"
    r"\+\s*番外|番外|完结|全本|全集)(?:[_\-\s]*\d{1,4})?\s*$", re.I)

# 作者标记。by 必须卡边界，否则 "Baby"、"Hobby" 会被啃掉半截。
# 而且 by 后面要求是「：」或中日韩字——不然英文书名里的 "Stand by Me" 会被切成
# "Stand"。中文上传者写的 by 后面几乎都是中文名，这个限制代价很小。
_RE_AUTHOR = re.compile(
    r"[\s\-_.·]*\s*(?:作者|著者)\s*[：:]?\s*"
    r"|[\s\-_.·]*\s*(?<![A-Za-z])(?:BY|By|by)\s*[：:]\s*"
    r"|[\s\-_.·]*\s*(?<![A-Za-z])(?:BY|By|by)\s*(?=[\u4e00-\u9fff])")

_RE_LEAD_TAG = re.compile(r"^\s*[\(\[【][^\)\]】]{0,24}[\)\]】]\s*")
# 「空格-横杠-空格」是作者分隔符的常见写法：边荒传说 - 黄易 / Rework - Jason Fried
# 要求两侧都有空格：不然「半-城」这种书名里的连字符会被误当分隔符
_RE_SEP = re.compile(r"\s+[-—–]\s+")
# 前导序号：16、书名 → 书名。分隔符必须显式存在，否则「24个比利」会被啃成「个比利」
_RE_LEAD_NUM = re.compile(r"^\s*\d{1,4}\s*[、.．,，]\s*(?=[^\d\s])")
_RE_LEAD_JUNK = re.compile(r"^[\s\-_—–、,，.。·@]+|[\s\-_—–、,，.。·@]+$")
# 「整个名字就是一对括号」的情况。内容里必须不含任何括号字符——否则
# 首尾各是一个括号的 [棋魂]因为爱你 作者：一叶（亮光） 会被错拆成
# 「棋魂]因为爱你 作者：一叶（亮光」（内容可以吞掉中间的 ]，fullmatch 照样成立）
_RE_WHOLE_WRAP = re.compile(r"[（(\[【]\s*([^（()）\[\]【】]{1,60}?)\s*[）)\]】]")


def title_name(name: str) -> str:
    """title 模式：把小说文件名清成「只有书名」，无变化时返回原名

    级联顺序（先强信号，后弱信号）：
      1. 完整书名号《…》→ 取括号内。天然跳过《《一光年之恋》》这种错套：
         因为要求内容里不含书名号，第一个能配上的其实是内层那一对。
      2. 残缺书名号 → 取最靠左的那个符号，右残取它前面、左残取它后面。
         真实数据里这种残缺名很多：暗香情撩》by：季安 / 单飞雪《甜上眉梢
      3. 作者标记（作者：/著者/BY：）→ 取标记之前的部分
      4. 前导标签 [悬疑] / 【类型】 → 去掉
      5. 「空格-空格」作者分隔 → 取前面：边荒传说 - 黄易 / Rework - Jason Fried
    最后统一清尾部标注（全本/完结/番外/TXT/卷次，整组丢）。
    清成空名字时返回原名——宁可保留脏名字，也不能产生无名文件。
    """
    base, ext = split_name(name)
    s = base.strip()
    if not s:
        return name

    # 先把广告尾巴去掉，它们会把书名号切碎（[xxx.com]《书名》这种）
    for pat in _AD_PATTERNS:
        s = re.sub(pat, "", s, flags=re.I)
    s = s.strip()
    if not s:
        return name

    m = re.search(r"《([^《》]{1,80})》", s)
    if m:
        s = m.group(1)
    else:
        # 残缺书名号：谁在前面谁说了算
        pos_close, pos_open = s.find("》"), s.find("《")
        if pos_close > 0 and (pos_open < 0 or pos_close < pos_open):
            s = s[:pos_close]
        elif pos_open >= 0 and pos_open + 1 < len(s):
            s = s[pos_open + 1:]
        else:
            # 整个名字就是一对括号包着的：【美人】→ 美人
            mw = _RE_WHOLE_WRAP.fullmatch(s)
            if mw and mw.group(1).strip():
                s = mw.group(1)
            else:
                ma = _RE_AUTHOR.search(s)
                if ma:
                    s = s[:ma.start()]
                else:
                    s2 = _RE_LEAD_TAG.sub("", s)
                    if s2 != s:
                        s = s2
                    else:
                        ms = _RE_SEP.search(s)
                        if ms:
                            s = s[:ms.start()]

    # 尾部标注清理：反复擦到不动为止（「（全本）+番外」要两轮才干净）
    for _ in range(4):
        prev = s
        s = _TITLE_TAIL_KW.sub("", s)
        s = _TITLE_TAIL_SINGLE.sub("", s)
        s = _TITLE_TAIL_VOL.sub("", s)
        s = _TITLE_TAIL_BARE.sub("", s)
        s = _RE_LEAD_TAG.sub("", s)
        s = _RE_LEAD_NUM.sub("", s)
        s = s.replace("《", "").replace("》", "")
        s = _RE_LEAD_JUNK.sub("", s)
        s = re.sub(r"\s{2,}", " ", s).strip()
        if s == prev or not s:
            break

    if not s:
        return name
    return f"{s}.{ext}" if ext else s


def rename_one(name: str, mode: str, p: dict, index: int = 0) -> str:
    """按模式算出一个文件的新名字；无变化时返回原名"""
    base, ext = split_name(name)
    new_base = base

    if mode == "replace":
        find, repl = p.get("find", ""), p.get("replace", "")
        if find:
            if p.get("case_sensitive"):
                new_base = base.replace(find, repl)
            else:
                new_base = re.sub(re.escape(find), repl, base, flags=re.I)

    elif mode == "affix":
        prefix, suffix = p.get("prefix", ""), p.get("suffix", "")
        new_base = f"{prefix}{base}{suffix}"

    elif mode == "serial":
        start = int(p.get("start", 1))
        width = int(p.get("width", 3))
        num = str(start + index).zfill(width)
        tpl = p.get("template", "{n}_{name}") or "{n}_{name}"
        new_base = tpl.replace("{n}", num).replace("{name}", base)

    elif mode == "regex":
        pat = p.get("pattern", "")
        repl = p.get("repl", "")
        flags = 0 if p.get("case_sensitive") else re.I
        if pat:
            try:
                new_base = re.sub(pat, repl, base, flags=flags)
            except re.error:
                return name      # 正则写错就原样保留

    elif mode == "clean":
        return clean_name(name)

    elif mode == "title":
        return title_name(name)

    else:
        return name

    new_base = new_base.strip()
    if not new_base:
        return name
    new = f"{new_base}.{ext}" if ext else new_base
    return new


def dedupe_newnames(rows, occupied=None, ondup="skip"):
    """新名字撞车时自动加 (2)(3)…

    rows: [{"dir": 所在目录, "name": 原名, "newname": 目标名}]，**原地改写** newname
    occupied: {目录: {已占用的小写名字}}。本地改名时用磁盘实况填（同目录里不会
              改名的那些文件）；网盘侧现在由 build_rename_plan 用**扫描结果**自
              动兜底——scan 出来的文件就是那个目录的实况，不必再花请求去问。
    ondup:   撞到 occupied（网盘/磁盘上**现在就有**的同名）怎么办：
             skip      （默认）保留那一个，把这次要改的编成 `A (2).txt`
             overwrite 不编号，直接顶掉。执行时由 apply_plan 先把旧的挪进
                       `_覆盖备份/<时间戳>/`，所以是「能找回的替换」

    撞名分两种，口径不同（与 build_organize_plan 一致）：
      · **本批内部**互相撞（两条都要改成 A.txt）→ 一律编号。那是这一趟刚排进去的
        真数据，绝不能自己顶掉自己
      · 撞到 occupied 里**本来就有**的 → 按 ondup 处理

    为什么必须去重：只保留书名会把「《A》作者：甲.txt」「A - 乙.txt」压成同一个
    「A.txt」。执行时两条争一个名字，必然一条成功一条报错，还会留下说不清的半截
    状态。加个 (2) 就都保住了，信息量也比丢掉一条强。

    返回被自动编号的条数（要如实告诉用户，不能悄悄改名）。
    """
    used = {}          # 本批已排进去的（撞了必须编号）
    ext = {}           # 目录里本来就有的（撞了按 ondup 决定）
    if occupied:
        for d, names in occupied.items():
            ext[d] = {n.lower() for n in names}
    auto = 0
    for r in rows:
        pool = used.setdefault(r["dir"], set())
        outside = ext.get(r["dir"], set())
        want = r["newname"]
        low = want.lower()
        if low in pool:                       # 本批内部撞名：没得商量，编号
            r["newname"] = cand = _numbered_name(want, pool | outside)
            pool.add(cand.lower())
            auto += 1
        elif low in outside:                  # 撞到目录里已有的那个
            if ondup == "overwrite":
                # 旧文件将被顶掉（执行时先备份），它占的名字让出来了——
                # 后面再有同名改过来时不能再顶第二次，那可能是刚改好的数据
                outside.discard(low)
                pool.add(low)
                r["overwrite"] = True
            else:
                r["newname"] = cand = _numbered_name(want, pool | outside)
                pool.add(cand.lower())
                auto += 1
        else:
            pool.add(low)
    return auto


def filter_by_exts(files, exts):
    """只留下指定后缀的文件 → (留下的, 后缀集合)

    exts 是「txt, mobi」这种逗号分隔的字符串，点号、空格、大小写都不挑：
    ".TXT"、 " txt "、"txt" 算同一个。留空/全空 = 不过滤（全部都要）。

    为什么单独做一个函数而不是塞进 build_rename_plan：改名、归档迟早都要
    这个口径，而「过滤了几个」必须能报出来——悄悄少处理一部分文件比报错更危险。
    返回后缀集合是给预览标题用的：「改名·只保留书名（txt,mobi）」一眼能认出来。
    """
    want = {e.strip().lower().lstrip(".") for e in str(exts or "").split(",")
            if e.strip()}
    if not want:
        return list(files), set()
    keep = []
    for f in files:
        _, ext = split_name(f["name"])
        if ext.lower() in want:
            keep.append(f)
    return keep, want


def build_rename_plan(files, mode: str, params: dict, occupied=None,
                      ondup: str = "skip"):
    """files: [{path, name, size}] → 返回 (ops, unchanged, auto)

    ops 只含真正改名的；auto 是撞名被自动编号的条数。

    occupied: {目录: {文件名}}。不传时**用扫描结果自己兜底**——files 就是那个目录
    的实况，不必再花请求去问网盘。这一层很关键：只保留书名会把「《A》作者：甲.txt」
    压成「A.txt」，而目录里往往**已经有一个 A.txt**（它自己不用改名，不在 ops 里）。
    以前网盘侧不传 occupied，dedupe 只看待改名的那几条，压根发现不了它，于是改名
    必然撞 -8 失败——「重名跳过」承诺的「保留旧的、把新的编成 A (2)」根本没兑现。
    （本地改名那边本来就用磁盘实况填，locals_rename._dedupe_against_disk）

    注意要**排除本批待改名的源文件名**：它们马上就让位了。不排除的话，只改大小写
    （A.txt → a.txt）会被自己撞到（网盘大小写不敏感），误编成「a (2).txt」。
    本地改名那边同理排除，见 _dedupe_against_disk 的 renaming。
    """
    ops, unchanged = [], []
    for i, f in enumerate(files):
        new = rename_one(f["name"], mode, params, i)
        if new and new != f["name"]:
            ops.append({"op": "rename", "path": f["path"], "name": f["name"],
                        "newname": new, "size": f.get("size", 0)})
        else:
            unchanged.append(f["path"])
    rows = [{"dir": o["path"].rpartition("/")[0], "name": o["name"],
             "newname": o["newname"]} for o in ops]

    if occupied is None:
        occupied = {}
        for f in files:
            d, _, n = f["path"].rpartition("/")
            occupied.setdefault(d, set()).add(n)
    else:
        occupied = {d: set(names) for d, names in occupied.items()}
    # 本批要改名的源文件自己马上让位，不算被占用
    for r in rows:
        pool = occupied.get(r["dir"])
        if pool:
            pool.discard(r["name"])

    before = [r["newname"] for r in rows]
    auto = dedupe_newnames(rows, occupied, ondup=ondup) if rows else 0
    for o, r, b in zip(ops, rows, before):
        o["newname"] = r["newname"]
        if r["newname"] != b:
            o["auto"] = True        # 撞名被自动编号，预览里要标出来
        if r.get("overwrite"):
            o["overwrite"] = True   # 会顶掉目录里已有的那个（执行时先备份）
    return ops, unchanged, auto


# ===========================================================================
# 整理：按后缀 / 按大类 / 按日期 归目录
# ===========================================================================

CATEGORY_MAP = {
    "视频": {"mp4", "mkv", "avi", "mov", "wmv", "flv", "rmvb", "rm", "ts", "m2ts",
          "webm", "mpg", "mpeg", "3gp", "vob", "m4v", "iso"},
    "音频": {"mp3", "flac", "wav", "ape", "aac", "ogg", "wma", "m4a", "opus", "dsf"},
    "图片": {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "svg",
          "heic", "raw", "cr2", "nef", "psd", "ai"},
    "文档": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md",
          "rtf", "odt", "csv", "epub", "mobi", "azw", "azw3", "djvu", "chm"},
    "压缩包": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "cab", "arj"},
    "代码": {"py", "js", "ts", "java", "c", "cpp", "h", "hpp", "go", "rs", "php",
          "rb", "sh", "bat", "json", "xml", "yml", "yaml", "sql", "html", "css"},
    "安装包": {"exe", "msi", "dmg", "apk", "deb", "rpm", "pkg", "appimage"},
    "字幕": {"srt", "ass", "ssa", "sub", "vtt", "idx"},
}


def category_of(ext: str) -> str:
    e = (ext or "").lower().lstrip(".")   # 传 ".mp4" 或 "mp4" 都能匹配
    for cat, exts in CATEGORY_MAP.items():
        if e in exts:
            return cat
    return "其它"


def _safe_folder(name: str) -> str:
    """从后缀派生的目录名要过一遍清洗。

    实测踩过的坑（2026-09-28 归档目录实况）：古怪文件名拆出来的「后缀」直接当
    目录名，攒出了 `com)]`（1088 个文件）、`pdf（滚雪球英文原版——巴菲特的传记）`、
    `李华东&amp` 这种目录——百度不许目录名带 \\ / : * ? " < > |，而且这些目录
    纯属垃圾，用户看见只会困惑。规则：首字符不是 ASCII 字母数字（比如后缀以
    中文开头，实为「李华东&amp」这种）直接归 noext；是的话只留 ASCII 字母数字。
    只用于 by=ext 分支；category 是固定中文分类名、date 是 YYYY-MM，都不许动。"""
    name = (name or "").strip()
    if not name or not (name[0].isascii() and name[0].isalnum()):
        return "noext"
    keep = "".join(ch for ch in name if ch.isascii() and ch.isalnum())
    return keep or "noext"


def _numbered_name(name: str, taken: set) -> str:
    """挑一个 taken 里没有的 `原名 (2).ext` / `原名 (3).ext`…

    编号插在后缀前面，保持扩展名不变（`《A》甲.txt` → `《A》甲 (2).txt`）。
    """
    base, ext = split_name(name)
    k = 2
    while True:
        cand = f"{base} ({k})" + (f".{ext}" if ext else "")
        if cand.lower() not in taken:
            return cand
        k += 1


def build_organize_plan(files, by: str, dest: str, sandbox: str, occupied=None,
                        ondup: str = "skip"):
    """files: [{path,name,size,mtime}] → 移动计划
    by: ext（按后缀） / category（按大类） / date（按修改年月 YYYY-MM）

    occupied: {目录: {文件名}}，扫描范围内各目录当前已有的文件名。有它就多做
    两件很实在的事（没有也不影响正确性，只是撞名兜底少一层）：
      1. 已经在目标目录里的文件不再生成操作——它压根不用动，硬搬一次只会在
         自己所在目录里撞自己的名（十万条量级下这是白花花的失败）
      2. 目标目录里已经有同名文件时，按 ondup 处理（见下）

    ondup 决定「撞到目标目录里**网盘现在就有**的同名文件」时怎么办：
      skip      （默认）那个旧文件留在原地不动，改给这次要搬的自动编号
                `A (2).txt` 搬进去。两边数据都在，归档也能收尾
      overwrite 不编号，直接搬。执行时 apply_plan 会先把被撞的旧文件挪进
                `_覆盖备份/<时间戳>/` 再搬运，所以是「能找回的替换」。

    注意第 2 条为什么必须做：把十几万个文件拍平塞进 /txt、/mobi 这种目录，撞名
    是必然的（实测 2000 条里就撞了 56 条）。撞了以后——
      · 跳过策略：结果符合预期（旧文件没被动），如果不编号它们就永远留在原地，
        每轮都报一批失败，收不了尾
      · 覆盖策略：旧文件被挤进备份区，同名越多备份区越膨胀

    还有一种撞名跟 ondup 无关：**这份计划内部**互相撞（两个不同目录的 A.txt
    今晚都要搬进同一个 /txt）。这种情况一律自动编号——那是我们自己刚搬过去的
    真数据，「覆盖」说的是替掉网盘上的**旧**文件，不是让自己把自己覆盖掉。
    撞名的移动不拆「改名+移动」两条，move 直接带 newname（一步到位）。
    """
    dest = dest.rstrip("/")
    overwrite = (ondup == "overwrite")
    # pre：网盘上现在就有的（扫描得来的实况）；claimed：这份计划已经排进去的
    pre = {d: {n.lower() for n in names} for d, names in (occupied or {}).items()}
    claimed = {}

    ops = []
    for f in files:
        _, ext = split_name(f["name"])
        if by == "ext":
            folder = _safe_folder(ext.lower() or "noext")
        elif by == "category":
            folder = category_of(ext)   # 固定的几个中文分类名，不用清洗
        elif by == "date":
            mt = f.get("mtime") or 0
            folder = (time.strftime("%Y-%m", time.localtime(mt))
                      if mt else "未知日期")
        else:
            raise PanError(f"未知的整理方式: {by}")
        d = f"{dest}/{folder}"
        p = f["path"]
        src_dir, _, name = p.rpartition("/")
        if src_dir == d:
            continue                    # 已经在新家了，别再折腾它
        low = name.lower()
        taken = claimed.setdefault(d, set())
        pool = pre.setdefault(d, set())
        hit_pre = low in pool           # 网盘上现在就有一个同名的
        if hit_pre and overwrite:
            # 覆盖：名字不改，执行时把被撞的旧文件先挪进备份区。它占的这个
            # 名字算被消耗掉了——后面再有同名文件搬进来时不能再覆盖第二次，
            # 那可是我们今晚刚搬过去的数据
            pool.discard(low)
            taken.add(low)
            new = name
        elif hit_pre or low in taken:
            new = _numbered_name(name, taken | pool)
            taken.add(new.lower())
        else:
            new = name
            taken.add(low)
        op = {"op": "move", "path": p, "name": name, "dest": d,
              "size": f.get("size", 0)}
        if new != name:
            # 撞名的直接在移动里改名（move 支持 newname），不再拆「改名+移动」
            # 两条——两条不但慢一倍，改名那步失败还会让后面移动撞出 -8，
            # 十万条量级下这种连锁失败是灾难（实测 27168 条改名里 25901 条失败）
            op["newname"] = new
            op["auto"] = True
        if hit_pre and overwrite:
            op["overwrite"] = True      # 让前端能报出「N 个会替换掉网盘已有的」
        ops.append(op)
    return ops


def occupy_dests(pan: PanFiles, ops, occupied: dict) -> int:
    """把「这份计划要用到的目标目录」的实况补进 occupied → 返回补了几个目录

    撞名检测的底子是 occupied，而它通常只由**扫描结果**构成——归档目录却常常不在
    扫描范围里（典型：扫描 /apps/x/小说，归档到 /apps/x/归档）。那种情况下归档处
    已有的同名文件完全看不见：撞名不编号 → 执行时撞 -8 失败 → 下一轮再扫再撞，
    永远收不了尾（这正是最早「2000 条只成功一小部分」的老病根）。

    所以按 ops 里真正出现的 dest 逐个补一次实况。分类目录通常也就十几个，
    代价远小于事后一轮轮的假失败。目录还不存在或读不了=里面没有东西可撞，跳过。
    """
    missing = sorted({o["dest"] for o in ops
                      if o.get("op") == "move" and o.get("dest")}
                     - set(occupied))
    n = 0
    for d in missing:
        try:
            names = {e["name"] for e in pan.list_dir(d) if not e.get("isdir")}
        except Exception:
            continue
        if names:
            occupied[d] = names
            n += 1
    return n


# ===========================================================================
# 删除：按条件筛选
# ===========================================================================

def filter_files(files, f: dict):
    """筛选条件：ext / name_contains / name_regex / min_mb / max_mb"""
    import fnmatch
    exts = {e.strip().lower().lstrip(".") for e in (f.get("exts") or "").split(",") if e.strip()}
    kw = [k.strip() for k in (f.get("keywords") or "").split(",") if k.strip()]
    pattern = (f.get("regex") or "").strip()
    min_mb = float(f.get("min_mb") or 0)
    max_mb = float(f.get("max_mb") or 0)
    keep = []
    for it in files:
        name = it["name"]
        _, ext = split_name(name)
        if exts and ext.lower() not in exts:
            continue
        if kw and not any(k.lower() in name.lower() for k in kw):
            continue
        if pattern:
            try:
                if not re.search(pattern, name, re.I):
                    continue
            except re.error:
                pass
        mb = (it.get("size") or 0) / 1024 / 1024
        if min_mb and mb < min_mb:
            continue
        if max_mb and mb > max_mb:
            continue
        keep.append(it)
    return keep


def build_delete_plan(files, f: dict):
    hits = filter_files(files, f)
    return [{"op": "delete", "path": h["path"], "name": h["name"],
             "size": h.get("size", 0)} for h in hits]


# ===========================================================================
# 空目录：找出「整棵子树里连一个文件都没有」的目录
# ===========================================================================

def build_merge_dirs_plan(entries, ondup: str = "skip"):
    """把 `归档/txt(1)` 这类副本目录合回主目录 → 返回 (ops, info)

    背景（2026-09-28 归档目录实况）：按后缀整理几轮下来，归档下 ~40% 是
    `(1)` 副本目录——mobi(1) 40957 个文件、pdf(1) 15861 个、epub(1) 8214 个，
    txt 与 txt(1) 之间有 7710 对同名文件。这些副本让「整理归档」永远收不了尾：
    每轮扫出来的计划都比真实文件多（一对同名文件在扫描里就是两条计划）。

    ondup 决定「主目录里已经有一个同名文件」时怎么办，与整理归档口径一致：
      skip      （默认）主目录那个不动，把副本里的编成 `A (2)` 搬进去
      overwrite 不编号，直接搬。执行时旧文件先被挪进 `_覆盖备份/<时间戳>/`
    计划内部互相撞名（两个副本都要把 A.txt 搬进同一个主目录）时一律自动编号，
    理由同 build_organize_plan：不能让自己今晚搬过去的数据被自己顶掉。

    两条安全红线：

    规则：同一位父目录下同时有 `X` 和 `X(1)`（`X(2)`…`X(9)` 也归到 X）⇒
    副本里的**文件**移进主目录，撞名的自动编号 (2)(3)。两条安全红线：
      · 副本里的**子目录**不动（move 目录进主目录，主目录有同名目录时会出
        各种意外，保守起见留给人工/后续处理）
      · 移空的副本目录**不在这份计划里删**——百度 delete 对目录是连根拔的，
        万一有文件没搬成，删目录就是把真数据销毁。空目录交给「空文件夹」
        页签，那边按扫描实况判空，安全。

    entries: PanFiles.list_dir(root, recursive=True) 的原始结果（目录+文件都要）。
    info = {pairs 合并对数, scanned 扫描文件数, moved 待移动文件数,
            auto 撞名自动编号数, with_subdirs 含子目录的副本数}
    """
    # 按父目录分组：{父: {目录名: 路径}}
    by_parent = {}
    for e in entries:
        if not e.get("isdir"):
            continue
        p = (e.get("path") or "").rstrip("/")
        parent, _, name = p.rpartition("/")
        if parent and name:
            by_parent.setdefault(parent, {})[name] = p

    # 只认 X(1)~X(9)：括号里是大数字的多半是正经名字（如「报告(2024)」），
    # 不能因为恰好存在「报告」就把它合进去
    def dup_of(name):
        m = re.match(r"^(.+)\(([1-9])\)$", name)
        return m.group(1) if m else None

    pairs = []                       # (主目录路径, 副本路径)
    for parent, names in by_parent.items():
        for name, path in names.items():
            base = dup_of(name)
            if base and base in names:
                pairs.append((names[base], path))

    # 各目录现有文件名（撞名编号要以主目录的实况为底）。连大小一起记：
    # 导出的 CSV 里有「大小」这一列，计划里不带它就只能给一列空格子
    files_by_dir = {}
    files_scanned = 0
    for e in entries:
        if e.get("isdir"):
            continue
        d, _, n = (e.get("path") or "").rpartition("/")
        if d and n:
            files_by_dir.setdefault(d, []).append((n, e.get("size") or 0))
        files_scanned += 1

    # 子目录按父目录记一份：副本里有子目录的要点出来
    subdirs_by_dir = {}
    for e in entries:
        if e.get("isdir"):
            d = (e.get("path") or "").rstrip("/").rpartition("/")[0]
            if d:
                subdirs_by_dir.setdefault(d, set()).add(e["path"])

    ops, moved, auto, overwrote, with_sub = [], 0, 0, 0, 0
    pre = {d: {n.lower() for n, _ in names}
           for d, names in files_by_dir.items()}
    claimed = {}
    overwrite = (ondup == "overwrite")
    for main_path, dup_path in pairs:
        pool = pre.setdefault(main_path, set())
        taken = claimed.setdefault(main_path, set())
        if subdirs_by_dir.get(dup_path):
            with_sub += 1
        for name, fsize in files_by_dir.get(dup_path) or []:
            low = name.lower()
            hit_pre = low in pool
            if hit_pre and overwrite:
                pool.discard(low)
                taken.add(low)
                new = name
            elif hit_pre or low in taken:
                new = _numbered_name(name, pool | taken)
                taken.add(new.lower())
            else:
                new = name
                taken.add(low)
            op = {"op": "move", "path": f"{dup_path}/{name}", "name": name,
                  "dest": main_path, "size": fsize}
            if new != name:
                op["newname"] = new
                op["auto"] = True
                auto += 1
            if hit_pre and overwrite:
                op["overwrite"] = True
                overwrote += 1
            ops.append(op)
            moved += 1

    info = {"pairs": len(pairs), "scanned": files_scanned,
            "moved": moved, "auto": auto, "overwrite": overwrote,
            "with_subdirs": with_sub}
    return ops, info


def build_empty_dir_plan(entries, root: str, skip: str = "", unreadable=None):
    """找出能删掉的空目录 → 返回 (ops, info)

    entries: PanFiles.list_dir(root, recursive=True) 的**原始**结果——目录和文件都要。
             不能用 list_files()，它把目录滤掉了，而这里要判定对象正是目录。
    root:    扫描起点，**它自己不参与删除**。两个理由：用户正站在这个目录里，
             把它删掉太意外；而且它下面若全是空的，删掉它的直接子目录同样清得干净。
    skip:    逗号分隔的名字关键字，命中的目录**连同整棵子树**一并跳过
             （默认由调用方传 "_覆盖备份" —— 别顺手把备份区连根端了）。
    unreadable: 扫描时**列不开**的目录路径列表（list_dir 的 failures）。
             这些目录里有没有文件是无从得知的，必须当成「非空」保护起来，
             否则「不知道有什么」会被当成「什么都没有」而删掉——那是用户的真数据。
             （实测百度会返回这种东西：父目录里有它，拿它的 path 去列回 -9。）

    判定规则：某目录（含所有层级的子目录）里一个文件都没有 ⇒ 空。
    被 skip 命中的目录自己不算，**它的祖先也一律不算**——否则删掉那个祖先会把
    受保护的目录一起捎走（典型场景：「只装着一个空的 _覆盖备份」的父目录）。
    返回的 ops 只含「最外层」空目录——删掉它，嵌在里面的空目录跟着消失。
    不逐层列出来的原因：一是预览会啰嗦几倍，二是后几条必然报 -9「文件不存在」
    的假失败（父目录一删，子目录就没了），把真实的失败也淹没掉。

    info = {empty_total 空目录总数, nested 会随根目录一起消失的嵌套数,
            dirs_scanned 扫到的目录数, files_scanned 扫到的文件数}
    """
    root = (root or "").rstrip("/")
    kws = [k.strip().lower() for k in (skip or "").split(",") if k.strip()]

    dirs, files_scanned = [], 0
    for e in entries:
        p = (e.get("path") or "").rstrip("/")
        if not p:
            continue
        if e.get("isdir"):
            if p != root:           # 兜底：list_dir 本就不会返回起点自己
                dirs.append(p)
        else:
            files_scanned += 1

    def rel_segs(p):
        rel = p[len(root):] if p.startswith(root) else p
        return [s.lower() for s in rel.strip("/").split("/") if s]

    def is_skipped(p):
        """自己或任意一级祖先命中关键字 ⇒ 跳过（整棵子树都不许动）"""
        return any(k in seg for seg in rel_segs(p) for k in kws)

    # 1. 目录里有没有文件：先标记文件的直属目录，再自下而上传染给祖先。
    #    被跳过的目录也要照常参与判定——否则「只含一个被跳过的空目录」的父目录
    #    会被误判成空，进而把受保护的目录一起带走。
    has_file = {d: False for d in dirs}
    for e in entries:
        if e.get("isdir"):
            continue
        parent = (e.get("path") or "").rstrip("/").rsplit("/", 1)[0]
        if parent in has_file:      # 直属 root 的文件：root 不入表，无需标记
            has_file[parent] = True
    # 2. 子树里有没有被跳过的目录，同样自下而上传染：父目录即便自己一个文件都没有，
    #    只要子树里藏着受保护的目录，它就不能算「可删的空目录」——删了会把保护对象带走
    skip_sub = {d: is_skipped(d) for d in dirs}
    for d in sorted(dirs, key=lambda x: x.count("/"), reverse=True):
        parent = d.rsplit("/", 1)[0]
        if parent not in has_file:
            continue
        if has_file[d]:
            has_file[parent] = True
        if skip_sub[d]:
            skip_sub[parent] = True

    # 2b. 打不开的目录（list_dir 的 failures）：里面装了什么我们**根本不知道**，
    #     绝不能当空目录删掉——就是那个「肖申克的救赎 斯蒂芬•金　」，天知道底下
    #     有没有书。它的祖先同样不能算空：删掉祖先会把它一起带走。所以把「它自己
    #     加上到 root 为止的每一级祖先」整条链都打上保护（精确路径匹配，不走 skip
    #     那套关键字——关键字是给人配的，这个是扫描的客观结果）
    prot = set()
    for u in (unreadable or []):
        p = (u or "").rstrip("/")
        while p and p != root and p.startswith(root + "/"):
            prot.add(p)
            p = p.rsplit("/", 1)[0]

    # 3. 空的里面，剔掉被 skip 命中的子树（自己命中，或子树里藏着命中的），
    #    以及所有打不开、无法确认是否真的空的目录
    cand = [d for d in dirs if not has_file[d] and not skip_sub[d] and d not in prot]

    # 4. 只留最外层：父目录也是候选的话，自己会随父目录一起消失，不必单列
    cand_set = set(cand)
    roots = [d for d in cand if d.rsplit("/", 1)[0] not in cand_set]
    roots.sort(key=lambda x: -x.count("/"))    # 深层在前，稳妥

    ops = [{"op": "delete", "path": d, "name": d.rsplit("/", 1)[-1], "isdir": True}
           for d in roots]
    info = {"empty_total": len(cand), "nested": len(cand) - len(roots),
            "dirs_scanned": len(dirs), "files_scanned": files_scanned,
            "protected": len(prot)}
    return ops, info


# ===========================================================================
# 计划校验（载入预览 / 执行前统一过一遍：拦越界与非法操作）
# ===========================================================================

def validate_plan(plan: dict, sandbox: str):
    """校验计划，返回 (ops, errors)"""
    ops = plan.get("ops") if isinstance(plan, dict) else plan
    if not isinstance(ops, list):
        return [], ["计划格式不对：需要 {\"ops\": [...]} 或直接的数组"]
    good, errs = [], []
    sb = sandbox.rstrip("/")
    for i, o in enumerate(ops, 1):
        if not isinstance(o, dict):
            errs.append(f"第 {i} 条不是对象"); continue
        op = (o.get("op") or "").lower()
        path = (o.get("path") or "").strip()
        if not path.startswith("/"):
            path = "/" + path
        if path != sb and not path.startswith(sb + "/"):
            errs.append(f"第 {i} 条越界（不在沙盒 {sb} 内）：{path}"); continue
        if op == "rename":
            if not o.get("newname"):
                errs.append(f"第 {i} 条 rename 缺 newname"); continue
            good.append({"op": "rename", "path": path, "newname": o["newname"]})
        elif op == "move":
            dest = (o.get("dest") or "").strip()
            if not dest.startswith("/"):
                dest = "/" + dest
            if dest != sb and not dest.startswith(sb + "/"):
                errs.append(f"第 {i} 条目标越界：{dest}"); continue
            it = {"op": "move", "path": path, "dest": dest.rstrip("/")}
            # newname（搬过去的同时改名）必须保住——完整计划执行前都要过这道
            # 校验，丢了它撞名编号就没了，真网盘上会整批撞 -8。
            # 只做最起码的把关：不能带路径分隔符、不能是 . ..
            nn = str(o.get("newname") or "").strip()
            if nn and "/" not in nn and nn not in (".", ".."):
                it["newname"] = nn
            good.append(it)
        elif op == "delete":
            # isdir 要带下去：删空文件夹与删文件在提示文案上必须分开说，
            # 丢掉这个标记就只能把「删除 12 个空文件夹」写成「删除 12 个文件」
            good.append({"op": "delete", "path": path,
                         **({"isdir": True} if o.get("isdir") else {})})
        else:
            errs.append(f"第 {i} 条未知操作: {op}")
    return good, errs


def _find_conflicts(pan: PanFiles, ren, mov):
    """找出将被 rename/move 撞到的网盘现有文件，返回它们的完整路径列表。

    rename 撞名点 = 源文件所在目录 + newname；move 撞名点 = dest + 源文件名。
    每个涉及目录只 list 一次（缓存），文件名按小写比较（网盘大小写不敏感）。

    一个坑：**只改大小写的文件会被自己撞到**。A.txt → a.txt 时，目标名 a.txt
    按小写比就是它自己（网盘大小写不敏感）。若不排除，覆盖模式会先把这个「被撞
    的旧文件」搬进备份区——搬的正是要改名的那个文件，于是改名必然失败，文件还
    被挪走了。所以命中的条目若就是某条 rename 的源路径本身，一律不算撞名。
    """
    targets = {}                       # 目录 -> {小写目标名}
    self_src = {}                      # 目录 -> {小写目标名: {源文件完整路径}}
    for o in ren:
        d, _, n = o["path"].rpartition("/")
        targets.setdefault(d, set()).add(o["newname"].lower())
        self_src.setdefault(d, {}).setdefault(o["newname"].lower(), set()).add(o["path"])
    for o in mov:
        # 带新名的 move 按**新名**查冲突（搬过去之后叫什么才是会不会撞的关键）
        n = o.get("newname") or o["path"].rsplit("/", 1)[-1]
        targets.setdefault(o["dest"], set()).add(n.lower())

    conflicts = []
    for d, names in targets.items():
        try:
            entries = pan.list_dir(d)
        except Exception:
            continue                   # 目录不存在=必然不撞名，跳过
        for e in entries:
            if e.get("isdir"):
                continue
            low = e.get("name", "").lower()
            if low not in names:
                continue
            full = f"{d}/{e['name']}"
            # 这条就是某条改名的源文件本身（只改大小写）→ 不是撞名，跳过
            if full in self_src.get(d, {}).get(low, set()):
                continue
            conflicts.append(full)
    return conflicts


def _mkdirs(pan: PanFiles, path: str):
    """一次建多级目录（百度 create 支持多级路径；已存在返回 True）"""
    try:
        return pan.mkdir(path)
    except Exception:
        return False


def _verify_uncertain(pan: PanFiles, fails: list, op_of: dict,
                      rounds: int = None, wait: float = None):
    """给「整批报错、结果未知」的条目做实地复核（默认核两遍）。

    背景：百度 filemanager 在高并发（比如同时还有上传任务在跑）、大批量提交时，
    会整批回一个错误码而 info 是空的。实测一次 2000 条的批量移动，18 批分别回了
    -9「文件不存在」和 111「Token 过期」，可源目录里那 2000 个文件**一个不剩**、
    全都到了目标目录。直接采信响应的话，用户会以为白干了 1800 条，甚至重复执行。

    判据（两个证据要同时成立，避免把「本来就不存在」也算成生效）：
      源目录里已经没有这个文件名  且  目标处（move 的 dest / rename 的新名）有它
    目录按需去重，每个只 list 一次——2000 条同源目录的操作，两三次请求就能核完。

    **为什么必须核好几遍**：百度的目录索引有延迟，刚搬完立刻 list，源目录里可能
    还挂着旧名字。实测一次 2000 条的整理归档：界面报「成功 388 / 失败 1612，另有
    1612 条复核后确认未生效」，事后抽查其中 12 条 —— 源处已无、目标处已有，**全都
    搬走了**。第一遍核对下的结论是错的，害人以为白干一场还要重跑一遍。所以隔几秒
    再核一次，**连续几遍都判「没生效」才敢说它真失败**。

    返回 (已确认生效的条数 {op: n}, 剩下的真失败列表)。
    """
    # 用 None 作默认值、进来再取模块常量：测试才能把等待时间调成 0 跑得快，
    # 而不用去 monkeypatch 已经绑死在函数签名上的默认参数
    rounds = VERIFY_ROUNDS if rounds is None else rounds
    wait = VERIFY_WAIT if wait is None else wait

    unc = [f for f in fails if f.get("uncertain")]
    rest = [f for f in fails if not f.get("uncertain")]
    if not unc:
        return {}, rest

    jobs = []                       # (fails 条目, 源目录, 源名, 目标目录, 目标名, op)
    need = {}                       # 目录 -> 只在核对用得上，这里只统计要不要 list
    for f in unc:
        p = str(f.get("path") or "")
        d, _, n = p.rpartition("/")
        o = op_of.get(p) or {}
        op = o.get("op") or ""
        dst_d = dst_n = ""
        if op == "move":
            # 搬过去之后的名字：带 newname 的用新名（核对的是「目标处有没有它」）
            dst_d = o.get("dest", "")
            dst_n = o.get("newname") or n
        elif op == "rename":
            dst_d, dst_n = d, o.get("newname", "")
        jobs.append((f, d, n, dst_d, dst_n, op))
        need[d] = True
        if dst_d:
            need[dst_d] = True

    done, pending = {}, list(jobs)
    last = {}                       # 最后一轮各目录的实况，用来写下失败原因
    for i in range(max(1, rounds)):
        if i:                       # 第一遍马上核（快路径），只有存疑才等
            time.sleep(wait)
        listed = {}
        for d in need:
            try:
                listed[d] = {e["name"].lower()
                             for e in pan.list_dir(d, recursive=False)
                             if not e["isdir"]}
            except Exception:
                listed[d] = None    # None = 这个目录读不到，无从核对
        last = listed

        nxt = []
        for f, d, n, dst_d, dst_n, op in pending:
            src = listed.get(d)
            if src is None or n.lower() in src:
                # 目录读不到，或文件**还在原处** —— 可能是索引没同步，留到下一遍
                nxt.append((f, d, n, dst_d, dst_n, op))
                continue
            if dst_d:
                dt = listed.get(dst_d)
                if dt is None or (dst_n and dst_n.lower() not in dt):
                    nxt.append((f, d, n, dst_d, dst_n, op))
                    continue
            done[op or "other"] = done.get(op or "other", 0) + 1
        if not nxt:
            pending = []
            break
        pending = nxt

    still = []
    for f, d, n, dst_d, dst_n, op in pending:
        f.pop("uncertain", None)
        # 失败要说到点子上：撞名、还在原处、压根没有，三种情况三种说法，
        # 用户一看就知道下一步该选「覆盖」还是别折腾了
        src, dst = last.get(d), (last.get(dst_d) if dst_d else None)
        if src is None or (dst_d and dst is None):
            why = "目录读不到，无从核对"
            # 目录都读不到，八成是连接出了问题；这时候重提一批，就算回了个
            # 「成功」也不敢信（没法核对）。不如如实报出来，让人手动重跑
            f["retryable"] = False
        elif n.lower() not in src:
            why = "原处和目标处都没有它（文件本就不在网盘上）"
            f["retryable"] = False      # 文件压根没有，重试也没用
        elif dst_d and dst_n and dst_n.lower() in dst:
            why = "原处还在，目标处已有同名文件（撞名未覆盖）"
            f["retryable"] = False      # 重试还是撞名，除非改用「覆盖」
        else:
            why = "原处还在，没生效"
            f["retryable"] = True       # 限流/被打回这类，再提交一次常常就成了
        f["msg"] = f"{f.get('msg')} → 核对 {max(1, rounds)} 次：{why}"
        still.append(f)
    # rest 是「响应里就明确报了错、又不属于可核对错误码」的那些（比如建目录失败），
    # 它们没进复核也必须回到失败清单里 —— 少了就是把失败静默吞掉
    return done, rest + still


def apply_plan(pan: PanFiles, ops: list, ondup: str = "skip", on_progress=None):
    """按类型分组执行计划 → 返回统计

    ondup: rename/move 撞到同名文件时的策略
           skip      跳过该条，保留网盘上已有的文件（默认，安全）
           overwrite 覆盖。注意：百度 filemanager 的 ondup 参数对 rename/move
                     不生效（实测撞名一律报 -8），所以覆盖用「先挪备份再执行」
                     实现：被撞的旧文件移动到 沙盒/_覆盖备份/时间戳/原目录结构
                     下，不删除、可找回，之后原操作就不会撞名了。

    on_progress(snap): 每完成一批回调一次，用来显示执行进度。snap =
        {"done": 已完成动作数, "total": 总动作数, "phase": 当前阶段文案,
         "fails": 失败条数}
        总动作数 = 改名 + 移动 + 删除，覆盖模式下再加「备份被撞文件」的条数。
        一次几百条要跑好几分钟，没有这个回调前端只能干等。
    """
    stat = {"rename": 0, "move": 0, "delete": 0, "backup": 0,
            "backup_dir": "", "fails": [],
            "verified": 0,          # 复核后确认已生效的条数（响应报错、实际成功）
            "retried": 0,           # 重试一轮后又被救回来的条数
            "stop_reason": ""}
    ren = [{"path": o["path"], "newname": o["newname"]} for o in ops if o["op"] == "rename"]
    # move 可能带 newname（撞名一步到位），只挑后端认识的键，别把 auto 等杂项
    # 也塞进 filelist 发给百度
    mov = [{"path": o["path"], "dest": o["dest"],
            **({"newname": o["newname"]} if o.get("newname") else {})}
           for o in ops if o["op"] == "move"]
    dele = [o["path"] for o in ops if o["op"] == "delete"]

    # 进度快照。用可变 dict 而非局部变量：闭包里改它不需要 nonlocal
    prog = {"done": 0, "total": len(ren) + len(mov) + len(dele),
            "phase": "", "fails": 0}

    def emit(phase=None):
        if phase:
            prog["phase"] = phase
        if on_progress:
            try:
                on_progress(dict(prog))
            except Exception:
                pass        # 进度显示出问题，绝不能影响真正的执行

    def tick(phase):
        """生成「一批提交完成」的回调：累加进度与失败数，然后汇报

        失败数由底层连同本批条数一起给出。不能等本函数返回后再从 stat 里数，
        那样进度上的失败数会滞后一批。
        """
        def cb(k, nf=0):
            prog["done"] += k
            prog["fails"] += nf
            emit(phase)
        return cb

    # 第一批返回前可能等好几分钟（百度按批处理），文案得让人知道是在等网盘，
    # 而不是「准备中」那种看不出进展的说法
    emit("提交中")

    # 覆盖模式：先找出撞名文件，挪进带时间戳的备份目录（保留原路径结构）
    if ondup == "overwrite" and (ren or mov):
        emit("检查重名文件")
        conflicts = _find_conflicts(pan, ren, mov)
        if conflicts:
            bdir = f"{pan.sandbox}/_覆盖备份/{time.strftime('%Y%m%d-%H%M%S')}"
            mv_ops = []
            for cf in conflicts:
                rel = cf[len(pan.sandbox) + 1:]
                bdest = f"{bdir}/{str(PurePosixPath(rel).parent)}"
                _mkdirs(pan, bdest)
                mv_ops.append({"path": cf, "dest": bdest})
            # 备份也是要实现的动作，算进总数；否则进度条先跑到别处再卡住，看着像死机
            prog["total"] += len(mv_ops)
            ok, fails = pan.move_batch(mv_ops, on_progress=tick("备份被撞的旧文件"))
            stat["backup"] = ok
            stat["backup_dir"] = bdir
            stat["fails"] += fails
            if fails:
                # 有文件没备份成功就不能继续覆盖（会导致误删），直接返回
                stat["fails"].insert(0, {"path": bdir,
                                         "msg": "备份失败，覆盖中止（见下方明细）"})
                return stat
            time.sleep(0.5)            # 等索引同步，避免紧跟着的操作读到旧状态

    # 移动前先把目标目录都建好（百度 move 不会自动建）
    dirs = sorted({m["dest"] for m in mov})
    for i, d in enumerate(dirs, 1):
        try:
            pan.mkdir(d)
        except Exception as e:
            stat["fails"].append({"path": d, "msg": f"建目录失败 {e}"})
            prog["fails"] += 1
        emit(f"准备目标目录 {i}/{len(dirs)}")     # 目录多时也得让人看到在动

    if ren:
        ok, fails = pan.rename_batch(ren, ondup="skip", on_progress=tick("正在改名"))
        stat["rename"] = ok; stat["fails"] += fails
    if mov:
        ok, fails = pan.move_batch(mov, ondup="skip", on_progress=tick("正在移动"))
        stat["move"] = ok; stat["fails"] += fails
    if dele:
        ok, fails = pan.delete_batch(dele, on_progress=tick("正在删除"))
        stat["delete"] = ok; stat["fails"] += fails

    # 站到磁盘上复核：报错的那些到底动没动。
    # 这一步是必须的——百度在大批量提交时会把已经生效的操作也报成失败，
    # 不核对就会把成功报成失败（实测 2000 条移动里 1612 条报错，其中 1366 条
    # 其实早就搬走了）。注意不光「整批报错」要核，info 里**逐条**给的错码
    # 一样会假报，所以 _verify_uncertain 按错误码挑出可核对的全部核一遍。
    by_op = {o["path"]: o for o in ops}
    if stat["fails"]:
        emit("核对执行结果")
        vdone, rest = _verify_uncertain(pan, stat["fails"], by_op)
        for op, n in vdone.items():
            stat[op] = stat.get(op, 0) + n
            stat["verified"] += n
        stat["fails"] = rest

    # 复核后仍失败的，有一部分只是「这一趟没赶上」——限流、整批被打回之类，
    # 再提交一次通常就成了。撞名和「文件压根不在」重试也没用，不做无用功。
    stop_reason = getattr(pan, "last_stop_reason", "")
    if not stop_reason:
        retry = [f for f in stat["fails"] if f.get("retryable")]
        if retry:
            emit(f"重试 {len(retry)} 条未生效的")
            keep = [f for f in stat["fails"] if not f.get("retryable")]
            sub = [by_op[f["path"]] for f in retry if f["path"] in by_op]
            prog["total"] += len(sub)      # 不然进度条会提前顶到 100%
            r2 = [{"path": o["path"], "newname": o["newname"]}
                  for o in sub if o["op"] == "rename"]
            # newname 必须带上：撞名编号过的移动如果丢掉新名重试，会撞上
            # 自己刚搬过去的文件，稳稳一个 -8
            m2 = [{"path": o["path"], "dest": o["dest"],
                   **({"newname": o["newname"]} if o.get("newname") else {})}
                  for o in sub if o["op"] == "move"]
            d2 = [o["path"] for o in sub if o["op"] == "delete"]
            f2 = []
            if r2:
                ok, fl = pan.rename_batch(
                    r2, ondup="skip", on_progress=tick("重试改名"))
                stat["rename"] += ok; f2 += fl
            if m2:
                ok, fl = pan.move_batch(
                    m2, ondup="skip", on_progress=tick("重试移动"))
                stat["move"] += ok; f2 += fl
            if d2:
                ok, fl = pan.delete_batch(d2, on_progress=tick("重试删除"))
                stat["delete"] += ok; f2 += fl
            v2 = {}
            if f2:
                v2, rest2 = _verify_uncertain(pan, f2, by_op)
                for op, n in v2.items():
                    stat[op] = stat.get(op, 0) + n
                keep += rest2
            # 重试里直接回成功的 + 又报错但复核确认生效的，都算「重试救回来的」
            stat["retried"] = (len(sub) - len(f2)) + sum(v2.values())
            stat["fails"] = keep
    if stop_reason:
        stat["stop_reason"] = stop_reason
    for f in stat["fails"]:
        f.pop("retryable", None)      # 内部标记，不往外传
    emit("已完成")
    return stat
