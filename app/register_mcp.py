# -*- coding: utf-8 -*-
"""把这一份雨滴图注册成本地 MCP 服务器（stdio），能连几个客户端就连几个。

为什么需要这个脚本，而不是在 README 里让用户手填一段 JSON：
    用户下载的是一个**包**，包解压到哪、路径就在哪，作者不可能预先写死。
    让用户自己去地址栏复制一段带空格的路径、再粘进 JSON、还要注意反斜杠转义 ——
    这一步的失败率远高于「双击一下」。

做法：脚本自己知道自己在哪（__file__），把**绝对路径**算出来写进去。
    用户全程不需要看到、也不需要输入任何路径。

★ 安全边界（多客户端的关键）：
    只碰**这个客户端已经装在这台机器上**的配置文件 —— 判据是配置所在的目录
    在不在。没装的客户端一律不创建、不猜路径。
    每次改之前先备份、先做 JSON 解析、写回前再解析一次；任何一步失败就不动文件。
    宁可这个客户端不连，也不能把人家的编辑器配置弄坏 —— 那是不可逆的。

输出落在两个地方：
    · 控制台（有人从终端跑时看这里）
    · data/注册结果.txt（从 连接Buddy.vbs 双击时没有控制台，结果得有个地方看）

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


def _home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def _appdata(*parts):
    base = os.environ.get("APPDATA") or _home("AppData", "Roaming")
    return os.path.join(base, *parts)


def clients():
    """已知的 MCP 客户端与它们的配置位置。

    路径写死是故意的：让脚本去「猜」别的客户端把配置放哪，猜错了就是
    在用户的磁盘上到处建空目录。宁可只认这几个标准位置。
    """
    mac = sys.platform == "darwin"
    win = sys.platform.startswith("win")
    out = [
        {"name": "WorkBuddy", "dir": _home(".workbuddy"),
         "cfg": _home(".workbuddy", "mcp.json")},
        {"name": "Cursor", "dir": _home(".cursor"),
         "cfg": _home(".cursor", "mcp.json")},
    ]
    if mac:
        cd = os.path.join(_home("Library", "Application Support"), "Claude")
        out.append({"name": "Claude Desktop", "dir": cd,
                    "cfg": os.path.join(cd, "claude_desktop_config.json")})
    elif win:
        cd = _appdata("Claude")
        out.append({"name": "Claude Desktop", "dir": cd,
                    "cfg": os.path.join(cd, "claude_desktop_config.json")})
    return out


def merge_into(cfg, entry):
    """往一个配置文件里 merge 一条，返回 (状态, 说明)。

    状态：ok / fail。每一步失败都**不改文件** —— 这是唯一的硬约束。
    """
    existed = os.path.exists(cfg)
    data = {}
    if existed:
        try:
            with io.open(cfg, encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except Exception as e:
            return ("fail", "解析失败，未修改：%s" % e)
        if not isinstance(data, dict):
            return ("fail", "内容不是对象，未修改")
        try:
            shutil.copy2(cfg, cfg + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        except Exception as e:
            return ("fail", "备份失败，未修改：%s" % e)

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers

    had = KEY in servers
    servers[KEY] = entry

    try:
        text = json.dumps(data, ensure_ascii=False, indent=2)
        json.loads(text)                      # 写之前自己先验一遍
        with io.open(cfg, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception as e:
        return ("fail", "写入失败：%s" % e)

    if not existed:
        return ("ok", "新建配置")
    return ("ok", "已更新" if had else "已新增条目")


def main():
    say()
    say("  Raindrop Chart · 连接 AI")
    say()
    say("  程序目录：%s" % ROOT)
    say("  解释器：  %s" % find_python())
    say()

    entry = {
        "type": "stdio",
        "command": find_python(),
        "args": [ENTRY, "--mcp"],
        "timeout": 30000,
        "autoApprove": [],
    }

    done, failed, absent = [], [], []

    for c in clients():
        # ★ 只碰已经装的：配置目录不在 = 这台机器没装这个客户端
        if not os.path.isdir(c["dir"]):
            absent.append((c["name"], c["cfg"]))
            continue
        state, msg = merge_into(c["cfg"], entry)
        if state == "ok":
            done.append(c["name"])
            say("  [完成] %-16s %s" % (c["name"], msg))
        else:
            failed.append((c["name"], msg))
            say("  [未改] %-16s %s" % (c["name"], msg))

    if absent:
        say()
        say("  这台机器上没有这些客户端（跳过，不会替你创建）：")
        for name, cfg in absent:
            say("    - %-16s %s" % (name, cfg))

    say()
    if done:
        say("  接下来（顺序别换）：")
        say("    1. 完全退出并重开 %s —— 工具清单在会话启动时才读，不重开不生效"
            % " / ".join(done))
        say("    2. 在连接器管理页找到 %s" % KEY)
        say("    3. 点「信任」")
        say()
        say("  想撤销：把同目录下最新的 xxx.bak-时间戳 改回原名即可。")
        flush_report()
        return 0

    # 一个都没连上：把该写什么打出来，由用户自己的客户端决定
    say("  没有可自动配置的客户端。")
    say("  如果你用的是别的 AI 客户端，在它的 MCP 配置里加这一段")
    say("  （路径按你自己的实际位置改）：")
    say()
    say('  {')
    say('    "mcpServers": {')
    say('      "%s": {' % KEY)
    say('        "type": "stdio",')
    say('        "command": "%s",' % find_python().replace("\\", "\\\\"))
    say('        "args": ["%s", "--mcp"],' % ENTRY.replace("\\", "\\\\"))
    say('        "timeout": 30000')
    say('      }')
    say('    }')
    say('  }')
    if failed:
        say()
        say("  上面有配置解析失败的客户端 —— 我没动它们的文件，你自己看过再决定。")
    flush_report()
    return 1


if __name__ == "__main__":
    sys.exit(main())
