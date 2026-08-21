# Descant

A desktop IDE built around Claude Code. It reads the session transcripts Claude
Code already writes, shows you where each session's context window actually
went, and lets you start and watch live sessions.

## Run it

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
cd app && DESCANT_PROJECTS_DIR=~/.claude/projects npm start
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

### The four extra panels (activity bar, left edge)

| Panel | What it does |
|---|---|
| **Tool loadout** | Every MCP server and skill attached to the repo, with what each costs **on every turn**. Toggle them off; convert an MCP server into a thin skill (the preview shows the saving before anything is written). Add servers here too — see below. |
| **Mined workflows** | Repeated tool sequences found in your transcript history, with the real commands and a ready `SKILL.md`. Sequences seen across *multiple sessions* rank highest. Name the skill and pick who gets it before writing. |
| **Capability library** | Search every skill and MCP tool across all repos. Ranking is BM25 + synonyms — lexical, not embeddings; the panel says so. |
| **Session inspector** | Click any session: where its context went, split by system prompt / tool schemas / MCP schemas / skill listings / conversation. |

MCP servers are **measured, not estimated** — Descant spawns each one and asks
it over JSON-RPC what tools it exposes, then counts the schemas.

### Attaching MCP servers to one repo

**Tool loadout → Add an MCP server to this repo.** Paste the command the way you
would type it (`uvx mcp-server-weather --verbose`) or an HTTP URL; Descant splits
argv for you. Two things the form does that hand-editing JSON does not:

- **Test connection** probes the server before it is attached, so you see its
  tool count and its per-turn token cost *first*. A forty-tool server is a
  decision, not a checkbox.
- **Scope** is an explicit choice, not an accident of which file you opened:
  *just me* writes to your per-project config in `~/.claude.json` (private to
  this machine — where anything carrying a credential belongs), *the team*
  writes `<repo>/.mcp.json` and enables it for you, because a `.mcp.json` server
  is opt-in per project and a save without that would appear to do nothing.

**Remove** deletes a definition this repo owns. Global servers can be detached
with the toggle but not deleted from here — they belong to every repo.

### Keeping skills organised, agent to agent

A mined workflow is only worth capturing if the next agent finds it, and *where*
it is written decides which agents those are. Every skill-writing action takes a
scope:

- **this repo** → `<repo>/.claude/skills/` — this project's agents.
- **every repo** → `~/.claude/skills/` — every agent on this machine.

So a workflow discovered in one project stops being that project's private lore.
The **Capability library** carries the same move for skills that already exist:
**Copy** promotes one to every repo, or hands it to a specific repo. Copies take
the whole skill directory, not just `SKILL.md` — a skill that shells out to a
script it ships with is useless without the script. Name collisions ask before
replacing, never silently.

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
