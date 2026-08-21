# IDEAS

## The problem

Claude Code carries every tool you have attached in every message you send. You
use maybe five of them. You pay for all ninety, every time.

You cannot fix that today because the off switch works **per server**, and
turning a server off means losing it. So everyone leaves everything on.

## The product

Keep five tools loaded. Make the other eighty-five **findable** instead of
loaded. Nothing is lost — you just stop paying rent on what you are not using.

## What it is

**A cache for MCP tools, keyed by repo.**

That is the whole design, and the vocabulary is worth using literally because it
answers the open questions for us:

| Cache term | Descant |
|---|---|
| Cache key | the repo |
| Working set / resident | the tools kept loaded, in context |
| Cache miss | `find_tool` → one round trip to reach a tool that isn't loaded |
| Backing store | the real MCP servers, spawned lazily |
| Eviction policy | *which five* — currently hand-picked |
| Hit rate | the metric that says whether any of this worked |

The repo is the right key because a repo's work is repetitive. The same handful
of tools come up again and again, and that set is stable enough to cache and
small enough to be worth caching.

**Everything else is extra.**

## What follows from taking the cache framing seriously

1. **Caches warm themselves.** Picking your five by hand is a cold cache you
   fill in manually. Transcript history already knows which tools this repo
   actually calls — seed the working set from it.
2. **Caches measure hit rate.** Log misses (`find_tool` reached for something)
   and dead residents (loaded, never called). That is the number that says
   whether the trade paid, and it needs real usage to produce, so nobody can
   fake it.
3. **Caches evict.** Once 1 and 2 exist, the set maintains itself. Not before —
   silently changing what is available would break the trust the whole thing
   runs on.

## The one way this differs from a real cache

A CPU cache miss is automatic. Here, a miss depends on the model *deciding* to
search — and if `find_tool` doesn't surface the right tool, it may as well not
exist. **Recall is the failure mode, not size.** It is currently unmeasured, and
it is the first thing to test.

## The cost

The proxy adds two tools of its own: **261 tokens, flat**, regardless of how many
servers it fronts. What it replaces grows linearly with your tool count. So it
is a loss only when there is almost nothing to front, and a rout at ninety tools.

Treat 261 as a fixed budget, not a starting point. Every future meta tool comes
out of it.

## Next

1. Warm the cache from transcript history — stop making people pick by hand.
2. Measure hit rate and misses.
3. Then, and only then, evict automatically.
