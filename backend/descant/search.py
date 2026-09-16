"""Find files, and find text inside them.

Uses ripgrep when it is on PATH and falls back to a plain walk when it is not,
so the feature works on a machine that has never installed anything — just
slower, and capped so a search of a huge tree can't wedge the server.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

#: Never worth walking into, in either backend.
SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".mypy_cache",
    ".pytest_cache",
    "target",
    ".DS_Store",
}

RG = shutil.which("rg")
TIMEOUT = 20


# --------------------------------------------------------------------------
# file names — the Cmd+P index
# --------------------------------------------------------------------------


def files(root: str, limit: int = 20000) -> list[str]:
    """Every file under *root*, repo-relative, honouring .gitignore via rg."""
    base = Path(root).expanduser()
    if not base.is_dir():
        return []
    if RG:
        try:
            out = subprocess.run(
                [RG, "--files", "--hidden", *_rg_globs()],
                cwd=base,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            ).stdout
            return out.splitlines()[:limit]
        except (subprocess.TimeoutExpired, OSError):
            pass

    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, base)
        for name in filenames:
            out.append(name if rel_dir == "." else os.path.join(rel_dir, name))
            if len(out) >= limit:
                return out
    return out


def _rg_globs() -> list[str]:
    globs: list[str] = []
    for d in SKIP_DIRS:
        globs += ["--glob", f"!{d}"]
    return globs


def match_paths(paths: list[str], query: str, limit: int = 50) -> list[dict]:
    """Rank *paths* against a fuzzy *query*, VS Code's Cmd+P behaviour.

    Scoring, in order of weight: a match in the basename beats one in the
    directory, a contiguous run beats scattered letters, and an earlier match
    beats a later one. That ordering is the whole reason this isn't a substring
    filter — "aj" should find `app/app.js` before `packages/adjacent/x.ts`.
    """
    q = query.strip().lower()
    if not q:
        return [{"path": p, "score": 0} for p in paths[:limit]]

    scored: list[tuple[int, str]] = []
    for path in paths:
        score = _fuzzy_score(path.lower(), q, path.rsplit("/", 1)[-1].lower())
        if score is not None:
            scored.append((score, path))
    scored.sort(key=lambda pair: (-pair[0], len(pair[1]), pair[1]))
    return [{"path": p, "score": s} for s, p in scored[:limit]]


def _fuzzy_score(haystack: str, needle: str, basename: str) -> int | None:
    base_hit = _subsequence_score(basename, needle)
    if base_hit is not None:
        return base_hit + 200
    return _subsequence_score(haystack, needle)


def _subsequence_score(text: str, needle: str) -> int | None:
    score, last, i = 0, -2, 0
    for ch in needle:
        i = text.find(ch, i)
        if i < 0:
            return None
        if i == last + 1:
            score += 12  # contiguous
        if i == 0 or text[i - 1] in "/_-. ":
            score += 8  # start of a word
        score -= i // 24  # later is worse, but mildly
        last = i
        i += 1
    return score


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------


def grep(
    root: str,
    query: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    whole_word: bool = False,
    include: str = "",
    max_results: int = 2000,
) -> dict:
    """Search file contents; results grouped by file, in path order."""
    base = Path(root).expanduser()
    if not query or not base.is_dir():
        return {"files": [], "matches": 0, "truncated": False, "engine": "none"}

    if RG:
        hits, truncated = _rg(base, query, regex, case_sensitive, whole_word, include, max_results)
        engine = "ripgrep"
    else:
        hits, truncated = _walk(base, query, regex, case_sensitive, whole_word, include, max_results)
        engine = "python"

    grouped: dict[str, list[dict]] = {}
    for hit in hits:
        grouped.setdefault(hit["path"], []).append(hit)
    files_out = [
        {"path": path, "matches": rows, "count": len(rows)}
        for path, rows in sorted(grouped.items())
    ]
    return {
        "files": files_out,
        "matches": len(hits),
        "truncated": truncated,
        "engine": engine,
    }


def _rg(base, query, regex, case_sensitive, whole_word, include, max_results):
    args = [RG, "--json", "--hidden", "--max-columns", "400", *_rg_globs()]
    if not regex:
        args.append("--fixed-strings")
    args.append("--case-sensitive" if case_sensitive else "--ignore-case")
    if whole_word:
        args.append("--word-regexp")
    if include:
        for pattern in include.split(","):
            pattern = pattern.strip()
            if pattern:
                args += ["--glob", pattern]
    args += ["--", query]

    try:
        proc = subprocess.run(args, cwd=base, capture_output=True, text=True, timeout=TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return [], True

    import json

    hits = []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "match":
            continue
        data = event["data"]
        text = data["lines"].get("text", "").rstrip("\n")
        for sub in data.get("submatches", []):
            hits.append(
                {
                    "path": data["path"].get("text", ""),
                    "line": data["line_number"],
                    "column": sub["start"],
                    "end": sub["end"],
                    "text": text[:400],
                }
            )
            if len(hits) >= max_results:
                return hits, True
    return hits, False


def _walk(base, query, regex, case_sensitive, whole_word, include, max_results):
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = query if regex else re.escape(query)
    if whole_word:
        pattern = rf"\b{pattern}\b"
    try:
        rx = re.compile(pattern, flags)
    except re.error:
        return [], False

    globs = [g.strip() for g in include.split(",") if g.strip()]
    hits = []
    for rel in files(str(base)):
        if globs and not any(Path(rel).match(g) for g in globs):
            continue
        full = base / rel
        try:
            if full.stat().st_size > 2_000_000:
                continue
            text = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for m in rx.finditer(line):
                hits.append(
                    {
                        "path": rel,
                        "line": n,
                        "column": m.start(),
                        "end": m.end(),
                        "text": line[:400],
                    }
                )
                if len(hits) >= max_results:
                    return hits, True
    return hits, False


def replace_in_file(root: str, rel_path: str, query: str, replacement: str, *, regex: bool, case_sensitive: bool) -> int:
    """Apply a search-and-replace to one file; returns how many it changed."""
    target = (Path(root).expanduser() / rel_path).resolve()
    base = Path(root).expanduser().resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise ValueError(f"refusing to edit {target}")
    text = target.read_text(encoding="utf-8")
    flags = 0 if case_sensitive else re.IGNORECASE
    rx = re.compile(query if regex else re.escape(query), flags)
    new_text, count = rx.subn(replacement if regex else replacement.replace("\\", "\\\\"), text)
    if count:
        target.write_text(new_text, encoding="utf-8")
    return count
