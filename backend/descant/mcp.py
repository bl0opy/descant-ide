"""MCP server discovery, real schema measurement, and skill conversion.

Three jobs:

1. **Discover** which MCP servers a repo has attached, across the several places
   Claude Code stores that (``.mcp.json``, ``~/.claude.json`` per-project
   config, settings files) and which are enabled.

2. **Measure** what they cost. This is the part nobody does: an MCP server's
   token cost is the JSON Schema of every tool it exposes, injected into your
   context on every single turn. We get the real number by *speaking the
   protocol* -- spawn the server, ``initialize``, ``tools/list``, and count the
   schemas it actually returns. Not an estimate from a table.

3. **Convert** a server into a thin skill: a ``SKILL.md`` whose frontmatter is a
   few dozen tokens, plus a small CLI that calls the underlying tool on demand.
   Same capability; the schemas stop being resident. This is the trade the
   loadout view is built to make legible.

The protocol work here is deliberately minimal -- JSON-RPC 2.0 over stdio, three
messages, hard timeouts, and every failure mode degrades to "unmeasured" rather
than raising. A server that needs credentials or a missing binary must not take
the panel down with it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import tokens

PROTOCOL_VERSION = "2024-11-05"
PROBE_TIMEOUT = 20.0


@dataclass
class McpTool:
    name: str
    description: str = ""
    schema: dict = field(default_factory=dict)

    @property
    def token_cost(self) -> int:
        """What this one tool's definition costs in every request."""
        blob = json.dumps(
            {"name": self.name, "description": self.description, "input_schema": self.schema},
            ensure_ascii=False,
        )
        return tokens.estimate_text(blob, dense=True)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "schema": self.schema,
            "token_cost": self.token_cost,
        }


@dataclass
class McpServer:
    name: str
    config: dict
    source: str  # where the config came from
    enabled: bool = True
    tools: list[McpTool] = field(default_factory=list)
    probed: bool = False
    probe_error: str = ""

    @property
    def transport(self) -> str:
        if self.config.get("url"):
            return self.config.get("type") or "http"
        return "stdio"

    @property
    def command_line(self) -> str:
        if self.config.get("url"):
            return self.config["url"]
        argv = [self.config.get("command", "")] + list(self.config.get("args") or [])
        return " ".join(a for a in argv if a)

    @property
    def token_cost(self) -> int:
        return sum(t.token_cost for t in self.tools)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "enabled": self.enabled,
            "transport": self.transport,
            "command": self.command_line,
            "probed": self.probed,
            "probe_error": self.probe_error,
            "tool_count": len(self.tools),
            "token_cost": self.token_cost,
            "tools": [t.to_dict() for t in self.tools],
        }


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def discover(repo_path: str) -> list[McpServer]:
    """Every MCP server visible to a session in *repo_path*.

    Precedence follows Claude Code: a project ``.mcp.json`` is the shared,
    checked-in definition; ``~/.claude.json`` holds per-project and global
    additions. Enablement is tracked separately from definition, so a server can
    be defined and switched off.
    """
    repo = Path(repo_path).expanduser()
    servers: dict[str, McpServer] = {}

    # 1. Checked-in project config.
    for name, cfg in (_read_json(repo / ".mcp.json").get("mcpServers") or {}).items():
        servers[name] = McpServer(name=name, config=cfg or {}, source=".mcp.json")

    # 2. Per-project and global config in ~/.claude.json.
    home_cfg = _read_json(Path.home() / ".claude.json")
    project_cfg = (home_cfg.get("projects") or {}).get(str(repo)) or {}
    for name, cfg in (home_cfg.get("mcpServers") or {}).items():
        servers.setdefault(name, McpServer(name=name, config=cfg or {}, source="global"))
    for name, cfg in (project_cfg.get("mcpServers") or {}).items():
        servers[name] = McpServer(name=name, config=cfg or {}, source="project")

    # 3. Enablement. Servers from .mcp.json are opt-in per project.
    enabled = set(project_cfg.get("enabledMcpjsonServers") or [])
    disabled = set(project_cfg.get("disabledMcpjsonServers") or [])
    for name, srv in servers.items():
        if name in disabled:
            srv.enabled = False
        elif srv.source == ".mcp.json":
            srv.enabled = name in enabled or "*" in enabled
    return sorted(servers.values(), key=lambda s: s.name.lower())


# --------------------------------------------------------------------------
# probing: speak just enough MCP to read the tool list
# --------------------------------------------------------------------------


async def _rpc_session(proc: asyncio.subprocess.Process) -> list[McpTool]:
    """Run initialize -> initialized -> tools/list against a live stdio server."""

    async def send(payload: dict) -> None:
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        await proc.stdin.drain()

    async def read_result(want_id: int) -> dict:
        # Servers are allowed to interleave notifications and log lines; skip
        # anything that is not the response we asked for.
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed the connection")
            try:
                msg = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"])[:200])
                return msg.get("result") or {}

    await send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "descant", "version": "0.1.0"},
            },
        }
    )
    await read_result(1)
    await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    result = await read_result(2)

    return [
        McpTool(
            name=t.get("name", "?"),
            description=t.get("description", "") or "",
            schema=t.get("inputSchema") or t.get("input_schema") or {},
        )
        for t in (result.get("tools") or [])
        if isinstance(t, dict)
    ]


async def probe(server: McpServer, cwd: str, timeout: float = PROBE_TIMEOUT) -> McpServer:
    """Measure a server's real tool schemas. Never raises."""
    if server.transport != "stdio":
        server.probe_error = f"{server.transport} transport not probed (stdio only)"
        return server

    command = server.config.get("command")
    if not command:
        server.probe_error = "no command in config"
        return server
    if shutil.which(command) is None and not Path(command).exists():
        server.probe_error = f"{command!r} not found on PATH"
        return server

    env = {**os.environ, **(server.config.get("env") or {})}
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            *(server.config.get("args") or []),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        server.tools = await asyncio.wait_for(_rpc_session(proc), timeout=timeout)
        server.probed = True
    except asyncio.TimeoutError:
        server.probe_error = f"no response within {timeout:.0f}s"
    except Exception as exc:
        server.probe_error = str(exc)[:200]
    finally:
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            # Reap it so we don't leak zombies across many probes.
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
    return server


async def discover_and_probe(repo_path: str, do_probe: bool = True) -> list[McpServer]:
    servers = discover(repo_path)
    if not do_probe:
        return servers
    # Concurrently, but each with its own timeout.
    await asyncio.gather(*(probe(s, repo_path) for s in servers), return_exceptions=True)
    return servers


# --------------------------------------------------------------------------
# conversion: MCP server -> thin skill
# --------------------------------------------------------------------------


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "mcp-server"


def _first_sentence(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    if not text:
        return ""
    cut = re.split(r"(?<=[.!?])\s", text)[0]
    return cut[:limit].rstrip()


CLIENT_SOURCE = '''#!/usr/bin/env python3
"""Minimal MCP stdio client — calls one tool and prints the result.

Generated by Descant. This exists so the server's tool schemas do not have to
live in the model's context: the model reads a short description here and shells
out, instead of carrying every schema on every turn.

    ./mcp_call.py <tool-name> '<json-arguments>'
    ./mcp_call.py --list
"""

import json
import subprocess
import sys
from pathlib import Path

CONFIG = json.loads((Path(__file__).parent / "server.json").read_text())
PROTOCOL_VERSION = "2024-11-05"


def rpc(proc, payload, want_id=None):
    proc.stdin.write(json.dumps(payload) + "\\n")
    proc.stdin.flush()
    if want_id is None:
        return None
    while True:
        line = proc.stdout.readline()
        if not line:
            raise SystemExit("mcp server closed the connection")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == want_id:
            if "error" in msg:
                raise SystemExit("mcp error: " + json.dumps(msg["error"]))
            return msg.get("result") or {}


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    proc = subprocess.Popen(
        [CONFIG["command"], *CONFIG.get("args", [])],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**__import__("os").environ, **CONFIG.get("env", {})},
    )
    try:
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "descant-skill", "version": "1.0"}}}, want_id=1)
        rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        if argv[0] == "--list":
            result = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                "params": {}}, want_id=2)
            for t in result.get("tools", []):
                print(f"{t['name']}: {t.get('description', '')}")
            return 0

        args = json.loads(argv[1]) if len(argv) > 1 else {}
        result = rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": argv[0], "arguments": args}}, want_id=3)
        for block in result.get("content", []):
            print(block.get("text", "") if isinstance(block, dict) else block)
        return 1 if result.get("isError") else 0
    finally:
        proc.kill()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


def render_skill(server: McpServer) -> dict:
    """Build the SKILL.md + client + config for a converted server.

    Returns the file contents rather than writing them, so the UI can show a
    preview and a token saving before anything touches disk.
    """
    slug = _slug(server.name)
    tool_lines = []
    for t in server.tools:
        desc = _first_sentence(t.description) or "(no description)"
        tool_lines.append(f"- `{t.name}` — {desc}")
    tool_block = "\n".join(tool_lines) if tool_lines else "- (run `--list` to enumerate)"

    # The description is the only part loaded every turn, so it stays short and
    # trigger-shaped: it has to be enough for the model to know when to look.
    trigger_terms = ", ".join(t.name.replace("_", " ") for t in server.tools[:6])
    description = (
        f"Call the {server.name} MCP server's tools ({len(server.tools)} available"
        f"{': ' + trigger_terms if trigger_terms else ''}). "
        f"Use when the task needs {server.name}."
    )[:480]

    skill_md = f"""---
name: {slug}
description: {description}
---

# {server.name} (via MCP)

This skill wraps the `{server.name}` MCP server. Its tool schemas are **not**
loaded into context — run the client below to list or call tools on demand.

## Usage

```bash
# See every tool and its description
./mcp_call.py --list

# Call one, passing arguments as JSON
./mcp_call.py <tool-name> '{{"arg": "value"}}'
```

## Available tools

{tool_block}

## Notes

- Arguments are the server's own JSON schema. `--list` prints descriptions;
  for the full schema of a single tool, consult the server's documentation.
- Exit status is non-zero when the tool reports an error.
"""

    return {
        "slug": slug,
        "files": {
            f"{slug}/SKILL.md": skill_md,
            f"{slug}/mcp_call.py": CLIENT_SOURCE,
            f"{slug}/server.json": json.dumps(
                {
                    "command": server.config.get("command", ""),
                    "args": server.config.get("args", []),
                    "env": server.config.get("env", {}),
                },
                indent=2,
            )
            + "\n",
        },
        "description": description,
    }


def conversion_savings(server: McpServer) -> dict:
    """Tokens before vs after converting this server to a skill."""
    before = server.token_cost
    rendered = render_skill(server)
    after = tokens.estimate_text(f"- {rendered['slug']}: {rendered['description']}\n")
    return {
        "before_tokens": before,
        "after_tokens": after,
        "saved_tokens": max(0, before - after),
        "ratio": round(before / after, 1) if after else 0.0,
    }


def write_skill(server: McpServer, dest_dir: str) -> dict:
    """Materialise the converted skill under *dest_dir*."""
    rendered = render_skill(server)
    root = Path(dest_dir).expanduser()
    written = []
    for rel, content in rendered["files"].items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        if p.suffix == ".py":
            p.chmod(0o755)
        written.append(str(p))
    return {"slug": rendered["slug"], "written": written, **conversion_savings(server)}


# --------------------------------------------------------------------------
# saving: defining a server for one repo
# --------------------------------------------------------------------------
#
# Discovery reads from two places, so saving has to choose between them, and the
# choice is not cosmetic:
#
# * ``project`` -> ``<repo>/.mcp.json``, the checked-in file the whole team gets.
# * ``local``   -> ``~/.claude.json`` under this project, private to this machine.
#
# ``local`` is the default precisely because it is the one that cannot surprise
# a collaborator. Anything carrying a credential belongs there.

SCOPES = ("local", "project")


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".descant-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def normalise_config(config: dict) -> dict:
    """Accept what a human types and return what Claude Code expects.

    A command pasted as one string is the common case (``uvx some-server --flag``)
    and splitting it here means the form does not have to make the user think
    about argv.
    """
    cfg = {k: v for k, v in (config or {}).items() if v not in (None, "", [], {})}

    if cfg.get("url"):
        cfg.setdefault("type", "http")
        cfg.pop("command", None)
        cfg.pop("args", None)
        return cfg

    command = (cfg.get("command") or "").strip()
    args = cfg.get("args") or []
    if isinstance(args, str):
        args = [a for a in args.split() if a]
    if command and not args and " " in command:
        parts = command.split()
        command, args = parts[0], parts[1:]
    if not command:
        raise ValueError("an MCP server needs either a command or a url")

    cfg["command"] = command
    if args:
        cfg["args"] = list(args)
    else:
        cfg.pop("args", None)
    env = cfg.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError("env must be an object of NAME -> value")
    if env:
        cfg["env"] = {str(k): str(v) for k, v in env.items()}
    else:
        cfg.pop("env", None)
    cfg.pop("type", None)
    return cfg


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("a server needs a name")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("names may use letters, digits, dot, dash and underscore only")
    return name


def save_server(repo_path: str, name: str, config: dict, scope: str = "local") -> dict:
    """Define (or redefine) an MCP server for one repo."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    name = validate_name(name)
    cfg = normalise_config(config)
    repo = Path(repo_path).expanduser()
    if not repo.is_dir():
        raise ValueError(f"{repo} is not a directory on this machine")

    if scope == "project":
        path = repo / ".mcp.json"
        data = _read_json(path)
        data.setdefault("mcpServers", {})[name] = cfg
        _write_json_atomic(path, data)
        # A .mcp.json server is opt-in per project, so a save that did not also
        # enable it would appear to do nothing.
        _set_mcpjson_enabled(repo, name, True)
    else:
        path = Path.home() / ".claude.json"
        data = _read_json(path)
        entry = data.setdefault("projects", {}).setdefault(str(repo), {})
        entry.setdefault("mcpServers", {})[name] = cfg
        # A previous detach must not silently outlive the redefinition.
        entry["disabledMcpjsonServers"] = [
            n for n in (entry.get("disabledMcpjsonServers") or []) if n != name
        ]
        _write_json_atomic(path, data)

    return {"name": name, "scope": scope, "config": cfg, "written": str(path)}


def _set_mcpjson_enabled(repo: Path, name: str, enabled: bool) -> None:
    path = Path.home() / ".claude.json"
    data = _read_json(path)
    entry = data.setdefault("projects", {}).setdefault(str(repo), {})
    enabled_list = [n for n in (entry.get("enabledMcpjsonServers") or []) if n != name]
    disabled_list = [n for n in (entry.get("disabledMcpjsonServers") or []) if n != name]
    (enabled_list if enabled else disabled_list).append(name)
    entry["enabledMcpjsonServers"] = enabled_list
    entry["disabledMcpjsonServers"] = disabled_list
    _write_json_atomic(path, data)


def delete_server(repo_path: str, name: str) -> dict:
    """Remove a server definition from wherever this repo defines it.

    Global servers in ``~/.claude.json`` are left alone — they belong to every
    repo, so deleting one here would be a surprise. Detaching is the right verb
    for those, and the toggle already does it.
    """
    repo = Path(repo_path).expanduser()
    removed: list[str] = []

    project_file = repo / ".mcp.json"
    data = _read_json(project_file)
    if name in (data.get("mcpServers") or {}):
        del data["mcpServers"][name]
        _write_json_atomic(project_file, data)
        removed.append(str(project_file))

    home_path = Path.home() / ".claude.json"
    home = _read_json(home_path)
    entry = (home.get("projects") or {}).get(str(repo)) or {}
    if name in (entry.get("mcpServers") or {}):
        del entry["mcpServers"][name]
        removed.append(str(home_path))
    if entry:
        entry["enabledMcpjsonServers"] = [
            n for n in (entry.get("enabledMcpjsonServers") or []) if n != name
        ]
        entry["disabledMcpjsonServers"] = [
            n for n in (entry.get("disabledMcpjsonServers") or []) if n != name
        ]
        home.setdefault("projects", {})[str(repo)] = entry
        _write_json_atomic(home_path, home)

    if not removed:
        raise ValueError(
            f"{name!r} is not defined for this repo — it may be a global server, "
            "which you can detach but not delete from here"
        )
    return {"name": name, "removed_from": removed}


async def probe_config(name: str, config: dict, cwd: str, timeout: float = PROBE_TIMEOUT) -> dict:
    """Spawn a candidate server and report what it exposes, without saving it.

    This is the whole argument for a form instead of hand-edited JSON: you find
    out what the server costs you *before* it is attached to anything.
    """
    server = McpServer(name=validate_name(name), config=normalise_config(config), source="draft")
    await probe(server, cwd, timeout=timeout)
    out = server.to_dict()
    out["ok"] = server.probed
    return out
