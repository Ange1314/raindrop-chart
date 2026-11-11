# Raindrop Chart — Details

> This is the reference, not the pitch. For what the thing does, read the
> [README](../README.en.md). What lives here: how to use it, how to change it, what breaks.

## Quick start

### 1. Check for Python

```bash
python --version
```

Need **3.8 or newer**. If the command is missing, install Python from [python.org](https://www.python.org/downloads/) — on Windows, **tick `Add python.exe to PATH` on the first installer screen**.

### 2. Get the code

```bash
git clone https://github.com/Ange1314/raindrop-chart.git
cd raindrop-chart
```

Or click `Code` → `Download ZIP` and unzip it. Don't run it from inside the archive.

> Don't want to touch a command line? You still need Python first — **there is no standalone build yet**
> (a zip with an embedded runtime isn't done). Once Python is in, daily use is one double-click.

### 3. Run

On Windows, starting it means double-clicking exactly one file:/n/n**`双击启动雨滴图.vbs`**

No second entry, nothing to choose between.

The server starts in the background and your browser opens `http://127.0.0.1:7788/` by itself.
There is **no console window** to leave open, and no "which one do I click" question — one entry only.

**If it fails:** a message box tells you why; the full traceback goes to `data/运行日志.txt`.

**macOS / Linux:**

```bash
chmod +x start.sh    # first time only
./start.sh
```

Your browser opens automatically. Keep the terminal window open — closing it stops the server.
(With the silent launcher there is no window; stop it via Task Manager → `pythonw.exe`.)

---

## Using it

![Drill down](docs/img/02-drill.png)

| Action | Result |
|---|---|
| **Click a bead** | Drill one level deeper |
| **Click the breadcrumb** | Jump back up to any level |
| **Hover** | Show name and value |
| **Drag a bead** | Move it aside to see what's behind — it springs back on release |
| **Right-click** | Menu: refresh / pin / expand / copy name / copy path / theme / add data source (CSV or folder) |
| **Esc** | Collapse all children |
| **Browser back / `Alt+←`** | Go up one level |
| **Search box** | Type a keyword, press Enter to jump to it. Searches **titles, file names and conversation text**; shows a snippet around the hit |
| **Top right 「树图 / 雨图」** | Switch between ring view and two-column tree view |

---

## Locking: hide items from AI, not from yourself

Every row on the left ends with two icons that do two different jobs:

| Icon | What it does | Tooltip |
|---|---|---|
| 👁 eye | Hide this level from the chart (**you** won't see it either) | 隐藏：在雨滴图中隐藏 |
| 🔒 lock | **Block AI from searching or touching it — you keep working as usual** | 锁定：禁止 AI 搜索和操作，但你可以正常使用 |

A lock behaves like a layer lock in Photoshop: the row is still there, the numbers are still there,
you can still open it — only the icon changes, into a **solid red** one. Nothing is duplicated, nothing is hidden.

- It only affects **outside readers**: the MCP interface and the search box can no longer see it.
  Your own view is untouched.
- The table header counts them: `已锁定 2 项 · 禁止 AI 读取`. The line is absent when nothing is locked.
- **Each item is locked separately**, one password prompt per item.
- Your password is **never stored**. Locally there is only a `salt` and a PBKDF2-SHA256 (600k rounds)
  `check` value — the file alone can't be reversed into the password.
- **Forgot it? Reset — and reset needs no password** (the key sits on your own disk). So "forget it and
  it's gone forever" would be a lie; this README won't tell it.

---

## Your own data

**Easiest:** drop a `.csv` into `data/sources/` (created on first run) and refresh the page. No restart needed. Delete the file and the bead disappears.

**Or:** right-click the canvas. Two entries at the bottom:

- **`新增数据源 · CSV`** — pick a `.csv`, it becomes one library
- **`新增数据源 · 文件夹`** — pick a folder; its **directory structure** (paths and file names only, not contents) is drawn as a tree

### CSV format

Three columns, header row required, **UTF-8 encoding**:

```csv
parent,name,value
,智能硬件,
,软件平台,
智能硬件,结构设计,42
智能硬件,电路板,38
软件平台,前端,45
软件平台,后端,52
```

- `parent` — the group this item belongs to. **Empty = top-level group**
- `name` — label shown on the bead
- `value` — determines bead size. Empty counts as 1

Need more levels? Write a path in `parent` with `/`:

```csv
parent,name,value
,Company,
Company/Engineering,Backend,50
Company/Engineering,Frontend,45
Company/Marketing,Ads,30
```

Intermediate levels are created automatically — you don't need to declare them.

See `examples/` for two ready-to-edit samples.

---

## Optional: read WorkBuddy sessions

If you have [WorkBuddy](https://www.workbuddy.cn/) installed, this reads its session logs (`~/.workbuddy/projects/*.jsonl`) and charts your conversations and tool calls. Useful for seeing where your steps actually went.

**Without WorkBuddy everything still works** — you just lose that one bead and use CSV instead.

Read-only. Nothing is written, modified, or uploaded.

---

## FAQ

**Filenames came out garbled after unzipping**

Some Windows built-in unzip tools ignore the UTF-8 flag on zip entries. Use **7-Zip** or
**Bandizip**, or just `git clone`.

**Double-clicking does nothing** — Check for a message box first; it usually says Python wasn't found.
If no box appears either, read the last lines of `data/运行日志.txt`. To see the output live,
run the equivalent yourself: `python app/live_server.py`.

**Blank page** — Check the terminal for errors. If the port is taken, the program auto-shifts (7788 → 7789 → …). **Use the address printed in the terminal**, not an old bookmark.

**Garbled Chinese** — Your CSV isn't UTF-8. In Excel use "CSV UTF-8 (Comma delimited)".

**Beads overlap** — Known limitation. Layout uses fixed placement, there's no general collision-avoidance algorithm. Splitting into smaller CSVs helps.

**Can I host this for others?** Don't. It reads local files, binds to `127.0.0.1`, and has no authentication. Share the CSV instead.

---

## Project layout

```
raindrop-chart/
├── 双击启动雨滴图.vbs       ← double-click this (Windows; the only entry, no window)
├── start.sh           ← macOS / Linux
├── 连接Buddy.vbs        ← double-click this too if you want AI to read the data
├── icon.ico           ← desktop icon (multi-size, transparent)
│
├── app/               [the software] replace this whole folder to upgrade
│   ├── live_server.py     Local server + parsing + HTTP API
│   ├── ring.html          Canvas page (styles and scripts inlined)
│   ├── datasource.py      Data source adapters: WorkBuddy / CSV / folder tree
│   ├── paths.py           Code-vs-data partition: which folder is which
│   ├── lockbox.py         Locking: password check value + which nodes are locked
│   ├── mcp_server.py      MCP interface (optional, see below)
│   ├── register_mcp.py    Implementation behind 连接Buddy.vbs (don't run directly)
│   ├── weights.py         Weight rules: how big a bead gets
│   ├── data.js            Offline demo data
│   └── examples/          Bundled sample CSVs (part of the software)
│
├── data/              [your data] created on first run; upgrades never touch it
│   └── sources/           Put your CSVs here (gitignored)
│
├── README.md / README.en.md      The pitch (three things, three steps)
├── LICENSE
└── docs/
    ├── DETAILS.md                This file
    └── img/                     Screenshots
```

**Why `app/` and `data/` are separate:** they have different lifespans. The program gets
replaced on every upgrade; your data must survive that. Split this way:

- upgrade → swap `app/` only, `data/` stays, your libraries are still there
- backup → copy `data/` only; it's the only place with your traces in it
- start over → delete `data/`, the next run recreates it empty
- share the program → ship `app/` alone; none of your data can leak out

The data folder can be relocated with the `RD_USER_DIR` environment variable.
The old flat layout (code and data in one folder) is still recognised, so upgrading
won't make the program lose your libraries.

- **Standard library only.** No `pip install`.
- `ring.html` inlines all CSS/JS; the only local dependency is `data.js`.
- Adding a data source = write a `Source` subclass and register it.
- Version number has a single source of truth: `VERSION` in `live_server.py`.

### MCP interface (optional)

Lets an AI read this workflow data directly. **Not needed for viewing the chart.**

#### This one needs a click — once

**Unzipping does not connect anything.** No AI client goes looking around your disk for a
folder; it needs a config entry saying which interpreter to run and which file to point at.
That step can't be skipped — but it only happens once.

Double-click **`连接Buddy.vbs`** (no console window; it shows one result dialog at the end):

1. Double-click it — wherever you unzipped the folder, **you never type a path**; the script
   works out its own absolute path
2. **Quit WorkBuddy completely and reopen it** (the tool list is read at session start only)
3. Open the connector page, find `raindrop-workflow`, click "Trust"

After that **you never click it again**: WorkBuddy starts the server itself from the config
(`app/live_server.py --mcp`). It works even if the web page is closed — only "jump the chart to
a level" requires the page to be open.

It checks which clients are installed and writes into **only those** — it never creates a
config for something that isn't there:

| Client | Config file |
|---|---|
| WorkBuddy | `~/.workbuddy/mcp.json` |
| Cursor | `~/.cursor/mcp.json` |
| Claude Desktop | Windows `%APPDATA%\Claude\claude_desktop_config.json`; macOS `~/Library/Application Support/Claude/claude_desktop_config.json` |

Each file gets exactly one merged entry (`raindrop-workflow`), every other server untouched.
It backs the file up first (`<config>.bak-<timestamp>`) and leaves it completely unchanged if
parsing fails. What got configured and what got skipped is written to `data/注册结果.txt`.

Add to `~/.workbuddy/mcp.json` (adjust the path):

```json
{
  "mcpServers": {
    "raindrop-workflow": {
      "type": "stdio",
      "command": "python",
      "args": ["<where you unzipped it>/raindrop-chart/app/live_server.py", "--mcp"],
      "timeout": 30000
    }
  }
}
```

Restart WorkBuddy, then find it in the connector page and click "Trust".

It exposes three tools:

| Tool | What it does |
|---|---|
| `workflow_tree` | Full workflow tree as JSON (conversation / space views), with trimming options |
| `workflow_report` | Text reports: overview, turns, slowest steps, file lineage, library stats, search… |
| `workflow_show` | Makes the chart jump to a given position — requires the page to be **open**, otherwise it says so plainly instead of pretending to succeed |

---

## Common misunderstandings

**"Install it and you see charts right away"** — true for CSV sources. For WorkBuddy session data you need WorkBuddy installed with existing sessions.

**"It can read my conversations"** — it reads **metadata** (when, which tool, which files), not conversation bodies.

**"What can the AI do with it"** — read data and navigate the chart. It cannot edit your files, send messages, or touch your accounts.

---

## Privacy

Your data does not leave your computer.

- The server binds to `127.0.0.1` only — not reachable from other machines on your network
- The entire codebase makes **exactly one** outbound connection: probing `127.0.0.1` to see if an instance is already running. No external domains
- No telemetry, no accounts, no uploads
- Session logs are opened read-only
- Cache (`data/_index.json`) and imported data (`data/sources/`) stay local and are gitignored

### What it stores locally

Besides the original session files (which are WorkBuddy's own), this program generates one
index file, `data/_index.json`, containing session titles, file paths, and **about 1.2 KB of body
text per session** (used for search).

| Stored | Why | Sensitivity |
|---|---|---|
| Session titles, timestamps | Displaying lists | Low |
| Names of touched files | Space view | Low |
| Body snippets (a few user prompts + assistant replies) | Body search | **Medium — may contain private text you wrote** |

**Never share this file.** The packaging script (`release/build_release.py`) has `_index.json`
in its exclusion list and re-scans the finished package to confirm it isn't there — but if you
copy the folder by hand, delete it first.

To clear the index: delete `data/_index.json`; it rebuilds on next start.

Verify it yourself:

```bash
grep -n "urlopen" live_server.py
```

One hit, pointing at `http://127.0.0.1:%d/api/ping`.

---

## License

[MIT](LICENSE)

---

## Known limitations

| Issue | Status |
|---|---|
| Beads can overlap with many items | Fixed placement, no general collision avoidance |
| Long names get truncated to `...` | Truncation without adaptive scaling |
| Only CSV and WorkBuddy sessions as sources | New types require code |
| No export | Screenshots only |
| Chinese UI only | No i18n yet |
| No persistent data-source dropdown | Switch sources by URL parameter, or import a CSV (which auto-switches) |
| Search covers titles and file names only | Conversation bodies are not indexed |
| At most 8 files kept per session | Index truncation to bound size; heavily-edited sessions may lose files |
| Circle sizes are not strictly comparable across views | Steps scale by duration, turns by step count, libraries by volume |
| AI-driven navigation requires the page to be open | `workflow_show` reports honestly instead of pretending |
