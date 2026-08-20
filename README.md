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
- **Sidebar** — sessions grouped by repo. The dot is the session's state:
  green spinner = running, grey = idle, amber = waiting on you, red = failed.
- **Center** — tabs. Opening a session opens its context breakdown; opening a
  file opens Monaco.
- **Right** — the agent log. Click any tool call to expand its input and result.
- **Bottom** — a real shell (`` Ctrl+` ``) rooted in the open session's repo.

To start a live session: pick the target repo in the dropdown at the bottom
right, type a prompt, hit **Run** (or `Cmd/Ctrl+Enter`).

Descant does **not** pass `--dangerously-skip-permissions`. When Claude Code
blocks a tool, you get an amber callout in the log saying what was blocked and
why, and the session goes to "needs you".

## Layout

```
backend/descant/     the brain — everything below is a thin client over it
  transcript.py      .jsonl parsing (format notes at the top of the file)
  inspector.py       context-cost analysis
  tokens.py          token estimation + the tool-schema size table
  runner.py          spawns and supervises live `claude` processes
  server.py          FastAPI + WebSockets
  cli.py             the terminal inspector
app/
  main.js            Electron main: supervises the backend, owns node-pty
  renderer/theme.css EVERY colour and spacing value in the app
  renderer/*.js      vanilla JS, no framework
fixtures/
  generate.py        regenerates the synthetic transcripts
  projects/          synthetic history in the real on-disk format
sandbox-repo/        throwaway repo that live runs execute against
```

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `DESCANT_PROJECTS_DIR` | bundled fixtures (app) / `~/.claude/projects` (CLI) | Transcript source |
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
