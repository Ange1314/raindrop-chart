# -*- coding: utf-8 -*-
"""把这一份雨滴图注册成本地 MCP 服务器（stdio）。

为什么需要这个脚本，而不是在 README 里让用户手填一段 JSON：
    用户下载的是一个**包**，包解压到哪、路径就在哪，作者不可能预先写死。
    让用户自己去地址栏复制一段带空格的路径、再粘进 JSON、还要注意反斜杠转义 ——
    这一步的失败率远高于「双击一下」。

做法：脚本自己知道自己在哪（__file__），把**绝对路径**算出来写进去。
    用户全程不需要看到、也不需要输入任何路径。

只做一件事：往 ~/.workbuddy/mcp.json 里 merge 一条，别的服务器条目一个不碰。
写之前先备份，解析失败就不动（宁可不改，也不要把别人的配置弄坏）。

输出落在两个地方：
    · 控制台（有人从终端跑时看这里）
    · data/注册结果.txt（从 注册MCP.vbs 双击时没有控制台，结果得有个地方看）

命令用 python.exe 而不是 pythonw.exe —— 无窗口版没有 stdout，
而 MCP 靠 stdout 传协议，换成 pythonw 会直接握手失败。
"""

import io
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))       # .../app
ROOT = os.path.dirname(HERE)                            # 包根

# 入口固定是 live_server.py：加 --mcp 后同一个进程就变成 MCP 服务器，
# 解析层和网页共用一份，不另做一个可执行文件。
ENTRY = os.path.join(HERE, "live_server.py")
KEY = "raindrop-workflow"

_LINES = []


def say(msg=""):
    """说一句，同时记下来 —— 双击启动时没有控制台，全靠这份记录。"""
    _LINES.append(msg)
    print(msg)


def flush_report():
    """把这次的输出写进 data/注册结果.txt。写不进去就算了，别因为日志失败而失败。"""
    try:
        import paths
        paths.ensure()
        p = os.path.join(paths.USER, "注册结果.txt")
        # utf-8-sig：记事本打开中文不乱码
        with io.open(p, "w", encoding="utf-8-sig", newline="\r\n") as fh:
            fh.write("\n".join(_LINES) + "\n")
        return p
    except Exception:
        return ""


def find_python():
    """填进配置里的解释器。

    优先 sys.executable —— 就是此刻正在跑这个脚本的那个 python，
    它一定存在、一定有标准库，且和用户刚验证过能用的那个是同一个。
    """
    exe = sys.executable
    if exe and os.path.isfile(exe):
        # 不要 pythonw：没有 stdout 就没有 MCP
        if os.path.basename(exe).lower() == "pythonw.exe":
            cand = os.path.join(os.path.dirname(exe), "python.exe")
            if os.path.isfile(cand):
                return cand
        return exe
    return "python"


def main():
    home = os.path.expanduser("~")
    cfg_dir = os.path.join(home, ".workbuddy")
    cfg = os.path.join(cfg_dir, "mcp.json")

    say()
    say("  Raindrop Chart · 连接 AI")
    say()
    say("  程序目录：%s" % ROOT)
    say("  配置文件：%s" % cfg)
    say()

    if not os.path.isdir(cfg_dir):
        say("  没找到 WorkBuddy 的配置目录，说明这台机器没装 WorkBuddy。")
        say("  MCP 是给 AI 客户端用的，没装就不用注册 —— 看图完全不受影响。")
        flush_report()
        return 1

    data = {"mcpServers": {}}
    if os.path.exists(cfg):
        try:
            with io.open(cfg, encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except Exception as e:
            say("  mcp.json 解析失败，为免弄坏它，本次不做任何修改：%s" % e)
            flush_report()
            return 1
        bak = cfg + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(cfg, bak)
            say("  已备份原配置：%s" % bak)
        except Exception as e:
            say("  备份失败，为免风险本次不做修改：%s" % e)
            flush_report()
            return 1

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers

    existed = KEY in servers
    servers[KEY] = {
        "type": "stdio",
        "command": find_python(),
        "args": [ENTRY, "--mcp"],
        "timeout": 30000,
        "autoApprove": [],
    }

    try:
        with io.open(cfg, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        say("  写入失败：%s" % e)
        flush_report()
        return 1

    say("  %s：%s" % ("已更新" if existed else "已新增", KEY))
    say("  其他服务器条目保持原样，一个没动。")
    say()
    say("  接下来（顺序别换）：")
    say("    1. 完全退出并重开 WorkBuddy —— 工具清单在会话启动时才读，不重开不生效")
    say("    2. 打开连接器管理页，找到 %s" % KEY)
    say("    3. 点「信任」")
    say()
    say("  想撤销：把上面那个 .bak 文件改回 mcp.json 即可。")
    flush_report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
