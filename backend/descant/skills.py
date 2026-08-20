"""Discover the skills a repo has loaded, and what they cost.

Skills are directories containing a ``SKILL.md`` whose YAML frontmatter carries
``name`` and ``description``.  The distinction that matters for context cost:

* the **listing** (name + description) is injected into *every* session, always,
  whether or not the skill is ever used;
* the **body** is only read when the skill is actually invoked.

So a skill with a 900-word description costs you on every single turn, while a
skill with a 40-word description and a 3000-word body costs almost nothing until
it fires.  Nobody surfaces that difference today, which is why it is a column in
Descant's loadout view.

Frontmatter is parsed without a YAML dependency: the subset skills actually use
is ``key: value`` with optional quoting, plus block scalars.  That keeps the
backend dependency-free, and an unparseable file degrades to "no metadata"
rather than an exception.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import tokens

SKILL_FILE = "SKILL.md"


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    source: str  # "user" | "project" | "plugin" | "synced"
    body_chars: int = 0
    enabled: bool = True

    @property
    def slug(self) -> str:
        return self.path.parent.name

    @property
    def listing_tokens(self) -> int:
        """What this skill costs on every turn, used or not."""
        return tokens.estimate_text(f"- {self.name}: {self.description}\n")

    @property
    def body_tokens(self) -> int:
        """What it costs the turn it actually fires."""
        return tokens.estimate_text("x" * self.body_chars)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "path": str(self.path),
            "source": self.source,
            "enabled": self.enabled,
            "listing_tokens": self.listing_tokens,
            "body_tokens": self.body_tokens,
            "body_chars": self.body_chars,
        }


# --------------------------------------------------------------------------
# frontmatter
# --------------------------------------------------------------------------

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return ``(metadata, body)``.  Tolerant by design -- see module docstring."""
    m = _FM.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), text[m.end() :]
    meta: dict[str, str] = {}
    key: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if key is not None:
            meta[key] = " ".join(x.strip() for x in buf).strip()

    for line in raw.split("\n"):
        m2 = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if m2:
            flush()
            key, rest = m2.group(1).lower(), m2.group(2).strip()
            # Block scalars (`description: |`) continue on following lines.
            buf = [] if rest in ("|", ">", "|-", ">-") else [rest]
        elif key is not None and line.strip():
            buf.append(line)
    flush()

    for k, v in list(meta.items()):
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            meta[k] = v[1:-1]
    return meta, body


def load_skill(skill_md: Path, source: str) -> Skill | None:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, body = parse_frontmatter(text)
    name = meta.get("name") or skill_md.parent.name
    return Skill(
        name=name,
        description=meta.get("description", ""),
        path=skill_md,
        source=source,
        body_chars=len(body),
    )


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


def _scan(root: Path, source: str, depth: int = 2) -> list[Skill]:
    """Find SKILL.md files under *root*, a couple of levels down.

    Depth matters because layouts differ: user skills sit at
    ``skills/<name>/SKILL.md`` but synced ones nest as
    ``skills/synced/<name>/SKILL.md``.
    """
    out: list[Skill] = []
    if not root.is_dir():
        return out
    for pattern in ["*/" + SKILL_FILE] + [
        "/".join(["*"] * d) + "/" + SKILL_FILE for d in range(2, depth + 2)
    ]:
        for p in sorted(root.glob(pattern)):
            sub = source
            if "synced" in p.parts:
                sub = "synced"
            out.append(load_skill(p, sub))
    return [s for s in out if s is not None]


def discover(repo_path: str | None = None) -> list[Skill]:
    """Every skill visible to a session in *repo_path*, deduped by name."""
    home = Path.home()
    found: list[Skill] = []
    found += _scan(home / ".claude" / "skills", "user")
    for plugin_skills in sorted((home / ".claude" / "plugins").glob("*/skills")):
        found += _scan(plugin_skills, "plugin")
    if repo_path:
        found += _scan(Path(repo_path).expanduser() / ".claude" / "skills", "project")

    # Project skills shadow user skills of the same name.
    rank = {"project": 0, "plugin": 1, "user": 2, "synced": 3}
    best: dict[str, Skill] = {}
    for s in found:
        cur = best.get(s.name)
        if cur is None or rank.get(s.source, 9) < rank.get(cur.source, 9):
            best[s.name] = s
    return sorted(best.values(), key=lambda s: s.name.lower())


def listing_cost(skills: list[Skill]) -> int:
    """Total always-on cost of a set of skills."""
    return sum(s.listing_tokens for s in skills)


def summary(repo_path: str | None = None) -> dict:
    found = discover(repo_path)
    return {
        "repo_path": repo_path,
        "skills": [s.to_dict() for s in found],
        "count": len(found),
        "listing_tokens": listing_cost(found),
        "body_tokens": sum(s.body_tokens for s in found),
    }
