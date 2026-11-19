# -*- coding: utf-8 -*-
"""
数据源适配层
==================================================================
把「原始数据长什么样」和「图怎么画」切开。

拆之前的问题
------------
live_server.py 里「读 WorkBuddy 会话 jsonl」和「建层级树」是缠在一起的，
要接第二个数据源（CSV / 手工录入 / 别的工具导出）就得动主逻辑，
每加一个源都要改一遍。

统一契约（每个数据源只做两件事）
--------------------------------
    root()           -> 树根 dict
    expand(node_id)  -> 某个懒加载节点的子节点 dict；不支持就返回 None

节点格式（和前端 ring.html 完全一致，前端不需要知道数据从哪来）
--------------------------------------------------------------
    {
      "name":     显示名,
      "kind":     'root' | 'workspace' | 'session' | 'turn' | 'step' | 'file' | 'group',
      "w":        权重（决定珠子半径）,
      "children": [...],     # 有子节点 = 可下钻
      "lazy":     True,      # 子节点要等点开才加载（可选）
      "id":       '...',     # lazy 节点必须有，expand() 用它
      ...                    # 其它字段原样透传给前端，前端不认识的忽略
    }

加新数据源的步骤
----------------
1. 写一个 Source 子类，实现 root()（必须）和 expand()（有懒加载才需要）
2. 在 REGISTRY 里注册
3. 前端不用改
"""

import io
import os
import re
import csv
import time
import shutil
import json
import hashlib
import datetime

import paths
import weights as WT

# 软件本体目录。示例 CSV 这类「随程序走」的东西挂在这里。
HERE = paths.CODE


class Source(object):
    """数据源基类。子类只需要填 key / label / root()。"""

    key = ""
    label = ""
    hint = ""
    # 这个源在磁盘上的入口（给人看的地址），树图左栏拿它当第一行。
    # 留空 = 不是「目录里的一堆记录」这种形态（比如单文件 CSV）。
    disk_label = ""
    # 内置示例（Hello World）。它只在「一个真库都没有」时露面 ——
    # 没有它，别人第一次打开会是白屏；有了它又跟真库一起计数，
    # 会让人以为软件自带一个用得上的库。见 list_sources()。
    is_example = False

    def root(self):
        raise NotImplementedError

    def expand(self, node_id):
        """懒加载：返回该节点的子节点。不支持懒加载的数据源直接返回 None。"""
        return None

    def summary(self, tree):
        """状态栏要显示的量。单位和口径由数据源自己定 ——
        会话记录数的是「会话/步」，CSV 数的是「分组/项」，不能写死。"""
        return {"count": 0, "countUnit": "", "steps": 0, "stepUnit": "",
                "leaves": 0}

    def describe(self):
        return {"key": self.key, "label": self.label, "hint": self.hint,
                "example": bool(self.is_example)}


# ==================================================================
#  数据源一：WorkBuddy 会话记录
#  这是目前唯一接真实数据的源。逻辑仍在 live_server.py 里，
#  这里只做适配，不复制一份 —— 否则两边口径会分叉。
# ==================================================================

class WorkBuddySource(Source):
    key = "workbuddy"
    label = "WorkBuddy 会话"
    hint = "读本机 ~/.workbuddy/projects 下的会话记录"

    @staticmethod
    def available():
        """本机跑没跑过 WorkBuddy。对方机器上通常为 False ——
        这不是错误，只是要换个数据源，别让页面停在空树上。"""
        return os.path.isdir(os.path.expanduser("~/.workbuddy/projects"))

    def __init__(self):
        # 延迟导入：live_server 在启动时要 import 本模块，顶层互相 import 会成环
        self._ls = None

    def _mod(self):
        if self._ls is None:
            import live_server
            self._ls = live_server
        return self._ls

    def root(self):
        ls = self._mod()
        tree = ls.build_library_tree(ls.scan_library())
        # 统一把懒加载节点的标识搬到 id 上，同时保留 sid 兼容现有前端
        for ws in tree.get("children", []):
            for s in ws.get("children", []):
                s["id"] = s.get("sid")
                s["source"] = self.key
        return tree

    def expand(self, node_id):
        tree = self._mod().load_session(node_id)
        if not tree:
            return None
        return tree

    def summary(self, tree):
        sessions = 0
        for ws in tree.get("children", []):
            sessions += len(ws.get("children", []))
        return {"count": sessions, "countUnit": "会话",
                "steps": tree.get("steps", 0), "stepUnit": "步",
                # leaves = 整库的叶子总数。跨库比大小时只能用它 ——
                # 会话数和「16 型」「4 个分组」不是一个量纲，放进同一个
                # log10 里算出来的直径差 11%，等于尺寸不表达任何东西。
                "leaves": tree.get("steps", 0) or 0}


# ==================================================================
#  数据源二：CSV（通用层级）
#  存在的意义不是「CSV 这个格式多重要」，而是证明上面那套契约
#  真的能让一个完全不同的数据源接进来，而渲染器一行不改。
#
#  格式（首行表头，三列）：
#      parent,name,value
#      ,订单履约,            <- parent 留空 = 一级分组
#      订单履约,下单,41
#      订单履约,支付,38
#
#  parent 支持多级：写完整路径，用 / 分隔
#      订单履约/仓储,拣货,7
# ==================================================================

class CsvSource(Source):
    key = "csv"
    label = "CSV（通用层级）"
    hint = "parent,name,value 三列；parent 写 / 分隔的完整路径，留空表示顶级"

    def __init__(self, path=None, key=None, label=None):
        # 没有 key = 内置那一个示例实例（REGISTRY 启动时造的那个）。
        # 用户导入的 CSV 永远带 key，所以不会被当成示例藏起来。
        self.is_example = not key
        if path is None:
            # 往后看一层 examples/：GitHub 仓库把示例放在 examples/ 里，
            # 根目录不再放一份，否则同一份数据两处维护、早晚不一致。
            for _fn in ("hello.csv", "data-example.csv",
                        os.path.join("examples", "hello.csv"),
                        os.path.join("examples", "orders.csv")):
                _p = os.path.join(HERE, _fn)
                if os.path.exists(_p):
                    path = _p
                    break
        self.path = path or os.path.join(HERE, "data-example.csv")
        if label is None and os.path.basename(self.path).lower().startswith("hello"):
            # 名字要短：珠子里放不下就自动省略号，读起来像坏了
            self.label = "Hello World"
            self.hint = "三层示例：词组 / 单词 / 单字母"
        # 允许实例级覆盖：用户导入的文件要各自是一个独立数据源
        if key:
            self.key = key
        if label:
            self.label = label
        self.hint = label or self.hint

    def root(self):
        tree = self._load()
        kids = tree.get("children") or []
        # 根名用库名，不要拼统计 —— 统计状态条已经报了，
        # 这里再写一遍「CSV · 2 个分组 / 9 项」会在面包屑里读成一串数字。
        tree["name"] = self.label or "CSV"
        tree["kind"] = "root"
        tree["source"] = self.key
        tree["children"] = kids
        return tree

    def _load(self):
        """把扁平的 parent,name,value 折成**任意深度**的树。

        parent 写完整路径（/ 分隔，不含虚拟根）：
            ""               -> 顶级
            "Hello"          -> Hello 的直接子
            "Hello/元音"     -> 再深一层
        中间层没写行也能用：按路径逐段补建。

        旧实现只取 parent.split("/")[0]，写「订单/仓储」时「仓储」整层被吞掉 ——
        文档承诺了多级、代码只做两级，这是这次一并修的。
        """
        root_node = {"kind": "root", "name": "", "children": [], "value": 0.0}
        if not os.path.exists(self.path):
            return root_node

        rows = []
        with io.open(self.path, encoding="utf-8-sig", newline="") as fh:
            rd = csv.DictReader(fh)
            # 防 BOM：utf-8-sig 只吃一个 BOM，文件要是带了两个（编辑器/脚本叠加
            # 写过就会），列名会变成 '\ufeffparent'，于是 parent 全读不到 ——
            # 整棵树会平铺在根下，看着像「层级没生效」。
            if rd.fieldnames:
                clean = []
                for f in rd.fieldnames:
                    f = (f or "").replace("\ufeff", "").strip()
                    clean.append(f)
                rd.fieldnames = clean
            for row in rd:
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                parent = (row.get("parent") or "").strip().strip("/")
                try:
                    val = float(row.get("value") or 0)
                except ValueError:
                    val = 0.0
                rows.append((parent, name, max(0.0, val)))

        # 第一遍：建节点、挂父子。同一个「父/名」出现两次 = 后者覆盖权重，不重复挂。
        index = {"": root_node}
        for parent, name, val in rows:
            full = (parent + "/" + name).strip("/") if parent else name
            parent_node = index.get(parent)
            if parent_node is None:
                parent_node = _ensure_path(index, root_node, parent)
            node = index.get(full)
            if node is None:
                node = {"kind": "item", "name": name, "children": [],
                        "value": val, "hint": parent}
                index[full] = node
                parent_node["children"].append(node)
            else:
                node["value"] = val

        # 第二遍：自底向上算权重。父的 value = 子之和，所以必须后序。
        _weigh(root_node)
        _sort_deep(root_node)
        return root_node

    def summary(self, tree):
        groups, items = _count_nodes(tree.get("children") or [])
        # steps 留 0：CSV 没有「总耗时」这种量，硬凑一个数会让状态条
        # 变成「3 分组 · 16 项 · 16 项」——同一个数说两遍。
        # 前端见到 steps 为 0 就不拼第三段。
        return {"count": groups, "countUnit": "分组",
                "steps": 0, "stepUnit": "",
                "leaves": items}


def _ensure_path(index, root_node, path):
    """按 / 逐段补建中间层，返回最深那一层。"""
    cur = root_node
    acc = ""
    for seg in path.split("/"):
        seg = seg.strip()
        if not seg:
            continue
        acc = (acc + "/" + seg).strip("/")
        nxt = index.get(acc)
        if nxt is None:
            nxt = {"kind": "group", "name": seg, "children": [],
                   "value": 0.0, "hint": ""}
            index[acc] = nxt
            cur["children"].append(nxt)
        cur = nxt
    return cur


def _weigh(n):
    """后序算权重。叶子 max(12, 100*v^0.35)，分组 100*max(1,sum)^0.42。"""
    kids = n.get("children") or []
    if kids:
        total = 0.0
        for c in kids:
            total += _weigh(c)
        n["value"] = total
        n["kind"] = "group"
        n["w"] = 100.0 * max(1.0, total) ** 0.42
        return total
    v = n.get("value") or 0.0
    n["kind"] = "item"
    n["w"] = max(12.0, 100.0 * v ** 0.35)
    return v


def _sort_deep(n):
    kids = n.get("children") or []
    kids.sort(key=lambda x: -(x.get("value") or 0))
    for c in kids:
        _sort_deep(c)


def _count_nodes(kids):
    """返回 (非叶节点数, 叶子数)。用于状态条和 tree.name。"""
    acc = [0, 0]

    def walk(arr):
        for n in arr:
            if n.get("children"):
                acc[0] += 1
                walk(n["children"])
            else:
                acc[1] += 1

    walk(kids)
    return acc[0], acc[1]


def _sum_w(kids):
    total = 0.0
    for k in kids:
        total += k.get("value") or 0
    return total


# ==================================================================
#  数据源三：结构树（文件夹 / 思维导图 / 流程图导出）
#
#  和 CSV 的根本区别：**CSV 是表格，没有层级，得靠 parent 列声明**。
#  这一类文件天生就是一棵树 —— 目录结构、导图节点、流程分支，
#  层级已经在文件里了，不需要解析内容，只需要读出「谁挂在谁下面」。
#
#  所以这里只吃两样东西：路径 + 大小。不读文件内容，不猜语义。
#  文件夹里的一个文件 = 树上最小的那颗圆，圆名就是文件名。
#
#  落盘格式（sources/xxx.tree.json）：
#      { "name": "my-project",
#        "files": [["src/a.js", 1024], ["README.md", 88], ...] }
#  路径用 / 分隔，第一段不要重复根名。
# ==================================================================

TREE_LID_PREFIX = "tree:"


class TreeSource(Source):
    key = "tree"
    label = "结构树"
    hint = "目录 / 导图导出的层级结构，只读结构不读内容"

    def __init__(self, path=None, key=None, label=None):
        self.path = path
        self._data = None
        self._data_stamp = None
        self._idx = None
        self._idx_stamp = None
        if key:
            self.key = key
        if label:
            self.label = label
            self.hint = label

    def _data_of(self):
        # 按 mtime+size 失效：文件被重新导入覆盖后要读到新的，
        # 不然界面还挂着上一版的结构（25 万行的文件读一次 1 秒，不能每次都读）
        stamp = None
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime, st.st_size)
        except Exception:
            pass
        if self._data is not None and self._data_stamp == stamp:
            return self._data
        self._data = {"name": self.label or "结构树", "files": []}
        if self.path and os.path.exists(self.path):
            try:
                with io.open(self.path, encoding="utf-8") as fh:
                    self._data = json.load(fh)
            except Exception:
                pass
        self._data_stamp = stamp
        self._idx = None            # 数据换了，索引跟着作废
        return self._data

    @property
    def src_path(self):
        """这份快照是从哪个目录扫来的。空串 = 不知道（浏览器导入的拿不到绝对路径）。

        有了它才能「重扫」：库里的文件是快照，原目录后来加的东西它永远不会知道，
        除非有人拿着这个路径再走一遍。
        """
        try:
            return str(self._data_of().get("src") or "")
        except Exception:
            return ""

    @property
    def scanned_at(self):
        """上次扫描的 Unix 秒。0 = 老数据，没记过。"""
        try:
            return int(self._data_of().get("at") or 0)
        except Exception:
            return 0

    # ---- 索引：一次扫全量，按「父路径」分组 ------------------------------
    # 为什么要有这一层：D 盘那种几十万个文件的目录，如果每点开一层都从头
    # 扫一遍文件清单，点一次等 0.2 秒、点十次等两秒。建一次索引（约 1 秒）
    # 之后每层都是 O(1) 查表，之后一直吃缓存，直到文件 mtime 变。
    def _index(self):
        self._data_of()             # 顺带保证 _data 是新的（它会清索引）
        if self._idx is not None:
            return self._idx

        agg = {}       # 路径 -> 子树总字节
        kids = {}      # 父路径 -> {子名: 是不是目录}
        seen = set()
        for item in (self._data.get("files") or []):
            if isinstance(item, (list, tuple)):
                raw, size = item[0], (item[1] if len(item) > 1 else 0)
            elif isinstance(item, dict):
                raw, size = item.get("path"), item.get("size", 0)
            else:
                raw, size = item, 0
            # 反斜杠统一成 /（Windows 传上来的路径会带 \）
            parts = [x for x in str(raw or "").replace("\\", "/").split("/") if x and x != "."]
            if not parts:
                continue
            try:
                size = float(size or 0)
            except (TypeError, ValueError):
                size = 0.0
            full = "/".join(parts)
            if full in seen:
                continue            # 同一路径出现两次 = 忽略后者（和以前一致）
            seen.add(full)

            acc = ""
            for i, seg in enumerate(parts):
                parent = acc
                acc = (acc + "/" + seg).strip("/")
                kids.setdefault(parent, {})[seg] = (i < len(parts) - 1)
                agg[acc] = agg.get(acc, 0.0) + size

        self._idx = (agg, kids)
        return self._idx

    def _node(self, parent, name, is_dir, agg, kids):
        path = (parent + "/" + name).strip("/")
        total = max(0.0, agg.get(path, 0.0))
        if is_dir:
            # 目录：子树总字节决定大小；子节点等点开才加载（lazy）
            return {"kind": "dir", "name": name, "children": [],
                    "value": total, "size": total,
                    "lazy": True, "lid": TREE_LID_PREFIX + self.key + "/" + path,
                    "w": 100.0 * max(1.0, total) ** 0.42}
        return {"kind": "file", "name": name, "value": total, "size": total,
                "w": (100.0 * total ** 0.35) if total > 0 else 12.0}

    def _layer(self, parent, agg, kids):
        items = [self._node(parent, n, is_dir, agg, kids)
                 for n, is_dir in (kids.get(parent) or {}).items()]
        items.sort(key=lambda x: (-(x.get("value") or 0), x.get("name") or ""))
        return items

    def root(self):
        """只建第一层。深处等点开再加载 —— 这是能装下几十万文件的关键。"""
        agg, kids = self._index()
        return {"kind": "root", "name": self._data.get("name") or self.label,
                "children": self._layer("", agg, kids), "value": 0.0}

    def expand(self, node_id, offset=0, limit=0, tail=False):
        """懒加载一层。node_id 是「相对根」的目录路径。"""
        rel = str(node_id or "").strip("/")
        agg, kids = self._index()
        if rel not in kids:
            return None                 # 不是目录（叶子）或路径不存在
        items = self._layer(rel, agg, kids)
        total = len(items)
        if limit and limit > 0:
            if tail:
                offset = max(0, total - limit)
            items = items[offset:offset + limit]
        tree = {"name": rel.split("/")[-1] if rel else (self.label or "结构树"),
                "children": items}
        if limit and limit > 0:
            tree["_total"] = total
            tree["_offset"] = offset
            tree["_limit"] = limit
        return tree

    def summary(self, tree):
        # 懒加载之后 tree 里只有第一层，不能再靠数树节点 —— 统计从索引直接取
        agg, kids = self._index()
        dirs = sum(1 for p in kids if p)
        files = sum(1 for p in agg if p not in kids)
        return {"count": dirs, "countUnit": "目录",
                "steps": files, "stepUnit": "文件",
                "leaves": files}


def _weigh_bytes(n):
    """按「字节数」算权重。

    和 CSV 那套口径分开写，因为值域完全不同：CSV 的 value 是业务数值，
    文件树是字节数（1B 到几个 GB）。压缩指数沿用 0.35/0.42，
    保证同一份数据里「大文件明显大、但不会吞掉整屏」。
    """
    kids = n.get("children") or []
    if kids:
        total = 0.0
        for c in kids:
            total += _weigh_bytes(c)
        n["value"] = total
        n["kind"] = "dir"
        n["w"] = 100.0 * max(1.0, total) ** 0.42
        return total
    v = n.get("value") or 0.0
    n["kind"] = "file"
    n["w"] = (100.0 * v ** 0.35) if v > 0 else 12.0
    return v


# ==================================================================
#  JSON 导入：任意 JSON -> 结构树
#
#  为什么在服务端折、而不是新增一个 JsonSource：
#  折成和「文件夹导入」一样的 {name, files:[[路径, 数值]]} 之后，
#  TreeSource、扫目录、断开、归档、树图两栏全都照旧 ——
#  这里只多加一种**入口**，不多一条并行的代码路径。
#  少一条路径 = 少一处「删一个另一个也消失」那种 bug。
# ==================================================================

JSON_MAX_ROWS = 20000      # 一行一颗珠子，再多就不是「看图」了
JSON_MAX_DEPTH = 32        # 防爆栈；正常数据的层级不会接近这个数


def _json_rows(obj, path="", depth=0, out=None):
    """任意 JSON -> [[路径, 数值], ...] 的扁平行。"""
    if out is None:
        out = []
    if len(out) >= JSON_MAX_ROWS:
        return out
    if depth > JSON_MAX_DEPTH:
        out.append([path or "…", 0.0])
        return out

    if isinstance(obj, dict):
        for k, v in obj.items():
            _json_rows(v, (path + "/" + str(k)).strip("/"), depth + 1, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, dict):
                # 数组里的对象：有名字就用名字当这一段的键，没有就用序号。
                # 不这么做的话 [{"name":"下单"},{"name":"支付"}] 会变成 0/1 两个圆。
                key = ""
                for kk in ("name", "title", "id", "key", "label"):
                    if isinstance(v.get(kk), (str, int)) and str(v.get(kk)).strip():
                        key = str(v[kk])
                        break
                if key:
                    rest = {k2: v2 for k2, v2 in v.items()
                            if k2 not in ("name", "title", "id", "key", "label")}
                    _json_rows(rest, (path + "/" + key).strip("/"), depth + 1, out)
                    continue
            _json_rows(v, (path + "/" + str(i)).strip("/"), depth + 1, out)
    else:
        val = 0.0
        if isinstance(obj, bool):
            val = 1.0 if obj else 0.0
        elif isinstance(obj, (int, float)):
            val = float(obj)
        else:
            val = 1.0          # 字符串 / null：每项一样大，至少画得出来
        out.append([path or "value", max(0.0, val)])
    return out


def tree_from_json(obj, fallback="JSON"):
    """把用户给的 JSON 折成结构树格式。

    认三种形状：
      1. 已经是结构树 `{name, files:[[路径, 数值]]}` → 原样用
      2. 嵌套对象 `{"项目":{"阶段A":12}}` → 每层键名是路径的一段
      3. 数组 `[{"name":"下单","n":41}, ...]` → 有 name 的当节点名
    叶子数值优先取数字（圆的尺寸才有意义），取不到当 1。
    """
    if isinstance(obj, dict) and isinstance(obj.get("files"), list):
        return {"name": str(obj.get("name") or fallback), "files": obj["files"]}

    name = fallback
    body = obj
    if isinstance(obj, dict) and isinstance(obj.get("name"), str) and obj["name"].strip():
        name = obj["name"].strip()
        body = {k: v for k, v in obj.items() if k != "name"}
    return {"name": name, "files": _json_rows(body)}


# ==================================================================
#  数据源四：Claude Code 会话
#
#  为什么需要它：上面那个 WorkBuddy 源只在本机装过 WorkBuddy 时才有数据。
#  别人从 GitHub 下载这个程序，打开看到的是一份示例 CSV —— 核心功能
#  （看自己「怎么把一件事做完」）等于没用上。Claude Code 把同样的东西写在
#  ~/.claude/projects/<路径转码>/<会话 uuid>.jsonl 里，一行一个事件，
#  层级结构和 WorkBuddy 的会话记录同构，所以能接进同一套渲染、前端零改动。
#
#  格式（2026-11 与官方文档、几个第三方解析器交叉核对过）：
#      {"type":"user","uuid":..,"parentUuid":..,"timestamp":"2025-01-15T10:30:45.123Z",
#       "sessionId":..,"cwd":"/Users/x/proj","message":{"role":"user","content":"帮我写个二分"}}
#      {"type":"assistant","message":{"content":[
#            {"type":"text","text":"..."},
#            {"type":"tool_use","id":"toolu_x","name":"Read","input":{...}}]}}
#      {"type":"user","message":{"content":[
#            {"type":"tool_result","tool_use_id":"toolu_x","content":"...","is_error":false}]}}
#      {"type":"system","subtype":"compact_boundary", ...}
#
#  三条判断规则（照格式文档，别自己发明）：
#    1. 「一条 user 记录」不等于「一个新回合」—— tool_result 也是 user 记录，
#       装的是工具输出、不是人说的话。把它算成回合，一个会话会凭空多出一倍
#       没有名字的空回合。
#    2. tool_result 靠 tool_use_id 与 tool_use 配对，耗时 = 两条记录的时间差。
#    3. timestamp 是 ISO 8601 字符串，而本程序内部一律用**毫秒整数**
#       （WorkBuddy 那边就是 ms）。不转换的话 at 全是 0，时间轴塌到原点。
# ==================================================================

CLAUDE_SID_PREFIX = "claude:"
CC_INDEX_VER = 1


def _cc_projects():
    """Claude Code 的会话目录。每次现算 —— 不要在 import 期把 HOME 固化进常量。

    RD_CLAUDE_DIR：数据不在默认位置时用（.claude 放同步盘、多账号各一份、
    或者测试要指到临时目录）。和 paths.resolve_user_dir 的 RD_USER_DIR 一个思路。
    """
    env = os.environ.get("RD_CLAUDE_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def _cc_ts(s):
    """ISO 8601 -> 毫秒整数。认不出来就 0：宁可没时间，也不要编一个假时间。"""
    if isinstance(s, (int, float)) and s:
        return int(s * 1000) if s < 1e11 else int(s)
    if not isinstance(s, str) or not s.strip():
        return 0
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    # fromisoformat 只认 3 位或 6 位小数，遇到 1/5 位要补齐，否则整条时间作废
    m = re.match(r"^(.*?\.)(\d+)(.*)$", t)
    if m and len(m.group(2)) not in (3, 6):
        t = m.group(1) + (m.group(2) + "000000")[:6] + m.group(3)
    try:
        d = datetime.datetime.fromisoformat(t)
    except Exception:
        return 0
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    try:
        return int(d.timestamp() * 1000)
    except Exception:
        return 0


def _cc_iter(path):
    """逐行读 jsonl。坏行跳过，绝不让一行脏数据毁掉整个会话。"""
    try:
        fh = io.open(path, encoding="utf-8", errors="replace")
    except Exception:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line or line[0] != "{":
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if isinstance(rec, dict):
                yield rec


def _cc_content(rec):
    m = rec.get("message")
    if isinstance(m, dict):
        return m.get("content")
    return rec.get("content")


def _cc_blocks(rec):
    c = _cc_content(rec)
    return c if isinstance(c, list) else []


def _cc_text(rec):
    """这条记录里的纯文字。user 的 content 可能是字符串，也可能是块数组。"""
    c = _cc_content(rec)
    if isinstance(c, str):
        return c
    out = []
    for b in (c or []):
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
            out.append(b["text"])
    return "\n".join(out)


def _cc_is_tool_result(rec):
    """这条 user 记录装的是工具结果，不是人说的话。"""
    if rec.get("isMeta"):
        return True
    blocks = _cc_blocks(rec)
    if not blocks:
        return False
    return all(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)


def _short(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n] + "…"


def _cc_stat(path):
    """一个会话文件的轻量统计。只数数和取标题，不建树 —— 索引层要跑得够快。"""
    turns = steps = 0
    t0 = t1 = 0
    title = cwd = ""
    for r in _cc_iter(path):
        ty = r.get("type")
        ts = _cc_ts(r.get("timestamp"))
        if ts:
            if not t0 or ts < t0:
                t0 = ts
            if ts > t1:
                t1 = ts
        if not cwd and isinstance(r.get("cwd"), str):
            cwd = r["cwd"]
        if ty == "assistant":
            for b in _cc_blocks(r):
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    steps += 1
        elif ty == "user" and not r.get("isCompactSummary") and not _cc_is_tool_result(r):
            turns += 1
            if not title:
                txt = _cc_text(r).strip()
                if txt:
                    title = _short(txt, 40)
    return {"turns": turns, "steps": steps, "t0": t0, "t1": t1,
            "title": title, "cwd": cwd}


def _cc_index_path():
    return os.path.join(paths.USER, "claude_index.json")


def _cc_load_index():
    try:
        with io.open(_cc_index_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        if int(d.get("ver") or 0) == CC_INDEX_VER and isinstance(d.get("files"), dict):
            return d["files"]
    except Exception:
        pass
    return {}


def _cc_save_index(files):
    try:
        if not os.path.isdir(paths.USER):
            os.makedirs(paths.USER, exist_ok=True)
        tmp = _cc_index_path() + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ver": CC_INDEX_VER, "files": files}, fh, ensure_ascii=False)
        os.replace(tmp, _cc_index_path())
    except Exception:
        pass


def _cc_scan(force=False):
    """扫 ~/.claude/projects。返回 [(rel, abspath, 项目目录名, 统计)]。

    缓存键是 rel + mtime + size，和 WorkBuddy 的 _index.json 一个思路：
    会话只追加不改写，mtime/size 没变就等于那条记录没变，不用重读。
    """
    root = _cc_projects()
    if not os.path.isdir(root):
        return []
    old = _cc_load_index()
    new, rows, dirty = {}, [], False
    try:
        dirs = sorted(os.listdir(root))
    except OSError:
        return []
    for d in dirs:
        dd = os.path.join(root, d)
        if not os.path.isdir(dd):
            continue
        try:
            names = sorted(os.listdir(dd))
        except OSError:
            continue
        for fn in names:
            if not fn.lower().endswith(".jsonl"):
                continue
            fp = os.path.join(dd, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            rel = d + "/" + fn
            hit = old.get(rel)
            if (hit and not force and hit.get("mtime") == st.st_mtime
                    and hit.get("size") == st.st_size):
                info = hit
            else:
                info = _cc_stat(fp)
                info["mtime"] = st.st_mtime
                info["size"] = st.st_size
                dirty = True
            new[rel] = info
            rows.append((rel, fp, d, info))
    if dirty or len(new) != len(old):
        _cc_save_index(new)
    return rows


def _cc_project_name(enc, cwd=""):
    """项目目录名 -> 可读名。

    优先用会话里记的 cwd：那是权威原文，大小写和路径分隔符都还在。
    目录名是「路径把所有非字母数字换成 -」的产物，**有损** —— 路径里本来就
    有 - 的时候（other-thing），`-Users-demo-other-thing` 到底是
    `other/thing` 还是 `other-thing`，从字符串上永远分不出来。
    所以只在没有 cwd 时退回去，而且只取末段：宁可短，也不要把两段不相干的
    拼起来误导人。实际上 Claude Code 每条记录都带 cwd，这条兜底很少走到。
    """
    if cwd:
        s = cwd.replace("\\", "/").rstrip("/")
        if s:
            return s.split("/")[-1] or s
    s = enc
    if re.match(r"^[A-Za-z]--", s):
        s = s[3:]
    else:
        s = s.lstrip("-")
    parts = [p for p in s.split("-") if p]
    return parts[-1] if parts else enc


def _cc_build_tree(path, sid, offset=0, limit=0, tail=False):
    """一个 jsonl -> 会话树（回合 -> 步骤）。

    步骤节点**不在这里造**，而是拼成 live_server.step_node 认识的形状交给它 ——
    中文名、工具族、ok 判定、hint 取哪个参数，口径只能有一份。
    """
    turns = []
    pending = {}        # tool_use_id -> (回合下标, calls 内下标)
    cur = -1

    for r in _cc_iter(path):
        ty = r.get("type")
        ts = _cc_ts(r.get("timestamp"))
        if ty == "user":
            if _cc_is_tool_result(r):
                for b in _cc_blocks(r):
                    if not isinstance(b, dict) or b.get("type") != "tool_result":
                        continue
                    ref = pending.pop(b.get("tool_use_id"), None)
                    if not ref:
                        continue
                    ti, ci = ref
                    if not (0 <= ti < len(turns)) or ci >= len(turns[ti]["calls"]):
                        continue
                    call = turns[ti]["calls"][ci]
                    gap = ts - (call.get("timestamp") or 0)
                    if ts and gap >= 0:
                        call["_dur"] = gap
                    call["_ok"] = not bool(b.get("is_error"))
                continue
            if r.get("isCompactSummary"):
                continue        # 压缩后自动生成的摘要，不是人说的话
            turns.append({"ts": ts, "name": _short(_cc_text(r), 11) or "（空）", "calls": []})
            cur = len(turns) - 1
            continue
        if ty != "assistant":
            continue
        for b in _cc_blocks(r):
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            if cur < 0:
                # 第一条就是工具调用（续聊/拼接过的文件）：给它一个兜底回合，
                # 否则这些调用会全部凭空消失
                turns.append({"ts": ts, "name": "（开头）", "calls": []})
                cur = 0
            call = {
                "name": b.get("name") or "?",
                "arguments": b.get("input") or {},
                "timestamp": ts,
                "callId": b.get("id") or "",
                "_ok": True,
                "_dur": 0,
            }
            pending[call["callId"]] = (cur, len(turns[cur]["calls"]))
            turns[cur]["calls"].append(call)

    if not turns:
        return {"name": "会话", "children": []}

    ls = ClaudeCodeSource()._mod()
    total = len(turns)
    if limit and limit > 0:
        if tail:
            offset = max(0, total - limit)
        sel = turns[offset:offset + limit]
    else:
        sel = turns

    out = []
    for t in sel:
        seq = sorted(t["calls"], key=lambda c: c.get("timestamp") or 0)
        ev = {"results": {}}
        for c in seq:
            cid = c.get("callId")
            if cid:
                ev["results"][cid] = {
                    "timestamp": (c.get("timestamp") or 0) + int(c.get("_dur") or 0),
                    "status": "completed" if c.get("_ok", True) else "error",
                }
        kids = [ls.step_node(c, ev, seq, i, t["name"]) for i, c in enumerate(seq)]
        if not kids:
            kids.append({"kind": "step", "name": "未执行", "value": 0, "w": WT.w_steps(),
                         "tool": "-", "family": "misc", "ok": True,
                         "hint": "这一轮没有产生工具调用", "at": t["ts"]})
        out.append({
            "kind": "turn",
            "name": t["name"],
            "children": kids,
            "w": WT.w_count(len(kids)),
            "at": t["ts"],
            "sid": sid,
        })

    tree = {"name": "会话", "children": out}
    if limit and limit > 0:
        tree["_total"] = total
        tree["_offset"] = offset
        tree["_limit"] = limit
    return tree


class ClaudeCodeSource(Source):
    key = "claude"
    label = "Claude Code 会话"
    hint = "读本机 ~/.claude/projects 下的 .jsonl 会话记录"
    disk_label = "~/.claude/projects"

    @staticmethod
    def available():
        return os.path.isdir(_cc_projects())

    def _mod(self):
        # 延迟 import：live_server 启动时要 import 本模块，顶层互相 import 会成环
        import live_server
        return live_server

    def root(self):
        groups = {}
        for rel, fp, enc, info in _cc_scan():
            groups.setdefault(enc, []).append((rel, fp, info))

        kids = []
        for enc, arr in groups.items():
            cwd = ""
            for _, _, info in arr:
                if info.get("cwd"):
                    cwd = info["cwd"]
                    break
            sess, steps_sum, turns_sum, files_sum = [], 0, 0, 0
            for rel, fp, info in arr:
                n_steps = int(info.get("steps") or 0)
                n_turns = int(info.get("turns") or 0)
                steps_sum += n_steps
                turns_sum += n_turns
                files_sum += int(info.get("files") or 0)
                sess.append({
                    "kind": "session",
                    "sid": CLAUDE_SID_PREFIX + rel,
                    "name": info.get("title") or rel.split("/")[-1][:8],
                    "children": [],
                    "lazy": True,
                    "w": WT.w_count(n_steps),
                    "steps": n_steps,
                    "turns": n_turns,
                    "files": int(info.get("files") or 0),
                    "at": info.get("t1") or info.get("t0") or 0,
                    "t0": info.get("t0") or 0,
                    # 会话的直接下级是回合，这里报轮数不报步数（步在回合下面）
                    "right": "%d 个回合" % n_turns,
                    "disk": rel,
                })
            sess.sort(key=lambda x: -(x.get("at") or 0))
            kids.append({
                "kind": "workspace",
                "name": _cc_project_name(enc, cwd),
                "wid": enc,
                "children": sess,
                "w": WT.w_count(steps_sum),
                "steps": steps_sum,
                "sessions": len(sess),
                "turns": turns_sum,
                "at": max([s["at"] for s in sess] or [0]),
                "t0": min([s["t0"] for s in sess if s.get("t0")] or [0]),
                "right": "%d 个会话" % len(sess),
                "disk": enc,
            })
        kids.sort(key=lambda x: -(x.get("at") or 0))
        return {"kind": "root", "name": self.label, "children": kids,
                "source": self.key}

    def expand(self, node_id, offset=0, limit=0, tail=False):
        rel = str(node_id or "")
        if not rel.startswith(CLAUDE_SID_PREFIX):
            return None
        rel = rel[len(CLAUDE_SID_PREFIX):]
        root = os.path.normpath(_cc_projects())
        p = os.path.normpath(os.path.join(root, rel.replace("/", os.sep)))
        # 只认 projects 底下的文件：这个 id 是从前端回来的，不能让它指到别处
        if not (p == root or p.startswith(root + os.sep)) or not os.path.isfile(p):
            return None
        return _cc_build_tree(p, node_id, offset, limit, tail)

    def summary(self, tree):
        sessions = steps = 0
        for ws in tree.get("children", []):
            sessions += len(ws.get("children", []))
            steps += ws.get("steps") or 0
        return {"count": sessions, "countUnit": "会话",
                "steps": steps, "stepUnit": "步",
                "leaves": steps}


def expand_node(sid, offset=0, limit=0, tail=False):
    """给 /api/session 用的分派器。

    claude: 前缀归 Claude Code，tree: 前缀归结构树的懒加载，
    其余仍走 live_server.load_session（WorkBuddy 那条老路一行都不动 ——
    它背后有索引、锁、分段等一串既有逻辑）。
    """
    s = str(sid or "")
    if s.startswith(CLAUDE_SID_PREFIX):
        return ClaudeCodeSource().expand(s, offset, limit, tail)
    if s.startswith(TREE_LID_PREFIX):
        rest = s[len(TREE_LID_PREFIX):]
        key, _, path = rest.partition("/")
        src = REGISTRY.get(key)
        if isinstance(src, TreeSource):
            return src.expand(path, offset, limit, tail)
        return None
    import live_server
    return live_server.load_session(sid, offset, limit, tail)


def claude_path_of(sid):
    """claude 会话 id -> 磁盘上的 jsonl 绝对路径。查不到返回空串。"""
    s = str(sid or "")
    if not s.startswith(CLAUDE_SID_PREFIX):
        return ""
    root = os.path.normpath(_cc_projects())
    p = os.path.normpath(os.path.join(root, s[len(CLAUDE_SID_PREFIX):].replace("/", os.sep)))
    if (p == root or p.startswith(root + os.sep)) and os.path.isfile(p):
        return p
    return ""


def source_available(key):
    """这个源在本机有没有东西可读。判断不了就说「有」——
    宁可露出一棵空树，也不要让一颗珠子凭空消失。"""
    src = REGISTRY.get(key)
    if src is None:
        return False
    fn = getattr(src, "available", None)
    if fn is None:
        return True
    try:
        return bool(fn())
    except Exception:
        return True


# ==================================================================
#  注册表
# ==================================================================

IMPORT_DIR = paths.IMPORT_DIR
# 断开的库不删，挪到这儿 —— 误点的代价必须小于重导一次
ARCHIVE_DIR = os.path.join(IMPORT_DIR, "_归档")


def _slug_of(base, prefix):
    """文件名 -> 数据源 key。**这个函数必须幂等**。

    导入时落盘的文件名就是按这个 key 起的（`tree-abc12345.tree.json`）。
    下次扫 sources/ 再算一遍时如果又套一层前缀，同一个文件会被注册成两个
    key：界面上出现两行同名库；删掉其中一行（文件被移走），另一行立刻
    变成指着空文件的死链 —— 用户看到的就是「删一个另一个也消失」。
    2026-11-12 修的就是这个。

    key 必须是纯 ASCII：它要进 URL。中文名硬塞进去会被百分号编码，
    前端解析参数时极易截断。显示名另有 label，不受影响。
    """
    if base.lower().endswith(".tree.json"):
        stem = base[:-len(".tree.json")]
    else:
        stem = os.path.splitext(base)[0]
    part = re.sub(r"[^\w\-]+", "-", stem, flags=re.ASCII).strip("-").lower()
    if part.startswith(prefix):
        return part
    return prefix + (part or hashlib.md5(base.encode("utf-8")).hexdigest()[:8])


def register_tree_file(path, label=None, key=None):
    """把一份结构树注册成数据源。同名重复导入覆盖同一个 key。"""
    base = os.path.basename(path)
    slug = key or _slug_of(base, "tree-")
    # 显示名优先用用户选的那个文件夹名（存在树文件的 name 里），
    # 退而求其次才是 sources/ 下的文件名 slug —— slug 是给 URL 用的，
    # 挂在珠子和两栏里读起来像乱码。
    if not label:
        try:
            with io.open(path, encoding="utf-8") as fh:
                label = (json.load(fh).get("name") or "").strip()
        except Exception:
            label = ""
    src = TreeSource(path, key=slug, label=label or slug)
    REGISTRY[slug] = src
    return src


def register_csv_file(path, label=None, key=None):
    """把用户导入的 CSV 注册成一个独立数据源。
    同名文件重复导入 → 覆盖同一个 key，不会堆出一串重复项。"""
    base = os.path.basename(path)
    slug = key or _slug_of(base, "import-")
    src = CsvSource(path, key=slug, label=label or base)
    REGISTRY[slug] = src
    return src


def load_imported(rescan=False):
    """把 sources/ 里已有的文件注册成数据源。

    rescan=False：启动时调用，全部注册一遍。
    rescan=True ：请求时调用，只登记「注册表里还没有的文件」。

    ⚠️ 为什么需要 rescan：以前只在 import 那一刻扫一次，用户把 .csv 丢进
    sources/ 再刷新页面是没有任何反应的，必须重启整个程序。而界面上、
    文档里写的都是「放进去刷新就行」—— 说的和做的对不上。改成请求时补扫，
    只做一次 os.listdir，成本在微秒级，可以挂在取源清单的路径上。
    """
    if not os.path.isdir(IMPORT_DIR):
        # 第一次运行（刚 clone 下来）没有 sources/，自动建一个。
        # 否则界面上写着「把 csv 放进 sources」，而那个文件夹根本不存在。
        try:
            os.makedirs(IMPORT_DIR, exist_ok=True)
        except Exception:
            return
    if rescan:
        # 反过来也要管：文件被删掉/改名后，注册表里的旧条目要跟着消失。
        # 否则界面上会挂着一颗点不开的珠子（路径已不存在），用户以为程序坏了。
        # 只清 import-/tree- 这两类，workbuddy 和内置示例不在此列。
        for _k in [k for k, v in REGISTRY.items()
                   if k.startswith(("import-", "tree-"))
                   and getattr(v, "path", None) and not os.path.exists(v.path)]:
            REGISTRY.pop(_k, None)
    for fn in sorted(os.listdir(IMPORT_DIR)):
        fp = os.path.join(IMPORT_DIR, fn)
        low = fn.lower()
        if not (low.endswith(".tree.json") or low.endswith(".csv")):
            continue
        if rescan:
            # 已登记过的跳过：同一个文件反复重新注册会不断造新实例，
            # 界面上看不出差别，但每 1.5 秒一轮的轮询会白白产生对象和 IO。
            # **必须走 _slug_of**，不能在这儿再抄一遍算法 —— 抄的那份
            # 忘了「文件名已经带前缀」这件事，同一个文件会被注册成两个 key。
            slug = _slug_of(fn, "tree-" if low.endswith(".tree.json") else "import-")
            if slug in REGISTRY:
                continue
        try:
            if low.endswith(".tree.json"):
                register_tree_file(fp)
            elif low.endswith(".csv"):
                register_csv_file(fp)
        except Exception:
            pass


REGISTRY = {}
for _cls in (WorkBuddySource, ClaudeCodeSource, CsvSource):
    _inst = _cls()
    REGISTRY[_inst.key] = _inst

load_imported()


def _fallback():
    """没指定源、或指定的源在本机没数据时，退到哪个。

    顺序就是「谁更可能真有数据」：WorkBuddy（本机装了才有）→ Claude Code
    （装了才有）→ 示例 CSV（谁都有，充数用）。
    """
    for k in ("workbuddy", "claude"):
        if REGISTRY.get(k) is not None and source_available(k):
            return REGISTRY[k]
    return REGISTRY["csv"]


def get_source(key):
    """取数据源。会话类数据源在本机没东西时，退到下一个 ——
    否则对方的机器上打开就是白屏，还以为是程序坏了。"""
    src = REGISTRY.get(key)
    if src is not None:
        if src.key in ("workbuddy", "claude") and not source_available(src.key):
            return _fallback()
        return src
    return _fallback()


def remove_source(key):
    """断开一个导入库：文件移到 sources/_归档/，再从注册表里摘掉。

    不真删 —— 点错一下就把用户自己的文件删了，没有这种道理。
    workbuddy（本机会话）和示例库没有「链接」可断，直接拒绝。
    返回 (ok, label, error)。
    """
    src = REGISTRY.get(key)
    if src is None:
        return False, "", "没有这个库"
    if src.is_example or key == "workbuddy":
        return False, "", "这个库没有可断开的链接"
    label = src.label
    path = getattr(src, "path", "") or ""
    if path and os.path.exists(path):
        try:
            if not os.path.isdir(ARCHIVE_DIR):
                os.makedirs(ARCHIVE_DIR)
            base = os.path.basename(path)
            dst = os.path.join(ARCHIVE_DIR, base)
            if os.path.exists(dst):
                root, ext = os.path.splitext(base)
                dst = os.path.join(ARCHIVE_DIR, "%s-%d%s" % (root, int(time.time()), ext))
            shutil.move(path, dst)
        except Exception as e:
            return False, label, "文件移不走：%s" % e
    REGISTRY.pop(key, None)
    return True, label, ""


def list_sources():
    """给界面用的源清单（顺序 = 渲染顺序 = 两边联动的行序）。

    示例库只在「一个真库都没有」时出现，而且只安静地摆在画面上充数：
    - 有一个真库（本机 WorkBuddy，或任何一个导入的库）→ 示例立刻不出现，
      也不参与任何计数（几颗珠、几个库、表头数字都不算它）。
    - 一个真库都没有 → 只有它，否则第一次打开是白屏。
    """
    # 每次取清单顺手补扫一遍 sources/，这样「丢文件进去 → 刷新页面」真的生效
    load_imported(rescan=True)
    real = [s for s in REGISTRY.values() if not s.is_example]
    usable = [s for s in real if source_available(s.key)]
    if usable:
        return [s.describe() for s in real]
    return [s.describe() for s in REGISTRY.values() if s.is_example]
