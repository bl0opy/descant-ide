# Parked: the MCP panels

Commented out of the UI, not deleted — see IDEAS.md for why.
The backend, API and tests behind these are all still live.

### The four extra panels (activity bar, left edge)

| Panel | What it does |
|---|---|
| **Tool loadout** | Every MCP server and skill attached to the repo, with what each costs **on every turn**. Toggle them off; pick which individual tools stay resident (**Tool residency**, below); convert an MCP server into a thin skill (the preview shows the saving before anything is written). Add servers here too — see below. |
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

### Tool residency: one proxy instead of every server

**Tool loadout → Tool residency.** Claude Code's only lever is per *server*, but
cost is never spread evenly across a server: it is three fat schemas out of
twenty tools. Descant closes that gap with a proxy — one stdio server called
`descant` that fronts every real server for the repo and exposes only the tools
in the **active group**.

The token saving is the obvious half. The half that matters longer is that every
tool call now goes through one place we own, which is the only way to learn which
tools a repo genuinely uses, which resident ones are dead weight, and what a
given server actually costs you.

Nothing becomes unavailable. Alongside the group it always exposes two tools:

- `find_tool` — search every tool the repo has, group or not, and get its real
  input schema back;
- `call_tool` — invoke any of them by name.

So a tool outside the group costs nothing until a task reaches for it, and then
one round trip instead of a permanent seat in every request. A group is a
residency list, not a restriction.

**Install** registers the proxy and detaches the servers it fronts — leaving
them attached would cost *more*, because you would carry the schemas and the
proxy. **Uninstall** puts back exactly what it took away, by name, so a server
you had switched off yourself stays off.

One thing worth knowing, because it surprised this build: `disabledMcpjsonServers`
governs `.mcp.json` servers only. A **local-scope** server — one defined under
your project in `~/.claude.json` — ignores it entirely and starts anyway
(`claude mcp list` will happily call a "detached" server connected). So Descant
detaches those by *parking* the definition in `~/.descant/parked/` and putting it
back verbatim when you re-attach.

Groups, the tool index and parked definitions all live under `~/.descant/`.
Nothing here is written into the repo.

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

