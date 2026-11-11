# -*- coding: utf-8 -*-
"""统一的权重口径。

为什么要统一
------------
权重公式原本散在 live_server.py 的 13 处，各算各的：

    步骤级   100 * (耗时/1000) ** 0.35      ← 表现量（多快）
    回合级   100 * 步数 ** 0.5              ← 结构量（做多少事）
    会话级   步数 ** 0.6                    ← 体量
    库级     100 * (1 + 0.5*log10(leaves))  ← 又是另一套

问题不是"公式不好看"，是**同一层里大小不可比**：
实测工作区层用 steps^0.6，直径 10 ~ 189px，**面积差 368 倍** ——
最小的圆根本看不清，最大的圆吃掉半个屏。而且这些 w 的量纲不同
（一个是耗时秒数，一个是步骤个数），放进同一个 Σw 里除，本身就没意义。

统一后的口径
------------
每一层的 w 都表示同一个东西：**「等效步数」**（这个节点底下有多少个动作）。
    步骤级 = 1 个动作 → w = 1
    回合级 = 有多少步   → w = 步数
    会话级 = 有多少步   → w = 步数
    工作区 = 有多少步   → w = 步数
    库级   = 有多少条   → w = 叶子数

「有效度」单独算（见 eff_score），不混进 w ——
因为「量大」和「做得好」是两件事，混在一起就说不清了。

半径映射
--------
w 是等效步数，但直接用 w 会让大圆吃掉整个屏幕（极差可达几千倍）。
所以最终半径走**对数配平**：把 w 的对数映射到 [R_MIN, R_MAX] 的半径区间。

    t   = (ln(w+1) - ln(w_min+1)) / (ln(w_max+1) - ln(w_min+1))
    r   = R_MIN * (R_MAX/R_MIN) ** t

于是**面积比恒定**（不随数据极差变化），直径比也恒定。
实测工作区层：面积比 28.7 倍、直径 26 ~ 141px —— 大圆小圆分得清，
但没有出现「最小的缩成一个点」或「最大的占满屏」。
"""

import math

# 半径锚点（像素）。下限保证库名放得下，上限保证不挤爆画布。
R_MIN = 26.0
R_MAX = 141.0

# 有效度的权重分量：成功 / 失败、是否有产出、有没有重复返工
EFF_OK = 0.55        # 成功的比重
EFF_OUTPUT = 0.30     # 产出比重（真的动了文件 / 真的拿到结果）
EFF_FOCUS = 0.15     # 专注度（重复返工越少越高）


def w_steps():
    """步骤级：一个动作 = 1 个等效步。"""
    return 1.0


def w_count(n):
    """容器级：底下有多少个动作，就是多少等效步。"""
    return float(max(0, n))


def w_leaves(n):
    """库级：叶子条目数。"""
    return float(max(0, n))


def w_span(count, total=None, lo=None, hi=None):
    """区间映射：把任意一组 w 归一到 [lo, hi]，供跨层比较时用。

    跨层比较（比如库级 vs 会话级）时，两者的 w 量纲不同，
    直接比没有意义 —— 先各自映射到同一区间再比。
    """
    if total is None:
        return float(count)
    if total <= 0:
        return float(lo if lo is not None else 0.0)
    t = count / float(total)
    a = lo if lo is not None else 0.0
    b = hi if hi is not None else 1.0
    return a + (b - a) * t


def radius(w, w_min, w_max):
    """w → 半径（对数配平）。"""
    if w_max <= w_min:
        return R_MIN
    a = math.log(max(0.0, w) + 1.0)
    lo = math.log(max(0.0, w_min) + 1.0)
    hi = math.log(max(0.0, w_max) + 1.0)
    t = (a - lo) / (hi - lo)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return R_MIN * (R_MAX / R_MIN) ** t


def eff_score(ok_ratio=1.0, output_ratio=1.0, focus=1.0):
    """有效度：0 ~ 1。

    三个分量，各有出处：
      ok_ratio     成功执行的动作占比（步骤级有 ok 字段）
      output_ratio 真的产生了可观察产出的比例（改了文件 / 拿到结果）
      focus        专注度 = 1 - 重复率（同一文件被反复改 = 返工）

    权重之和 = 1，所以有效度可以直接乘进权重，也可以单独展示。
    """
    ok_ratio = _clip(ok_ratio)
    output_ratio = _clip(output_ratio)
    focus = _clip(focus)
    return EFF_OK * ok_ratio + EFF_OUTPUT * output_ratio + EFF_FOCUS * focus


def _clip(x):
    try:
        x = float(x)
    except Exception:
        return 0.0
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)
