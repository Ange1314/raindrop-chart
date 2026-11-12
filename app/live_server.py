# -*- coding: utf-8 -*-
"""
Raindrop Chart · 实时数据源
------------------------------------------------------------
把 WorkBuddy 的会话记录（JSONL）实时翻译成「点-环」模型：

    会话（一个点）
      └─ 用户回合（圆周上的珠子）
           └─ 工具调用（再点开才看到，按耗时决定大小）

页面每 1.5 秒拉一次 /api/tree，有新事件就从天上掉一颗雨滴。
本文件不改动、不写入任何会话数据，只读取。

启动：
    python live_server.py
    → 浏览器打开 http://127.0.0.1:7788/
"""

import io
import json
import hashlib
import math
import os
import re
import sys
import time
import secrets
import glob
import sqlite3
import subprocess
import webbrowser
import bisect
import threading
import urllib.request
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

# ------------------------------------------------------------------
# 启动兜底：下面这几个 import 一旦失败（压缩包解压不全、被安全软件删了
# 某个 .py、下载中断），进程会在进入 main() 之前就死掉 —— 那时还没有任何
# 日志重定向，用户双击 pythonw 启动的结果就是「什么都不发生」，
# 连一个能查的线索都没有。
# 所以先挂一个最小可用的失败记录器：写文件 + 弹窗（pythonw 下没有控制台，
# 只剩这两条路能说话）。
# ------------------------------------------------------------------
_BOOT_HERE = os.path.dirname(os.path.abspath(__file__))


def _boot_log_dir():
    """启动失败日志落哪儿。

    分发态把日志放进 ../data，不放进 app/ —— 程序目录必须是只读的，
    否则「整目录替换升级」时会把用户的日志一起换掉。
    这时候 paths.py 可能正是缺的那个，所以这里不能 import paths，只能自己判。
    """
    if os.path.basename(_BOOT_HERE).lower() in ("app", "src"):
        d = os.path.join(os.path.dirname(_BOOT_HERE), "data")
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            pass
    return _BOOT_HERE


def _boot_fail(title, detail):
    """启动早期失败时的唯一出口。写日志 + 尽力弹窗，绝不静默退出。"""
    txt = "%s\n\n%s\n" % (title, detail)
    log_path = os.path.join(_boot_log_dir(), "运行日志.txt")
    try:
        with io.open(log_path, "a", encoding="utf-8") as fh:
            fh.write("\n===== %s 启动失败 =====\n%s\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), txt))
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, txt + "\n（详情写在这里：%s）" % log_path,
            "Raindrop Chart 启动失败", 0x10)
    except Exception:
        try:
            print(txt, file=sys.stderr)
        except Exception:
            pass


try:
    # 数据源适配层：把「读数据」和「建树」隔开。
    # datasource.py 只在函数内部反向 import 本模块，所以这里顶层导入不会成环。
    from datasource import (get_source, list_sources, register_csv_file,
                            register_tree_file, IMPORT_DIR, WorkBuddySource,
                            CsvSource, remove_source)

    # 统一的权重口径（等效步数 + 对数配平的半径映射 + 有效度）。
    # 别再在别处自己写公式 —— 13 处各算各的，是「大小不可比」的根源。
    import weights as WT

    # 锁定：密码控制某些节点在接口层不出现。密码不落盘，只存 salt + check。
    from lockbox import LockBox
except Exception as _boot_err:
    # 报错要指名道姓。只说「缺少必需的 .py」等于让用户自己猜是哪一个——
    # 实测最常缺的就是后加的模块（paths.py / lockbox.py），
    # 而旧文案里根本没列它们，等于把用户往错的方向推。
    _miss = ""
    _ename = repr(_boot_err)
    for _mod, _file in (("paths", "paths.py"), ("lockbox", "lockbox.py"),
                        ("datasource", "datasource.py"), ("weights", "weights.py")):
        if ("'%s'" % _mod) in _ename:
            _miss = "\n★ 这次缺的是：%s —— 把它补回去就能启动。\n" % _file
            break
    _boot_fail(
        "缺少必需的 Python 文件，程序无法启动。",
        "错误：%r\n%s\n"
        "通常是因为：\n"
        "  · 压缩包没有解压完整（app 文件夹里的 .py 要全部在）\n"
        "  · 杀毒软件把某个 .py 当可疑文件删掉了\n"
        "  · 只复制了几个文件，没有整个文件夹一起拷\n\n"
        "核对一下 app 文件夹里应该有这些（一个都不能少）：\n"
        "  live_server.py   datasource.py   weights.py   mcp_server.py\n"
        "  ring.html        lockbox.py      paths.py      register_mcp.py"
        % (_boot_err, _miss))
    raise SystemExit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
# 分区：HERE = 软件本体（只读、可整目录替换）；
#       USER = 软件的信息（索引、导入的库、锁、日志），升级时不动。
# 两种布局由 paths.py 判定，开发态退化成「同一个目录」，老用户升级不受影响。
import paths                                # noqa: E402
USER = paths.USER
IMPORT_DIR = paths.IMPORT_DIR
# 锁：一个 json 文件、一把密码、一份被锁清单。密码本身不进这个文件。
LOCK = LockBox(os.path.join(USER, "_lock.json"))
PROJECTS = os.path.expanduser("~/.workbuddy/projects")
PORT = int(os.environ.get("RD_PORT", "7788"))
APP_ID = "raindrop-chart"          # /api/ping 自报家门用，判断端口上是不是自己
# 唯一版本号：mcp_server 直接读它（SERVER_VERSION = LS.VERSION），不再各写一份。
# ⚠️ 还有一处：ring.html 右下角 #ver —— 那是字符串字面量，改版本要手改。
#    （改版时记得两处一起改，或者直接跑 release/build_release.py 让它自动对齐）
VERSION = "2.7.0"
SERVER_PORT = [PORT]               # 实际监听到的端口（可能因占用后移）

# 页面标题：用来认出「已经开着的那个窗口」。
# ⚠️ 和 ring.html 的 <title> 必须一致 —— 对不上就永远认不出来，
#    表现是退回「每点一次桌面图标多一个标签」。
PAGE_TITLE = "Raindrop Chart · 点与周长"

# 工具名 -> 中文短名（点上的字要短，点开才看全名）
TOOL_ZH = {
    "Bash": "终端", "PowerShell": "终端", "Read": "读文件", "Write": "写文件",
    "Edit": "改文件", "Glob": "找文件", "Grep": "搜内容", "ViewImage": "看图",
    "WebFetch": "抓网页", "WebSearch": "搜索", "present_files": "交付",
    "show_widget": "画图", "widget_guidelines": "画图", "TaskCreate": "建任务",
    "TaskUpdate": "改任务", "TaskList": "看任务", "Skill": "技能",
    "Agent": "派子任务", "AskUserQuestion": "问用户", "ToolSearch": "找工具",
    "DeferExecuteTool": "调工具", "automation_update": "定时任务",
}

# 工具归族：同族连续出现更容易被合并成「一组」，读起来才有意义
FAMILY = {
    "Bash": "exec", "PowerShell": "exec",
    "Read": "io", "Write": "io", "Edit": "io", "Glob": "io", "Grep": "io",
    "ViewImage": "see", "present_files": "see",
    "WebFetch": "web", "WebSearch": "web",
}


def project_dir_for(cwd):
    """C:\\Users\\x\\Desktop\\A B  ->  c-Users-x-Desktop-A B"""
    s = cwd.replace(":", "-").replace("\\", "-").replace("/", "-")
    return s


def find_session_file(path=None):
    """挑一个会话文件。优先级：显式 path > 环境变量 RD_SESSION > 项目目录内最新 > 全局最新。

    ⚠️ 这里有个坑（2026-11-09 实测踩到并修）：以前 State 只在进程启动时算一次，
    MCP 是长驻进程，于是永远读「启动那一刻最新」的会话 —— 第二个窗口一开，
    status / turns / slowest 全部喂另一个会话的数据，而且输出看着完全正常。
    现在 State.current() 每次都调本函数，代价只是几次 stat。
    """
    forced = path or os.environ.get("RD_SESSION")
    if forced:
        return os.path.abspath(forced)
    cwd = os.getcwd()
    cands = []
    d = os.path.join(PROJECTS, project_dir_for(cwd))
    if os.path.isdir(d):
        cands = glob.glob(os.path.join(d, "*.jsonl"))
    if not cands:
        cands = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    if not cands:
        return None
    return max(cands, key=lambda p: os.path.getmtime(p))


def active_sessions(window=180, limit=4):
    """最近 window 秒内还在被写的会话，按新到旧。

    用来回答一个「文件系统答不了的问题」：现在到底哪个是当前会话？
    如果一个窗口在跑，只有 1 个活跃文件，结论可信；
    如果两个窗口同时开工，这里会返回 2 个 —— 那就不是选得对不对，是存在歧义，
    必须把候选摊给调用方看，别装作用户只有一个会话。
    """
    cwd = os.getcwd()
    d = os.path.join(PROJECTS, project_dir_for(cwd))
    cands = glob.glob(os.path.join(d, "*.jsonl")) if os.path.isdir(d) else []
    if not cands:
        cands = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    now = time.time()
    rows = []
    for p in cands:
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if now - m <= window:
            rows.append({"path": p, "mtime": m, "age": int(now - m)})
    rows.sort(key=lambda r: -r["mtime"])
    return rows[:limit]


def clean_user_text(raw):
    """从用户消息里取出他真正问的那句话，去掉系统注入的上下文。"""
    if not raw:
        return ""
    # 注意：上下文压缩后的消息会带着历史 <user_query>，真正的问题在最后一个
    ms = re.findall(r"<user_query>(.*?)</user_query>", raw, re.S)
    if ms:
        raw = ms[-1]
    else:
        raw = re.sub(r"<system-reminder[\s\S]*?</system-reminder>", " ", raw)
        raw = re.sub(r"<[a-z_\-]+[^>]*>[\s\S]*?</[a-z_\-]+>", " ", raw)
    raw = re.sub(r"@image#\d+:[^\s]+", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    # 自动化通知 / 被截断的标签不是用户提问，别让它变成一个怪名字的珠子
    if not raw or raw.startswith("<") or "</task" in raw or raw.startswith("</"):
        return "系统通知"
    return raw


def short(s, n=16):
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


_EVENTS_CACHE = {}     # path -> (sig, events)
_EVENTS_CACHE_MAX = 6  # 只留最近几个，别把内存吃满


def load_events(path):
    """读整个 JSONL。行数不固定，末尾可能是半行，所以逐行容错。

    ⚠️ 预筛很重要：实测最大的会话文件有 154 MB / 52351 行，
    其中 file-history-snapshot（5769 行 / 44MB）和 reasoning（13276 行 / 19MB）
    **解析出来也从不用**。按裸子串预筛掉，能少解析 19000 行、读 60MB。
    这是「打开超大会话很卡」的后端那一半。

    ⚠️ 缓存：解析一次要 1.3 秒（读 154MB），同一个会话会被反复点开
    （加载、跳转到某回合、再加载…）。按 (mtime, size) 缓存，文件没变就直接复用。
    """
    try:
        st = os.stat(path)
        sig = (st.st_mtime, st.st_size)
    except OSError:
        return None
    hit = _EVENTS_CACHE.get(path)
    if hit and hit[0] == sig:
        return hit[1]

    tool_calls = []
    results = {}
    users = []
    title = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # 预筛：只解析用得上的四种行
                #   function_call / function_call_result / message / ai-title
                # 裸子串匹配，避免对 44MB 的快照做 json.loads
                if ("function_call" not in line
                        and '"message"' not in line
                        and "ai-title" not in line
                        and '"aiTitle"' not in line):
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                t = o.get("type")
                if t == "function_call":
                    tool_calls.append(o)
                elif t == "function_call_result":
                    results[o.get("callId")] = o
                elif t == "message" and o.get("role") == "user":
                    users.append(o)
                elif t == "ai-title":
                    title = o.get("aiTitle") or title
    except FileNotFoundError:
        return None
    ev = {"calls": tool_calls, "results": results, "users": users, "title": title}
    if len(_EVENTS_CACHE) >= _EVENTS_CACHE_MAX:
        _EVENTS_CACHE.clear()      # 简单粗暴：到上限就全清，别做 LRU 的复杂度
    _EVENTS_CACHE[path] = (sig, ev)
    return ev


def _user_text(u):
    """取一条 user 消息的正文。

    ⚠️ content 有两种形态（实测 107 个会话里有 19 条是字符串）：
       · 绝大多数：[{"type":"input_text","text":"..."}]
       · 少数：  "直接就是一串文字"
    以前只按列表处理，遇到字符串就 .get() 崩掉 → 整个会话打不开（HTTP 500）。
    这里两种都收下。
    """
    c = u.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, dict):
        c = [c]
    if not isinstance(c, list):
        return ""
    for item in c:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "input_text":
            return item.get("text") or ""
        if item.get("type") == "text" and not item.get("type"):
            return item.get("text") or ""
    # 兜底：没有 input_text 但有 text 字段的
    for item in c:
        if isinstance(item, dict) and item.get("text"):
            return item["text"]
    return ""


def split_turns(ev):
    """会话 -> 回合（calls 已按时间归属）。两个视图共用这一份切分，口径不会分叉。

    ⚠️ 归属查找必须用二分，不能线性扫：
       原来每个 call 都从头扫一遍 turns（13215 × 2340 ≈ 3000 万次），
       在 154MB 的会话上要 0.6 秒。改成 bisect 后是 O(calls log turns)。
    """
    users, calls = ev["users"], ev["calls"]
    turns = []
    for u in users:
        txt = _user_text(u)
        turns.append({
            "ts": u.get("timestamp") or 0,
            "name": short(clean_user_text(txt), 11) or "（空）",
            "calls": [],
        })
    if not turns:
        return []
    turns.sort(key=lambda x: x["ts"])
    # 回合的时间戳（已排序）→ bisect 用
    ts_list = [t["ts"] for t in turns]
    for c in calls:
        ts = c.get("timestamp") or 0
        # 找最后一个 ts <= c.ts 的回合；没有就归到第一个
        idx = bisect.bisect_right(ts_list, ts) - 1
        if idx < 0:
            idx = 0
        turns[idx]["calls"].append(c)
    return turns


FILE_TOOLS = ("Read", "Write", "Edit")


def step_node(c, ev, seq, i, turn_name=""):
    """一个工具调用 -> 一个珠子。file 字段是「空间」侧的钩子。"""
    ts = c.get("timestamp") or 0
    nxt = seq[i + 1].get("timestamp") if i + 1 < len(seq) else None
    res = ev["results"].get(c.get("callId"))
    rts = (res or {}).get("timestamp")
    if rts and rts >= ts:
        dur = rts - ts
    elif nxt and nxt >= ts:
        dur = int((nxt - ts) * 0.9)
    else:
        dur = 0
    dur = max(80, min(dur, 300000))

    name = c.get("name") or "?"
    ok = (res or {}).get("status", "completed") in ("completed", None, "success")
    args = c.get("arguments") or ""
    hint = ""
    fpath = None
    try:
        a = json.loads(args) if isinstance(args, str) else (args or {})
        if name in FILE_TOOLS and isinstance(a.get("file_path"), str):
            fpath = a["file_path"]
        for k in ("command", "file_path", "path", "url", "query"):
            if isinstance(a.get(k), str):
                val = a[k]
                # 路径只显示文件名：40 字的预算不该浪费在一长串上级目录上
                if k in ("file_path", "path"):
                    val = os.path.basename(val.replace("\\", "/"))
                hint = short(val, 40)
                break
    except Exception:
        hint = short(str(args), 40)

    node = {
        "kind": "step",
        "name": TOOL_ZH.get(name, name),
        "value": dur,
        # w 统一为「等效步数」：一个动作就是 1。
        # 原来的 100*(耗时/1000)^0.35 是**表现量**，和上层的「结构量」量纲不同，
        # 放一起没法比。耗时改由「有效度」体现（见 eff_* 字段），不混进尺寸。
        "w": WT.w_steps(),
        "tool": name,
        "family": FAMILY.get(name, "misc"),
        "ok": bool(ok),
        "hint": hint,
        "turn": turn_name,
        "at": ts,
    }
    if fpath:
        node["file"] = fpath
    return node


def build_tree(ev, offset=0, limit=0, sid=""):
    """对话视图：会话 -> 回合 -> 工具调用。

    offset/limit：只构建其中一段回合。
    实测有个 2340 回合的会话，全量构建要 0.75s、序列化出 3.9MB，
    浏览器拿到后画 2340 颗珠子要算 11 秒 —— 所以分段给。

    sid：盖在**回合**上（不给步骤盖）。回合是这一步的磁盘归属 ——
    步骤珠想「在资源管理器中打开」时，往上找一层就能拿到它所属会话的 .jsonl。
    每个步骤都盖一遍的话，一个两千回合的会话要多出上百 KB，纯浪费。
    """
    turns = split_turns(ev)
    if not turns:
        return {"name": ev["title"] or "会话", "children": []}

    total_turns = len(turns)
    if limit and limit > 0:
        turns = turns[offset:offset + limit]

    out_turns = []
    for t in turns:
        seq = sorted(t["calls"], key=lambda c: c.get("timestamp") or 0)
        kids = [step_node(c, ev, seq, i, t["name"]) for i, c in enumerate(seq)]
        if not kids:
            kids.append({"kind": "step", "name": "未执行", "value": 0, "w": WT.w_steps(),
                         "tool": "-", "family": "misc", "ok": True,
                         "hint": "这一轮没有产生工具调用", "at": t["ts"]})
        out_turns.append({
            "kind": "turn",
            "name": t["name"],
            "children": kids,
            "w": WT.w_count(len(kids)),
            "at": t["ts"],
            **({"sid": sid} if sid else {}),
        })
    tree = {"name": ev["title"] or "本次会话", "children": out_turns}
    if limit and limit > 0:
        tree["_total"] = total_turns
        tree["_offset"] = offset
        tree["_limit"] = limit
    return tree


def build_space_tree(ev):
    """空间视图：工作区文件 -> 动过它的回合 -> 具体动作。

    这才是「链接空间与对话」：同一条边的两个方向，
    从对话看是「这一轮改了哪些文件」，从空间看是「这个文件被哪几轮改过」。
    """
    turns = split_turns(ev)
    if not turns:
        return {"name": "工作区", "children": []}

    files = {}      # 归一路径 -> {"name":.., "turns": {turn_idx: [steps]}}
    for ti, t in enumerate(turns):
        seq = sorted(t["calls"], key=lambda c: c.get("timestamp") or 0)
        for i, c in enumerate(seq):
            node = step_node(c, ev, seq, i, t["name"])
            fp = node.get("file")
            if not fp:
                continue
            # 身份按 rel 归一去重：同一个文件在记录里可能写成 C:\... 和 c:/...
            # 两种形式（Windows 路径不分大小写），不归一就会变成两个圆。
            rk = rel_path(fp)
            k = rk.lower() if os.name == "nt" else rk
            rec = files.setdefault(k, {"turns": {}, "count": 0, "rel": rk, "fp": fp})
            rec["count"] += 1
            rec["turns"].setdefault(ti, {"turn": t, "steps": []})["steps"].append(node)

    out_files = []
    for k_, rec in files.items():
        fp = rec["fp"]
        kids = []
        for ti in sorted(rec["turns"]):
            item = rec["turns"][ti]
            kk = len(item["steps"])
            kids.append({
                "kind": "turn",
                "name": item["turn"]["name"],
                "children": item["steps"],
                "w": WT.w_count(kk),
                "at": item["turn"]["ts"],
            })
        name = fp.replace("\\", "/").rstrip("/").split("/")[-1]
        out_files.append({
            "kind": "file",
            "name": name,
            # 显示用 name 就够了，但身份必须用 rel —— 见 rel_path 的说明
            "rel": rec["rel"],
            "path": fp,
            "children": kids,
            "w": WT.w_count(rec["count"]),
            "at": 0,
            "count": rec["count"],
        })

    out_files.sort(key=lambda x: -x["count"])
    return {"name": "工作区", "children": out_files}


# ==================================================================
#  资料库索引层
#  99 个会话 / 543MB / 2.7 万次调用 —— 全量 json.loads 要几十秒，
#  所以只做字符串预筛，命中才解析。索引缓存成 _index.json。
# ==================================================================

LIB_CACHE = os.path.join(USER, "_index.json")
RE_TS = re.compile(r'"timestamp"\s*:\s*(\d+)')

# 正文索引：每个会话存一批正文片段，让「搜正文」能命中。
# 代价实测：每会话约 1.2 KB → 全库索引 +130 KB（原 321 KB），扫描速度不变。
TEXT_SNIPPET_CHARS = 400   # 每条片段最多几个字
TEXT_MAX_CHARS = 1200      # 每个会话总共存多少字（够覆盖大部分会话，又不至于把索引撑爆）
TEXT_USER_SHARE = 0.5      # 用户提问与助手回复各占一半额度
TEXT_MAX_ITEMS = 10        # 每一侧最多收几条
# 为什么要分份额：之前 user 侧先把额度用光，assistant 侧一个字都进不去，
# 结果「面包屑」这类**只出现在助手回复里**的词永远搜不到。
# 为什么要限条数：assistant 的回复动辄几千字，只按字数限的话，
# 前两条就把额度吃光 —— 而关键词往往出现在**中后段**的回复里。
RE_FPATH = re.compile(r'"file_path"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _home_prefixes():
    """本机用户目录的几种写法 —— 从环境变量动态推导，不写死任何用户名。

    旧版本在这里硬编码了 "c:/users/lenovo/"，
    结果别人机器上（用户名不是 lenovo）前缀剥不掉，
    工作区会显示成 "c-Users-张三-Desktop-..." 这种难看的形式。

    规则：剥到「用户目录」这一层为止，即 C:/Users/张三/ 之后的内容才是工作区名。
    """
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    if not home:
        return []
    low = home.lower()
    out = [low + "/"]
    # 会话记录的 cwd 里，用户名大小写可能与本机不同 —— 统一用小写比对
    if low.startswith("c:/users/"):
        out.append("c:/users/")
    return out


def ws_from_cwd(cwd):
    """C:/Users/张三/Desktop/GonJui/Raindrop Chart -> Desktop/GonJui/Raindrop Chart
       比从转义后的目录名反推可靠：目录名里的 "-" 既可能是层级也可能是名字本身。"""
    if not cwd:
        return None
    s = cwd.replace("\\", "/").rstrip("/")
    low = s.lower()
    for pre in _home_prefixes():
        if low.startswith(pre):
            s = s[len(pre):]
            break
    return s or None


# 用户目录下可能作为工作区顶层的文件夹。只用于把「Desktop-Xxx」还原成
# 「Desktop/Xxx」—— 白名单，不是黑名单：见 "-" 就切会把 snake-life 切成
# snake/life，把 2026-09-26-08-40-05 切成五段。
_ROOT_DIRS = ("Desktop", "WorkBuddy", "Documents", "Downloads", "Pictures",
              "Music", "Videos", "OneDrive", "Desktop2")


def ws_label(dirname):
    """c-Users-张三-Desktop-GonJui-Raindrop Chart -> Desktop/GonJui/Raindrop Chart

    目录名是把路径里的 "/" 与 "\\" 换成 "-" 得到的，
    所以要剥掉的前缀 = 用户目录路径换 "-" 后的形态（大小写不敏感比对）。
    """
    s = dirname
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    if home:
        # "C:/Users/张三" -> "c-users-张三"
        # ★ 必须去掉盘符的冒号：目录名里冒号是非法字符，实际存下来是
        #   "c-Users-Lenovo-…"，而 "c:-users-lenovo" 永远匹配不上 ——
        #   结果这条前缀剥不掉，工作区名就成了 "c-Users-Lenovo/2026-…"。
        stem = home.lower().replace("/", "-").replace(":", "")
        if s.lower().startswith(stem):
            s = s[len(stem):]
            # 后面紧跟的 "-" 是分隔符，去掉
            if s.startswith("-"):
                s = s[1:]
    # 顶层目录名的还原：剥完用户目录后，剩下的开头是 "Desktop-xxx" /
    # "WorkBuddy-xxx"。只把**第一层**的 "-" 还原成 "/"，而且只认这几个
    # 真实存在的顶层目录 —— 见 "-" 就切会把 snake-life 切成 snake/life。
    for root in _ROOT_DIRS:
        if s.lower().startswith(root.lower() + "-"):
            s = s[:len(root)] + "/" + s[len(root) + 1:]
            break
    # 剩下的 "-" 哪些是层级、哪些是名字本身的一部分，无法从字符串判断。
    # 用文件系统当裁判：从右往左试，把能拼成真实目录的那个 "-" 还原。
    # 拼不出来的保持原样（宁可少还原一层，也不要把名字切坏）。
    s = _restore_dashes(s, os.path.expanduser("~").replace("\\", "/"))
    return s or dirname


def _restore_dashes(s, base):
    """把 "CZh-个人信息-面试文档" 里的层级 "-" 还原成 "/"。

    字符串本身分不清「层级分隔」和「名字里本来就有 -」，
    所以拿文件系统当裁判：枚举所有把 "-" 换成 "/" 的组合，
    挑第一个在磁盘上真实存在的整路径。

    为什么全枚举而不是从左往右贪心：贪心在第三层就断了 ——
    "CZh-个人信息-面试文档" 的第一步候选 "CZh/个人信息-面试文档" 不存在，
    于是整个放弃，第三层永远还原不出来。层数 ≤ 6，2^6 次 isdir 很快。
    """
    if "-" not in s or not base:
        return s
    dashes = [i for i, ch in enumerate(s) if ch == "-"]
    if len(dashes) > 8:            # 防御：太长的名字不值得为它枚举 256 次
        return s
    from itertools import product
    for mask in product((0, 1), repeat=len(dashes)):
        buf = list(s)
        for pos, on in zip(dashes, mask):
            if on:
                buf[pos] = "/"
        cand = "".join(buf)
        if os.path.isdir(os.path.join(base, cand.replace("/", os.sep))):
            return cand
    return s


def scan_one(path):
    """单个会话文件 -> 元数据。只解析需要的行。

    first_step_line / last_step_line：第一个和最后一个动作在本文件里的行号。
    存它是为了让"检索命中 → 直接跳读原文"成为两步：
    search 给出 path 和行号区间，调用方 Read 那一段就行，不用从头扫几千行。
    """
    steps = turns = 0
    first_step_line = last_step_line = total_lines = 0
    files = {}
    t0 = t1 = None
    title = None
    first_ask = None
    cwd = None
    texts = []       # 正文片段（搜正文用）
    texts_len = 0    # 已收了多少字（控制索引体积）
    texts_user_len = 0   # 用户提问侧单独计数（它和助手回复侧各占一半额度）
    texts_asst_n = 0     # 助手回复侧已收几条
    try:
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            for lno, ln in enumerate(fh, 1):
                total_lines = lno
                hit_ts = RE_TS.search(ln)
                if hit_ts:
                    ts = int(hit_ts.group(1))
                    if 1e12 < ts < 2e12:
                        t0 = ts if t0 is None else min(t0, ts)
                        t1 = ts if t1 is None else max(t1, ts)
                if cwd is None and '"cwd"' in ln:
                    try:
                        _o = json.loads(ln)
                        if _o.get("cwd"):
                            cwd = _o["cwd"]
                    except Exception:
                        pass
                if '"function_call"' in ln:
                    steps += 1
                    if steps == 1:
                        first_step_line = lno
                    last_step_line = lno
                    # file_path 藏在 arguments 这个「被转义的 JSON 字符串」里，
                    # 所以按 'file_path' 这个裸子串预筛，命中才做轻量解析
                    if "file_path" in ln:
                        try:
                            o = json.loads(ln)
                            a = o.get("arguments")
                            if isinstance(a, str):
                                a = json.loads(a)
                            fp = a.get("file_path") if isinstance(a, dict) else None
                            if isinstance(fp, str) and fp.strip():
                                base = fp.replace("\\", "/").rstrip("/").split("/")[-1]
                                if base:
                                    files[base] = files.get(base, 0) + 1
                        except Exception:
                            pass
                elif '"role": "user"' in ln or '"role":"user"' in ln:
                    turns += 1
                    # 没有 ai-title 的会话占大多数，回落到第一个提问当名字，
                    # 否则资料库里全是 e355568f 这种 sid
                    try:
                        o = json.loads(ln)
                        txt = clean_user_text(_user_text(o))
                    except Exception:
                        txt = ""
                    if title is None and first_ask is None and txt:
                        first_ask = txt
                    # 正文片段：搜正文用（用户提问这一侧）
                    if txt and txt != "系统通知" and texts_user_len < TEXT_MAX_CHARS * TEXT_USER_SHARE:
                        take = txt[:TEXT_SNIPPET_CHARS]
                        texts.append(take)
                        texts_user_len += len(take)
                elif '"role": "assistant"' in ln or '"role":"assistant"' in ln:
                    # 正文片段：助手回复这一侧。
                    # 很多关键词只出现在这里（比如「面包屑」——那是助手在讲的概念，
                    # 用户提问里未必有），所以两边都要收，各占一半额度。
                    #
                    # ⚠️ 只取回复的**中段**：开头的结论和结尾的收尾都很长，
                    # 而关键词往往在中间的展开部分。取 [15%, 75%] 这一段。
                    if texts_asst_n < TEXT_MAX_ITEMS:
                        try:
                            o = json.loads(ln)
                            c = o.get("content")
                            if isinstance(c, str):
                                a = c
                            elif isinstance(c, list):
                                a = "".join(x.get("text") or "" for x in c
                                            if isinstance(x, dict) and x.get("type") in ("text", "output_text"))
                            else:
                                a = ""
                            a = re.sub(r"<[^>]+>", " ", a)
                            a = re.sub(r"\s+", " ", a).strip()
                            if len(a) > TEXT_SNIPPET_CHARS:
                                s = int(len(a) * 0.15)
                                e = int(len(a) * 0.75)
                                a = a[s:e]
                            if a:
                                take = a[:TEXT_SNIPPET_CHARS]
                                texts.append(take)
                                texts_len += len(take)
                                texts_asst_n += 1
                        except Exception:
                            pass
                elif title is None and '"ai-title"' in ln:
                    m = re.search(r'"aiTitle"\s*:\s*"((?:[^"\\]|\\.)*)"', ln)
                    if m:
                        title = m.group(1)
    except Exception:
        pass
    return {
        "id": os.path.splitext(os.path.basename(path))[0],
        "ws": ws_from_cwd(cwd) or ws_label(os.path.basename(os.path.dirname(path))),
        "title": title or first_ask or "",
        "title_ai": title or first_ask or "",   # 原始 AI 标题：每次覆盖前从它重置
        "t0": t0 or 0, "t1": t1 or 0,
        "turns": turns, "steps": steps,
        "first_step_line": first_step_line, "last_step_line": last_step_line,
        "lines": total_lines,                     # 文件总行数，给 Read 的 limit 定上限
        "files": dict(sorted(files.items(), key=lambda kv: -kv[1])[:8]),
        "text": " ".join(texts),                   # 正文片段，搜正文用（用户提问 + 助手回复）
        "size": os.path.getsize(path),
        "mtime": int(os.path.getmtime(path)),
        "path": path,
    }


# ---------- 用户自定义标题（WorkBuddy 的 sessions.custom_title）----------
WB_DB = os.path.join(os.path.expanduser("~"), ".workbuddy", "workbuddy.db")


def _custom_titles():
    """读 WorkBuddy 自己的库，取「它显示的名字」。

    优先级：sessions.custom_title（你手动改的）> sessions.title（WorkBuddy 起的名）。
    为什么还要 title：自动化任务跑出来的会话，jsonl 里 aiTitle 抓的是系统下发的
    英文指令原文（". Do not create a new automation for any trigger…"），
    而 sessions.title 里存的正是任务名（"格律诗"、"花呗 11-08 到期提醒"）。

    只读打开，失败返回空表 —— 绝不能因为读不到标题把整个页面拖崩。
    """
    out = {}
    if not os.path.exists(WB_DB):
        return out
    conn = None
    try:
        uri = "file:" + WB_DB.replace("\\", "/") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.5)
        cur = conn.execute(
            "SELECT id, title, custom_title, is_background_automation FROM sessions")
        for sid, ti, ct, auto in cur.fetchall():
            v = (ct or "").strip() or (ti or "").strip()
            if sid and v:
                out[sid] = (v, bool(auto))
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


def apply_custom_titles(items):
    """把「用户改过的名字」盖到 AI 自动标题上。

    每次都先从 title_ai 重置再覆盖：这样你把名字改回去（custom_title 清空），
    也能立刻恢复成原本的 aiTitle，不会卡在上一次的旧名字。
    """
    for it in items:
        ai = it.get("title_ai")
        if ai is None:                     # 老缓存没有这个字段，就地补齐
            ai = it.get("title") or ""
            it["title_ai"] = ai
        it["title"] = ai
    m = _custom_titles()
    if not m:
        return items
    for it in items:
        rec = m.get(it.get("id"))
        if rec:
            it["title"] = rec[0]
            it["__auto"] = rec[1]

    # 定时任务每天跑一次、每次一条会话，名字全一样（「格律诗」×20）。
    # 同名的自动化会话补上日期，否则雨图里一片重名。
    var_cnt = {}
    for it in items:
        if it.get("__auto"):
            var_cnt[it["title"]] = var_cnt.get(it["title"], 0) + 1
    for it in items:
        if it.get("__auto") and var_cnt.get(it["title"], 0) > 1:
            t = it.get("t1") or 0
            if t:
                ds = time.strftime("%m-%d", time.localtime(t / 1000.0))
                if ds not in it["title"]:
                    it["title"] = it["title"] + " · " + ds
        it.pop("__auto", None)

    # 同一分钟里跑了不止一次（「格律诗 · 09-01」×3），再补时分，保证每条都能点准
    cnt2 = {}
    for it in items:
        cnt2[it["title"]] = cnt2.get(it["title"], 0) + 1
    for it in items:
        if cnt2.get(it["title"], 0) > 1:
            t = it.get("t1") or 0
            if t:
                hm = time.strftime("%H:%M", time.localtime(t / 1000.0))
                if hm not in it["title"]:
                    it["title"] = it["title"] + " " + hm
    return items


def scan_library(force=False):
    """按文件增量缓存。

    旧做法是「全部文件的签名凑成一个整体签名」——只要有 1 个文件变了，
    100 个文件全部重扫（实测 3.4 秒）。而 WorkBuddy 一直在写当前会话的
    jsonl，所以几乎每次刷新都撞上全量重扫。改成每个文件各存一条签名，
    变了的才重扫，其余直接吃缓存。
    """
    files = sorted(glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")))

    cache = {}
    if not force and os.path.exists(LIB_CACHE):
        try:
            raw = json.load(io.open(LIB_CACHE, encoding="utf-8"))
            # ver 3 起按文件存；旧格式（sig+items）直接丢弃，重扫一次升级。
            # ver 4：item 多了 first_step_line / last_step_line / lines。
            # 缓存命中条件是「mtime+size 没变」，字段变了它不会自己失效，
            # 所以必须升版本号逼一次全量重扫，否则读到的还是老 item（没有行号）。
            if raw.get("ver") == 5:
                cache = raw.get("by_path") or {}
        except Exception:
            cache = {}

    by_path = {}
    items = []
    hit = miss = 0
    for f in files:
        try:
            st = os.stat(f)
            sig = "%d:%d" % (st.st_mtime, st.st_size)
        except OSError:
            continue
        rec = cache.get(f)
        if rec and rec.get("sig") == sig and rec.get("item"):
            items.append(rec["item"])
            by_path[f] = rec
            hit += 1
            continue
        try:
            it = scan_one(f)
            items.append(it)
            by_path[f] = {"sig": sig, "item": it}
            miss += 1
        except Exception as e:
            import sys as _sys
            _sys.stderr.write("扫描失败 %s: %r\n" % (os.path.basename(f), e))

    items.sort(key=lambda x: -(x["t1"] or 0))
    if miss:
        try:
            io.open(LIB_CACHE, "w", encoding="utf-8").write(
                json.dumps({"ver": 5, "by_path": by_path}, ensure_ascii=False))
        except Exception:
            pass
        import sys as _sys
        _sys.stderr.write("索引：缓存命中 %d · 新扫 %d\n" % (hit, miss))
    # 标题是「后处理」：重命名不改 jsonl，进不了缓存签名，所以每次都盖一遍
    return apply_custom_titles(items)


def build_library_tree(items):
    """资料库 -> [空间 x8 | 任务] -> (任务里 10 个任务) -> 会话。

    会话仍是懒加载叶子（带 sid），点开才读明细。
    「任务」节点标记 transparent：它在左右树里是“透视层”，
    不额外占一级键路径，这样左列 c-Users-…-2026-09-26-08-40-05
    和右列 任务 / 任务·查看今日AI日报要点 仍能对上同一行。
    """
    groups = {}
    for it in items:
        groups.setdefault(it["ws"], []).append(it)

    def sess_kids(arr):
        kids, steps = [], 0
        for it in arr:
            steps += it["steps"]
            kids.append({
                "kind": "session",
                "sid": it["id"],
                "name": it["title"] or it["id"][:8],
                "children": [],
                "lazy": True,
                "w": WT.w_count(it["steps"]),
                "steps": it["steps"],
                "turns": it["turns"],
                "files": it["files"],
                "at": it["t1"] or it["t0"] or 0,
                "t0": it["t0"],
            })
        kids.sort(key=lambda x: -(x["at"] or 0))
        for k in kids:
            # 会话的直接下级是回合，所以这里报轮数，别报步数（步在回合下面）
            k["right"] = "%d 个回合" % (k.get("turns") or 0)
        return kids, steps

    def ws_node(ws, arr, name, wid, kind="workspace", transparent=False):
        kids, steps = sess_kids(arr)
        node = {
            "kind": kind,
            "name": name,
            "wid": wid,
            "right": "%d 个会话" % len(kids),
            "children": kids,
            "w": WT.w_count(steps),
            "steps": steps,
            "sessions": len(kids),
            "turns": sum((k.get("turns") or 0) for k in kids),
            "at": max((k["at"] or 0) for k in kids) if kids else 0,
            "t0": min((k.get("t0") or 0) for k in kids if k.get("t0")) if kids else 0,
        }
        if transparent:
            node["transparent"] = True
        return node

    out, task_nodes = [], []
    for ws, arr in groups.items():
        if is_task_ws(ws):
            task_nodes.append(ws_node(ws, arr, ws_display(ws, items), ws))
            continue
        out.append(ws_node(ws, arr, ws_display(ws, items), ws))

    if task_nodes:
        task_nodes.sort(key=lambda x: -(x["at"] or 0))
        # 「任务」这一层不额外占键路径（transparent），所以它的子节点直接挂在根下
        out.append({
            "kind": "tasks",
            "name": "任务",
            "wid": TASKS_WID,
            "transparent": True,
            "right": "%d 个任务" % len(task_nodes),
            "tasks": len(task_nodes),
            "sessions": sum(t["sessions"] for t in task_nodes),
            "turns": sum(t["turns"] for t in task_nodes),
            "children": task_nodes,
            "w": WT.w_count(sum(t["steps"] for t in task_nodes)),
            "steps": sum(t["steps"] for t in task_nodes),
            "at": max(t["at"] for t in task_nodes),
        })

    out.sort(key=lambda x: -(x["at"] or 0))
    return {
        # 只报直接下级（空间）的数量；会话数是下一层的事，不往上挂
        "name": "WorkBuddy资料库",
        "right": "%d 个空间" % len(out),
        "kind": "root",
        "children": out,
        "steps": sum(i["steps"] for i in items),
    }


def _sz(n):
    if not n:
        return ""
    return ("%.1f MB" % (n / 1048576.0)) if n > 1048576 else ("%d KB" % round(n / 1024.0))


def _fmt_at(at, with_sec=False):
    try:
        return time.strftime("%m-%d %H:%M:%S" if with_sec else "%m-%d %H:%M",
                             time.localtime((at or 0) / 1000.0))
    except Exception:
        return ""


def resolve_reveal(body):
    """把前端给的身份（sid / rel / wid）翻成磁盘上真实存在的路径。

    为什么不让前端直接传路径：那样这个接口就成了「打开任意路径」的开关，
    页面里任何一段脚本都能让本机弹出资源管理器。只认身份，路径服务端自己查，
    查不到就明确说查不到 —— 不猜。

    返回 (路径, 是不是目录)；查不到返回 (None, None)。
    """
    HOME = os.path.expanduser("~")
    sid = str(body.get("sid") or "")
    rel = str(body.get("rel") or "")
    wid = str(body.get("wid") or "")

    # 0. 磁盘树自己报的相对路径（相对 ~/.workbuddy/projects）最准 ——
    #    它说的就是「这一行在磁盘上的那个位置」，不会被同名的 wid 抢走。
    #    带 .. 或绝对路径的一律不认：这个字段只该是 projects 底下的相对路径。
    disk = str(body.get("disk") or "")
    if disk:
        root = os.path.normpath(PROJECTS)
        p = os.path.normpath(os.path.join(root, disk))
        if (p == root or p.startswith(root + os.sep)) and os.path.exists(p):
            return p, os.path.isdir(p)

    # 1. 会话：sid 就是库里那条 item 的 id，顺着它拿绝对路径最准
    if sid:
        try:
            for it in scan_library():
                if it.get("id") == sid:
                    p = it.get("path") or ""
                    if p and os.path.exists(p):
                        return os.path.normpath(p), False
        except Exception:
            pass

    # 2. 文件：rel 是相对主目录的路径
    if rel:
        p = os.path.join(HOME, rel.replace("/", os.sep).replace("\\", os.sep))
        if os.path.exists(p):
            return os.path.normpath(p), os.path.isdir(p)

    # 3. 工作区：wid 也是相对主目录的路径（Desktop/GonJui/Raindrop Chart）。
    #    目录可能已被改名/删掉，那就退一步按 projects 的 slug 规则反查。
    if wid:
        p = os.path.join(HOME, wid.replace("/", os.sep).replace("\\", os.sep))
        if os.path.isdir(p):
            return os.path.normpath(p), True
        slug = wid.replace(":", "").replace("/", "-").replace("\\", "-")
        p2 = os.path.join(PROJECTS, slug)
        if os.path.isdir(p2):
            return os.path.normpath(p2), True

    return None, None


def shorten_path_disp(children):
    """路径型名字只留「能分开自己的那一小段」。

    工作区的 name 是相对主目录的全路径（Desktop/CZh/个人信息/面试文档、
    WorkBuddy/2026-05-10-task-1）。画到圆上就是一条塞不下的长串，
    裁完只剩「Desktop/…」，十几颗圆全长得一样，谁也认不出谁。

    这里只写 disp（给人看的），name / wid / rel 一个都不动 ——
    地址、锁、接口查询全走那些字段，换个显示名不该影响任何一件。

    规则：从最后一段往前取，取到在本层唯一为止。
        个人信息/面试文档  →  面试文档
        Desktop/CZh        →  CZh
    同层真撞了才带父级（面试文档 vs 另一个面试文档 → CZh/面试文档）。
    """
    # 只对「路径型身份」的节点做压缩：工作区有 wid、文件有 rel。
    # 不加这条限制会误伤 —— 会话标题里出现斜杠（「写/读文件对比」）时
    # 会被当成路径，"写/读文件对比" 被压成 "读文件对比"，名字就变了。
    idx = [i for i, c in enumerate(children)
           if "/" in (c.get("name") or "") and (c.get("wid") or c.get("rel"))]
    if not idx:
        return children
    segs = {}
    for i in idx:
        segs[i] = [s for s in children[i]["name"].split("/") if s]
    for i in idx:
        s = segs[i]
        for d in range(1, len(s) + 1):
            suf = "/".join(s[-d:])
            if sum(1 for j in idx if "/".join(segs[j][-d:]) == suf) == 1:
                children[i]["disp"] = suf
                break
    return children


def dedupe_disp(children):
    """同一层里出现同名时，给显示名补上“能区分开”的那一小段。

    只写 disp（给人看的），不动 name（那是数据）。前端优先读 disp、
    没有就退回 name —— 所以这个函数随时可以关掉，不会污染数据。
    """
    if not children:
        return children
    # 先做路径压缩：不管有没有重名，全路径一律不该画在圆上
    shorten_path_disp(children)
    if len(children) < 2:
        return children

    groups = {}
    for i, c in enumerate(children):
        groups.setdefault(c.get("name") or "", []).append(i)

    for name, idxs in groups.items():
        if len(idxs) < 2:
            continue

        done = False

        # ① 有 rel 的（文件）：去掉所有人共有的前缀，剩下的才是区分信息
        if all(children[i].get("rel") for i in idxs):
            parts = [children[i]["rel"].split("/") for i in idxs]
            L = 0
            while True:
                if any(len(p) <= L + 1 for p in parts):
                    break                       # 保住文件名，不往前吃掉
                seg = parts[0][L]
                if any(p[L] != seg for p in parts):
                    break
                L += 1
            for k, i in enumerate(idxs):
                rest = parts[k][L:]
                if len(rest) <= 1:
                    children[i]["disp"] = rest[0] if rest else name
                elif len(rest) == 2:
                    children[i]["disp"] = rest[0] + "/" + rest[1]
                else:
                    children[i]["disp"] = rest[0] + "/…/" + rest[-1]
            done = len({children[i]["disp"] for i in idxs}) == len(idxs)

        # ② 有时间戳的（回合 / 会话）：用时间区分
        if not done and all(children[i].get("at") for i in idxs):
            for i in idxs:
                children[i]["disp"] = name + " · " + _fmt_at(children[i]["at"])
            done = len({children[i]["disp"] for i in idxs}) == len(idxs)
            if not done:                        # 同一分钟还撞，精确到秒
                for i in idxs:
                    children[i]["disp"] = name + " · " + _fmt_at(children[i]["at"], True)
                done = len({children[i]["disp"] for i in idxs}) == len(idxs)

        # ③ 兜底：加序号。宁可丑，也不留两个一模一样的
        if not done:
            for k, i in enumerate(idxs):
                children[i]["disp"] = "%s · %d" % (name, k + 1)
    return children


def tag_uid(children):
    """给没有稳定身份的节点补一个。

    回合节点原来只有 name，同名的回合在前端会拿到同一个地址 ——
    24 个「系统通知」因此被算成同一颗圆（不是重名，是并成了一颗）。
    at 是毫秒时间戳，同一会话内必不同，拿它当身份最稳。
    """
    for c in children:
        if c.get("uid"):
            continue
        # 已经有稳定身份的就别再加一层：addrSeg 本来就会走 rel/sid/wid 分支。
        # 加了 "sid:" 前缀反而制造不一致 —— 同一个节点在一个接口里是 usid%3Axxx、
        # 在另一个接口里是 xxx，两个视图就对不上了。
        if c.get("rel") or c.get("sid") or c.get("wid") or c.get("srcKey"):
            continue
        if c.get("at"):
            c["uid"] = "%s:%d" % (c.get("kind") or "n", int(c["at"]))
        else:
            c["uid"] = "%s:%s" % (c.get("kind") or "n", c.get("name") or "")
    return children


def polish_tree(node):
    """把“同层重名”和“身份缺失”两件事一次性做掉，递归到整棵树。

    幂等：每次都从 name/rel/at 重算，所以可以反复调用，不会越加越长。
    """
    ch = node.get("children") or []
    if ch:
        tag_uid(ch)
        dedupe_disp(ch)
        for c in ch:
            polish_tree(c)
    return node


def build_disk_tree(items):
    """磁盘上真实的样子：projects/ -> 工作区目录名 -> *.jsonl 文件名。

    和 build_library_tree 吃同一批 items，所以左右两棵树可以按 sid 对上：
    左边那个 UUID.jsonl，就是右边那个「会话标题」。
    """
    groups = {}
    for it in items:
        groups.setdefault(os.path.dirname(it.get("path") or ""), []).append(it)

    plain, task_dirs = [], []
    for d, arr in groups.items():
        arr.sort(key=lambda x: -(x.get("t1") or 0))
        kids = []
        for it in arr:
            kids.append({
                "kind": "file",
                "name": os.path.basename(it.get("path") or ""),
                "mono": True,
                # 这里原来跟了一句「→ 会话标题」。左栏是「磁盘上长什么样」，
                # 一行里塞文件名 + 标题两串东西，反而两头都读不清；
                # 标题在右栏「资料库」那边本来就是主名，不用在左栏重报一遍。
                "right": "%d 步 · %s" % (it.get("steps") or 0, _sz(it.get("size"))),
                "sid": it.get("id"),
                "children": [],
            })
        node = {
            "kind": "dir",
            "name": os.path.basename(d),
            "wid": (arr[0].get("ws") or os.path.basename(d)),   # 与右列同一个身份
            # 这一行在磁盘上的真实位置（相对 ~/.workbuddy/projects）。
            # 为什么非要有它：右列同一个工作区也带 wid，但两边的「真实位置」不是一回事 ——
            # 左列是 projects 下那个机器命名的目录，右列是用户桌面上的项目文件夹。
            # 只给 wid，两边都会解析到桌面那个，左列点「打开」就跳到别处去了。
            "disk": os.path.basename(d),
            "mono": True,
            "right": "%d 个会话" % len(arr),
            "children": kids,
            "at": max((x.get("t1") or 0) for x in arr),
        }
        # 右列把「任务型目录」收成了一个「任务」节点。左列若平铺 18 个目录，
        # 两列的层级就不一样了，逐行对照必然错位 —— 所以左列也要有同一层聚合。
        if is_task_ws(arr[0].get("ws")):
            task_dirs.append(node)
        else:
            plain.append(node)

    out = plain
    if task_dirs:
        task_dirs.sort(key=lambda x: -(x["at"] or 0))
        out = plain + [{
            "kind": "tasks",
            "name": "任务",
            "wid": TASKS_WID,
            # 和右列的「任务」一样是透视层：子节点沿用根的键路径，左右才连得上
            "transparent": True,
            "note": "虚拟层 · 磁盘上没有这一层",
            "right": "%d 个目录" % len(task_dirs),
            "children": task_dirs,
            "at": max(d["at"] for d in task_dirs),
        }]
    out.sort(key=lambda x: -(x["at"] or 0))
    return {
        "kind": "root",
        "name": os.path.basename(PROJECTS) + "/",
        "mono": True,
        "note": "~/.workbuddy/projects",
        "right": "%d 个条目" % len(out),
        "children": out,
    }


# 任务型目录（机器生成的名字）——在雨图里按「任务」显示
AUTO_WS = re.compile(r"^(20\d\d-\d\d-\d\d|automation-|task-|BI$|每日汇报-)")
# 只认「机器生成」的那三种。这两个要拿**最后一段**去匹配：
# 工作区名现在是「WorkBuddy/2026-05-10-task-1」这种带前缀的全路径，
# 用整串去 match 一律不中（^ 锚在开头，开头是 WorkBuddy/），
# 结果 8 个任务目录各占一个圆，名字还全是「2026-10-29-22-05-45」——
# 既挤又认不出。BI / 每日汇报- 不进这一条：那是人自己建的文件夹，
# 不该被并进「任务」。
AUTO_DIR = re.compile(r"^(20\d\d-\d\d-\d\d|automation-|task-)")


def is_task_ws(ws):
    """这个工作区是机器生成的任务目录吗。

    ★ 判据必须接受「带前缀」和「不带前缀」两种形态：
      带前缀 = 正常态（WorkBuddy/2026-05-10-task-1）；
      不带  = 老数据 / 代码在根目录时的退化成（2026-05-10-task-1）。
      只按前者判断会让老目录突然变成普通工作区，只按后者就是现在这个 bug。
    """
    if not ws:
        return False
    if AUTO_WS.match(ws):
        return True
    return bool(AUTO_DIR.match(ws.split("/")[-1] or ""))
TASKS_WID = "__tasks__"     # 「任务」空间的固定键

# WorkBuddy「助理」功能的默认工作区。助理内部代号就是 Claw
# （app.asar 里的类名是 WeixinClawBotClient，设置面板叫 ClawSettingsPanel，
#  微信长轮询游标落在 ~/.workbuddy/claw-state/weixin/），
# 所以这个目录里装的其实是「用微信对话」的全部内容。
CLAW_WS = "Claw"


# ------------------------------------------------------------------
#  双栏对照：左 = 磁盘上的链接地址，右 = 雨图里的库
#
#  两栏讲的是同一批东西，所以必须同一份清单、同一个顺序、同一个 wid，
#  否则联动（悬停 / 滚动 / 展开）会连错行。
#  旧版只有 WorkBuddy 一棵树 —— 导入一个新库，这一页完全不动，
#  用户看到的是「磁盘上 18 个目录」和「9 个空间」，跟自己刚加的库无关。
# ------------------------------------------------------------------

def _count_leaves(node):
    """数叶子（文件 / 会话）。中间层本身不算。"""
    n = 0
    for c in (node.get("children") or []):
        n += _count_leaves(c) if c.get("children") else 1
    return n


def _addr_of(path):
    """地址列怎么显示：程序目录里的写成相对路径，家目录里的写成 ~/…
    绝对路径太长，会把左列挤成一行省略号，反而认不出这是哪个库。"""
    if not path:
        return ""
    fp = os.path.abspath(path).replace("\\", "/")
    for root, pfx in ((HERE, ""), (os.path.expanduser("~"), "~/")):
        r = root.replace("\\", "/").rstrip("/")
        if fp == r:
            return pfx or "."
        if fp.startswith(r + "/"):
            return pfx + fp[len(r) + 1:]
    return fp


def _size_text(n):
    """小文件也要有话可说：124 字节写成「0 KB」等于没写。"""
    n = n or 0
    if n >= 1048576:
        return "%.1f MB" % (n / 1048576.0)
    if n >= 1024:
        return "%d KB" % round(n / 1024.0)
    return "%d B" % int(n)


def build_columns():
    disk_kids, lib_kids = [], []
    for s in list_sources():
        sk = s.get("key") or ""
        # 没装 WorkBuddy 的机器上这颗永远是空的，别在这儿摆一行空地址
        if sk == "workbuddy" and not WorkBuddySource.available():
            continue
        try:
            src = get_source(sk)
            tree = src.root()
            sm = src.summary(tree) or {}
        except Exception:
            continue

        label = s.get("label") or sk
        kids = tree.get("children") or []
        # 双栏里这两棵树也要过一遍显示名压缩：工作区名是相对主目录的全路径，
        # 树图那一行塞得下，但和雨图的圆共用一份 disp —— 不在这儿压，
        # 切到雨图那边就又是一串 Desktop/…。dedupe_disp 幂等，重复调用无害。
        tag_uid(kids)
        dedupe_disp(kids)
        cnt = sm.get("count") or 0
        unit = sm.get("countUnit") or ""
        is_wb = (src.key == "workbuddy")
        is_csv = isinstance(src, CsvSource)
        # 文件夹导入的显示名要用用户选的那个文件夹名（存在树文件的 name 里），
        # 不是 sources/ 下的文件名 slug
        if not is_wb and not is_csv:
            label = tree.get("name") or label

        # ---- 左：这个东西在磁盘上到底是什么 ----
        if is_wb:
            try:
                dkids = build_disk_tree(scan_library()).get("children") or []
            except Exception:
                dkids = []
            left = {"kind": "src", "wid": sk,
                    "name": "~/.workbuddy/projects", "mono": True,
                    # "." = projects 目录本身。有它这一行才点得开
                    "disk": ".",
                    "children": dkids,
                    "right": "%d 个 jsonl" % _count_leaves({"children": dkids})}
        else:
            p = getattr(src, "path", "") or ""
            try:
                size = os.path.getsize(p)
            except Exception:
                size = 0
            if is_csv:
                # CSV 在磁盘上就是一个文件，没有可展开的结构
                left = {"kind": "src", "wid": sk, "name": _addr_of(p) or label,
                        "mono": True, "children": [], "right": _size_text(size)}
            else:
                # 文件夹导入：浏览器不给绝对路径（安全限制），
                # 只能指向我们落盘的那份结构文件 —— 不假装知道原目录在哪
                left = {"kind": "src", "wid": sk, "name": label,
                        "note": _addr_of(p), "children": kids,
                        "right": "%d 个目录 · %d 个文件" % (cnt, sm.get("steps") or 0)}
        disk_kids.append(left)

        # ---- 右：雨图里它叫什么、量是多少 ----
        # 量取「叶子」那一档：文件夹库的 count 是目录数，说「2 个目录」既少又和左栏重复，
        # 改说文件数；会话 / 分组本来就是叶子，照旧。
        lu, lc = unit, cnt
        if lu == "目录" and sm.get("stepUnit"):
            lu, lc = sm.get("stepUnit"), sm.get("steps") or 0
        lib_kids.append({
            "kind": "src", "wid": sk, "name": label, "children": kids,
            "right": ("%d 个%s" % (lc, lu)) if lc else "",
        })

    # 根行的数字由表头负责（前端 rowsOf 对 depth 0 不渲染 right）：
    # 表头已经报了「N 个链接地址 / N 个库」，紧挨着的下一行再说一遍同一个数，
    # 只会让人以为有两种口径。
    return {
        "disk": {"kind": "root", "name": "Raindrop Chart",
                 "right": "%d 个链接地址" % len(disk_kids),
                 "children": disk_kids},
        "lib": {"kind": "root", "name": "Raindrop Chart",
                "right": "%d 个库" % len(lib_kids),
                "children": lib_kids},
    }


def ws_display(ws, items):
    """工作区在界面上的显示名。原始键 wid 永远不变，只换显示，不影响查询与联动。"""
    if ws == CLAW_WS:
        return "微信助理"
    return task_label(ws, items) or ws


def task_label(ws, items):
    """任务型目录（时间戳 / automation- / task- / BI / 每日汇报-）没有人类可读的名字。

    这类目录其实就是 WorkBuddy 里的「任务」，所以给它一个「任务 · 会话标题」的名字，
    让它作为一个独立空间被人认出来。返回 None 表示不是任务型目录。
    """
    if not is_task_ws(ws):
        return None
    cand = []
    for it in items:
        if it.get("ws") != ws:
            continue
        t = it.get("t1") or 0
        title = (it.get("title") or "").strip()
        if title:
            cand.append((t, title))
    if not cand:
        return "任务 · 未命名"
    cand.sort(key=lambda x: -x[0])
    # 优先挑“人写的”标题：含中文、不以标点开头（跳掉自动化写进来的英文指令原文）
    pick = None
    for t, title in cand:
        if re.search(r"[\u4e00-\u9fff]", title) and not re.match(r"^[\s\.\-\*#>]", title):
            pick = title
            break
    if pick is None:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", ws or "")
        pick = ("%s-%s 的任务" % (m.group(2), m.group(3))) if m else "未命名"
    pick = re.sub(r"^[\s\.\-\*#>]+", "", pick or "").strip()   # 去掉开头标点
    if len(pick) > 18:
        pick = pick[:18] + "…"
    return "任务 · " + pick


def build_shelves(items):
    """只到「空间」这一层。会话等点开再拉 —— 首屏别一次扛 100 个会话。

    任务型目录（时间戳/automation 等）不各占一个圆，全部收进一个「任务」空间，
    结构变成：资料库 -> [空间 x8 | 任务] -> (任务里再有 10 个任务) -> 会话。
    """
    groups = {}
    for it in items:
        g = groups.get(it["ws"])
        if g is None:
            g = groups[it["ws"]] = {"ws": it["ws"], "n": 0, "steps": 0,
                                    "turns": 0, "bytes": 0, "at": 0, "t0": 0, "sess": []}
        g["n"] += 1
        g["steps"] += it.get("steps") or 0
        g["turns"] += it.get("turns") or 0
        g["bytes"] += it.get("size") or 0
        g["at"] = max(g["at"], it.get("t1") or 0)
        # 寿命的左端点：没有它就只能画「在哪停的」，画不出「活了多久」。
        # 取 min 而不是第一次遇到的，是因为 items 的顺序不保证按时间。
        _s = it.get("t0") or 0
        if _s and (not g["t0"] or _s < g["t0"]):
            g["t0"] = _s
        g["sess"].append((it.get("steps") or 0, it["title"] or it["id"][:8]))

    def top3(g):
        return [{"name": t[1], "steps": t[0]}
                for t in sorted(g["sess"], key=lambda x: -x[0])[:3]]

    out, tasks = [], []
    for g in groups.values():
        if is_task_ws(g["ws"]):
            tasks.append(g)
            continue
        out.append({
            "kind": "workspace",
            "name": ws_display(g["ws"], items),
            "wid": g["ws"],          # 原始键：接口查询、左右树联动都用它
            "children": [],          # 点开才填
            "lazyWs": True,
            "w": WT.w_count(g["steps"]),
            "steps": g["steps"],
            "turns": g["turns"],
            "sessions": g["n"],
            "bytes": g["bytes"],
            "at": g["at"],
            "t0": g["t0"],
            "top": top3(g),
        })

    if tasks:
        n = sum(g["n"] for g in tasks)
        st = sum(g["steps"] for g in tasks)
        tn = sum(g["turns"] for g in tasks)
        by = sum(g["bytes"] for g in tasks)
        at = max(g["at"] for g in tasks)
        t0 = min((g["t0"] for g in tasks if g["t0"]), default=0)
        merged = {"sess": [x for g in tasks for x in g["sess"]]}
        out.append({
            "kind": "tasks",
            "name": "任务",
            "wid": TASKS_WID,
            "children": [],
            "lazyWs": True,
            "w": WT.w_count(st),
            "steps": st,
            "turns": tn,
            "sessions": n,
            "tasks": len(tasks),
            "bytes": by,
            "at": at,
            "t0": t0,
            "top": top3(merged),
        })

    out.sort(key=lambda x: -(x["at"] or 0))
    return out


def build_ws_children(items, ws):
    """某个工作区下的会话，结构跟 /api/library 的会话节点一致。

    ws == TASKS_WID 时，返回的是「任务」空间里的那 10 个任务节点，
    每个任务再点一层才是它的会话。
    """
    if ws == TASKS_WID:
        by = {}
        for it in items:
            if not is_task_ws(it["ws"]):
                continue
            g = by.setdefault(it["ws"], {"n": 0, "steps": 0, "turns": 0, "at": 0, "t0": 0})
            g["n"] += 1
            g["steps"] += it.get("steps") or 0
            g["turns"] += it.get("turns") or 0
            g["at"] = max(g["at"], it.get("t1") or 0)
            _s2 = it.get("t0") or 0
            if _s2 and (not g["t0"] or _s2 < g["t0"]):
                g["t0"] = _s2
        kids = []
        for name, g in by.items():
            kids.append({
                "kind": "workspace",
                "name": task_label(name, items) or name,
                "wid": name,                 # 再点一层用它拉会话
                "children": [],
                "lazyWs": True,
                "right": "%d 个会话" % g["n"],
                "w": WT.w_count(g["steps"]),
                "steps": g["steps"],
                "turns": g["turns"],
                "sessions": g["n"],
                "at": g["at"],
                "t0": g["t0"],
            })
        kids.sort(key=lambda x: -(x["at"] or 0))
        return kids

    arr = [it for it in items if it["ws"] == ws]
    arr.sort(key=lambda x: -(x["t1"] or 0))
    kids = []
    for it in arr:
        kids.append({
            "kind": "session",
            "sid": it["id"],
            "name": it["title"] or it["id"][:8],
            "children": [],
            "lazy": True,
            "w": WT.w_count(it["steps"]),
            "steps": it["steps"] or 0,
            "turns": it["turns"] or 0,
            "files": it["files"],
            "at": it["t1"] or it["t0"] or 0,
            "t0": it["t0"],
        })
    return kids


_HOME_ABS = os.path.expanduser("~").replace("\\", "/").rstrip("/")


def rel_path(fp):
    """文件节点的**稳定身份**：相对用户主目录的路径。

    三个坑都要避开，缺一个这堆数据就攒不成资产：
      · 只用 basename（原来就是）→ 两个 index.html 撞成同一个点。
        实测库里 index.html 有十几个，全挤在一个圆上。
      · 用绝对路径 → 带用户名，换台机器 / 换个账户名，所有边全断。
      · 用哈希 → 人读不懂，AI 也不好推理，改个目录名就换身份。

    相对主目录的路径同时满足：跨会话一致、跨机器一致、不重名、可读。
        C:/Users/张三/Desktop/项目A/MVP/index.html
          → Desktop/项目A/MVP/index.html
    """
    p = str(fp or "").replace("\\", "/").strip()
    if not p:
        return ""
    # 主目录前缀不区分大小写（Windows 上 c:/users 和 C:/Users 是同一个地方）
    if _HOME_ABS and p.lower().startswith(_HOME_ABS.lower() + "/"):
        return p[len(_HOME_ABS) + 1:]
    # 不在主目录下（比如 D:/work/...）：去掉盘符，其余层级原样保留
    m = re.match(r"^[A-Za-z]:/(.*)$", p)
    return m.group(1) if m else p


def load_session(sid, offset=0, limit=0, tail=False):
    """按需读取一个会话的完整树（回合 -> 工具）。

    ⚠️ 超大会话（实测有一个 2342 回合 / 3.8MB JSON）一次全给会把浏览器卡死：
       前端要把每个回合画成一颗珠子，2340 颗的物理布局要算 11 秒。
    所以支持**分段取**：limit=0 表示全给（老行为），limit>0 表示从 offset 起给 limit 个回合。

    tail=True：取**最后** limit 个回合（忽略 offset）。
       实时刷新要用这个 —— 「最新的东西」在文件末尾，而头部窗口是静止的：
       一个 240 回合的会话按头部取 200 条，最新 40 回合根本不在里面，
       于是「对话一直在动、点开却一动不动」。
    """
    items = scan_library()
    for it in items:
        if it["id"] == sid:
            ev = load_events(it["path"])
            if ev is None:
                return None
            if tail and limit and limit > 0:
                total = len(split_turns(ev))
                offset = max(0, total - limit)
            tree = build_tree(ev, offset, limit, sid)
            tree["sid"] = sid
            return tree
    return None


class State:
    """当前会话的缓存。**不缓存「哪个会话」**，只缓存它的解析结果。

    分界线很重要：会话路径每次现算（便宜、必须新鲜），
    解析结果按 mtime+size 缓存（贵、可以复用）。
    以前把路径也存进 __init__，等于「进程启动那一刻的会话」被永久钉住。
    """

    def __init__(self):
        self.path = None          # 每次 current() 现算，别在这里定盘
        self.lock = threading.Lock()
        self.cache = None
        self.space = None
        self.sig = None

    def current(self, view="chat", path=None, with_active=False):
        with self.lock:
            self.path = find_session_file(path)
            if not self.path or not os.path.exists(self.path):
                return {"seq": 0, "tree": {"name": "没有找到会话记录", "children": []},
                        "meta": {"file": None, "path": self.path}}
            st = os.stat(self.path)
            sig = (st.st_mtime, st.st_size)
            if sig != self.sig:
                ev = load_events(self.path)
                if ev is not None:
                    # 会话 id 就是文件名去掉 .jsonl —— 让回合珠带上它，
                    # 步骤珠往上找一层就能定位到自己所在的那个 .jsonl
                    _sid = os.path.splitext(os.path.basename(self.path or ""))[0]
                    self.cache = build_tree(ev, 0, 0, _sid)
                    self.space = build_space_tree(ev)
                    # 根节点也要标 sid：雨图里会话那颗珠就是根，
                    # 它没有 sid 的话「打开」会往上找到工作区去，指错一层
                    if _sid:
                        self.cache["sid"] = _sid
                        self.space["sid"] = _sid
                    self.sig = sig
            calls = 0
            for t in (self.cache or {}).get("children", []):
                calls += len(t.get("children") or [])
            tree = (self.space if view == "space" else self.cache) or {"name": "读取中", "children": []}
            # 同层重名补区分名 + 缺稳定身份的补 uid（幂等，缓存对象上反复跑没事）
            polish_tree(tree)
            # ★ 这里**不**过滤锁：它是 HTTP 和 MCP 共用的底层。
            # 「用户自己看得见、AI 看不见」只能按请求来源分流，
            # 分流点在 Handler（_flt_*）和 mcp_server._lib() 两处 ——
            # 在这一层剪掉的话，用户自己也就看不到那一行了。
            meta = {
                "file": os.path.basename(self.path),
                "path": self.path,
                # 会话名只存在于 chat 树上（space 树根固定叫「工作区」），
                # 所以单独提出来——否则空间视角的报表会不知道自己在读谁的会话。
                "title": (self.cache or {}).get("name") or "",
                "mtime": int(st.st_mtime),
                "size": st.st_size,
                "turns": len((self.cache or {}).get("children", [])),
                "calls": calls,
                "server": int(time.time()),
            }
            if with_active:
                # 只说事实：最近三分钟还有谁在写。1 个 = 没歧义；2 个以上 = 别信默认值。
                meta["active"] = active_sessions()
            return {
                "seq": int(st.st_mtime * 1000),
                "view": view,
                "tree": tree,
                "meta": meta,
            }


STATE = State()


# ---------------------------------------------------------------- 前端联动
# 雨滴图（浏览器）和 MCP（AI）之间只有一条通道：命令。
# 规则：前端在，才发命令；前端不在，AI 就只给文字，不乱动、也不假装做到了。
#
# 「在」的判定只能靠心跳 —— 服务常驻在 127.0.0.1，浏览器却可能早关了。
# 端口通只证明「服务活着」，不证明「有人在看」，这两件事必须分开。
BEAT = {"at": 0.0}                       # 最近一次前端报到（time.time()）
CMD = {"seq": 0, "data": None, "at": 0.0}
CMD_TTL = 30.0                           # 命令最多躺 30 秒，过期作废
ALIVE_WINDOW = 15.0                      # 心跳断这么久 = 前端不在了
_beat_lock = threading.Lock()


# ------------------------------------------------------------ 「谁在看」分流
# 锁要实现的不是「把这一层藏起来」，而是「**你自己照常看得见，AI 读不到**」。
# 这两件事不可能靠一份数据同时满足，只能靠「谁在请求」分流：
#
#   打开过页面 → 服务端发了一枚 rd_ui cookie → 这个请求是「本人在看」→ 原样给
#   没带这枚 cookie → 外部读取（AI / 脚本 / MCP 的 HTTP 旁路）→ 剪掉被锁的
#
# 为什么用 cookie 而不是加参数：浏览器 fetch 同源会自动带上，前端一行都不用改，
# 也就不会出现「某个接口忘了带参数 → 锁在那里失效」这种漏洞。
# 这不是密码学边界（能读页面响应头的人可以抄走它），它挡的是「自动扫描」这一类 ——
# 而这个软件要防的正是这一类。
UI_TOKENS = {}


def ui_issue_token():
    """发一枚「本人在看」的凭据。只保留最近若干枚，旧的自动作废。"""
    t = secrets.token_hex(16)
    UI_TOKENS[t] = time.time()
    if len(UI_TOKENS) > 128:
        for k in sorted(UI_TOKENS, key=UI_TOKENS.get)[:64]:
            UI_TOKENS.pop(k, None)
    return t


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def _is_ui(self):
        ck = self.headers.get("Cookie") or ""
        if not ck:
            return False
        for part in ck.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "rd_ui" and v in UI_TOKENS:
                return True
        return False

    def _flt_tree(self, tree):
        """外部读取：剪掉被锁的（连子树）。本人：原样。"""
        if self._is_ui():
            return tree
        return LOCK.keep(tree) or {"name": "（已全部锁定）", "children": []}

    def _flt_kids(self, kids):
        if self._is_ui():
            return kids
        return LOCK.drop(kids)

    def _flt_blocked(self, key):
        return False if self._is_ui() else LOCK.blocked(key)

    def _flt_columns(self, cols):
        """树图双栏：左右两棵树各剪一次锁。

        ★ 这个接口原来漏了 —— 锁了工作区，树图那边照样把它的名字、
          会话的 sid、步数、磁盘目录名全吐出来。它和 /api/tree 走的是
          不同的构造函数（build_columns 自己拼两棵树），所以
          _flt_tree 管不到它，必须单独挡一次。

        右边的合计（「N 个会话」）不重算，和 /api/library 保持一致：
        被锁的东西连名字都不出现，但父节点上的合计仍是全量 ——
        重算等于告诉对方「这里少了几项」。
        """
        if self._is_ui() or not cols:
            return cols
        out = {}
        for side in ("disk", "lib"):
            t = cols.get(side)
            out[side] = (LOCK.keep(t)
                         or {"kind": "root", "name": "（已全部锁定）", "children": []}) if t else t
        return out

    def log_message(self, *a):
        pass  # 安静一点，别刷屏

    def end_headers(self):
        # 静态文件默认不带任何缓存头，浏览器会按 Last-Modified 做启发式缓存，
        # 于是「改完代码 F5 却看不到变化」—— 统一要求每次回源验证。
        # 只对页面资源加，JSON 接口自己已经发 no-store，别重复。
        p = self.path.split("?")[0]
        if p.endswith((".html", ".htm", ".css", ".js")):
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            # 打开页面就发一枚「本人在看」的凭据（已经有了就不换，
            # 免得每刷新一次就往表里塞一条）
            if p.endswith((".html", ".htm")) and not self._is_ui():
                self.send_header("Set-Cookie",
                                 "rd_ui=%s; Path=/; SameSite=Strict" % ui_issue_token())
        super().end_headers()

    def _json(self, obj, code=200):
        """统一 JSON 响应。新端点用这个，别再手抄一遍 header。"""
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        base = self.path.split("?")[0]

        # AI 让雨滴图跳到某一层。只在雨滴图开着的时候才生效。
        # 没开就明确拒绝，不做「发了但没人收到」这种事 ——
        # 那会让 AI 报成功、界面上什么都没有，是查不出来的假象。
        if base == "/api/show":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            except Exception:
                body = {}
            chain = body.get("chain") or []
            if not isinstance(chain, list) or not chain:
                self._json({"ok": False, "reason": "chain 不能为空"}, 400)
                return
            chain = [str(x)[:80] for x in chain[:6]]      # 6 层足够，别让人传一整个目录树
            now = time.time()
            with _beat_lock:
                quiet = now - (BEAT["at"] or 0.0)
                if quiet > ALIVE_WINDOW:
                    self._json({"ok": False, "reason": "雨图没打开", "quiet": round(quiet, 1)})
                    return
                CMD["seq"] += 1
                CMD["data"] = {"chain": chain, "sid": body.get("sid") or "",
                               "id": CMD["seq"], "by": "ai"}
                CMD["at"] = now
                seq = CMD["seq"]
            self._json({"ok": True, "id": seq, "chain": chain})
            return

        # 在资源管理器里打开这一行对应的磁盘位置。
        # 前端只传身份（sid/rel/wid），路径由 resolve_reveal 查 —— 见那里的说明。
        if base == "/api/reveal":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            except Exception:
                body = {}
            p, isdir = resolve_reveal(body)
            if not p:
                self._json({"ok": False, "reason": "这一行在磁盘上找不到对应位置"}, 404)
                return
            # dry：只要路径，不弹窗。「复制路径」走这条 ——
            # 复制一个东西不该顺手在屏幕上开一个窗口。
            if body.get("dry"):
                self._json({"ok": True, "path": p, "dir": bool(isdir)})
                return
            try:
                if isdir:
                    subprocess.Popen(["explorer", os.path.normpath(p)])
                else:
                    # /select, 让资源管理器打开所在文件夹并选中这个文件
                    subprocess.Popen(["explorer", "/select," + os.path.normpath(p)])
            except Exception as e:
                self._json({"ok": False, "reason": "打不开：%s" % e}, 500)
                return
            self._json({"ok": True, "path": p, "dir": bool(isdir)})
            return

        # ---------------- 锁定 ----------------
        # 密码控制哪些节点在接口层不出现。文件本身一个字节都不动。
        # 密码不落盘：磁盘上只有 salt + check（见 lockbox.py）。
        if base == "/api/lock":
            self._json(LOCK.state())
            return

        if base == "/api/lock/pwd":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            except Exception:
                body = {}
            ok, err = LOCK.set_pwd(str(body.get("old") or ""), str(body.get("new") or ""))
            self._json({"ok": ok, "error": err, "has_pwd": LOCK.has_pwd()},
                       200 if ok else 400)
            return

        # 忘记密码：清空密码和所有锁定。不需要旧密码（见 lockbox.reset 的说明）
        if base == "/api/lock/reset":
            try:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
            except Exception:
                pass
            LOCK.reset()
            self._json({"ok": True, "has_pwd": False, "count": 0})
            return

        if base == "/api/lock/toggle":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            except Exception:
                body = {}
            # 前端传 target（rel/sid/wid/uid 原始字段），后端用同一套优先级算身份。
            # 不能只收地址：地址是 encodeURIComponent 过的，和 node_uid 永远对不上。
            tgt = body.get("target") or {}
            uid = LOCK.node_uid(tgt) if tgt else str(body.get("uid") or "")
            ok, err, on = LOCK.toggle(str(body.get("pwd") or ""), uid)
            self._json({"ok": ok, "error": err, "locked": on, "uid": uid,
                        "count": len(LOCK.locked)}, 200 if ok else 400)
            return

        # 导入一个 CSV 当新数据源。服务只监听 127.0.0.1，请求必然来自本机。
        if base == "/api/source/remove":
            try:
                q = parse_qs(urlparse(self.path).query)
                key = (q.get("key") or [""])[0]
                ok, label, err = remove_source(key)
                self._json({"ok": ok, "key": key, "label": label,
                            "error": err, "sources": list_sources()},
                           200 if ok else 400)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if base == "/api/source/add":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    self._json({"ok": False, "error": "文件是空的"}, 400)
                    return
                if n > 8 * 1024 * 1024:
                    self._json({"ok": False, "error": "超过 8MB，太大了"}, 400)
                    return
                raw = self.rfile.read(n).decode("utf-8", "replace")
                q = parse_qs(urlparse(self.path).query)
                fname = os.path.basename((q.get("name") or ["import.csv"])[0]) or "import.csv"
                # 只保留安全字符：这个文件名会落盘，不能带路径分隔符
                safe = re.sub(r"[^\w.\-]+", "_", fname) or "import.csv"
                if not safe.lower().endswith(".csv"):
                    safe += ".csv"
                os.makedirs(IMPORT_DIR, exist_ok=True)
                fp = os.path.join(IMPORT_DIR, safe)
                io.open(fp, "w", encoding="utf-8").write(raw)
                src = register_csv_file(fp, label=fname)
                self._json({"ok": True, "key": src.key, "label": src.label,
                            "sources": list_sources()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        # 导入一棵结构树：**只收「路径 + 大小」，不收文件内容**。
        # 文件夹、思维导图、流程图导出来的都是这种形状 —— 层级本来就在
        # 路径里，不需要解析。一个文件夹几千个文件，请求也就几百 KB。
        if base == "/api/source/tree":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    self._json({"ok": False, "error": "没有收到内容"}, 400)
                    return
                if n > 32 * 1024 * 1024:
                    self._json({"ok": False, "error": "路径清单太大（>32MB）"}, 400)
                    return
                payload = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
                files = payload.get("files") or []
                if not files:
                    self._json({"ok": False, "error": "里面没有文件"}, 400)
                    return
                name = str(payload.get("name") or "结构树").strip() or "结构树"
                os.makedirs(IMPORT_DIR, exist_ok=True)
                # 同名重复导入覆盖同一份 —— 不要堆出一串同名库
                slug = "tree-" + hashlib.md5(("tree:" + name).encode("utf-8")).hexdigest()[:8]
                fp = os.path.join(IMPORT_DIR, slug + ".tree.json")
                io.open(fp, "w", encoding="utf-8").write(
                    json.dumps({"name": name, "files": files}, ensure_ascii=False))
                src = register_tree_file(fp, label=name, key=slug)
                self._json({"ok": True, "key": src.key, "label": src.label,
                            "files": len(files), "sources": list_sources()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        self._json({"ok": False, "error": "未知接口"}, 404)

    def do_GET(self):
        base = self.path.split("?")[0]
        # 锁的当前状态（前端渲染锁图标要用：有没有设密码、哪些被锁）
        if base == "/api/lock":
            self._json(LOCK.state())
            return

        # 可用数据源清单。前端靠它渲染切换器 —— 加新数据源不用改前端代码
        if base == "/api/sources":
            self._json({"sources": list_sources()})
            return

        # 正文搜索：标题只回答「这条会话叫什么」，回答不了「里面说了什么」。
        # 语料在 _index.json 每个条目自己的 text 字段（每会话一段采样摘要）。
        # 为什么不把 text 一起放进 /api/library 让前端本地搜：全库正文有
        # 几 MB，随首屏一起下发等于把「打开就卡」这件事写死进架构里。
        # 所以关键词在服务端匹配，只回命中会话 + 命中处上下文。
        if base == "/api/search":
            qs = parse_qs(urlparse(self.path).query)
            kw = (qs.get("q") or [""])[0].strip()
            if not kw:
                self._json({"hits": []})
                return
            try:
                lim = int((qs.get("limit") or ["40"])[0])
            except ValueError:
                lim = 40
            lim = max(1, min(lim, 200))
            low = kw.lower()
            hits = []
            try:
                for it in scan_library():
                    name = it.get("title") or ""
                    body = it.get("text") or ""
                    fnames = " ".join((it.get("files") or {}).keys())
                    pos = body.lower().find(low)
                    where = ""
                    if low in name.lower():
                        where = "标题"
                    elif low in fnames.lower():
                        where = "文件"
                    elif pos >= 0:
                        where = "正文"
                    if not where:
                        continue
                    # 锁：被锁的会话连搜索都不该出现。否则「搜一个词能搜到」
                    # 就等于绕过了锁 —— 正文片段会带出被锁内容。
                    if self._flt_blocked("sid:" + (it.get("id") or "")):
                        continue
                    # 锁的是工作区时也要挡住：只查 sid 会漏掉「整片工作区被锁」
                    if self._flt_blocked(it.get("ws") or ""):
                        continue
                    snip = ""
                    if pos >= 0:
                        a = max(0, pos - 42)
                        b = min(len(body), pos + len(kw) + 42)
                        snip = (body[a:b].replace("\n", " ").replace("\r", " ").strip()
                                if b > a else "")
                    hits.append({
                        "sid": it.get("id") or "",
                        "name": name or "(无标题)",
                        "ws": it.get("ws") or "",
                        "steps": it.get("steps") or 0,
                        "t1": it.get("t1") or 0,
                        "where": where,
                        "snippet": snip,
                    })
                # 标题命中排在正文命中前面：用户打一个词，多半是在找那件事的名字
                rank = {"标题": 0, "文件": 1, "正文": 2}
                hits.sort(key=lambda h: (rank.get(h["where"], 9), -(h["t1"] or 0)))
                hits = hits[:lim]
            except Exception as e:
                self._json({"hits": [], "error": str(e)}, 500)
                return
            self._json({"hits": hits, "q": kw})
            return

        # 双栏对照（树图那一页）：两栏一次给全，避免左右先后到、闪一下。
        # 每次都按数据源清单现算 —— 新增一个库，这里立刻多一行。
        if base == "/api/columns":
            try:
                self._json(self._flt_columns(build_columns()))
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        # 顶级目录：Raindrop Chart 下并列几个库，一个数据源一颗珠子。
        # 清单由 list_sources() 决定 —— 它已经把示例库处理掉了：
        # 只要有一个真库，示例就不出现、也不计数（见 datasource.list_sources）。
        if base == "/api/roots":
            kids = []
            for s in list_sources():
                sk = s.get("key")
                # 没装 WorkBuddy 的机器上这颗永远是空的，而且 get_source
                # 会回退到 csv —— 于是「WorkBuddy 会话」会拿示例的数字
                # 冒充会话数，还挂着「已连接」。宁可不显示。
                if sk == "workbuddy" and not WorkBuddySource.available():
                    continue
                cnt, unit, leaves = 0, "项", 0
                try:
                    src = get_source(sk)
                    sm = src.summary(src.root())
                    cnt = sm.get("count") or 0
                    unit = sm.get("countUnit") or "项"
                    leaves = sm.get("leaves") or 0
                except Exception:
                    pass
                # 权重用对数压缩：库与库之间只是入口，不该因为「会话多 97 倍」
                # 就画成一个占满屏的大球、把别的库挤出视野。半径按 sqrt(w) 走，
                # 这里压平之后最大最小只差一成左右，读起来是「几个并列的库」。
                # 半径由前端按「绝对刻度」算，这里只把叶子总数交出去。
                # 旧的 100*(1+0.5*log10(1+cnt)) 已经删掉：cnt 的口径每个源
                # 都不一样（会话数 / 分组数 / 文件数），跨库不可比，
                # 而且 log10 压完之后真实体量差 1000 倍、直径只差 11%。
                k = {
                    "kind": "src",
                    "srcKey": sk,
                    "name": s.get("label") or sk,
                    "hint": ("%d 个%s · 点击进入" % (cnt, unit)) if cnt else (s.get("hint") or ""),
                    "n": leaves,
                    "w": WT.w_leaves(leaves),
                    "count": cnt,
                    "countUnit": unit,
                    "connected": sk == "workbuddy",
                }
                # 这一颗珠子的磁盘位置就是 projects 目录本身。
                # 不给的话雨图里点「在资源管理器中打开」会报「没有磁盘位置」——
                # 它是整张图最外面那颗，恰恰是最该点得开的一颗。
                if sk == "workbuddy":
                    k["disk"] = "."
                # 最新事件时间。雨图靠它判断「这一层有没有新东西进来」——
                # 源本身不产生事件（事件在会话的最深处），不给时间戳的话，
                # 停在根层时永远检测不到更新，波纹也就永远不冒。
                if sk == "workbuddy":
                    try:
                        k["at"] = max((it.get("t1") or 0) for it in scan_library()) or 0
                    except Exception:
                        k["at"] = 0
                kids.append(k)
            # 锁：整个数据源被锁时，库这一层就不出现（根下面直接少一个球）
            kids = self._flt_kids(kids)
            self._json({
                "seq": int(time.time() // 5),
                "source": "",
                "tree": {"kind": "root", "name": "Raindrop Chart",
                         "right": "%d 个库" % len(kids),
                         "children": kids, "steps": 0},
                "count": len(kids), "countUnit": "个库",
                "steps": 0, "stepUnit": "",
            })
            return

        # 前端心跳 + 取命令，二合一。
        # 为什么合成一个端点：前端本来 1.5 秒就要拉一次数据，心跳和命令
        # 搭同一趟车回来，不多开请求，也不用给前端加定时器。
        if base == "/api/beat":
            now = time.time()
            with _beat_lock:
                BEAT["at"] = now
                cmd = None
                if CMD["data"] is not None:
                    if now - CMD["at"] <= CMD_TTL:
                        cmd = CMD["data"]
                    CMD["data"] = None       # 不管过没过期，只给一次
            self._json({"alive": 1, "cmd": cmd})
            return

        # 探活：只回一行 JSON。第二个 bat 双击前先问一句「你是不是已经在跑」
        if base == "/api/ping":
            body = json.dumps({
                "app": APP_ID,
                "pid": os.getpid(),
                "port": SERVER_PORT[0],
                "dir": HERE,
                "version": VERSION,
                "url": "http://127.0.0.1:%d/" % SERVER_PORT[0],
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 退出：无窗口启动时这是唯一能关掉服务的入口。
        # 先回完响应再杀进程 —— 立刻 os._exit 会让浏览器收到连接重置，
        # 前端以为「退出失败」，其实是回包没发出去。
        if base == "/api/quit":
            self._json({"ok": True, "pid": os.getpid()})
            threading.Timer(0.45, lambda: os._exit(0)).start()
            return

        # 资料库：工作区 -> 会话（会话是懒加载叶子，不带明细）
        if base == "/api/library":
            try:
                q = parse_qs(urlparse(self.path).query)
                skey = (q.get("source") or ["workbuddy"])[0]
                src = get_source(skey)
                tree = src.root()
                polish_tree(tree)
                # 锁：被锁的节点连子树一起不出现。keep 返回新对象，不动数据源缓存。
                tree = self._flt_tree(tree)
                sm = src.summary(tree)
                self._json({
                    "tree": tree,
                    "source": src.key,
                    "count": sm["count"],
                    "countUnit": sm["countUnit"],
                    "steps": sm["steps"],
                    "stepUnit": sm["stepUnit"],
                    "scanned": int(time.time()),
                })
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        # 只到工作区这一层：首屏要快，会话等点开再拉
        if base == "/api/shelves":
            try:
                q = parse_qs(urlparse(self.path).query)
                skey = (q.get("source") or ["workbuddy"])[0]
                # 必须走 get_source 再比 key，不能直接比字符串：
                # 没装 WorkBuddy 的机器上 get_source("workbuddy") 会退成 csv，
                # 这里若还按原字符串分支，就会绕过回退、把空树甩给前端。
                src = get_source(skey)
                if src.key != "workbuddy":
                    tree = src.root()
                    tree = self._flt_tree(tree)
                    sm = src.summary(tree)
                    # seq 用源文件 mtime：内容不变就别让前端每 1.5 秒重画一遍
                    try:
                        sig = int(os.path.getmtime(src.path))
                    except Exception:
                        sig = 0
                    self._json({
                        "seq": sig,
                        "source": src.key,
                        "tree": {"name": tree.get("name"),
                                 "right": "%d 个%s" % (sm["count"], sm["countUnit"]),
                                 "children": tree.get("children", []),
                                 "steps": sm["steps"]},
                        "count": sm["count"], "countUnit": sm["countUnit"],
                        "steps": sm["steps"], "stepUnit": sm["stepUnit"],
                        "shelves": sm["count"],
                    })
                    return
                items = scan_library()
                kids0 = build_shelves(items)
                # 递归整层：只处理第一层的话，深一层（会话）和雨图就对不上
                polish_tree({"children": kids0})
                # 锁：被锁的空间整个消失。统计跟着减 —— 否则「N 个空间 / M 步」
                # 和图上看到的对不上，而且会泄漏「被锁的东西有多大」。
                kids = self._flt_kids(kids0)
                all_steps = sum(i.get("steps") or 0 for i in items)
                s0 = sum(k.get("steps") or 0 for k in kids0)
                s1 = sum(k.get("steps") or 0 for k in kids)
                all_sess = len(items)
                n0 = sum(k.get("sessions") or 0 for k in kids0)
                n1 = sum(k.get("sessions") or 0 for k in kids)
                steps = max(0, all_steps - (s0 - s1))
                sess_n = max(0, all_sess - (n0 - n1))
                body = json.dumps({
                    # 用文件时间戳之和当 seq：文件真变了才让前端重画
                    "seq": int(sum(i.get("mtime") or 0 for i in items) % 2147483647),
                    "source": "workbuddy",
                    "tree": {"name": "WorkBuddy资料库",
                             "right": "%d 个空间" % len(kids),
                             "children": kids, "steps": steps},
                    "count": sess_n, "steps": steps, "shelves": len(kids),
                    "countUnit": "会话", "stepUnit": "步",
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            return

        # 某个工作区下的会话：点工作区时才请求，只扫这一层
        if base == "/api/ws":
            try:
                q = parse_qs(urlparse(self.path).query)
                # 工作区名可能是「任务 · xxx」这类标签，优先按原始键 wid 查
                name = (q.get("wid") or q.get("name") or [""])[0]
                items = scan_library()
                kids = build_ws_children(items, name)
                tag_uid(kids)
                dedupe_disp(kids)
                # 锁：整个工作区被锁 -> 一个会话都不给（连名字、数量都不给）
                if self._flt_blocked("wid:" + name) or self._flt_blocked(name):
                    self._json({"name": name, "children": [], "count": 0, "steps": 0,
                                "locked": True})
                    return
                kids = self._flt_kids(kids)
                body = json.dumps({
                    "name": name,
                    "children": kids,
                    "count": len(kids),
                    "steps": sum(k.get("steps") or 0 for k in kids),
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            return

        # 磁盘真实结构（双树对照视图的左半边）
        if base == "/api/disk":
            try:
                items = scan_library()
                tree = build_disk_tree(items)
                # 和资料库树、雨图用同一套身份规则。少这一步，
                # 左列（磁盘）和右列（资料库）的同一行会拿到两个地址，联动就断了。
                polish_tree(tree)
                tree = self._flt_tree(tree)
                body = json.dumps({
                    "tree": tree,
                    "summary": "%d 个工作区 · %d 个 .jsonl" % (
                        len(tree.get("children") or []), len(items)),
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            return

        # 按需加载单个会话的明细（回合 -> 工具）
        if base == "/api/session":
            try:
                q = parse_qs(urlparse(self.path).query)
                sid = (q.get("id") or [""])[0]
                # 分段取：超大会话一次全给会卡死浏览器（见 load_session 的说明）
                try:
                    off = int((q.get("offset") or ["0"])[0])
                except Exception:
                    off = 0
                try:
                    lim = int((q.get("limit") or ["0"])[0])
                except Exception:
                    lim = 0
                # tail=1：取最后一段。实时刷新只看得到「最新」，取头部等于永远看不到新东西。
                tl = (q.get("tail") or ["0"])[0] in ("1", "true", "yes")
                tree = load_session(sid, off, lim, tl) if sid else None
                # 锁：会话被锁就当作不存在（404），连「有这个东西」都不透露。
                # 工作区被锁时，它下面的会话同样查不到 —— 否则知道 sid 就能绕过。
                if sid:
                    if self._flt_blocked("sid:" + sid) or self._flt_blocked(sid):
                        tree = None
                    elif tree and not self._is_ui() and LOCK.locked:
                        for _it in scan_library():
                            if _it.get("id") == sid:
                                if self._flt_blocked(_it.get("ws") or ""):
                                    tree = None
                                break
                payload = {"tree": tree, "id": sid} if tree else {"error": "找不到会话 %s" % sid}
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200 if tree else 404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            return

        if base in ("/api/tree", "/api/state"):
            try:
                view = "space" if "view=space" in self.path else "chat"
                cur = STATE.current(view)
                cur = self._flt_tree(cur)
                body = json.dumps(cur, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            return
        base = self.path.split("?")[0]
        # 试验稿已并进正式页，旧链接别撞 404 —— 直接带过去
        if base == "/ring-drag.html":
            self.send_response(302)
            self.send_header("Location", "/ring.html?src=live")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if base in ("/", "/index.htm"):
            # 必须发 302，不能只是内部改 self.path。
            # 内部改只能让服务器找对文件，浏览器地址栏还是 /，
            # 页面里 location.search 拿不到 src=live → LIVE=false → 读 data.js 的假数据。
            # 原有查询串要原样带过去，否则 ?source=csv 这种参数会在这一跳丢掉。
            _q = urlparse(self.path).query
            if _q:
                loc = "/ring.html?" + _q
                if "src=live" not in _q:
                    loc += "&src=live"
            else:
                loc = "/ring.html?src=live"
            self.send_response(302)
            self.send_header("Location", loc)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if base.startswith("/ring") and base.endswith(".html") and "src=live" not in self.path:
            # 实验页（ring-drag.html 之类）同理，跳一下把参数写进地址栏
            q = self.path[len(base):]
            self.send_response(302)
            self.send_header("Location", base + (q + "&src=live" if q else "?src=live"))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return super().do_GET()


def probe_ours(port, timeout=0.8):
    """问一下这个端口上是不是已经有一个 Raindrop 在跑。
    是 → 返回它的 /api/ping 内容；不是（别的程序、或者旧版本没有这个端点）→ None。"""
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/ping" % port, timeout=timeout) as r:
            info = json.loads(r.read().decode("utf-8"))
        if info.get("app") == APP_ID:
            return info
    except Exception:
        pass
    return None


PORT_SCAN = 12          # 从 PORT 起往后扫这么多个端口


def find_ours(start=None, span=PORT_SCAN):
    """扫一段端口找自己的实例，返回 [(port, info), ...]，按端口升序。

    为什么必须扫一段、不能只探 PORT：
    第一个实例发现 7788 被占会漂到 7789；第二个实例如果还只探 7788，
    就以为「没人跑」，又起一个，撞上 7789 被占再漂到 7790……
    双击十次能堆出十个进程，而且越往后越难发现（实测在用户机器上堆了 6 个）。
    探活范围必须覆盖「它可能漂到哪儿」。

    timeout 压到 0.25s：未监听的端口在 Windows 上是立即 RST，不会真等满 0.25；
    真等满的只有「端口通了但不回话」这种异常情况，扫 12 个最多也就 3 秒。
    """
    out = []
    base = PORT if start is None else start
    for p in range(base, base + span):
        info = probe_ours(p, timeout=0.25)
        if info:
            out.append((p, info))
    return out


def copy_to_clipboard(text):
    """把地址塞进剪贴板，方便直接粘到浏览器地址栏。失败就算了 —— 这不是必需功能。

    三个平台各有一套命令，都走 stdin 传文本，避免命令行转义问题：
      Windows clip（且必须用 ANSI 码页，即 gbk）
      macOS   pbcopy
      Linux   xclip / xsel（多数发行版默认没装，装了才有效）
    """
    cmds = []
    if sys.platform.startswith("win"):
        cmds = [["clip"]]
    elif sys.platform == "darwin":
        cmds = [["pbcopy"]]
    else:
        cmds = [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for cmd in cmds:
        try:
            data = text.encode("gbk" if sys.platform.startswith("win") else "utf-8", "replace")
            subprocess.run(cmd, input=data, check=False, timeout=3)
            return True
        except Exception:
            continue
    return False


def write_addr_file(url):
    """地址兜底：剪贴板没成功、或者窗口被手滑关掉，还能从文件里找到。
    比只依赖剪贴板可靠 —— 剪贴板会被后续复制覆盖。"""
    try:
        io.open(os.path.join(USER, "当前地址.txt"), "w", encoding="utf-8").write(
            url + "\n\n"
            "把上面这行粘到 WorkBuddy 右侧栏「网页」页签的地址栏，回车。\n"
            "启动脚本的窗口关掉后服务就停了，下次重新双击。\n")
    except Exception:
        pass


def focus_existing_page():
    """把已经打开的雨滴图窗口抬到前面。认不出来就返回 False。

    为什么要有这个：浏览器**没有**「聚焦已有标签页」的接口。本地程序能做的只有
    webbrowser.open，而 Chrome 收到它一律新开一个标签 —— 用户点几次桌面图标，
    就堆几个同地址的标签，自己都分不清哪个是活的。

    Windows 上还有一条路：那个标签如果正是浏览器当前激活的页，窗口标题就等于
    页面标题。枚举顶层窗口按标题认出来，再抬到前台。认不出来就老老实实开新的。

    只用标准库 ctypes，不引第三方包（这个项目的铁律）。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        EnumWindows = user32.EnumWindows
        GetWindowTextW = user32.GetWindowTextW
        GetWindowTextLengthW = user32.GetWindowTextLengthW
        IsWindowVisible = user32.IsWindowVisible

        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if not IsWindowVisible(hwnd):
                return True
            n = GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            GetWindowTextW(hwnd, buf, n + 1)
            if PAGE_TITLE in buf.value:
                found.append(hwnd)
                return False        # 找到一个就够，停
            return True

        EnumWindows(_cb, 0)
        if not found:
            return False

        hwnd = found[0]
        SW_RESTORE = 9
        try:
            if user32.IsIconic(hwnd):     # 最小化着就先还原，否则只会任务栏闪一下
                user32.ShowWindow(hwnd, SW_RESTORE)
            if user32.SetForegroundWindow(hwnd):
                return True
            # 前台锁：Windows 只让「当前前台进程」把窗口抬起来。这里通常满足
            # （是从用户双击拉起的进程里调用的），失败就退一步。
            user32.BringWindowToTop(hwnd)
            return True
        except Exception:
            return False
    except Exception:
        return False


def open_page(url, reuse=True):
    """开页面。已经有开着的就抬到前面，没有再开新标签。

    reuse=False 用于「服务刚起来」那一刻：此时同一个 URL 上可能挂着上一轮留下的
    旧标签，那是断线状态（或者显示着旧数据）。把它抬上来等于给用户看一个假的页面，
    还不如老实地开一个新的。只有确认「服务本来就在跑」时才复用。

    返回 True 表示开了新标签，False 表示复用/聚焦了已有的。
    """
    if reuse and focus_existing_page():
        print("  页面已经开着，已把它抬到前面（没有再开新标签）")
        return False
    webbrowser.open(url)
    return True


def announce(url, buddy):
    """把「开了、在哪、下一步」说清楚。这块之前只打一行 '已启动'，
    结果用户不知道要去哪看 —— 界面问题，不是功能问题。"""
    bar = "=" * 60
    print()
    print(bar)
    print("  Raindrop Chart 已就绪")
    print()
    print("  地址    " + url)
    if buddy:
        print("          ^ 已复制到剪贴板，直接 Ctrl+V 就行")
    print()
    if not os.path.isdir(PROJECTS):
        print("  数据源  CSV（本机没有 WorkBuddy 会话记录，这是正常的）")
        print("  怎么用  点页面右下角 + 导入自己的 CSV，")
        print("          或把 .csv 放进 sources/ 目录后刷新页面。")
        print("  关掉    直接叉掉网页就行。要彻底停掉后台进程：")
        print("          Windows 任务管理器结束 pythonw.exe；macOS/Linux 回到终端按 Ctrl+C。")
        print()
    if buddy and os.path.isdir(PROJECTS):
        # 只有「本机确实装了 WorkBuddy」才教他粘到右侧栏。
        # buddy 只表示「别自动开浏览器」，不代表对方有 WorkBuddy ——
        # 这两件事以前被当成一件，于是没装 WorkBuddy 的人会看到
        # 「回到 WorkBuddy」而不知道该去哪。
        print("  下一步")
        print("    1. 回到 WorkBuddy")
        print("    2. 右侧栏点「网页」页签")
        print("    3. 地址栏 Ctrl+V 粘贴，回车")
        print("    4. 进去后左上角切：对话 / 空间 / 全库")
    elif buddy:
        print("  下一步")
        print("    地址已复制到剪贴板，粘到手边任意浏览器打开即可。")
    elif os.path.isdir(PROJECTS):
        print("  下一步")
        print("    浏览器应该已经自己打开了。没开就把上面地址粘到手边浏览器。")
    else:
        # 没有 WorkBuddy 的机器上再教他「回 WorkBuddy 右侧栏」纯属添乱
        print("  下一步")
        print("    浏览器应该已经自己打开了。没开就把上面地址粘到手边浏览器。")
        print("    页面右下角 + 可以随时换一份 CSV。")
    print()
    print("  这个窗口不要关 —— 关了就是断流。")
    print("  地址同时存到了：%s" % os.path.join(USER, "当前地址.txt"))
    print(bar)
    print()


def main():
    # 首次运行：把「软件的信息」那几个目录补齐（分发态是 <包根>/data）。
    # 放在这里而不是 import 阶段 —— import 不该有副作用。
    paths.ensure()

    # --mcp：同一个 python.exe 换个入口就是 MCP 服务器。
    # 网页和 AI 因此共用一份解析层，也不用为 MCP 单独维护第二个可执行文件。
    if "--mcp" in sys.argv:
        sys.path.insert(0, HERE)
        import mcp_server
        return mcp_server.main()

    # pythonw 启动时 sys.stdout 是 None，print 会直接抛异常。
    # 换成文件句柄：既救活 print，也给「双击没反应」留下一份能读的堆栈。
    if sys.stdout is None or not hasattr(sys.stdout, "write"):
        try:
            _lf = io.open(os.path.join(USER, "运行日志.txt"), "a", encoding="utf-8")
            _lf.write("\n===== %s 启动 =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            sys.stdout = _lf
            sys.stderr = _lf
        except Exception:
            pass

    # 兜底：不管有没有控制台，未捕获的异常都要在 运行日志.txt 里留一份。
    # 无窗口启动时这是「双击没反应」唯一的线索来源。
    def _log_exc(t, v, tb):
        import traceback
        try:
            with io.open(os.path.join(USER, "运行日志.txt"), "a", encoding="utf-8") as fh:
                fh.write("\n===== %s 崩溃 =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                traceback.print_exception(t, v, tb, file=fh)
        except Exception:
            pass
        try:
            traceback.print_exception(t, v, tb)
        except Exception:
            pass
    try:
        sys.excepthook = _log_exc
    except Exception:
        pass

    # 输出走管道时 Python 默认块缓冲，公告框会一直卡在缓冲区里不显示，
    # 看起来就像「启动了什么都没说」。强制行缓冲。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    # 对方机器上没有 WorkBuddy 是常态，不能因此拒绝启动 ——
    # 退到 CSV 数据源照样能用，装了会自己用上会话记录。
    has_buddy = os.path.isdir(PROJECTS)
    if not has_buddy:
        print("未发现 WorkBuddy 会话目录：", PROJECTS)
        print("→ 本次以 CSV 数据源启动。把 .csv 放进 sources/，或点右下角 + 导入。")

    # 先主动探活：端口段里已经有自己在跑，就不要再起第二个实例。
    # Windows 的 SO_REUSEADDR 允许重复 bind 同一端口，靠 bind 抛 OSError 是检测不出来的，
    # 结果会是「几个进程都占着端口，请求全落到最早那个」——必须前置探活。
    # 必须扫一段而不是只看 PORT：实例会因端口占用往后漂，只探 PORT 就永远找不到漂走的那个。
    found = find_ours()
    buddy = os.environ.get("RD_NOOPEN") == "1"
    # 只有「同一个目录里的同一份程序」才算自己已经在跑。
    # 只比 APP_ID 会误判：一台机器上放两个副本时，双击新的那个会被旧实例顶掉，
    # 结果打开的永远是旧页面 —— 这种 bug 用户根本说不清，只会说「没更新」。
    # dir 为空 = 端口上跑的是没这个字段的旧版本，不能当成自己 ——
    # 空串经过 abspath 会变成 cwd，恰好等于 HERE，会误判成「就是我在跑」。
    same_copy = None
    for _p, _info in found:
        _rdir = _info.get("dir") or ""
        if _rdir and os.path.normcase(os.path.abspath(_rdir)) == os.path.normcase(HERE):
            same_copy = _info
            break
    if same_copy:
        url = same_copy.get("url") or ("http://127.0.0.1:%d/" % PORT)
        write_addr_file(url)
        announce(url, buddy)
        print("  （这个是已经在跑的那个实例，不需要再起第二个）")
        print()
        if buddy:
            copy_to_clipboard(url)
        else:
            threading.Timer(0.3, lambda: open_page(url)).start()
        # pythonw / VBS 启动时 stdin 是 None，直接 .isatty() 会 AttributeError 崩掉。
        if sys.stdin is not None and sys.stdin.isatty():
            input("  按回车关闭这个窗口……")
        return 0

    if has_buddy:
        print("会话记录：", find_session_file())

    # 端口被别的程序占就往后找，不要直接崩 —— 重复双击是常事。
    # 每换一个端口都先探活：Windows 的 SO_REUSEADDR 允许两个实例绑同一个端口，
    # 只看 bind 是否抛异常是发现不了的，结果请求全落到最早那个实例上。
    srv = None
    port = PORT
    for _ in range(12):
        if probe_ours(port) is not None:
            print("端口 %d 上已经有一个实例，换 %d" % (port, port + 1))
            port += 1
            continue
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            print("端口 %d 被别的程序占用，换 %d" % (port, port + 1))
            port += 1
    if srv is None:
        print("连续 12 个端口都被占用，放弃。请关掉旧的窗口再试。")
        return 1

    SERVER_PORT[0] = port
    url = "http://127.0.0.1:%d/" % port
    write_addr_file(url)
    announce(url, buddy)
    if buddy:
        copy_to_clipboard(url)
    else:
        threading.Timer(0.7, lambda: open_page(url, reuse=False)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
