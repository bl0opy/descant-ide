"""Mine transcripts for repeated tool sequences worth turning into skills.

The premise: every session's full tool history is already on disk. If the agent
has run ``pytest -x`` then ``ruff check`` then ``git diff`` in that order eight
times across four sessions in one repo, that is not a coincidence — it is an
undocumented workflow, and it is costing full tool-call round trips every time.
Descant finds those and proposes a skill.

How it works
------------
1. **Normalise** each tool call into a stable *signature*. Raw commands never
   repeat exactly (paths, flags, arguments differ) so ``pytest -x tests/foo.py``
   and ``pytest -x tests/bar.py`` both become ``Bash(pytest)``. The normaliser is
   the whole ballgame: too coarse and everything collides, too fine and nothing
   ever repeats.
2. **Extract n-grams** of consecutive signatures, length 2..6, per session.
3. **Require at least one command.** A sequence of pure file reads and edits is
   not a workflow worth naming, however often it recurs.
4. **Score** by how much a skill would actually save: how often the sequence
   recurs, across how many distinct sessions, times the round-trip cost of the
   calls it would collapse. Cross-session recurrence is weighted heavily —
   something repeated three times in one session is often just a retry loop,
   whereas the same sequence in three sessions is a habit.
5. **Suppress subsumed candidates**: if ``A→B→C`` is a strong candidate, the
   ``A→B`` inside it is not reported separately unless it also occurs alone.

Everything here is deterministic and offline — no model in the loop. The output
is a *proposal* for a human to accept, not an auto-generated skill.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import tokens
from .transcript import Session

MIN_N = 2
MAX_N = 6
#: A sequence must appear at least this many times overall to be considered.
MIN_OCCURRENCES = 3
#: ...or appear in at least this many distinct sessions, which matters more.
MIN_SESSIONS = 2

# Commands that say nothing about a workflow — navigation and noise.
_BORING = {"cd", "ls", "pwd", "echo", "cat", "which", "true", "clear", "export"}

# Shell operators that separate commands within one Bash invocation.
_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")


def _command_head(command: str) -> str:
    """Reduce a shell command to its identifying head.

    ``npm run test -- --watch`` -> ``npm run test``
    ``git commit -m 'whatever'`` -> ``git commit``
    ``python -m pytest tests/x`` -> ``python -m pytest``
    """
    command = command.strip()
    if not command:
        return ""
    # Take the first command in a chain; the rest become their own signatures.
    first = _SPLIT.split(command)[0].strip()
    parts = [p for p in first.split() if p]
    # Drop leading env assignments (FOO=bar cmd).
    while parts and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[0]):
        parts.pop(0)
    if not parts:
        return ""

    head = [parts[0]]
    rest = parts[1:]
    # Keep subcommands (git commit, npm run, cargo test) but stop at the first
    # thing that looks like an argument rather than a verb.
    for p in rest[:2]:
        if p.startswith("-"):
            if p == "-m" and head[0] == "python":  # python -m pytest
                head.append(p)
                continue
            break
        if "/" in p or "." in p or "*" in p:
            break
        head.append(p)
        if head[0] not in {"git", "npm", "yarn", "pnpm", "cargo", "go", "make", "docker", "python"}:
            break
    return " ".join(head)


def signature(kind: str, tool_name: str | None, tool_input: dict | None) -> str | None:
    """A stable identity for one tool call, or None if it should be ignored."""
    if kind != "tool_use" or not tool_name:
        return None
    tool_input = tool_input or {}

    if tool_name == "Bash":
        head = _command_head(str(tool_input.get("command", "")))
        if not head or head.split()[0] in _BORING:
            return None
        return f"Bash({head})"

    if tool_name in ("Read", "Write", "Edit", "NotebookEdit"):
        # The extension is the signal — "edits a Python file" — not the path.
        path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        ext = Path(path).suffix.lower() or "?"
        return f"{tool_name}({ext})"

    if tool_name in ("Grep", "Glob"):
        return f"{tool_name}()"

    if tool_name.startswith("mcp__"):
        return tool_name

    return f"{tool_name}()"


def session_signatures(session: Session) -> list[str]:
    out = []
    for ev in session.events:
        sig = signature(ev.kind, ev.tool_name, ev.tool_input)
        if sig:
            out.append(sig)
    return out


@dataclass
class Candidate:
    sequence: tuple[str, ...]
    occurrences: int = 0
    sessions: set[str] = field(default_factory=set)
    repos: set[str] = field(default_factory=set)
    example_commands: list[str] = field(default_factory=list)
    #: True when this came from collapsing a repeating loop, which means its
    #: rotations and wrap-around fragments are the same workflow, not new ones.
    cyclic: bool = False

    @property
    def length(self) -> int:
        return len(self.sequence)

    @property
    def score(self) -> float:
        """Rank by plausible value, not raw frequency.

        Cross-session breadth dominates: a sequence seen in four sessions is a
        workflow; the same count inside one session is usually a retry loop.
        """
        breadth = len(self.sessions)
        return self.occurrences * (1 + 1.6 * (breadth - 1)) * (self.length ** 1.3)

    @property
    def estimated_savings(self) -> int:
        """Tokens a skill would avoid, roughly.

        Each collapsed call costs its tool-call block plus the model's
        deliberation about what to run next. ~180 tokens per round trip is
        conservative against what the transcripts actually show.
        """
        return int(self.occurrences * self.length * 180)

    def to_dict(self) -> dict:
        return {
            "sequence": list(self.sequence),
            "occurrences": self.occurrences,
            "session_count": len(self.sessions),
            "sessions": sorted(self.sessions),
            "repos": sorted(self.repos),
            "length": self.length,
            "score": round(self.score, 1),
            "estimated_savings": self.estimated_savings,
            "example_commands": self.example_commands[:8],
            "proposed_skill": propose_skill(self),
        }


def _example_commands(sessions: list[Session], seq: tuple[str, ...]) -> list[str]:
    """Real commands behind a signature sequence, for the proposal body."""
    wanted = set(seq)
    out: list[str] = []
    seen: set[str] = set()
    for s in sessions:
        for ev in s.events:
            sig = signature(ev.kind, ev.tool_name, ev.tool_input)
            if sig in wanted and ev.tool_name == "Bash":
                cmd = str((ev.tool_input or {}).get("command", "")).strip()
                # Commands routinely carry heredocs and multi-line commit
                # messages; the example is meant to jog memory, not reproduce
                # the whole invocation.
                cmd = cmd.split("\n", 1)[0].strip()
                if len(cmd) > 120:
                    cmd = cmd[:117].rstrip() + "…"
                if cmd and cmd not in seen:
                    seen.add(cmd)
                    out.append(cmd)
    return out


def mine(sessions: list[Session], repo_path: str | None = None) -> list[Candidate]:
    """Find repeated tool sequences across *sessions*."""
    if repo_path:
        sessions = [s for s in sessions if s.repo_path == repo_path]

    counts: Counter = Counter()
    by_session: dict[tuple[str, ...], set[str]] = defaultdict(set)
    by_repo: dict[tuple[str, ...], set[str]] = defaultdict(set)

    for s in sessions:
        sigs = session_signatures(s)
        for n in range(MIN_N, MAX_N + 1):
            for i in range(len(sigs) - n + 1):
                gram = tuple(sigs[i : i + n])
                # A sequence of one repeated call is a retry, not a workflow.
                if len(set(gram)) == 1:
                    continue
                counts[gram] += 1
                by_session[gram].add(s.session_id)
                by_repo[gram].add(s.repo_path)

    candidates = []
    for gram, count in counts.items():
        n_sessions = len(by_session[gram])
        if count < MIN_OCCURRENCES and n_sessions < MIN_SESSIONS:
            continue
        # A skill has to encode *actions*. "Read a file, then edit it" is what
        # every agent does in every session; naming it saves nothing. Requiring
        # at least one command keeps the output to workflows a human would
        # actually want to write down.
        if not any(g.startswith("Bash(") or g.startswith("mcp__") for g in gram):
            continue
        candidates.append(
            Candidate(
                sequence=gram,
                occurrences=count,
                sessions=by_session[gram],
                repos=by_repo[gram],
                example_commands=_example_commands(sessions, gram),
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=True)
    return _suppress_subsumed(_merge_cyclic(candidates))


def _base_period(seq: tuple[str, ...]) -> tuple[str, ...]:
    """Collapse a sequence that is just one unit repeated.

    A tight edit/compile loop produces ``A→B→C→A→B→C`` as a 6-gram, which is not
    a different workflow from ``A→B→C`` — it is the same one, twice. Reporting
    both (plus every rotation) buries the finding under its own echoes.
    """
    n = len(seq)
    for p in range(1, n):
        # Smallest p where the sequence is a window onto `unit` repeated. Note
        # this must NOT require n % p == 0: a 3-step loop observed over 5 steps
        # (A→B→C→A→B) is still that loop, just caught mid-cycle, and requiring
        # exact division lets every partial cycle through as its own "finding".
        if all(seq[i] == seq[i + p] for i in range(n - p)):
            return seq[:p]
    return seq


def _canonical_rotation(seq: tuple[str, ...]) -> tuple[str, ...]:
    """Smallest rotation, so cyclic workflows collide on one representative.

    ``Edit→compile→Read`` and ``Read→Edit→compile`` are the same loop observed
    from different starting points.
    """
    return min(tuple(seq[i:] + seq[:i]) for i in range(len(seq)))


def _merge_cyclic(candidates: list[Candidate]) -> list[Candidate]:
    """Fold periodic repeats and rotations into a single candidate each."""
    merged: dict[tuple[str, ...], Candidate] = {}
    for cand in candidates:
        base = _base_period(cand.sequence)
        key = _canonical_rotation(base)
        keep = merged.get(key)
        if keep is None:
            merged[key] = Candidate(
                sequence=base,
                occurrences=cand.occurrences,
                sessions=set(cand.sessions),
                repos=set(cand.repos),
                example_commands=list(cand.example_commands),
                cyclic=base != cand.sequence,
            )
            continue
        keep.cyclic = keep.cyclic or base != cand.sequence
        # Same workflow seen another way: take the strongest evidence, and
        # prefer the representative that starts where the loop most often does.
        keep.occurrences = max(keep.occurrences, cand.occurrences)
        keep.sessions |= cand.sessions
        keep.repos |= cand.repos
        for c in cand.example_commands:
            if c not in keep.example_commands:
                keep.example_commands.append(c)
    return sorted(merged.values(), key=lambda c: c.score, reverse=True)


def _suppress_subsumed(candidates: list[Candidate]) -> list[Candidate]:
    """Drop short candidates that only ever appear inside a stronger longer one.

    Without this the output is dominated by every prefix and suffix of the same
    workflow, which reads as noise even though each row is technically true.
    """
    kept: list[Candidate] = []
    for cand in candidates:  # already sorted strongest first
        subsumed = False
        for bigger in kept:
            if cand.length >= bigger.length:
                continue
            # A cyclic workflow's wrap-around fragments (C→A for loop A→B→C)
            # are part of the same loop, so search the doubled sequence.
            haystack = bigger.sequence * 2 if bigger.cyclic else bigger.sequence
            if not _contains(haystack, cand.sequence):
                continue
            # Only suppress when it adds no independent evidence.
            if cand.occurrences <= bigger.occurrences:
                subsumed = True
                break
        if not subsumed:
            kept.append(cand)
    return kept


def _contains(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


# --------------------------------------------------------------------------
# proposal rendering
# --------------------------------------------------------------------------


def _verb(sig: str) -> str:
    m = re.match(r"Bash\((.+)\)$", sig)
    if m:
        return m.group(1)
    m = re.match(r"(\w+)\(", sig)
    return m.group(1).lower() if m else sig


def _slug(seq: tuple[str, ...]) -> str:
    words = [re.sub(r"[^a-z0-9]+", "-", _verb(s).lower()).strip("-") for s in seq]
    return "-".join(w for w in words if w)[:48].strip("-") or "mined-workflow"


def propose_skill(cand: Candidate) -> dict:
    """Render a SKILL.md proposal for a mined sequence."""
    slug = _slug(cand.sequence)
    steps = "\n".join(f"{i + 1}. `{_verb(s)}`" for i, s in enumerate(cand.sequence))
    examples = "\n".join(f"```bash\n{c}\n```" for c in cand.example_commands[:4])
    where = ", ".join(Path(r).name for r in sorted(cand.repos))

    description = (
        f"Run the {' → '.join(_verb(s) for s in cand.sequence)} workflow. "
        f"Observed {cand.occurrences}x across {len(cand.sessions)} session(s) in {where}."
    )[:480]

    body = f"""---
name: {slug}
description: {description}
---

# {' → '.join(_verb(s) for s in cand.sequence)}

Mined from {cand.occurrences} occurrence(s) across {len(cand.sessions)} session(s)
in {where}. This sequence recurred often enough to be worth naming.

## Steps

{steps}

## Commands actually used

{examples or '_No shell commands in this sequence — it is a tool pattern._'}

## Notes

- Generated by Descant from transcript history. Review before relying on it;
  the commands are what was observed, not necessarily what is correct.
- Adjust paths and flags — the miner deliberately generalises them away.
"""
    return {
        "slug": slug,
        "description": description,
        "skill_md": body,
        "listing_tokens": tokens.estimate_text(f"- {slug}: {description}\n"),
    }


def summary(sessions: list[Session], repo_path: str | None = None, limit: int = 20) -> dict:
    cands = mine(sessions, repo_path)[:limit]
    return {
        "repo_path": repo_path,
        "candidate_count": len(cands),
        "candidates": [c.to_dict() for c in cands],
        "total_estimated_savings": sum(c.estimated_savings for c in cands),
    }
