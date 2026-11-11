# -*- coding: utf-8 -*-
"""
Raindrop · 工作流 MCP 服务器
------------------------------------------------------------------
把「对话」和「空间」两份视角注册成 WorkBuddy 可以直接调用的工具。

它不是另一个产品，就是本项目的另一张皮：
解析层直接复用 live_server.py，所以网页上看到的数和这里返回的数永远一致。

形态：本地 stdio MCP（零依赖，只用 Python 标准库）
注册方式见同目录 MCP-注册片段.json，重启 WorkBuddy 后生效。

约定：stdout 只走协议，日志一律 stderr。
"""

import sys
import os
import json
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass

import live_server as LS

SERVER_NAME = "raindrop-workflow"
# 版本号不在这里维护：live_server.VERSION 是唯一来源。
# 以前 mcp_server 自己写 "2.0.0"、dist 写 "2.2.0"、页面写 "2.2.1"，三处各说各话，
# initialize 报给客户端的版本是假的。现在直接继承，永远同步。
SERVER_VERSION = getattr(LS, "VERSION", "0.0.0")


def log(*a):
    sys.stderr.write(" ".join(str(x) for x in a) + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- 数据读取

def _snap(view="chat", path=None):
    """当前会话的快照。path 传了就只读那一个，否则每次现算「最近被写的会话」。

    ★ 这里也必须剪掉被锁的节点。原因和 _lib() 一样：
      STATE.current 是「用户自己看」和「AI 读」共用的底层，
      它自己不再做过滤（否则用户连自己锁了哪一行都看不见），
      所以过滤必须落在每个「外部读取」的入口上 —— 就是这里。
    """
    tree = LS.STATE.current(view, path, with_active=True)
    lb = getattr(LS, "LOCK", None)
    if lb is None or not tree:
        return tree
    return lb.keep(tree) or {"name": "（已全部锁定）", "children": []}


def _resolve_session(s):
    """把 session 参数解析成 jsonl 绝对路径。

    接受三种写法：完整路径 / 标题片段 / id 前 8 位。
    ⚠️ 这里也踩过坑：输入「雨滴图V0」时，「测试查找雨滴图V0会话」的标题也包含它，
    按时间倒序会先命中后者 —— 用户想指 V0，拿到的却是刚开的那个新会话。
    规则改成：完全相等优先 → 否则标题短的优先（越短越接近精确指向）→ 再按新旧。
    解析不出来就原样返回，让 State 去失败；绝不偷偷回落到「最新那个」。
    """
    s = (s or "").strip()
    if not s:
        return None
    if os.path.exists(s):
        return os.path.abspath(s)
    low = s.lower()
    hits = []
    for it in _lib():
        title = (it.get("title") or "")
        if low == title.lower():
            return it.get("path") or s
        if low in title.lower() or (it.get("id") or "").lower().startswith(low):
            hits.append(it)
    if not hits:
        return s
    hits.sort(key=lambda it: (len(it.get("title") or ""), -(it.get("t1") or 0)))
    return hits[0].get("path") or s


def _sess(args, view="chat"):
    s = args.get("session")
    d = _snap(view, _resolve_session(s))
    d["_pinned"] = bool((s or "").strip())
    return d


def _hms(sec):
    """meta 里的时间戳是秒，不是毫秒 —— 别和 _hhmm 混用。"""
    import time
    if not sec:
        return "--:--:--"
    try:
        return time.strftime("%H:%M:%S", time.localtime(sec))
    except Exception:
        return "--:--:--"


def _head(d):
    """报表头：我到底读的是哪个会话。

    这一行是这次修复的核心 —— 工具可以选错，但绝不能「选错了还装没事」。
    有歧义时把候选全列出来，调用方一眼能看出该不该信下面的数。
    """
    meta = d.get("meta") or {}
    tree = d.get("tree") or {}
    p = meta.get("path") or ""
    if not p:
        return "会话：没有找到会话记录\n"
    if not meta.get("mtime"):
        # 指定了但文件不存在：明说，别回落到「最新那个」装作成功
        return "会话：没找到指定的会话（%s），已放弃 —— 不会自动改读别的会话\n" % p
    out = "会话：《%s》 · %s · 改于 %s%s" % (
        meta.get("title") or tree.get("name") or "未命名",
        os.path.basename(p)[:8], _hms(meta.get("mtime")),
        "  [已指定]" if d.get("_pinned") else "")
    act = meta.get("active") or []
    if len(act) > 1 and not d.get("_pinned"):
        out += "\n⚠️ 最近 3 分钟有 %d 个会话在写，默认取了最新的（上面这个）。" % len(act)
        out += "要换会话请加 session=标题 / id 前 8 位："
        for a in act:
            out += "\n   %s  %-8s %d 秒前" % (
                _hms(a.get("mtime")), os.path.basename(a.get("path") or "")[:8], a.get("age") or 0)
    return out + "\n"


def _hhmm(ms):
    import time
    try:
        return time.strftime("%H:%M", time.localtime((ms or 0) / 1000.0)) if ms else "--:--"
    except Exception:
        return "--:--"


def _fmt_ms(ms):
    return "%.1fs" % (ms / 1000.0) if ms >= 1000 else "%dms" % ms


def _where(it):
    """会话的落点：jsonl 路径 + 动作所在行号区间。

    检索类工具一律带上它，调用方拿到命中就能直接 Read 那一段，
    不用再去别处查 id、自己拼 projects/<ws>/<id>.jsonl。
    缩进对齐到"在命中行下面缩进 5 格"。
    """
    p = it.get("path") or ""
    if not p:
        return ""
    s = "\n     " + p
    if it.get("first_step_line"):
        s += "\n     动作分布 行 %d–%d / 共 %d 行" % (
            it["first_step_line"], it.get("last_step_line") or 0, it.get("lines") or 0)
    return s


def _walk_steps(node, out):
    for k in node.get("children") or []:
        if k.get("kind") == "step":
            out.append(k)
        else:
            _walk_steps(k, out)
    return out


# ---------------------------------------------------------------- 工具

def tool_status(args):
    d = _sess(args, "chat")
    t = d["tree"]
    turns = t.get("children") or []
    steps = _walk_steps(t, [])
    secs = sum((s.get("value") or 0) for s in steps)
    out = [
        _head(d).rstrip("\n"),
        "回合 %d · 步骤 %d · 工具总耗时 %.1f 秒" % (len(turns), len(steps), secs / 1000.0),
    ]
    if turns:
        cur = turns[-1]
        out.append("")
        out.append("最近一轮：「%s」%d 步" % (cur.get("name", ""), len(cur.get("children") or [])))
        for s in (cur.get("children") or [])[-6:]:
            out.append("  · %-4s %-8s %s" % (
                s.get("name", "?"), _fmt_ms(s.get("value") or 0), s.get("hint", "")))
    return "\n".join(out)


def tool_turns(args):
    d = _sess(args, "chat")
    turns = d["tree"].get("children") or []
    limit = int(args.get("limit") or 20)
    rows = []
    for i, tn in enumerate(turns):
        kids = tn.get("children") or []
        secs = sum((k.get("value") or 0) for k in kids)
        files = sorted({os.path.basename(k["file"]) for k in kids if k.get("file")})
        tail = ("  改过: " + ", ".join(files[:3])) if files else ""
        rows.append("%2d. %-20s %2d 步 %7s%s" % (
            i, LS.short(tn.get("name", ""), 18), len(kids), _fmt_ms(secs), tail))
    return _head(d) + "共 %d 个回合：\n%s" % (len(turns), "\n".join(rows[:limit]))


def tool_slowest(args):
    d = _sess(args, "chat")
    steps = _walk_steps(d["tree"], [])
    steps.sort(key=lambda s: -(s.get("value") or 0))
    n = int(args.get("limit") or 8)
    lines = ["最慢的 %d 步：" % min(n, len(steps))]
    for s in steps[:n]:
        lines.append("  %-8s %-5s %s" % (_fmt_ms(s.get("value") or 0), s.get("name", ""),
                                          LS.short(s.get("hint", ""), 46)))
    return _head(d) + "\n".join(lines)


def tool_space_files(args):
    d = _sess(args, "space")
    t = d["tree"]
    files = t.get("children") or []
    limit = int(args.get("limit") or 20)
    lines = ["%s（%d 个文件）：" % (t.get("name", "工作区"), len(files))]
    for f in files[:limit]:
        turns = f.get("children") or []
        names = [LS.short(x.get("name", ""), 12) for x in turns[:3]]
        lines.append("  %2d 次 · %d 轮  %s" % (
            f.get("count") or 0, len(turns), f.get("name", "")))
        # 路径必须打出来：同一个 ring.html 在根目录和 dist/ 各有一份，
        # 只报名字的话「37 次」和「24 次」两行无法区分是谁。
        if f.get("path"):
            lines.append("     " + f["path"])
        if names:
            lines.append("     ← " + " / ".join(names))
    return _head(d) + "\n".join(lines)


def tool_file_history(args):
    """某个文件被哪几轮、用什么动作改过 —— 空间视角下的完整谱系。

    同名文件（根目录 ring.html / dist/RaindropChart-v2.5.0/ring.html）全部列出，
    以前命中第一个就 return，等于把另一个文件的谱系吞掉了。
    """
    want = (args.get("name") or "").strip().lower()
    if not want:
        return "需要 name 参数，例如 index.html"
    d = _sess(args, "space")
    hits = []
    for f in d["tree"].get("children") or []:
        if want in (f.get("name") or "").lower() or want in (f.get("path") or "").lower():
            hits.append(f)
    if not hits:
        return "没找到被读过或写过的文件：%s" % want
    blocks = ["%s 在 %d 个路径上出现过：" % (want, len(hits))] if len(hits) > 1 else []
    for f in hits:
        lines = ["%s  (%d 次改动)" % (f.get("path") or f.get("name"), f.get("count") or 0)]
        for tn in f.get("children") or []:
            lines.append("  %s 「%s」" % (_hhmm(tn.get("at")), tn.get("name", "")))
            for s in tn.get("children") or []:
                lines.append("      %-5s %-8s %s" % (s.get("name", ""), _fmt_ms(s.get("value") or 0),
                                                     LS.short(s.get("hint", ""), 44)))
        blocks.append("\n".join(lines))
    return _head(d) + "\n\n".join(blocks)


def _lib():
    """资料库条目（已剔除被锁的部分）。

    ★ 锁必须在这里挡一次。MCP 是 `import live_server` 直接读内存的，
      不经过 HTTP，所以 HTTP 端点的过滤管不到这里 ——
      「锁了但 AI 照样能读」就等于没锁，而且是用户最想不到的那条路。
    """
    items = LS.scan_library()
    lb = getattr(LS, "LOCK", None)
    if not lb or not getattr(lb, "locked", None):
        return items
    out = []
    for it in items:
        if lb.blocked("sid:" + (it.get("id") or "")):
            continue
        if lb.blocked(it.get("ws") or ""):
            continue
        out.append(it)
    return out


def tool_library(args):
    items = _lib()
    ws = {}
    for it in items:
        g = ws.setdefault(it["ws"], [0, 0])
        g[0] += 1
        g[1] += it["steps"]
    rows = ["资料库：%d 个会话 · %d 步 · %d 个工作区" % (
        len(items), sum(i["steps"] for i in items), len(ws))]
    for name, (n, st) in sorted(ws.items(), key=lambda kv: -kv[1][1])[:8]:
        rows.append("  %-26s %2d 会话 %6d 步" % (name[:26], n, st))
    return "\n".join(rows)


def tool_search(args):
    q = (args.get("q") or "").strip().lower()
    if not q:
        return "需要 q 参数，例如 '时间线' 或 'index.html'"
    hits = []
    for it in _lib():
        if q in (it["title"] or "").lower():
            hits.append((it, "标题"))
        elif any(q in f.lower() for f in it["files"]):
            hits.append((it, "文件"))
        elif q in (it.get("text") or "").lower():
            # 正文命中：给出片段，让人知道是哪儿命中了
            pos = (it.get("text") or "").lower().find(q)
            frag = (it.get("text") or "")[max(0, pos - 20):pos + 60].replace("\n", " ")
            hits.append((it, "正文「%s…」" % frag))
    if not hits:
        return "没有匹配「%s」的会话。\n（搜的是标题、文件名和用户提问的正文片段；助手回复不进索引）" % q
    out = ["匹配「%s」：%d 条" % (q, len(hits))]
    for it, why in hits[:15]:
        out.append("  %s  %-30s %5d 步  (命中%s)%s" % (
            LS_ts(it["t1"]), (it["title"] or it["id"][:8])[:30], it["steps"], why, _where(it)))
    return "\n".join(out)


def tool_file_sessions(args):
    name = (args.get("name") or "").strip().lower()
    if not name:
        return "需要 name 参数，例如 index.html"
    rows = []
    for it in _lib():
        for f, c in it["files"].items():
            if name in f.lower():
                rows.append((c, it))
                break
    if not rows:
        return "没有任何会话动过「%s」。" % name
    rows.sort(key=lambda x: -x[0])
    out = ["动过「%s」的会话：%d 个" % (name, len(rows))]
    for c, it in rows[:15]:
        out.append("  %s  %-28s %3d 次%s" % (
            LS_ts(it["t1"]), (it["title"] or it["id"][:8])[:28], c, _where(it)))
    return "\n".join(out)


def LS_ts(ms):
    import time
    try:
        return time.strftime("%m-%d %H:%M", time.localtime((ms or 0) / 1000.0)) if ms else "  --  "
    except Exception:
        return "  --  "


def _prune(node, depth, limit, level=0):
    """按 depth / limit 裁剪点-环树，但保留原始规模信息。

    limit  只作用于顶层（最近 N 个），因为最近的在数组尾部。
    depth  1=只留根  2=到回合  3或0=全展开。
    被裁掉的节点不影响父节点的其它字段；只要 children 比原本少，
    就补一个 _n 告诉你「本来有几个」，避免消费方把裁剪误读成没数据。
    """
    out = dict(node)
    src = node.get("children") or []
    total = len(src)
    items = src[-limit:] if (level == 0 and limit and total > limit) else src
    if (not depth) or level + 1 < depth:
        kids = [_prune(k, depth, 0, level + 1) for k in items]
    else:
        kids = []
    out["children"] = kids
    if len(kids) != total:
        out["_n"] = total
    return out


def tool_tree(args):
    """完整点-环树（JSON），供外部可视化 / 批量分析消费。"""
    view = args.get("view") or "chat"
    if view not in ("chat", "space"):
        view = "chat"
    d = _sess(args, view)
    tree = d["tree"]
    # 只裁 children，不裁根：根上挂着会话名 + meta，调用方靠它判断读的是谁。
    tree = dict(tree)
    tree.setdefault("meta", d.get("meta") or {})
    limit = int(args.get("limit") or 0)
    depth = int(args.get("depth") or 0)
    if limit or depth:
        tree = _prune(tree, depth, limit)
    return json.dumps(tree, ensure_ascii=False)


def tool_report(args):
    """文字报表分发器：一个入口装下全部人读视图。"""
    kind = (args.get("kind") or "").strip()
    hit = REPORTS.get(kind)
    if not hit:
        return "未知 kind：%s\n可用：%s" % (
            kind or "(空)", "、".join("%s(%s)" % (k, v[1]) for k, v in REPORTS.items()))
    return hit[0](args)


def tool_show(args):
    """让雨滴图（网页）跳到某一层。

    只在雨滴图开着的时候才生效 —— 判断依据是前端心跳，不是端口通不通。
    没开就明说，不写命令、不假装做到：否则 AI 报「已定位」而界面没动静，
    这种假象比直接失败更难查。
    """
    chain = args.get("chain")
    if isinstance(chain, str):
        chain = [chain]                    # 字符串 = 一层，不猜着拆（库名本身可能带 /）
    if not chain or not isinstance(chain, list):
        return "需要 chain，例如 [\"项目A\"] 或 [\"项目A\",\"某次会话\"]"

    # 找「本目录这一份」正在跑的实例 —— 别的副本开的雨图不该被我们指挥
    url = None
    for _p, info in LS.find_ours():
        d = info.get("dir") or ""
        if d and os.path.normcase(os.path.abspath(d)) == os.path.normcase(HERE):
            url = (info.get("url") or ("http://127.0.0.1:%d/" % _p)).rstrip("/")
            break
    if not url:
        return "雨图没打开（服务没起）。这次只给结论，不去动界面。"

    try:
        req = urllib.request.Request(
            url + "/api/show",
            data=json.dumps({"chain": chain, "sid": args.get("sid") or ""}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return "连不上雨图：%s" % e

    if d.get("ok"):
        return "雨图已跳到：%s" % " › ".join(chain)
    return "没跳：%s" % (d.get("reason") or "未知原因")


# ---------------------------------------------------------------- 工具清单
# v2 只暴露两个入口，理由：原来 9 个工具里只有 workflow_tree 出 JSON（真接口），
# 其余 8 个都是把同一棵树摊平成文字给人看——它们是「探针」不是「管道」。
# 探针塞进一个分发器，管道单独留出来，工具数 9 → 2，能力一个不少。

REPORTS = {
    "status":        (tool_status,        "概况·无参数"),
    "turns":         (tool_turns,         "回合·可选 limit"),
    "slowest":       (tool_slowest,       "最慢步·可选 limit"),
    "files":         (tool_space_files,   "文件·可选 limit"),
    "file_history":  (tool_file_history,  "文件谱系·需 name"),
    "library":       (tool_library,       "全库·无参数"),
    "search":        (tool_search,        "检索·需 q"),
    "file_sessions": (tool_file_sessions, "文件反查·需 name"),
}

_KIND_DOC = "｜".join("%s=%s" % (k, v[1]) for k, v in REPORTS.items())

TOOLS = [
    ("workflow_tree",
     "读取工作流的完整点-环结构（JSON）。这是本项目唯一的数据接口，做可视化、批量分析、"
     "导出都走它。view=chat 是对话视角（会话→回合→步骤），view=space 是空间视角"
     "（工作区→文件→回合→步骤）。根节点带 meta.path / meta.active，可确认读的是哪个会话。"
     "⚠️ 不带参数是全量：chat 视角当前约 111KB / 3 万 token，很贵。"
     "只是想看结构就先带 depth=2；只要最近几轮就带 limit=5。"
     "被裁掉的分支会带上 _n 字段告诉你原本有多少个子节点。",
     {"type": "object", "properties": {
         "view":  {"type": "string", "enum": ["chat", "space"],
                   "description": "chat=对话视角（默认），space=空间视角"},
         "limit": {"type": "integer",
                   "description": "只取最近 N 个顶层节点（最近的在尾部）。0=不限，默认 0"},
         "depth": {"type": "integer",
                   "description": "展开层数：1=只留根，2=到回合/文件，3=到步骤。0=不限，默认 0"},
         "session": {"type": "string",
                     "description": "指定会话：标题片段 / id 前 8 位 / jsonl 路径。"
                                    "不传则取最近被写的那个（多窗口时可能不是你要的）"}},
      "additionalProperties": False}, tool_tree),

    ("workflow_report",
     "读取工作流的文字报表（给人看的摘要）。按 kind 选功能——" + _KIND_DOC + "。"
     "不知道有什么就传 kind=status。需要具体数据而不是摘要时，改用 workflow_tree。"
     "所有报表首行都会标出「读的是哪个会话」，多窗口同时开工时会列出候选。",
     {"type": "object", "properties": {
         "kind":  {"type": "string", "enum": list(REPORTS.keys()),
                   "description": "报表类型，默认 status"},
         "name":  {"type": "string", "description": "文件名或路径片段，kind=file_history / file_sessions 时必填"},
         "q":     {"type": "string", "description": "搜索关键词，kind=search 时必填"},
         "limit": {"type": "integer", "description": "返回条数，各 kind 有自己的默认值"},
         "session": {"type": "string",
                     "description": "指定会话：标题片段 / id 前 8 位 / jsonl 路径。"
                                    "不传则取最近被写的那个（search / library / file_sessions 不受影响）"}},
      "required": ["kind"], "additionalProperties": False}, tool_report),

    ("workflow_show",
     "让雨滴图（网页）跳到指定层级。只在雨滴图开着时才生效——"
     "没开它会返回「雨图没打开」，什么都不做。所以调用前不用先查状态，直接调即可。"
     "chain 是从顶到下的名字数组（跟 search / library 返回的名字一致），"
     "例如 [\"项目A\"] 或 [\"项目A\",\"某次会话\"]。"
     "跳完只放一圈波纹，不做高亮。",
     {"type": "object", "properties": {
         "chain": {"type": "array", "items": {"type": "string"},
                   "description": "层级名，从顶层往下。传单个字符串也当一层"},
         "sid":   {"type": "string",
                   "description": "可选：会话 id，给了就定位到那一条具体的"}},
      "required": ["chain"], "additionalProperties": False}, tool_show),
]

BY_NAME = {t[0]: t[3] for t in TOOLS}


# ---------------------------------------------------------------- 协议

def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(mid, result):
    send({"jsonrpc": "2.0", "id": mid, "result": result})


def fail(mid, code, msg):
    send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}})


def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        reply(mid, {
            "protocolVersion": params.get("protocolVersion") or "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
        return

    if method in ("notifications/initialized", "initialized"):
        return

    if method == "tools/list":
        reply(mid, {"tools": [
            {"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS
        ]})
        return

    if method == "tools/call":
        name = params.get("name")
        fn = BY_NAME.get(name)
        if not fn:
            fail(mid, -32602, "未知工具：%s" % name)
            return
        try:
            text = fn(params.get("arguments") or {})
            reply(mid, {"content": [{"type": "text", "text": text}]})
        except Exception as e:
            log("tools/call %s 失败: %r" % (name, e))
            reply(mid, {"content": [{"type": "text", "text": "执行失败：%s" % e}],
                        "isError": True})
        return

    if mid is not None:
        fail(mid, -32601, "未实现的方法：%s" % method)


def main():
    log("[%s] 已启动，会话：%s" % (SERVER_NAME, LS.find_session_file()))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            log("坏消息，跳过：", e)
            continue
        try:
            handle(msg)
        except Exception as e:
            log("处理异常：", repr(e))
            if msg.get("id") is not None:
                fail(msg["id"], -32603, str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
