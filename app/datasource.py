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

import paths

# 软件本体目录。示例 CSV 这类「随程序走」的东西挂在这里。
HERE = paths.CODE


class Source(object):
    """数据源基类。子类只需要填 key / label / root()。"""

    key = ""
    label = ""
    hint = ""
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

class TreeSource(Source):
    key = "tree"
    label = "结构树"
    hint = "目录 / 导图导出的层级结构，只读结构不读内容"

    def __init__(self, path=None, key=None, label=None):
        self.path = path
        self._data = None
        if key:
            self.key = key
        if label:
            self.label = label
            self.hint = label

    def _data_of(self):
        if self._data is not None:
            return self._data
        self._data = {"name": self.label or "结构树", "files": []}
        if self.path and os.path.exists(self.path):
            try:
                with io.open(self.path, encoding="utf-8") as fh:
                    self._data = json.load(fh)
            except Exception:
                pass
        return self._data

    def root(self):
        d = self._data_of()
        root_node = {"kind": "root", "name": d.get("name") or self.label,
                     "children": [], "value": 0.0}
        index = {"": root_node}

        for item in (d.get("files") or []):
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
            parent_p = "/".join(parts[:-1])
            name = parts[-1]
            parent_node = index.get(parent_p)
            if parent_node is None:
                parent_node = _ensure_path(index, root_node, parent_p)
            full = (parent_p + "/" + name).strip("/")
            if full in index:
                continue                      # 同一路径出现两次 = 忽略后者
            node = {"kind": "file", "name": name, "children": [],
                    "value": max(0.0, size), "size": max(0.0, size),
                    "hint": parent_p}
            index[full] = node
            parent_node["children"].append(node)

        _weigh_bytes(root_node)
        _sort_deep(root_node)
        return root_node

    def summary(self, tree):
        dirs, files = _count_nodes(tree.get("children") or [])
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
#  注册表
# ==================================================================

IMPORT_DIR = paths.IMPORT_DIR
# 断开的库不删，挪到这儿 —— 误点的代价必须小于重导一次
ARCHIVE_DIR = os.path.join(IMPORT_DIR, "_归档")


def register_tree_file(path, label=None, key=None):
    """把一份结构树注册成数据源。同名重复导入覆盖同一个 key。"""
    base = os.path.basename(path)
    stem = base[:-len(".tree.json")] if base.lower().endswith(".tree.json") else base
    ascii_part = re.sub(r"[^\w\-]+", "-", stem, flags=re.ASCII).strip("-").lower()
    slug = key or ("tree-" + (ascii_part or hashlib.md5(base.encode("utf-8")).hexdigest()[:8]))
    # 显示名优先用用户选的那个文件夹名（存在树文件的 name 里），
    # 退而求其次才是 sources/ 下的文件名 slug —— slug 是给 URL 用的，
    # 挂在珠子和两栏里读起来像乱码。
    if not label:
        try:
            with io.open(path, encoding="utf-8") as fh:
                label = (json.load(fh).get("name") or "").strip()
        except Exception:
            label = ""
    src = TreeSource(path, key=slug, label=label or stem)
    REGISTRY[slug] = src
    return src


def register_csv_file(path, label=None, key=None):
    """把用户导入的 CSV 注册成一个独立数据源。
    同名文件重复导入 → 覆盖同一个 key，不会堆出一串重复项。"""
    base = os.path.basename(path)
    stem = os.path.splitext(base)[0]
    # slug 必须是纯 ASCII：它要进 URL。中文名硬塞进去会被百分号编码，
    # 前端解析参数时极易截断（踩过：source=import-%E9... 被读成 import）。
    # 保留能用的英文数字，其余退化成稳定哈希 —— 显示名另有 label，不受影响。
    ascii_part = re.sub(r"[^\w\-]+", "-", stem, flags=re.ASCII).strip("-").lower()
    slug = key or ("import-" + (ascii_part or hashlib.md5(base.encode("utf-8")).hexdigest()[:8]))
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
            # 前缀必须和 register_csv_file / register_tree_file 里的算法一致，
            # 否则算出来的 key 永远不在表里，等于没跳过、每轮都重新注册。
            if low.endswith(".tree.json"):
                stem = fn[:-len(".tree.json")]
                prefix = "tree-"
            else:
                stem = os.path.splitext(fn)[0]
                prefix = "import-"
            ascii_part = re.sub(r"[^\w\-]+", "-", stem, flags=re.ASCII).strip("-").lower()
            slug = prefix + (ascii_part or hashlib.md5(fn.encode("utf-8")).hexdigest()[:8])
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
for _cls in (WorkBuddySource, CsvSource):
    _inst = _cls()
    REGISTRY[_inst.key] = _inst

load_imported()


def get_source(key):
    """取数据源。workbuddy 在没装 WorkBuddy 的机器上是空的，
    这种情况退到 CSV，而不是把一棵空树甩给前端 —— 否则对方打开就是白屏。"""
    src = REGISTRY.get(key)
    if src is not None:
        if src.key == "workbuddy" and not WorkBuddySource.available():
            return REGISTRY["csv"]
        return src
    return REGISTRY["workbuddy"]


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
    usable = [s for s in real
              if s.key != "workbuddy" or WorkBuddySource.available()]
    if usable:
        return [s.describe() for s in real]
    return [s.describe() for s in REGISTRY.values() if s.is_example]
