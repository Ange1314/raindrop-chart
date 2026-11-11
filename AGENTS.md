# AGENTS.md — instructions for an AI agent

You are installing **Raindrop Chart** for the human who downloaded it.
Read this file completely, then execute it. Do not improvise beyond it.

If you can only do one thing: run step 2, then tell the human to restart you.

---

## What this is

A local, offline viewer that turns files / conversation history into a clickable
circle diagram, plus an MCP server so an AI client (WorkBuddy / Buddy) can read the
same structure. Nothing leaves the machine: the server binds `127.0.0.1` only.

---

## Hard rules — do not break these

1. **Never write into `app/`.** It is the program. It must stay replaceable.
2. **Never delete or rewrite `data/`.** That is the human's own data.
3. **Never upload, publish, push, or `git push`.** Installing is not publishing.
4. **Never modify the human's `~/.workbuddy/mcp.json` by hand.** Use
   `app/register_mcp.py` — it merges one entry and backs the file up first.
5. **Ask before installing anything** (`pip`, system packages). Nothing is needed:
   the program uses the Python standard library only.
6. Do not edit source files to "fix" something. If a step fails, report it.

---

## Layout

```
双击启动雨滴图.vbs   Windows launcher (silent, no console)
连接Buddy.vbs        Windows: register this app as an MCP server (silent)
start.sh             macOS / Linux launcher
app/                 the program  — read-only for you
data/                user data    — created on first run; never touch
docs/                screenshots + detailed manual
```

---

## Step 0 · Environment check

Run and record the results:

```bash
python --version || py --version
```

- `3.8` or newer → continue
- not found → stop and tell the human to install Python 3.8+ from python.org,
  ticking *Add python.exe to PATH*. **Do not try to install it for them.**

Optional sanity check:

```bash
python -c "import sys;print(sys.executable)"
```

That path is what will be written into the MCP config.

---

## Step 1 · Start the local server

**Windows (preferred — matches what the human sees):**

```bash
wscript "双击启动雨滴图.vbs"
```

It starts hidden and opens the browser by itself.

**Any OS (console, for debugging):**

```bash
python app/live_server.py
```

Keep it running. It prints the address, e.g. `http://127.0.0.1:7788/`.

> The port starts at 7788 and **drifts upward** if taken (7789, 7790, …).
> Always read the real port from `GET /api/ping` → `url`, never assume.

Verify:

```bash
curl -s http://127.0.0.1:7788/api/ping
```

Expected: JSON with `"app": "raindrop-chart"` and a `version`.

---

## Step 2 · Register as an MCP server (this is what lets you read the data)

**Windows:**

```bash
wscript "连接Buddy.vbs"
```

**Any OS:**

```bash
python app/register_mcp.py
```

Exit code `0` = done. It:
- merges exactly one entry (`raindrop-workflow`) into `~/.workbuddy/mcp.json`
- leaves every other server untouched
- writes a timestamped backup next to it (`mcp.json.bak-*`)
- computes the absolute path itself — **you never supply a path**

Expected output ends with three next steps for the human.

---

## Step 3 · Verify the MCP entry actually works

Do not trust "it wrote a file". Run the handshake:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}' \
  | python app/live_server.py --mcp
```

Expected: one JSON line containing `"name": "raindrop-workflow"` and the version.

If that works, the config is good.

---

## Step 4 · Hand back to the human

Tell them exactly this:

1. **Quit the AI client completely and reopen it** — the tool list is read at session
   start; without a restart the tools will not appear.
2. Open the connector page, find `raindrop-workflow`, click **Trust**.
3. Then you will have three tools: `workflow_tree`, `workflow_report`, `workflow_show`.

Do not claim the tools are available before the client has been restarted and trusted.

---

## Failure table

| Symptom | Cause | What to do |
|---|---|---|
| `python` not found | not installed / not on PATH | stop, ask the human to install it |
| dialog: "缺少必需的 Python 文件" + `ModuleNotFoundError: paths` | `app/paths.py` is missing — the package is incomplete | stop and tell the human to re-extract the whole folder; do not try to recreate the file |
| `Address already in use` | port taken | normal — it drifts; read the port from `/api/ping` |
| `/api/ping` returns another app | different program on that port | read `url` from the JSON, it will have drifted |
| register exit code `1` with "解析失败" | their `mcp.json` is malformed | **do not fix it**; report the file path and the error |
| register says "没找到 WorkBuddy 的配置目录" | WorkBuddy not installed | install is complete anyway; MCP is optional |
| tools missing after restart | client not restarted, or entry not trusted | restart again, then Trust in the connector page |

---

## What you may then do with this data (and what you may not)

Allowed: read structure, answer questions about where time went, which files were
touched repeatedly, what a session actually did.

Not allowed: modify or delete the human's files, send messages, touch accounts,
or treat locked items as readable — the lock filters these few items out of your
view by design. Do not try to work around it, and do not report them as "missing".

---

## 中文摘要（给人看的，方便你核对 AI 要做什么）

AI 会做四件事，全部在本机完成，不上传任何东西：

1. 检查有没有 Python 3.8+（没有就停下来让你装，不会替你装）
2. 启动本地服务（Windows 用 `双击启动雨滴图.vbs` 的等价命令）
3. 跑一次 `app/register_mcp.py`，往 `~/.workbuddy/mcp.json` 合并一条配置（写前自动备份）
4. 用一次 `initialize` 握手验证配置真的能连

然后它应该提醒你：**完全退出并重开 Buddy，再在连接器页点「信任」**。
没重启之前，工具不会出现——这不是装失败。

**它不该做的事**：改 `app/`、删 `data/`、手动改你的 `mcp.json`、装任何依赖、发布任何东西。
如果它做了其中任何一件，你可以直接叫停。
