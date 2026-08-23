# Descant — Product Direction

**Core thesis:** The AI coding-agent market has strong models and strong capability tools, but no one is tracking what actually happens once you run agents concurrently, at scale, or across a team. That's not a capability gap — it's a **visibility and coordination gap**. Descant's native architecture (many concurrent Claude Code sessions across repos) sits exactly where this gap lives. We build there, not on token compression — Anthropic is already commoditizing that.

---

## Why not token compression

Anthropic already ships Tool Search (lazy MCP schema loading, on by default) and pushes code-execution patterns that cut MCP overhead ~98.7% (150K → 2K tokens on a Google Drive → Salesforce workflow). If Descant's pitch is "we shrink your MCP tokens," that ground disappears under us as Anthropic's own defaults improve. Reference: Anthropic engineering blog, "Code execution with MCP" (Nov 2025); GitHub `anthropics/claude-code` issues #18298, #44536 (Tool Search auto-enable gaps — still imperfect, but closing).

**Decision: treat compression as table stakes, not the product.**

---

## The 8 confirmed gaps — nobody has built these

### 1. Cross-session, cross-repo cost aggregation
Claude Code's `/cost` and `/usage` are local-per-machine, per-session only. Run 5 concurrent sessions across 5 repos and nothing aggregates the picture — not Claude Code, not Cursor, not any third-party tool (ccusage, cc-ledger, AgentsRoom all stop at single-account/session).
**This is the biggest, cleanest gap and matches Descant's architecture exactly.**

### 2. Accurate MCP cost attribution
Even Claude Code's own `/context` command *overstates* MCP token usage — it double-counts per-tool system-prompt overhead (documented in Async-Let's analysis, "Do MCP Servers Really Eat Half Your Context Window?"). No tool gives a trustworthy "this MCP server costs you X tokens / $Y right now" number. People are optimizing blind.

### 3. Cache-drift detection
No tool alerts on collapsing cache-hit rate. The March 2026 Anthropic incident (silent cache-TTL cut from 1hr → 5min, no changelog) caused 17–32% cost inflation across ~119,866 measured API calls; one developer documented $2,530 in surprise overpayment. It was caught by developers hand-parsing JSONL logs — not by any vendor dashboard. Source: community incident analysis + Anthropic's April 2026 postmortem (confirmed 3 overlapping product changes; usage limits reset in v2.1.116, April 20).

### 4. Cache-safety guardrails on config changes
Toggling an MCP server, switching models mid-session, or reordering tools silently invalidates the entire cached prefix (Anthropic caches tools → system → messages, in that order — GitHub issue #2808). No tool warns you *before* an action blows up your cache. Model-switching specifically nukes KV cache because caches are architecture-isolated per model.

### 5. Team/manager rollups
Anthropic's own usage data is local-only by design. A manager cannot see team-wide spend without manual export/stitching (per Claude Code cost docs: "no native way to aggregate usage across" developers). Anthropic's own benchmark: ~$13/dev/active day, $150–250/dev/month, <$30/day for 90% of users — but nobody can see this rolled up per team, repo, or feature.

### 6. Audit trails tied to spend and decisions
Standard logging (Datadog, CloudTrail) captures that an API call happened — not the prompt, retrieved context, or tool chain that produced it. EU AI Act high-risk logging obligations take effect **August 2, 2026**. Procurement teams in regulated industries are reportedly making audit-trail capability a gating criterion. Nobody in the dev-tool space owns this at the IDE layer (infra-layer tools like Langfuse/Helicone/Braintrust are close but not IDE-native or concurrency-aware).

### 7. Cross-session coordination
Parallel agents on related/shared repos collide: conflicting file writes, duplicate CI jobs, lost state (e.g., Browser MCP session timeouts at 5 min). One developer measured that isolating one session per repo cut tokens 57% and rework 4× vs. a single overloaded cross-repo session — but isolation alone doesn't solve *coordination*. Early attempts exist (claude-presence MCP, Anthropic's May 2026 "agent view" preview) but nothing mature.

### 8. MCP server inventory / security visibility
Security teams can't enumerate which MCP servers, skills, and hooks their agents have quietly accumulated. Island.io's 2026 scan of 33,563 published MCP server builds (475,865 tools) found ~49% had at least one security finding, and ~1 in 8 exposed a tool capable of executing code, deleting data, or an irreversible action on first call. No IDE-level inventory layer exists.

---

## The numbers worth remembering (all sourced, caveat noted where estimated)

- **MCP context tax, per-server:** GitHub MCP alone ≈ 17,600–55,000 tokens depending on toolset version and measurement method (multiple independent sources: MCP Compressor/StackOne, MindStudio, Nebulagg/dev.to, GitHub issue #2808's ~1,000 tokens/tool rule of thumb).
- **MCP context tax, aggregate:** A 10-server setup routinely hits 50,000–100,000+ tokens before a single user prompt is processed; GitHub+Slack+Sentry alone can consume ~72% of a 200K window "on idle."
- **Team-scale dollar cost (illustrative, vendor-sourced — treat as directional):** ~$375/month for a 5-dev team (DeployStack model, 75K tokens/session × 10 sessions/day); scaling implies ~$750–$3,750/month for 10–50 engineers. Higher-volume models (1,000 req/day) range from $5,100/month to $64,350/month across sources — wide spread because these are all blog-vendor worked examples, not audited figures.
- **MCP vs. CLI overhead ratio:** measured 4–32× more tokens for MCP vs. equivalent CLI on identical tasks (Scalekit, 75-run benchmark).
- **Prompt caching savings:** Anthropic cache reads = 0.1× input price (90% discount); break-even at 2nd cache hit on a 5-min TTL. Realistic multi-turn savings land 59–78.5% at moderate hit rates. One documented case: $720/month → $72/month (90%).
- **Multi-agent token cost:** Anthropic's own engineering blog states multi-agent systems use ~15× the tokens of single-chat interactions, with token usage explaining ~80% of performance variance in their own research-agent system.
- **Failure rate in multi-agent systems:** UC Berkeley MAST taxonomy (NeurIPS 2025, arXiv:2503.13657) — 1,600+ annotated traces across 7 frameworks show failure rates of 41–86.7%, 14 distinct failure modes.
- **Shadow AI cost (adjacent enterprise risk):** IBM/Ponemon 2025 Cost of a Data Breach Report — breaches involving high shadow-AI levels cost $4.63M on average, ~$670K more than low/no-shadow-AI breaches; shadow AI was a factor in 20% of breaches.
- **AI governance spend (market signal):** Gartner (Feb 2026) projects AI governance platform spend reaching $492M in 2026, surpassing $1B by 2030.

---

## Competitive reality check

No incumbent — Claude Code, Cursor, Codex CLI, GitHub Copilot, Google Antigravity, Cline/Roo/Aider, OpenCode, Kiro, Hermes/OpenClaw — has cross-session/cross-repo cost+cache visibility, accurate MCP attribution, or audit trails. This is confirmed structurally, not just currently missing:
- **Usage-based vendors (Cursor, Copilot, Codex)** are financially disincentivized from helping you spend less.
- **Anthropic** controls Claude Code but won't build cross-vendor or cross-account tooling — and has already shown willingness to change caching behavior silently (March 2026) and cut off third-party subscription billing (April 4, 2026 — hit Cline, Cursor, Windsurf, OpenClaw).
- **Infra-layer observability tools** (Langfuse, Helicone, Braintrust, TrueFoundry) are close in spirit but sit outside the IDE and aren't concurrency-aware.

**Whole market is shifting to metered pricing** (Copilot went full usage-based June 1, 2026; Cursor and Windsurf/Devin Desktop followed similar paths) — meaning the cost-surprise problem this direction addresses is getting *more* acute, not less, as we build.

---

## Focus order

1. **Unified cost/cache dashboard** — live, per-session, per-repo, per-MCP-server: tokens, dollars, cache-hit rate. More accurate than Claude Code's own `/context`. This is the wedge; it's directly native to Descant's multi-session architecture and nobody else has it.
2. **Cache-safety guardrails** — warn before MCP toggles / model switches that will invalidate the cache; alert on cache-drift (the March-incident scenario). Turns a one-time trust event into an ongoing product reason to exist.
3. **Cross-session coordination** — prevent the conflicting-write/duplicate-CI failure modes that come from concurrent agents on related repos.
4. **Team rollups + audit trails** — the enterprise/compliance wedge, timed to EU AI Act enforcement (Aug 2, 2026). Harder for Anthropic to want to build (cross-org, compliance-specific), so likely the most durable moat long-term.

**Explicitly deprioritized:** MCP token reduction/compression as a headline feature — already being solved upstream by Anthropic.

## Key risk to monitor

Platform dependency on Claude Code. Anthropic has already (a) silently changed cache behavior with real cost consequences for users, and (b) cut off competing third-party tools from subscription billing. Any of the above gaps that Anthropic decides to close natively (an "agent view" cost rollup, for instance, already previewed May 2026) shrinks our window. Move fast on #1–2; treat #4 as the fallback moat if Anthropic starts closing the earlier gaps.

## Open item

No public footprint for "Descant" was found during research (no site, repo, or launch as of Aug 2026) — this doc reflects market positioning only, not a validated product spec. Validate feature scope and billing/platform-dependency posture against this direction before building.
