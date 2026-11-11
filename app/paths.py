# -*- coding: utf-8 -*-
"""目录分区：**软件本体**与**软件的信息**分开放。

为什么要有这个文件：
    程序和数据混在一个目录里，迟早出两类事故 ——
    升级时把用户的数据覆盖掉；或者打包时把用户的数据一起发出去。
    这两件事都是不可逆的，所以分区必须写进代码，不能靠人记。

两种布局，同一份代码都认：

    开发态（代码就在根目录）
        Raindrop Chart/
        ├── live_server.py  ring.html  …     ← CODE：软件本体
        ├── sources/  _index.json  …         ← USER：软件的信息
        └── …

    分发态（代码收进 app/，数据单独 data/）
        raindrop-chart/
        ├── 启动.bat                          ← 唯一的入口
        ├── app/  live_server.py  ring.html   ← CODE：整目录可替换
        └── data/ sources/  _index.json  …    ← USER：升级时不动

判定规则只有一条：**代码在 app/ 或 src/ 下面，数据就放它的兄弟目录 data/**。
其余情况（代码在根、单文件 exe、外部指定）都退化成「代码目录就是数据目录」，
和老版本行为完全一致 —— 老用户升级不会找不到自己的库。

覆盖顺序（优先级从高到低）：
    1. 环境变量 RD_USER_DIR —— 便携版 / 多实例 / 把数据放到同步盘时用
    2. 分发态约定：<包根>/data
    3. 代码目录本身

⚠️ USER 可能不存在（刚 clone、刚解压）。谁要用谁调 ensure()，
   不要在 import 阶段创建目录 —— 那样 `python -c "import live_server"`
   也会在只读介质上留下一堆空文件夹。
"""

import os

# 软件本体所在目录。所有 .py / ring.html / data.js 都在这儿。
CODE = os.path.dirname(os.path.abspath(__file__))

# 包根（分发态）。开发态时它等于项目根，没人用它。
ROOT = os.path.dirname(CODE)

# 代码被收进子目录 = 分发态。名字取 app 或 src 都行，别新增第三种。
_PACKED_DIRS = ("app", "src")


def is_packed():
    """代码是不是被收进了子目录（= 分发态布局）。"""
    return os.path.basename(CODE).lower() in _PACKED_DIRS


def resolve_user_dir():
    """算出「软件的信息」该落在哪。"""
    env = os.environ.get("RD_USER_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if is_packed():
        return os.path.join(ROOT, "data")
    return CODE


# 软件的信息：导入的库、索引缓存、锁、运行日志。
# 这个目录可以整个删掉 —— 删了就是回到「第一次打开」，程序照样起得来。
USER = resolve_user_dir()

# 用户导入的 CSV / 文件夹树落在这里
IMPORT_DIR = os.path.join(USER, "sources")


def ensure():
    """首次运行时把数据目录补齐。返回 USER。"""
    for d in (USER, IMPORT_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    return USER
