"""One MCP server that fronts all the others, exposing only a chosen subset.

The problem this exists for is granularity. Claude Code's only lever is
per-server: ``enabledMcpjsonServers`` attaches or detaches a whole server, and
permission rules can stop a tool being *called* without stopping its schema
being *loaded*. But cost is never spread evenly across a server -- it is three
fat schemas out of twenty tools. So the honest choice today is "carry all of it
or none of it", and people carry all of it.

The proxy replaces that choice. It registers as a single stdio MCP server,
speaks the protocol upstream to every real server, and exposes downstream only
the tools in the repo's **active group**. Everything else stays out of context
without being unavailable:

* ``find_tool``  -- search every known upstream tool by meaning-ish keywords and
  get its real schema back, on demand;
* ``call_tool``  -- invoke any upstream tool by name, in the group or not.

That pair is the whole trick. A tool outside the group costs nothing until a
task reaches for it, and then it costs one round trip instead of a permanent
seat in every request. A group is therefore not a restriction, it is a
*residency list*.

Two deliberate constraints:

* **Upstream configs are resolved live** from Claude Code's own config on every
  start (``mcp.discover``), never copied. Editing a server in the Loadout keeps
  working, and there is still exactly one definition of any server. The cached
  index is only for *searching* tools without spawning anything.
* **Upstream servers are spawned lazily** and reused. A group naming one tool
  from one server never starts the other four.

The runtime loop is deliberately synchronous: it serves exactly one client
(Claude Code) which awaits each response before sending the next, so a
sequential read-handle-write loop is the honest shape, and it is testable
without an event loop. Discovery and measurement -- the parts Descant's UI calls
-- are async, matching the rest of the backend.

    python3 -m descant.proxy --repo /path/to/repo     # speaks MCP on stdio
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import mcp, tokens

#: The name the proxy registers itself under, and the one name it must never
#: treat as an upstream -- or it would spawn itself, forever.
PROXY_NAME = "descant"

#: Downstream tools are ``<server>__<tool>``. Always namespaced, even when a
#: name would be unique: a collision appearing later must not silently reroute
#: an existing call.
NAMESPACE_SEP = "__"

PROTOCOL_VERSION = mcp.PROTOCOL_VERSION
UPSTREAM_TIMEOUT = 60.0

DEFAULT_GROUP = "default"


# --------------------------------------------------------------------------
# config: groups live user-local, never in the repo
# --------------------------------------------------------------------------
#
# A group is a per-machine working preference -- what *you* are doing in this
# repo this week -- so it is stored beside Descant's own state rather than in
# the tree. Same reasoning as loadout.py: Descant does not write files the team
# shares.


def config_dir() -> Path:
    return Path.home() / ".descant" / "proxy"


def _slug(repo_path: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(Path(repo_path).expanduser())).strip("-")
    return s.lower() or "repo"


def config_path(repo_path: str) -> Path:
    return config_dir() / f"{_slug(repo_path)}.json"


def _blank(repo_path: str) -> dict:
    return {
        "repo": str(Path(repo_path).expanduser()),
        "active": DEFAULT_GROUP,
        "groups": {},
        "index": {},
        "index_configs": {},
        "detached": [],
    }


def load_config(repo_path: str) -> dict:
    path = config_path(repo_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _blank(repo_path)
    if not isinstance(data, dict):
        return _blank(repo_path)
    base = _blank(repo_path)
    base.update({k: v for k, v in data.items() if k in base})
    return base


def save_config(repo_path: str, cfg: dict) -> Path:
    path = config_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic: a crash mid-write must not orphan the proxy
    return path


def active_group(cfg: dict) -> dict:
    groups = cfg.get("groups") or {}
    return groups.get(cfg.get("active")) or {}


def group_members(cfg: dict, group: str | None = None) -> dict[str, list[str]]:
    """``{server: [tool, ...]}`` for a group, with ``*`` expanded via the index."""
    g = (cfg.get("groups") or {}).get(group) if group else active_group(cfg)
    index = cfg.get("index") or {}
    out: dict[str, list[str]] = {}
    for server, names in ((g or {}).get("tools") or {}).items():
        known = [t.get("name", "") for t in index.get(server, [])]
        if names == "*" or (isinstance(names, list) and "*" in names):
            out[server] = known
        else:
            wanted = [n for n in (names or []) if isinstance(n, str)]
            # Keep unknown names: the index may be stale, and dropping a tool
            # the user chose would be a silent, confusing edit.
            out[server] = wanted
    return {s: t for s, t in out.items() if t}


# --------------------------------------------------------------------------
# the index: every upstream tool, searchable without spawning anything
# --------------------------------------------------------------------------


async def refresh_index(repo_path: str, cfg: dict | None = None) -> dict:
    """Probe every upstream server and cache what it exposes.

    Disabled servers are indexed too, and that is the point: detaching a server
    is exactly what the proxy asks you to do, and a detached server's tools must
    stay findable.
    """
    cfg = cfg or load_config(repo_path)
    servers = [s for s in await mcp.discover_and_probe(repo_path, do_probe=True)
               if s.name != PROXY_NAME]
    index: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for s in servers:
        if s.probed:
            index[s.name] = [t.to_dict() for t in s.tools]
        elif s.probe_error:
            errors[s.name] = s.probe_error
    cfg["index"] = index
    # Keep each server's launch config alongside its tools. Installing *parks*
    # a local-scope server -- its definition leaves Claude Code's config
    # entirely, because that is the only detach Claude Code honours -- so live
    # discovery alone would leave the proxy unable to start the very servers it
    # fronts. Refresh always runs before install, so this captures them first.
    cfg["index_configs"] = {s.name: s.config for s in servers if s.config}
    save_config(repo_path, cfg)
    return {"servers": len(index), "tools": sum(len(v) for v in index.values()),
            "unmeasured": errors}


_WORD = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def search_index(index: dict, query: str, limit: int = 8) -> list[dict]:
    """Rank upstream tools against a query.

    Overlap scoring, not BM25 and not embeddings: this runs inside the proxy
    process on every ``find_tool`` call over a few hundred tools, and the
    ranking difference at that size is noise. ``library.py`` is where the real
    retrieval lives; if that grows an embedding backend this should call it.
    """
    q = set(_terms(query))
    if not q:
        return []
    scored = []
    for server, tools in (index or {}).items():
        for t in tools:
            name_terms = set(_terms(t.get("name", "")))
            desc_terms = set(_terms(t.get("description", "")))
            server_terms = set(_terms(server))
            score = (
                3.0 * len(q & name_terms)
                + 1.0 * len(q & desc_terms)
                + 1.5 * len(q & server_terms)
            )
            # A substring hit catches "forecast" in "get_forecast_hourly",
            # which term-splitting alone would miss.
            for term in q:
                if term in t.get("name", "").lower():
                    score += 1.0
            if score > 0:
                scored.append((score, server, t))
    scored.sort(key=lambda r: (-r[0], r[1], r[2].get("name", "")))
    return [
        {"tool": qualified(server, t.get("name", "")), "server": server, **t}
        for _, server, t in scored[:limit]
    ]


def qualified(server: str, tool: str) -> str:
    return f"{server}{NAMESPACE_SEP}{tool}"


def resolve_tool_name(index: dict, name: str) -> tuple[str, str] | None:
    """Map a downstream tool name back to ``(server, tool)``.

    Accepts the namespaced form, and a bare tool name when exactly one server
    offers it -- models drop the prefix, and failing on that would be pedantry.
    An ambiguous bare name resolves to nothing rather than guessing.
    """
    for server, tools in (index or {}).items():
        prefix = server + NAMESPACE_SEP
        if name.startswith(prefix):
            return server, name[len(prefix):]
    hits = [(server, t.get("name"))
            for server, tools in (index or {}).items()
            for t in tools if t.get("name") == name]
    return hits[0] if len(hits) == 1 else None


# --------------------------------------------------------------------------
# cost: what the group actually saves
# --------------------------------------------------------------------------

META_TOOLS = [
    {
        "name": "find_tool",
        "description": (
            "Search every MCP tool available to this repo, including ones not "
            "currently loaded, and return their names and input schemas. Use "
            "before call_tool when you do not already know a tool's arguments."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What the tool should do."},
                "limit": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "call_tool",
        "description": (
            "Call any MCP tool available to this repo by name, whether or not it "
            "is loaded. Names are 'server__tool'; use find_tool for its schema."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "server__tool"},
                "arguments": {"type": "object", "description": "The tool's own arguments."},
            },
            "required": ["tool"],
        },
    },
]


def _tool_cost(tool: dict) -> int:
    blob = json.dumps(
        {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "input_schema": tool.get("schema") or tool.get("inputSchema") or {},
        },
        ensure_ascii=False,
    )
    return tokens.estimate_text(blob, dense=True)


def meta_cost() -> int:
    return sum(_tool_cost({**t, "schema": t["inputSchema"]}) for t in META_TOOLS)


def savings(repo_path: str, cfg: dict | None = None, group: str | None = None) -> dict:
    """Tokens carried every turn with every server attached, versus via a group.

    ``before`` counts every *indexed* tool, which is the state the proxy is
    replacing: all servers attached, all schemas resident.
    """
    cfg = cfg or load_config(repo_path)
    index = cfg.get("index") or {}
    before = sum(_tool_cost(t) for tools in index.values() for t in tools)

    members = group_members(cfg, group)
    resident = 0
    for server, names in members.items():
        by_name = {t.get("name"): t for t in index.get(server, [])}
        for n in names:
            if n in by_name:
                resident += _tool_cost(by_name[n])
    after = resident + meta_cost()
    return {
        "before_tokens": before,
        "after_tokens": after,
        "saved_tokens": max(0, before - after),
        "ratio": round(before / after, 1) if after else 0.0,
        "resident_tools": sum(len(v) for v in members.values()),
        "total_tools": sum(len(v) for v in index.values()),
        "meta_tokens": meta_cost(),
    }


# --------------------------------------------------------------------------
# editing groups
# --------------------------------------------------------------------------


def set_group(
    repo_path: str,
    name: str,
    tools: dict,
    description: str = "",
    activate: bool = True,
) -> dict:
    """Create or replace a group. ``tools`` is ``{server: [tool, ...] | "*"}``."""
    name = (name or "").strip()
    if not name:
        raise ValueError("a group needs a name")
    if not isinstance(tools, dict):
        raise ValueError("tools must be an object of server -> tool names")

    cfg = load_config(repo_path)
    cfg.setdefault("groups", {})[name] = {
        "description": description,
        "tools": {str(k): v for k, v in tools.items()},
    }
    if activate:
        cfg["active"] = name
    save_config(repo_path, cfg)
    return {"group": name, "active": cfg["active"], **savings(repo_path, cfg, name)}


def activate_group(repo_path: str, name: str) -> dict:
    cfg = load_config(repo_path)
    if name not in (cfg.get("groups") or {}):
        raise ValueError(f"no group named {name!r} for this repo")
    cfg["active"] = name
    save_config(repo_path, cfg)
    return {"active": name, **savings(repo_path, cfg, name)}


def delete_group(repo_path: str, name: str) -> dict:
    cfg = load_config(repo_path)
    if name not in (cfg.get("groups") or {}):
        raise ValueError(f"no group named {name!r} for this repo")
    del cfg["groups"][name]
    if cfg.get("active") == name:
        cfg["active"] = next(iter(cfg["groups"]), DEFAULT_GROUP)
    save_config(repo_path, cfg)
    return {"deleted": name, "active": cfg.get("active")}


def suggest_group(cfg: dict, budget_tokens: int = 1200) -> dict:
    """A starting group: the cheapest tools that fit a per-turn budget.

    Cheap-first rather than "most useful first" because usefulness is not
    something this can know, and cost is. It is a first draft to edit, which is
    why it is a suggestion and not what ``install`` silently applies.
    """
    priced = sorted(
        (
            (_tool_cost(t), server, t.get("name", ""))
            for server, tools in (cfg.get("index") or {}).items()
            for t in tools
        ),
        key=lambda r: r[0],
    )
    picked: dict[str, list[str]] = {}
    spent = meta_cost()
    for cost, server, name in priced:
        if spent + cost > budget_tokens:
            continue
        picked.setdefault(server, []).append(name)
        spent += cost
    return picked


# --------------------------------------------------------------------------
# install: register the proxy, detach what it now fronts
# --------------------------------------------------------------------------
#
# Installing without detaching would be strictly worse than doing nothing: the
# schemas would still load *and* you would pay for the proxy on top. So the two
# halves are one operation, and uninstall puts back exactly what it took away --
# recorded by name, so a server the user detached themselves stays detached.


def backend_root() -> Path:
    """The directory ``descant`` is importable from."""
    return Path(__file__).resolve().parent.parent


def proxy_config(repo_path: str) -> dict:
    return {
        "command": sys.executable or "python3",
        "args": ["-m", "descant.proxy", "--repo", str(Path(repo_path).expanduser())],
        "env": {"PYTHONPATH": str(backend_root())},
    }


async def install(repo_path: str, group: str | None = None, budget_tokens: int = 1200) -> dict:
    """Index the repo's servers, register the proxy, detach the originals."""
    from . import loadout  # local: the runtime half of this module must stay light

    cfg = load_config(repo_path)
    stats = await refresh_index(repo_path, cfg)
    cfg = load_config(repo_path)

    if group is None:
        group = cfg.get("active") or DEFAULT_GROUP
    if group not in (cfg.get("groups") or {}):
        cfg.setdefault("groups", {})[group] = {
            "description": f"auto-suggested, ~{budget_tokens} tokens/turn",
            "tools": suggest_group(cfg, budget_tokens),
        }
    cfg["active"] = group

    saved = mcp.save_server(repo_path, PROXY_NAME, proxy_config(repo_path), scope="local")

    detached = []
    for server in mcp.discover(repo_path):
        if server.name == PROXY_NAME or not server.enabled:
            continue
        loadout.set_mcp_enabled(repo_path, server.name, False)
        detached.append(server.name)
    cfg["detached"] = detached
    save_config(repo_path, cfg)

    return {
        "installed": saved,
        "group": group,
        "detached": detached,
        "index": stats,
        **savings(repo_path, cfg, group),
    }


def uninstall(repo_path: str, reattach: bool = True) -> dict:
    """Remove the proxy and put back the servers it displaced."""
    from . import loadout

    cfg = load_config(repo_path)
    reattached = []
    if reattach:
        for name in cfg.get("detached") or []:
            loadout.set_mcp_enabled(repo_path, name, True)
            reattached.append(name)
    cfg["detached"] = []
    save_config(repo_path, cfg)

    removed = None
    try:
        removed = mcp.delete_server(repo_path, PROXY_NAME)
    except ValueError:
        pass  # never installed, or already gone -- reattaching was the real work
    return {"removed": removed, "reattached": reattached}


def status(repo_path: str) -> dict:
    """Everything the panel needs, without probing anything."""
    cfg = load_config(repo_path)
    installed = any(s.name == PROXY_NAME and s.enabled for s in mcp.discover(repo_path))
    index = cfg.get("index") or {}
    return {
        "repo_path": str(Path(repo_path).expanduser()),
        "installed": installed,
        "active": cfg.get("active"),
        "config_path": str(config_path(repo_path)),
        "detached": cfg.get("detached") or [],
        "groups": {
            name: {
                **g,
                **savings(repo_path, cfg, name),
            }
            for name, g in (cfg.get("groups") or {}).items()
        },
        "index": {
            server: [
                {"name": t.get("name"), "description": t.get("description", ""),
                 "token_cost": _tool_cost(t)}
                for t in tools
            ]
            for server, tools in index.items()
        },
        "suggestion": suggest_group(cfg),
    }


# --------------------------------------------------------------------------
# runtime: the process Claude Code actually spawns
# --------------------------------------------------------------------------


class Upstream:
    """One real MCP server, started on first use and kept for the session.

    Started lazily because a group naming one tool from one server has no
    business booting the other four -- that would move the cost from context to
    startup rather than removing it.
    """

    def __init__(self, name: str, config: dict, cwd: str):
        self.name = name
        self.config = config
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self._next_id = 100

    def _start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        command = self.config.get("command")
        if not command:
            raise RuntimeError(f"{self.name}: no command in config (http transport?)")
        self.proc = subprocess.Popen(
            [command, *(self.config.get("args") or [])],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env={**os.environ, **(self.config.get("env") or {})},
        )
        self._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "descant-proxy", "version": "0.1.0"},
        }}, want_id=1)
        self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _rpc(self, payload: dict, want_id: int | None = None) -> dict | None:
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        if want_id is None:
            return None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"{self.name}: server closed the connection")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # servers are allowed to log on stdout; skip noise
            if msg.get("id") == want_id:
                if "error" in msg:
                    raise RuntimeError(f"{self.name}: {json.dumps(msg['error'])[:300]}")
                return msg.get("result") or {}

    def call(self, tool: str, arguments: dict) -> dict:
        self._start()
        self._next_id += 1
        return self._rpc(
            {"jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
             "params": {"name": tool, "arguments": arguments or {}}},
            want_id=self._next_id,
        ) or {}

    def list_tools(self) -> list[dict]:
        self._start()
        self._next_id += 1
        result = self._rpc(
            {"jsonrpc": "2.0", "id": self._next_id, "method": "tools/list", "params": {}},
            want_id=self._next_id,
        ) or {}
        return [t for t in (result.get("tools") or []) if isinstance(t, dict)]

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def _text(payload: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": payload}], "isError": is_error}


class Proxy:
    """The downstream half: what Claude Code sees when it talks to ``descant``."""

    def __init__(self, repo_path: str):
        self.repo = str(Path(repo_path).expanduser())
        self.cfg = load_config(self.repo)
        # Live, not cached: one definition of a server, wherever it is edited.
        self.configs = {s.name: s.config for s in mcp.discover(self.repo)
                        if s.name != PROXY_NAME}
        for name, cached in (self.cfg.get("index_configs") or {}).items():
            self.configs.setdefault(name, cached)
        self.upstreams: dict[str, Upstream] = {}

    # -- tool surface ---------------------------------------------------

    def resident_tools(self) -> list[dict]:
        index = self.cfg.get("index") or {}
        out = []
        for server, names in group_members(self.cfg).items():
            by_name = {t.get("name"): t for t in index.get(server, [])}
            for n in names:
                t = by_name.get(n)
                if not t:
                    continue
                out.append({
                    "name": qualified(server, n),
                    "description": t.get("description", ""),
                    "inputSchema": t.get("schema") or {"type": "object"},
                })
        return out

    def tools(self) -> list[dict]:
        return self.resident_tools() + META_TOOLS

    # -- dispatch -------------------------------------------------------

    def upstream(self, server: str) -> Upstream:
        if server not in self.upstreams:
            cfg = self.configs.get(server)
            if cfg is None:
                raise RuntimeError(f"no MCP server named {server!r} is defined for this repo")
            self.upstreams[server] = Upstream(server, cfg, self.repo)
        return self.upstreams[server]

    def call(self, name: str, arguments: dict) -> dict:
        if name == "find_tool":
            return self.find_tool(arguments)
        if name == "call_tool":
            return self.call_tool(arguments)
        return self._route(name, arguments)

    def _route(self, name: str, arguments: dict) -> dict:
        resolved = resolve_tool_name(self.cfg.get("index") or {}, name)
        if resolved is None:
            # Fall back to the live server list: a tool added since the last
            # index refresh is still callable, it just was not searchable.
            if NAMESPACE_SEP in name:
                head, tail = name.split(NAMESPACE_SEP, 1)
                if head in self.configs:
                    resolved = (head, tail)
        if resolved is None:
            return _text(
                f"unknown tool {name!r}. Use find_tool to search what this repo offers.",
                is_error=True,
            )
        server, tool = resolved
        try:
            return self.upstream(server).call(tool, arguments)
        except Exception as exc:
            return _text(f"{server}: {exc}", is_error=True)

    def find_tool(self, arguments: dict) -> dict:
        query = str((arguments or {}).get("query") or "")
        limit = int((arguments or {}).get("limit") or 8)
        hits = search_index(self.cfg.get("index") or {}, query, limit=limit)
        if not hits:
            known = sum(len(v) for v in (self.cfg.get("index") or {}).values())
            return _text(
                f"no match for {query!r} among {known} tools."
                + ("" if known else " The tool index is empty — refresh it in Descant.")
            )
        lines = []
        for h in hits:
            lines.append(
                f"{h['tool']}\n  {h.get('description', '')}\n"
                f"  arguments: {json.dumps(h.get('schema') or {}, ensure_ascii=False)}"
            )
        return _text(
            "\n\n".join(lines)
            + "\n\nCall one with call_tool({\"tool\": \"<name>\", \"arguments\": {...}})."
        )

    def call_tool(self, arguments: dict) -> dict:
        name = str((arguments or {}).get("tool") or "")
        if not name:
            return _text("call_tool needs a 'tool' name", is_error=True)
        return self._route(name, (arguments or {}).get("arguments") or {})

    # -- protocol -------------------------------------------------------

    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        mid = msg.get("id")

        if method == "initialize":
            return self._ok(mid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": PROXY_NAME, "version": "0.1.0"},
            })
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return self._ok(mid, {})
        if method == "tools/list":
            return self._ok(mid, {"tools": self.tools()})
        if method in ("resources/list", "prompts/list"):
            key = method.split("/")[0]
            return self._ok(mid, {key: []})
        if method == "tools/call":
            params = msg.get("params") or {}
            return self._ok(mid, self.call(params.get("name", ""),
                                           params.get("arguments") or {}))
        if mid is None:
            return None  # an unknown notification is not ours to answer
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}

    @staticmethod
    def _ok(mid, result: dict) -> dict | None:
        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def close(self) -> None:
        for up in self.upstreams.values():
            up.close()


def serve(repo_path: str, stdin=None, stdout=None) -> int:
    proxy = Proxy(repo_path)
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                response = proxy.handle(msg)
            except Exception as exc:  # one bad call must not end the session
                response = {"jsonrpc": "2.0", "id": msg.get("id"),
                            "error": {"code": -32603, "message": str(exc)[:300]}}
            if response is not None:
                stdout.write(json.dumps(response) + "\n")
                stdout.flush()
    finally:
        proxy.close()
    return 0


def main(argv: list[str]) -> int:
    repo = os.getcwd()
    if "--repo" in argv:
        i = argv.index("--repo")
        if i + 1 < len(argv):
            repo = argv[i + 1]
    return serve(repo)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
