# Descant — progress

**Start here**, in this order:

1. `README.md` — the one command to run it.
2. `backend/descant/transcript.py` — the format notes at the top are the most
   load-bearing thing I learned tonight.
3. `backend/descant/inspector.py` — the analysis the whole app displays.
4. `app/renderer/theme.css` — every colour and spacing value, for the restyle.

Run `cd app && npm install && npm start`. It boots pointed at bundled fixtures
and shows 7 sessions across 3 repos.

---

## Status

| # | Item | State |
|---|------|-------|
| 1 | Context-cost inspector CLI | ✅ verified on real **and** fixture data |
| 2 | FastAPI `/sessions` + WebSockets | ✅ verified |
| 3 | Electron shell, session list | ✅ verified |
| 4 | Live session streaming | ✅ verified against real `claude` processes |
| 5 | Terminal (`node-pty` + `xterm.js`) | ✅ verified — real shell, real output |
| 6 | Monaco editor + diff | ✅ verified — diff opens from agent edits |
| 7 | Multiple concurrent sessions | ✅ verified — 3 simultaneous runs, correct routing |
| 8 | MCP → skill converter | ⛔ **not started** (stretch; deliberately skipped) |

Items 1–7 are done. Every "verified" above means I drove the real app headless
and looked at the result, not that the code compiles. Item 8 was left untouched
rather than half-built.

Beyond the list: **multi-turn conversations** (follow-up turns via `--resume`)
and a **40-check test suite** (`cd backend && python3 -m tests.test_descant`).

---

## Real vs mocked

| Thing | Real or mocked |
|---|---|
| Transcript parser | **Real.** Written against an actual `~/.claude/projects` transcript found in this sandbox, not from the brief. |
| `MEASURED USAGE` numbers | **Real.** Straight from the `usage` blocks Claude Code writes. |
| Assistant-side context rows | **Real.** Derived from measured output tokens. |
| User messages / tool results / injected context rows | **Estimated** (chars-per-token), anchored to measured totals. |
| Tool-schema token figures | **Estimated from a hand-written table** (`tokens.TOOL_SCHEMA_TOKENS`). The softest number in the app. |
| Live streaming | **Real.** Spawns `claude -p --output-format stream-json` and parses its actual output. |
| Permission handling | **Real.** Observed `system/permission_denied` in live runs, twice, and rendered it. |
| Terminal | **Real** `node-pty` shell. |
| Editor / diff | **Real** Monaco; diffs reconstructed from actual edit events. |
| `fixtures/projects/` | **Synthetic** (`fixtures/generate.py`), in the exact real format. |
| `sandbox-repo/` | Real files, deliberately trivial — a safe target for live runs. |

**Nothing in the UI is a hardcoded mock.** Every number on screen is computed
from a transcript or a live process.

---

## Decisions, and why

### Trusted the disk over the brief

The sandbox turned out to have a real `~/.claude/projects` (this session's own
transcript) *and* a working, authenticated `claude` CLI. So the parser and the
runner were both written against observed behaviour. Where the brief and reality
disagreed, reality won:

**Line types are much broader** than `user`/`assistant` — also `attachment`,
`last-prompt`, `atis-latch`, `queue-operation`, `summary`. Unknown types are
skipped as inert metadata, so a format change degrades to missing detail rather
than a crash. There's a test for this.

**One API request emits several assistant lines that all repeat the same
`usage` block.** Summing per line triple-counts your token usage. Deduped on
`requestId`. This is the single easiest thing to get wrong here, so it's the
first test in the suite.

**`attachment` lines are injected context** — skill listings, system reminders,
tool listings — that cost real tokens and that no UI I know of shows you. On the
real transcript they were **~11% of the whole window**. That's now a headline row.

**The live stream vocabulary is richer than described.** The CLI emits
`system/init`, `system/permission_denied`, `system/post_turn_summary` (with
`status_category` and `needs_action`), `system/task_summary`, `rate_limit_event`,
and a terminal `result` carrying `permission_denials` and real cost.
`post_turn_summary.needs_action` turned out to be a better "waiting on you"
signal than anything I'd have invented.

**The child inherits `CLAUDE_SESSION_ID` from the environment.** A run spawned
without an explicit `--session-id` wrote into *another session's* transcript — I
hit this while probing, and there are now two transcripts on this box sharing one
session id as evidence. `runner.py` always pins a fresh id and strips that
variable from the child env. Silent data corruption, avoided.

**`claude -p` answers once and exits.** So a multi-turn conversation is a
*series* of processes stitched together by `--resume`. The `Run` object is what
makes that read as one thread instead of N sidebar entries.

### Token estimation, and how honest it is

`tiktoken` installs but can't fetch its BPE files through the sandbox proxy, and
it'd be the wrong vocabulary for Claude anyway. So: chars-per-token ratios (3.8
prose / 3.1 JSON-and-code, in `config.py`), calibrated against measured `usage`
deltas in the real transcript.

**The report never leans on the heuristic where a real number exists.** Two
anchors do most of the work:

- **The preamble is measured by difference.** At a session's first request the
  window holds system prompt + tool schemas + exactly one user turn. The total is
  measured and the turn is estimable, so `preamble = first_request_tokens −
  estimate(first turn)` is a real number. Only its *split* into system-prompt vs
  tool-schemas uses the soft table, and only tools the session actually called
  can be counted — schemas loaded but never used fold into the system-prompt
  line. The UI labels the preamble `measured` or `estimated` accordingly.
- **Assistant-side rows come from measured output tokens.** Thinking is stored as
  a redacted stub, so estimating it from characters undercounted badly. Thinking
  now uses the exact `thinking_tokens`; remaining output is apportioned across
  replies and tool-call inputs by character ratio.

**Measured drift against the real window: −7%** (was −23% before the output-token
work). The report states its own drift in NOTES rather than hiding it.

### Other choices

**Repo path comes from `cwd`, not the directory name.** The encoding
(`-Users-ayan-code-harbor`) replaces `/` with `-` and is ambiguous. Every
conversation line carries an exact `cwd`, so that wins. The fallback decoder
disambiguates against the filesystem (longest existing joining wins) — writing a
test caught it reading `-home-user-descant-ide` as `/home/user/descant/ide`.

**One event shape for live and historical.** `events.py` normalises a parsed
`.jsonl` line and a live `stream-json` line into the same wire format, so the
agent panel has exactly one renderer. `WS /ws/replay/{id}` plays history through
the live path — a free way to demo streaming without burning tokens.

**No database, no frontend framework.** Transcripts parse on demand as
instructed; the server has an mtime+size parse cache. The frontend is vanilla JS
in five files. I never hit a wall that justified more.

---

## Pointing at your real `~/.claude/projects`

Zero code changes:

```bash
cd app && DESCANT_PROJECTS_DIR=~/.claude/projects npm start
```

It's read in one place (`config.projects_dir()`) and already defaults to
`~/.claude/projects` — the Electron app just overrides that to fixtures so a
fresh clone shows something. The CLI already reads your real history with no env
var at all.

**Three caveats before you do:**

1. **Scale.** Fixtures are 7 small sessions. A year of real history is thousands
   of transcripts, some tens of MB. The mtime cache means you pay the parse cost
   once per file per change, but the *first* load still walks everything — expect
   a slow first paint. If it's bad, the fix is to read only each file's head and
   tail for the list view and parse fully on click. ~30 lines, not done.
2. **Sidechains.** Subagent (`isSidechain: true`) events are parsed and counted
   but not visually separated. On a transcript with heavy subagent use the agent
   panel will interleave them confusingly. The data to fix it is already on every
   event (`meta.sidechain`).
3. **Duplicate session ids are possible** (see the `CLAUDE_SESSION_ID` note
   above) — two transcripts in different repos can share one id. The CLI reports
   the ambiguity and asks you to narrow with `--repo`; the app keys on
   `session_id` alone and would show the first match. Rare, cosmetic.

None is a blocker; all three are stated so they aren't a surprise.

---

## Restyling — where the theme lives

**`app/renderer/theme.css`.** Every colour, spacing step, font stack, radius and
layout metric is a CSS custom property in that one file. `app.css` references
those variables exclusively — a literal hex code or pixel value anywhere else in
the CSS is a bug.

Two things that make the swap cheaper than it looks:

- **The xterm terminal theme is read out of those same variables at runtime**
  (`term.js` → `themeFromCss()`).
- **So is the Monaco theme** (`editor.js` → `defineTheme()`).

So neither the terminal nor the editor carries its own palette; both restyle
with everything else.

Current pass is deliberately plain: VS Code Dark Modern lineage, greys carrying
the UI, one blue (`--accent: #3B82F6`) used only for selection, focus and the
active-tab underline. Status dots are `--status-running` / `--status-idle` /
`--status-needs-input` / `--status-failed`. The only animation is the running
spinner (`--spinner-duration`).

---

## Known gaps and rough edges

- **Item 8 not started.** Deliberate — 1–7 first, as instructed.
- **No file watching.** The sidebar polls `/api/runs` every 2.5s for live status,
  but only re-reads transcripts on **Reload**. A live session's own transcript
  won't appear in the session list until you hit that button.
- **Center panel doesn't follow live runs.** Selecting a run updates the agent
  panel but leaves whatever tab was open in the center.
- **Permission blocks are surfaced, not actionable.** You get the amber callout
  explaining what was blocked; approving still means editing your Claude Code
  settings or re-prompting. Interactive approval needs
  `--input-format stream-json` and a permission-prompt tool, which is a real
  chunk of work, not a polish item.
- **The explorer is a flat file list**, not a tree. Fine for small repos, poor
  for large ones.
- **Editor is read-write but there is no save.** Edits in Monaco are not
  persisted; it's a viewer today.
- **CSP warning on boot** is expected — Monaco needs `unsafe-eval`.
- Electron `console-message` deprecation warning on newer Electron; harmless.

Nothing is left mid-edit. Every gap above is a thing not built, not a thing
half-built.

---

## Verifying without a screen

The whole app was developed headless. `DESCANT_SCREENSHOT=<path>` grabs the
window and exits; `DESCANT_DRIVE=<file.js>` runs an expression in the renderer
first, so a whole flow can be photographed:

```bash
cd app && DESCANT_SCREENSHOT=/tmp/shot.png DESCANT_SCREENSHOT_DELAY=8000 \
  DESCANT_DRIVE=/tmp/drive.js \
  xvfb-run -a ./node_modules/.bin/electron . --no-sandbox
```

That's how the live-run, permission-block, terminal and diff paths were all
confirmed working.

```bash
cd backend && python3 -m tests.test_descant     # 42 checks, no pytest needed
```
