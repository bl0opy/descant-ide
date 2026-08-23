# Descant

A desktop IDE built around Claude Code, for people who run more than one agent
at a time.

Claude Code tells you about the session in front of you. `/cost` is per-session,
`/context` estimates what MCP costs you, and neither has anything to say about
the other four sessions you have running across three repos. Descant reads the
transcripts Claude Code already writes — every repo, live, including sessions
you started in some other terminal — and measures what they're actually
spending: system prompt, tool schemas, MCP schemas, skill listings,
conversation. Measured by spawning the servers and counting, not estimated.

Today that's per-session and honest about it. The direction is the fleet view —
see `IDEAS.md` for what's built versus what that thesis still owes.

## Run it

```bash
./bin/descant
```

Or put it on your `PATH` once and forget where the repo lives:

```bash
ln -s "$PWD/bin/descant" ~/.local/bin/descant
descant            # fixtures
descant --real     # your real history in ~/.claude/projects
descant <dir>      # any projects directory
```

The script installs the node and Python dependencies on first run and pins the
backend to the repo's `.venv`. The manual equivalent is still:

```bash
cd app && npm install && npm start
```

That's it. Electron starts the Python backend itself; you don't need a second
terminal. It ships pointed at the bundled fixtures, so you'll see six sessions
across three repos on first launch.

**Prerequisites:** Node 18+, Python 3.10+, and `pip install --break-system-packages -r backend/requirements.txt`
(FastAPI + uvicorn). The `claude` CLI needs to be on `PATH` for live runs;
everything else works without it.

### Point it at your real history

```bash
descant --real
# or: cd app && DESCANT_PROJECTS_DIR=~/.claude/projects npm start
```

No code changes needed — that's the only knob. See `PROGRESS.md` for the two
caveats worth knowing before you do.

## The context inspector, without the app

Same analysis, in the terminal:

```bash
cd backend
DESCANT_PROJECTS_DIR=../fixtures/projects python3 -m descant.cli list
DESCANT_PROJECTS_DIR=../fixtures/projects python3 -m descant.cli show 93c9da93
```

Drop `DESCANT_PROJECTS_DIR` to read your real `~/.claude/projects`. Add `--json`
to either for machine-readable output.

## What you're looking at

- **Left rail** — sessions, file explorer, terminal toggle.
- **Sidebar** — sessions grouped by repo. **New** starts a fresh conversation;
  hovering a session reveals a delete button (its transcript is the only record,
  so it asks first). The **Explorer** view is a real folder tree with filetype
  icons. **+ File** / **+ Folder** create at the root, and hovering any folder
  reveals its own **+** buttons; names are typed inline in the tree, so you can
  see where the thing will land. The dot is the session's state:
  green spinner = running, grey = idle, amber = waiting on you, red = failed.
- **Center** — tabs. Opening a session opens its context breakdown; opening a
  file opens Monaco.
- **Right** — the chat. You talk to Claude Code here; opening a session from the
  sidebar swaps it for that session's transcript, and **Back to chat** returns.
  Click any tool call to expand its input and result.
- **Bottom** — a real shell (`` Ctrl+` ``) rooted in the open session's repo.
  This is where live sessions actually run.

### Parked panels (MCP loadout, residency, mining, library)

Four panels — tool loadout, tool residency (the MCP proxy), mined workflows and
the capability library — are **commented out of the activity bar** as of the
thesis realignment. They are built, tested and working; the bet moved to
cross-session visibility, so they are parked rather than deleted. Everything
behind them is untouched, and restoring them is three lines in
`app/renderer/app.js` (search for `PARKED`).

Their original documentation is kept verbatim in `docs/parked-mcp-panels.md`.

## Chat

**The agent panel on the right is the conversation** — Claude Code inside the app — not a terminal hosting the CLI, and not a transcript you
watch after the fact. You type, the reply streams in, tool calls appear inline
and expand, and the conversation keeps its context across turns.

**Permissions are answered here.** When a tool needs approval the chat shows a
card naming exactly what was asked for, with the rule it would grant:

| Button | What it does |
|---|---|
| **Allow once** | Grants that rule for this conversation and picks up where Claude stopped. |
| **Always allow** | Also writes the rule to `<repo>/.claude/settings.local.json`, so next session starts knowing. |
| **Deny** | Declines; the conversation continues without it. |

Rules are as narrow as the call: approving one `git status` grants
`Bash(git status:*)`, not all of Bash. The dropdown in the chat header picks the
posture up front — ask about everything, auto-accept edits, or plan-only (read
and think, change nothing).

Under the hood a chat is one long-lived
`claude -p --input-format stream-json --output-format stream-json`. Because
headless Claude Code cannot be asked "may I?" mid-stream, approving restarts the
process with `--resume <session-id> --allowedTools <rule>` — same session, one
more thing permitted, nothing lost but a second. `--dangerously-skip-permissions`
is still never passed; every approval is a specific rule a person clicked.

The backend follows the app down, so quitting never leaves a `claude` process
running with no window to show for it.

**Send** talks to Claude (`Cmd/Ctrl+Enter` in the composer). **Run**, beside it,
does something deliberately different: it opens the terminal and executes what
you typed as a shell command, verbatim.

## Editing and saving

The centre pane is Monaco, and it writes back: edit a file, `Cmd/Ctrl+S` saves
it. A tab with unsaved changes carries an amber dot and asks before closing.
Saves go through a temp file in the same directory and are moved into place, so
an interrupted write cannot leave a half-written source file behind.

Each open file keeps its own live Monaco model, so switching tabs preserves the
buffer, its undo history and its cursor — the tab bar never re-seeds the editor
from a stale snapshot. A file too large to read in full opens read-only and
refuses to save, because writing back a partial buffer would delete the rest.

**Auto-save** is in Settings, off by default — *after a pause in typing* (with a
configurable delay) or *when the editor loses focus*. Off is the default on
purpose: Descant sits alongside agents reading and writing the same files, and a
buffer that saves itself mid-edit is a surprise. Autosaves are silent; a failed
save always says so.

## Running the open file

The **▶ arrow in the title bar** runs whatever the editor is showing —
`Cmd/Ctrl+Enter` does the same. Hover it to see the exact command first.

Shortcuts are real menu accelerators (**Run** and **File** in the menu bar), not
renderer key handlers: Monaco and xterm both swallow keystrokes before the page
sees them, so a binding made in the page would work everywhere *except* inside a
file, which is where you need it. In the composer `Cmd/Ctrl+Enter` still means
send.

The interpreter comes from the *repo*, not a fixed table: a project with a
`.venv` gets its own python rather than whatever is first on `PATH`, and a Rust
file inside a crate is built with `cargo run` from the crate root instead of
being compiled alone. The command is typed into the terminal rather than run in a
hidden subprocess, so you can see it, edit it, and re-run it. A file type with no
runner leaves the arrow disabled and says why.

## Settings

The gear at the bottom of the activity bar. Everything in it is wired to
something real — default permission mode for new chats, auto-save, editor and
terminal font size, whether the explorer shows dotfiles, and whether MCP servers
are probed
(measuring costs a process per server, so it can be turned off). It also lists
the environment variables actually in effect, and can reset pane sizes or close
every live conversation at once.

## Right-click menus

Two-finger tap (or right-click) gets a menu wherever one is useful:

| Where | What you get |
|---|---|
| **File tree** | Open / Run, New File / New Folder on a folder, Copy Path, Copy Name, Reveal in Terminal, Delete |
| **Session row** | Open, Copy Session ID, Delete Session |
| **Tab** | Close, Close Others, Close All |
| **Editor** | Save, Run File, Copy Path |
| **Terminal** | Paste, Clear |

## Panes

Every boundary is draggable — sidebar, agent panel, and the terminal's height.
Sizes persist across launches; double-click a splitter to reset it.

**Run opens `claude` in the terminal panel** — the real interactive CLI, not a
headless subprocess. That means permission prompts appear in the terminal and
you answer them there, normally. **Stop** sends Ctrl-C to that shell.

The agent panel follows along by tailing the transcript Claude Code writes, so
it shows the conversation as a readable log while the terminal shows the CLI
itself. Because it reads transcripts rather than owning a process, it also picks
up sessions you started yourself in any terminal — just point it at the repo.

Descant never passes `--dangerously-skip-permissions`.

## Layout

```
backend/descant/     the brain — everything below is a thin client over it
  transcript.py      .jsonl parsing (format notes at the top of the file)
  inspector.py       context-cost analysis
  tokens.py          token estimation + the tool-schema size table
  chat.py            the two-way conversation: one live `claude` per chat
  tailer.py          follows a live session by watching its transcript
  runner.py          headless `claude -p` runner (kept for the HTTP API)
  server.py          FastAPI + WebSockets
  cli.py             the terminal inspector
app/
  main.js            Electron main: supervises the backend, owns node-pty
  renderer/theme.css EVERY colour and spacing value in the app
  renderer/chat.js   the chat controller — streaming, and the approval cards
  renderer/fileicons.js  Catppuccin-palette filetype icons
  renderer/resize.js     draggable splitters
  renderer/contextmenu.js  right-click menus
  renderer/settings.js     settings, persisted locally
  renderer/*.js      vanilla JS, no framework
fixtures/
  generate.py        regenerates the synthetic transcripts
  projects/          synthetic history in the real on-disk format
sandbox-repo/        throwaway repo that live runs execute against
```

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `DESCANT_PROJECTS_DIR` | fixtures **+** `~/.claude/projects` (app) / `~/.claude/projects` (CLI) | Transcript sources; accepts a `:`-separated list |
| `DESCANT_LIVE_PROJECTS_DIR` | `~/.claude/projects` | Where the tailer looks for live sessions |
| `DESCANT_CLAUDE_BIN` | `claude` | Which CLI to spawn |
| `DESCANT_PORT` | `8787` | Backend port |
| `DESCANT_NO_SPAWN` | unset | Don't start the backend; attach to a running one |
| `DESCANT_DEBUG` | unset | Mirror backend + renderer logs to stdout |
| `DESCANT_SCREENSHOT` | unset | Grab the window to this path and exit (headless checks) |

## Developing

Run the backend on its own and point the app at it:

```bash
cd backend && DESCANT_PROJECTS_DIR=../fixtures/projects python3 -m descant.server
cd app && npm run start:nobackend
```

Regenerate fixtures with `python3 fixtures/generate.py` (deterministic).

Run the tests with `cd backend && python3 -m tests.test_descant`.

**If the terminal panel will not start:** node-pty ships a `spawn-helper` binary
and npm drops its executable bit on extraction, which surfaces only as a bare
`posix_spawnp failed.`. `npm install` now repairs it via a postinstall hook; run
`npm run fix-pty` if you ever hit it by hand.
