# IDEAS

Working doc. Not a roadmap — a place to argue about what Descant is *for*
before building more of it. `PROGRESS.md` says what exists; this says what
should.

---

## The thesis

**Descant is the context layer for Claude Code.**

Not an IDE that happens to show token counts. The IDE is the *housing*. The
product is: *you cannot manage what you cannot see, and nothing shows you what
is actually in the window.*

Everything already built lines up behind that sentence:

| Feature | What it really is |
|---|---|
| Context inspector | **See** where the window went |
| Loadout panel | **See** what each attachment costs per turn |
| MCP → skill converter | **Shrink** one attachment |
| Skill mining | **Create** an attachment that pays for itself |
| Capability library | **Find** an attachment across repos |
| Tool residency proxy | **Control** context at tool granularity |

The proxy is the one that changes the category. The other five work *around*
Claude Code's per-server switch. The proxy **replaces the switch**. That is the
thing nobody else has, and it is what the next six months should be about.

### The one-line pitch

> Claude Code lets you turn a server on or off. Descant lets you decide, per
> tool, what is worth a seat in every single request — and makes everything
> else one round trip away instead of gone.

### What we are explicitly *not*

- Not a Cursor/VS Code competitor. Monaco is there so you can look at a file
  the agent touched, not so you write code in it all day.
- Not an MCP registry / marketplace. The library indexes what *you already
  have*; discovery of servers you don't have is someone else's problem.
- Not a Claude Code replacement or a fork. Descant reads what the CLI writes and
  writes what the CLI reads. The day it needs a patched CLI, it has lost.
- Not an agent framework. No orchestration, no swarms. One CLI, seen clearly.

---

## The wedge, stated precisely

Three facts, all of them verified in this repo, that together are the whole
opportunity:

1. **Cost is not spread evenly across a server.** It is three fat schemas out
   of twenty tools. (`proxy.py` module docstring.)
2. **Claude Code's only lever is per-server.** Permission rules stop a tool
   being *called*, not its schema being *loaded*.
3. **Injected context is ~11–18% of a real window and invisible in every other
   UI.** (`PROGRESS.md`, measured on real transcripts.)

So: the unit of cost is the *tool*, the unit of control is the *server*, and
most of the bill is in a category nobody renders. Descant closes all three gaps.

That is the moat. Guard it. Every idea below gets judged on whether it deepens
that or dilutes it.

---

## The honest problem to solve next

From `PROGRESS.md`, stated plainly:

> The two meta tools cost ~260 tokens that never go away, so proxying a small
> server is a loss.

**The proxy has a floor, and the floor is currently hand-managed.** The panel
prints the number and makes the human do the math. That is fine for a demo and
wrong for a product. Nearly every strong idea below is a variation on *making
the residency decision itself automatic and evidence-based*.

Related open question worth settling early: `find_tool` is BM25 + synonyms, same
as the library. When it misses, the model doesn't know the tool exists. **A
residency system is only as good as its recall.** Measuring that miss rate is
probably the single highest-value experiment available.

---

## Ideas, bucketed

Scored on **moat** (does it deepen the wedge?) and **cost**. `→` marks my
recommendation.

### A. Residency intelligence — *the core bet*

→ **A1. Suggested groups from transcript evidence.** Mining already knows which
tools you actually call, per repo, from real history. Feed that into residency:
"these 6 tools cover 94% of your calls in this repo; the other 31 were called 4
times total." Turns a checkbox grid into a recommendation with a number behind
it. **Moat: high. Cost: low — both halves already exist.** This is the next
thing to build.

→ **A2. Regret accounting.** After install, log every `find_tool`/`call_tool`
miss. A miss is a tool that should probably be resident; a resident tool never
called for two weeks should probably be evicted. Show *net* tokens saved
against the real counterfactual, not the estimate. **Moat: very high** — this is
the number no competitor can fake, because it requires having been in the loop.
**Cost: medium.**

**A3. Task-shaped groups.** Groups keyed to intent rather than repo —
"debugging", "release", "data pull" — switchable from the panel or by a phrase
in the prompt. Attractive, but adds a mode the user must remember to set.
Weaker than A1/A2, which infer. **Defer.**

**A4. Auto-eviction / self-tuning residency.** The endgame of A1+A2: the group
maintains itself. Do **not** build before A2 exists — silently changing what
tools are available is exactly the kind of magic that erodes trust in a tool
whose whole premise is *showing* you things.

**A5. Progressive schema disclosure.** Resident tools carry a *truncated*
schema; full schema on demand via a meta tool. Halves the cost of residency
itself. Real risk: a model that fills in a truncated schema wrongly and fails a
call. **Prototype and measure before believing it.**

### B. Making the invisible visible — *the original strength*

→ **B1. Context budget over time.** The inspector is per-session and static.
A repo-level view — "your preamble grew 4k tokens this month, here's the commit
where `.mcp.json` changed" — turns a diagnostic into a habit. **Moat: medium.
Cost: medium.** Strong second priority.

→ **B2. Fix the softest number.** `tokens.TOOL_SCHEMA_TOKENS` is a hand-written
table and `PROGRESS.md` already calls it "the softest number in the app." The
proxy spawns servers and reads real schemas — measure tool schemas the same way
and delete the table. **Moat: low, credibility: high, cost: low.** Do it.

**B3. Preamble diff between two sessions.** "Why did this session cost 30% more
than that one?" Cheap, genuinely useful, no new subsystem.

**B4. Sidechain separation.** Already a known gap; the data (`meta.sidechain`)
is on every event. Subagents are where context goes to die and nobody renders
them separately. Small, and it compounds with B1.

### C. Cross-repo / team

**C1. Shareable loadouts.** Commit a residency group; a teammate gets the same
window. Natural extension of the existing scope discipline (Descant never
rewrites a checked-in `.mcp.json`).

**C2. Org-level capability library.** The index is already cross-repo. Making it
cross-*person* is where a paid tier would live.

**C3. Cost attribution per repo/person.** Reporting. Real demand, but it drifts
toward "dashboard company" and away from the wedge. **Low priority.**

### D. Adjacent, and mostly traps

**D1. LSP bridge.** `PROGRESS.md` calls it "a project of its own." It is, and
it's someone else's. **No.**

**D2. Support other agents (Cursor, Codex, Copilot).** Tempting — the proxy is
protocol-level and mostly portable. But the inspector is built on Claude Code's
transcript format, which is the actual differentiator. Breadth here costs the
depth that makes it good. **Not until the Claude Code story is undeniable.**

**D3. Embeddings for the library.** Already scoped: swap `score_query()`, change
nothing else. Worth doing *when* A2 shows recall is the bottleneck — that's the
evidence that turns a nice-to-have into a fix. Tie it to `find_tool`, not just
the library.

**D4. Prompt/context optimization suggestions (rewrite your CLAUDE.md).** Feels
adjacent and is not. It's an LLM feature in a product whose credibility comes
from being deterministic, measured and offline. **No.**

---

## What "best in the world at" means, concretely

If Descant is doing its job, these are true and nowhere else is:

1. You can answer **"what is in my context window right now, and what did each
   part cost me?"** — down to the individual tool schema.
2. You can **evict a single tool** without losing access to it.
3. You can see the **net** effect of that decision against what actually
   happened, not an estimate.
4. Everything above is **measured, offline and deterministic** — no model in the
   loop deciding what your context should be.

Point 4 is the one to defend hardest. It's the reason the numbers are
believable, and it is the opposite of what a competitor rushing this would do.

---

## Ordered next moves

1. **A1** — evidence-backed suggested groups. Highest moat per hour; both halves
   already exist.
2. **B2** — measure tool schemas, delete the estimate table. Cheap, and it makes
   every other number more defensible.
3. **A2** — regret accounting. The number nobody else can produce.
4. **B1** — context budget over time.
5. Then re-read this doc before touching anything in C or D.

---

## Open questions

- What is `find_tool`'s actual miss rate on real tool names? Everything in
  bucket A rests on it, and it is currently unmeasured.
- Is the ~260-token meta-tool floor reducible? One merged meta tool instead of
  two, or a terser schema, changes the break-even from ~forty-tool servers to
  something much more common.
- Does residency change *model behaviour* beyond cost — does a smaller toolset
  make it choose better? Plausible, unproven, and if true it's a bigger story
  than tokens.
- Who is this for, exactly: the individual power user drowning in MCP servers,
  or the team lead who owns the bill? A1/A2 serve the first, C1/C2 the second.
  **Unresolved, and it decides the next six months.**
