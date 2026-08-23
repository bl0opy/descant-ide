# IDEAS

## The thesis, corrected

The old version of this doc argued Descant was **a cache for MCP tools** — keep
five loaded, make the other eighty-five findable, sell the token saving.

That thesis is dead, and it is worth being explicit about why: **compression is
not novel and it is not ours.** Anthropic ships Tool Search (lazy schema
loading, on by default) and pushes code-execution patterns that cut MCP overhead
by ~98%. Every month we spend on "we shrink your MCP tokens" is a month the
ground moves under us. See `DIRECTION.md` for the sourced version of this.

So: **token reduction is table stakes, not the product.**

## The actual gap

Nobody knows what happens when you run agents **concurrently, across repos, over
time.**

`/cost` and `/usage` are per-session, per-machine. `/context` overstates MCP cost
(it double-counts per-tool system-prompt overhead). Nothing tracks cache-hit rate,
nothing warns you before an action invalidates your cached prefix, nothing
aggregates five sessions across five repos into one picture, and nothing notices
when two agents are about to write the same file.

That is not a capability gap — the models are fine and the tools are fine. It is
a **visibility and coordination gap**, and it is structural: usage-priced vendors
have no reason to help you spend less, and Anthropic has no reason to build
cross-account or cross-vendor tooling.

## Why Descant, specifically

Descant is already the only thing in the room that reads **every transcript,
across every repo, while sessions are live.** That was built as plumbing. It is
actually the asset.

- The tailer attaches to transcripts on disk, so it sees sessions started
  *outside* the app — any `claude` in any terminal.
- The inspector already decomposes one window into system prompt / tool schemas /
  MCP schemas / skill listings / conversation, from real side-channel data rather
  than estimates.
- The proxy sits in the tool path for every call the agent makes.

Three sources of ground truth, already wired. The product is what you build on
top of them.

## What this does to the proxy

The proxy stays. Its **justification** changes.

It was the product; it is now **the instrumented path**. Because every call goes
through `find_tool` / `call_tool`, the proxy is the only place that can answer:
which tools this repo actually calls, which resident tools are dead weight, how
often a miss happens, and what a given MCP server genuinely costs. Those are
measurements nothing else can produce.

The token saving is a side effect we still take. We just stop leading with it.

The old cache vocabulary survives as **mechanism**, not as pitch:

| Cache term | Descant |
|---|---|
| Cache key | the repo |
| Resident set | the tools kept loaded |
| Miss | `find_tool` — one round trip instead of a permanent seat |
| Hit rate | the number that says whether the trade paid |

And the failure mode is unchanged: **recall, not size.** A miss here depends on
the model *deciding* to search. If `find_tool` doesn't surface the right tool, it
may as well not exist. Still unmeasured. Still the first thing to test — but now
it is one measurement among several, not the whole bet.

## What to build, in order

1. **Cross-session, cross-repo cost view.** Live: tokens, dollars, cache-hit
   rate — per session, per repo, per MCP server. More accurate than `/context`,
   because we measure schemas instead of guessing them. This is the wedge and it
   is native to how Descant already works.
2. **Cache-safety guardrails.** Warn *before* the MCP toggle or model switch
   that invalidates the cached prefix; alert on hit-rate drift. Anthropic
   silently cut cache TTL in March 2026 and it was caught by developers
   hand-parsing JSONL — exactly the file we already parse.
3. **Cross-session coordination.** Concurrent agents on related repos collide:
   conflicting writes, duplicate CI. We already know which sessions are live and
   which repos they hold. Start with detection, not arbitration.
4. **Team rollups and audit trails.** Cross-org and compliance-shaped — the
   least likely thing for Anthropic to want to build, so the most durable moat.
   EU AI Act high-risk logging obligations land Aug 2, 2026.

**Explicitly deprioritized:** MCP token reduction as a headline. Keep the
mechanism, drop the pitch.

## What has to be true for this to work

- **Our numbers must be better than `/context`'s**, and visibly so. The whole
  proposition is "you were optimizing blind." One wrong number and it's gone.
- **The fleet view has to be worth opening when nothing is wrong.** A dashboard
  you only check after a surprise bill is a postmortem tool, not a product. #2
  and #3 are what make it a thing you leave open.
- **Platform risk is real.** Anthropic previewed an "agent view" in May 2026. If
  they close #1, #4 is the fallback. Move fast on #1–2.

## Open question

None of the above is validated against a user. `DIRECTION.md` is market
positioning, not a product spec. The next honest step is to instrument #1
against **our own** concurrent sessions and see whether the picture it produces
changes a decision we'd otherwise have made.
