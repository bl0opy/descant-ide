"""Per-repo tool loadout: what is attached, what it costs, and toggling it.

The problem this exists for: people over-attach MCP servers because nothing
tells them the price. Every server's tool schemas ride along in *every* request
for the life of the project, and the only visible symptom is that context fills
up faster than it should. Put a token number next to each row and the decision
makes itself.

Writes go to the narrowest scope that works, so Descant never edits a file that
belongs to the team:

* MCP enablement -> ``~/.claude.json`` under this project (user-local, matching
  where Claude Code already records ``enabledMcpjsonServers``).
* Skill enablement -> ``<repo>/.claude/settings.local.json``, which is
  git-ignored by convention.

We never rewrite a checked-in ``.mcp.json`` or ``settings.json``: those are the
shared definition, and silently editing them would surprise everyone else.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from . import mcp, skills, tokens

#: Claude Code's own key for skills switched off in a project.
DISABLED_SKILLS_KEY = "disabledSkills"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".descant-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic, so a crash can't leave a half-written config


def _project_entry(repo_path: str) -> tuple[Path, dict, dict]:
    """The ``~/.claude.json`` blob for this project, created if absent."""
    home_path = Path.home() / ".claude.json"
    data = _read_json(home_path)
    projects = data.setdefault("projects", {})
    entry = projects.setdefault(str(Path(repo_path).expanduser()), {})
    return home_path, data, entry


def _local_settings(repo_path: str) -> tuple[Path, dict]:
    p = Path(repo_path).expanduser() / ".claude" / "settings.local.json"
    return p, _read_json(p)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


async def get(repo_path: str, probe: bool = True) -> dict:
    """The full loadout for a repo, with real costs where we can measure them."""
    servers = await mcp.discover_and_probe(repo_path, do_probe=probe)
    _, _, entry = _project_entry(repo_path)
    _, local = _local_settings(repo_path)
    disabled_skills = set(local.get(DISABLED_SKILLS_KEY) or [])

    found_skills = skills.discover(repo_path)
    for s in found_skills:
        s.enabled = s.name not in disabled_skills and s.slug not in disabled_skills

    mcp_active = sum(s.token_cost for s in servers if s.enabled)
    mcp_total = sum(s.token_cost for s in servers)
    skill_active = sum(s.listing_tokens for s in found_skills if s.enabled)
    skill_total = sum(s.listing_tokens for s in found_skills)

    return {
        "repo_path": repo_path,
        "mcp_servers": [s.to_dict() for s in servers],
        "skills": [s.to_dict() for s in found_skills],
        "totals": {
            "mcp_active_tokens": mcp_active,
            "mcp_total_tokens": mcp_total,
            "mcp_savings_available": mcp_total - mcp_active,
            "skill_active_tokens": skill_active,
            "skill_total_tokens": skill_total,
            "always_on_tokens": mcp_active + skill_active,
        },
        "conversion_candidates": [
            {"name": s.name, **mcp.conversion_savings(s)}
            for s in servers
            if s.enabled and s.probed and s.tools
        ],
        "unmeasured": [s.name for s in servers if not s.probed],
    }


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def set_mcp_enabled(repo_path: str, name: str, enabled: bool) -> dict:
    home_path, data, entry = _project_entry(repo_path)
    enabled_list = list(entry.get("enabledMcpjsonServers") or [])
    disabled_list = list(entry.get("disabledMcpjsonServers") or [])

    if enabled:
        if name not in enabled_list:
            enabled_list.append(name)
        disabled_list = [n for n in disabled_list if n != name]
    else:
        enabled_list = [n for n in enabled_list if n != name]
        if name not in disabled_list:
            disabled_list.append(name)

    entry["enabledMcpjsonServers"] = enabled_list
    entry["disabledMcpjsonServers"] = disabled_list
    _write_json(home_path, data)
    return {"name": name, "enabled": enabled, "written": str(home_path)}


def set_skill_enabled(repo_path: str, name: str, enabled: bool) -> dict:
    path, local = _local_settings(repo_path)
    disabled = list(local.get(DISABLED_SKILLS_KEY) or [])
    if enabled:
        disabled = [n for n in disabled if n != name]
    elif name not in disabled:
        disabled.append(name)
    local[DISABLED_SKILLS_KEY] = disabled
    _write_json(path, local)
    return {"name": name, "enabled": enabled, "written": str(path)}


async def convert_mcp_to_skill(repo_path: str, name: str, disable_after: bool = True) -> dict:
    """Convert one server to a skill, and optionally detach the server.

    Detaching is the point — a converted server that stays attached costs *more*,
    not less, because you are now carrying both the schemas and the skill.
    """
    servers = await mcp.discover_and_probe(repo_path, do_probe=True)
    server = next((s for s in servers if s.name == name), None)
    if server is None:
        raise ValueError(f"no MCP server named {name!r} for {repo_path}")
    if not server.probed:
        raise ValueError(f"cannot convert {name!r}: {server.probe_error or 'not probed'}")

    dest = Path(repo_path).expanduser() / ".claude" / "skills"
    result = mcp.write_skill(server, str(dest))
    if disable_after:
        result["detached"] = set_mcp_enabled(repo_path, name, False)
    return result


def write_mined_skill(repo_path: str, slug: str, skill_md: str) -> dict:
    """Materialise a mined skill proposal into the repo."""
    safe = "".join(c for c in slug if c.isalnum() or c in "-_") or "mined-skill"
    path = Path(repo_path).expanduser() / ".claude" / "skills" / safe / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(skill_md, encoding="utf-8")
    return {"slug": safe, "path": str(path)}
