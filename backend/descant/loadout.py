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
import shutil
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


#: Where a detached local-scope server's definition waits. See ``set_mcp_enabled``.
PARKED_DIR = Path.home() / ".descant" / "parked"


def _parked_path(repo_path: str) -> Path:
    from . import proxy  # one slugging rule for both, and proxy owns it

    return PARKED_DIR / f"{proxy._slug(repo_path)}.json"


def _park(repo_path: str, name: str, config: dict) -> None:
    path = _parked_path(repo_path)
    data = _read_json(path)
    data[name] = config
    _write_json(path, data)


def _unpark(repo_path: str, name: str) -> dict | None:
    path = _parked_path(repo_path)
    data = _read_json(path)
    config = data.pop(name, None)
    if config is not None:
        _write_json(path, data)
    return config


def set_mcp_enabled(repo_path: str, name: str, enabled: bool) -> dict:
    """Attach or detach one server.

    Two mechanisms, because Claude Code has two. ``disabledMcpjsonServers`` is
    exactly what its name says -- it governs servers declared in a ``.mcp.json``
    -- and a **local-scope** server, defined under this project in
    ``~/.claude.json``, ignores it completely: listed as disabled, it still
    starts. Verified against ``claude mcp list``, which kept reporting a
    "detached" server as connected.

    So for those the only honest detach is to take the definition out, and the
    only safe way to do that is to keep it. Detaching parks the config under
    ``~/.descant/parked/`` and attaching puts it back verbatim. Descant owns
    that file, rather than an unknown key inside Claude Code's config that a
    future version would be within its rights to drop.
    """
    home_path, data, entry = _project_entry(repo_path)
    local_servers = entry.get("mcpServers") or {}

    if enabled and name not in local_servers:
        restored = _unpark(repo_path, name)
        if restored is not None:
            entry.setdefault("mcpServers", {})[name] = restored
            _write_json(home_path, data)
            return {"name": name, "enabled": True, "restored": True,
                    "written": str(home_path)}
    elif not enabled and name in local_servers:
        _park(repo_path, name, local_servers[name])
        del entry["mcpServers"][name]
        _write_json(home_path, data)
        return {"name": name, "enabled": False, "parked": True,
                "written": str(home_path)}

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


# --------------------------------------------------------------------------
# skills: writing, promoting, sharing
# --------------------------------------------------------------------------
#
# A mined workflow is only worth capturing if the next agent can find it, and
# where it is written decides which agents those are:
#
# * ``repo``   -> ``<repo>/.claude/skills/`` — this project's agents only.
# * ``user``   -> ``~/.claude/skills/``      — every agent on this machine.
#
# The second is what "keep it organised agent to agent" actually means: a
# workflow discovered in one repo stops being that repo's private lore.

SKILL_SCOPES = ("repo", "user")


def safe_slug(slug: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in (slug or "").lower())
    return cleaned.strip("-") or "mined-skill"


def skills_root(scope: str, repo_path: str | None = None) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "skills"
    if scope == "repo":
        if not repo_path:
            raise ValueError("repo scope needs a repo path")
        return Path(repo_path).expanduser() / ".claude" / "skills"
    raise ValueError(f"scope must be one of {SKILL_SCOPES}")


def write_mined_skill(
    repo_path: str,
    slug: str,
    skill_md: str,
    scope: str = "repo",
    overwrite: bool = False,
) -> dict:
    """Materialise a mined skill proposal at the chosen scope."""
    safe = safe_slug(slug)
    path = skills_root(scope, repo_path) / safe / "SKILL.md"
    existed = path.exists()
    if existed and not overwrite:
        raise FileExistsError(
            f"a skill named {safe!r} already exists at {path} — save under a "
            "different name, or confirm the overwrite"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(skill_md, encoding="utf-8")
    return {
        "slug": safe,
        "scope": scope,
        "path": str(path),
        "overwritten": existed,
        "available_to": "every repo on this machine" if scope == "user" else Path(repo_path).name,
    }


def copy_skill(source_path: str, scope: str, repo_path: str | None = None,
               slug: str | None = None, overwrite: bool = False) -> dict:
    """Copy an existing skill directory to another scope or repo.

    Copies the whole directory, not just ``SKILL.md`` — a skill that shells out
    to a script it ships with is useless without the script.
    """
    src = Path(source_path).expanduser()
    if src.name == "SKILL.md":
        src = src.parent
    if not (src / "SKILL.md").is_file():
        raise ValueError(f"{src} is not a skill directory")

    dest = skills_root(scope, repo_path) / safe_slug(slug or src.name)
    if dest.resolve() == src.resolve():
        raise ValueError("source and destination are the same skill")
    existed = dest.exists()
    if existed and not overwrite:
        raise FileExistsError(f"{dest} already exists — confirm the overwrite to replace it")
    if existed:
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return {
        "slug": dest.name,
        "scope": scope,
        "path": str(dest),
        "from": str(src),
        "overwritten": existed,
    }


def delete_skill(path: str) -> dict:
    """Remove a skill directory. Only ever inside a ``.claude/skills`` tree."""
    target = Path(path).expanduser()
    if target.name == "SKILL.md":
        target = target.parent
    if "skills" not in target.parts or ".claude" not in target.parts:
        raise ValueError(f"refusing to delete {target}: not inside a .claude/skills directory")
    if not (target / "SKILL.md").is_file():
        raise ValueError(f"{target} is not a skill directory")
    shutil.rmtree(target)
    return {"deleted": str(target)}
