"""Git and GitHub, shelled out to the tools that already work.

No libgit2, no GitPython. `git` is installed on every machine that has a repo on
it, its porcelain formats are stable by contract, and anything this module can't
express you can still type into the terminal panel three inches below. `gh` is
optional and every call degrades to "not installed" rather than an error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

TIMEOUT = 20


class GitError(RuntimeError):
    """A git invocation that failed, carrying whatever git said about it."""


def _run(repo: str, *args: str, timeout: int = TIMEOUT, check: bool = True) -> str:
    cwd = Path(repo).expanduser()
    if not cwd.is_dir():
        raise GitError(f"not a directory: {cwd}")
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            # A pager or a credential prompt here would hang the request forever.
            env={**os.environ, "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError:
        raise GitError("git is not installed")
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out after {timeout}s")
    if check and proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout or f"git {args[0]} failed").strip())
    return proc.stdout


def is_repo(repo: str) -> bool:
    try:
        return _run(repo, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except GitError:
        return False


def root(repo: str) -> str:
    return _run(repo, "rev-parse", "--show-toplevel").strip()


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

#: XY codes from `git status --porcelain=v1`, in the order the UI shows them.
_LABELS = {
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "U": "conflict",
    "?": "untracked",
    "!": "ignored",
    " ": "",
}


def status(repo: str) -> dict:
    """Branch, upstream divergence, and every changed path.

    `-z` rather than the human format: a filename with a space or a newline in
    it is legal, and parsing the quoted form is how tools get this wrong.
    """
    out = _run(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--branch")
    parts = out.split("\0")

    branch, upstream, ahead, behind, detached = "", "", 0, 0, False
    files: list[dict] = []

    i = 0
    while i < len(parts):
        raw = parts[i]
        i += 1
        if not raw:
            continue
        if raw.startswith("## "):
            branch, upstream, ahead, behind, detached = _parse_branch_line(raw[3:])
            continue
        index, worktree, path = raw[0], raw[1], raw[3:]
        renamed_from = ""
        if index in ("R", "C"):
            # Rename entries are followed by their source path as its own record.
            renamed_from = parts[i] if i < len(parts) else ""
            i += 1
        files.append(
            {
                "path": path,
                "index": index.strip(),
                "worktree": worktree.strip(),
                "staged": index not in (" ", "?"),
                "unstaged": worktree not in (" ",),
                "untracked": index == "?",
                "conflict": "U" in (index, worktree),
                "label": _LABELS.get(worktree if worktree != " " else index, ""),
                "renamed_from": renamed_from,
            }
        )

    files.sort(key=lambda f: f["path"])
    return {
        "repo": root(repo),
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "detached": detached,
        "files": files,
        "staged": [f for f in files if f["staged"]],
        "changes": [f for f in files if not f["staged"]],
        "clean": not files,
    }


def _parse_branch_line(line: str) -> tuple[str, str, int, int, bool]:
    """`main...origin/main [ahead 2, behind 1]` and its many degenerate forms."""
    if line.startswith("HEAD (no branch)"):
        return "HEAD", "", 0, 0, True
    divergence = ""
    if " [" in line:
        line, _, divergence = line.partition(" [")
        divergence = divergence.rstrip("]")
    branch, _, upstream = line.partition("...")
    ahead = behind = 0
    for chunk in divergence.split(", "):
        name, _, n = chunk.partition(" ")
        if name == "ahead" and n.isdigit():
            ahead = int(n)
        elif name == "behind" and n.isdigit():
            behind = int(n)
    return branch.strip(), upstream.strip(), ahead, behind, False


# --------------------------------------------------------------------------
# diffs
# --------------------------------------------------------------------------


def show(repo: str, path: str, rev: str = "HEAD") -> str:
    """One file's contents at *rev*, or "" when it did not exist there."""
    try:
        return _run(repo, "show", f"{rev}:{path}")
    except GitError:
        return ""


def diff(repo: str, path: str = "", staged: bool = False) -> str:
    args = ["diff", "--no-color"]
    if staged:
        args.append("--cached")
    if path:
        args += ["--", path]
    return _run(repo, *args)


def diff_sides(repo: str, path: str, staged: bool = False) -> dict:
    """The two texts a side-by-side diff needs.

    Staged view compares HEAD to the index; unstaged compares the index (which
    is HEAD for an unmodified path) to what is on disk. An untracked file has no
    left side at all.
    """
    st = status(repo)
    entry = next((f for f in st["files"] if f["path"] == path), None)
    if entry and entry["untracked"]:
        return {"before": "", "after": _read(st["repo"], path), "untracked": True}
    if staged:
        return {"before": show(repo, path, "HEAD"), "after": _stage_text(repo, path)}
    before = _stage_text(repo, path)
    if not before and not (entry and entry["staged"]):
        before = show(repo, path, "HEAD")
    return {"before": before, "after": _read(st["repo"], path)}


def _stage_text(repo: str, path: str) -> str:
    try:
        return _run(repo, "show", f":{path}")
    except GitError:
        return ""


def _read(repo_root: str, path: str) -> str:
    p = Path(repo_root) / path
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------


def stage(repo: str, paths: list[str]) -> dict:
    if paths:
        _run(repo, "add", "--", *paths)
    else:
        _run(repo, "add", "-A")
    return status(repo)


def unstage(repo: str, paths: list[str]) -> dict:
    # `restore --staged` on a repo with no commits yet has no HEAD to restore
    # from; `rm --cached` is the equivalent that works there.
    try:
        if paths:
            _run(repo, "restore", "--staged", "--", *paths)
        else:
            _run(repo, "reset")
    except GitError:
        _run(repo, "rm", "--cached", "-r", "--", *(paths or ["."]))
    return status(repo)


def discard(repo: str, paths: list[str]) -> dict:
    """Throw away working-tree changes. Destructive; the UI confirms first."""
    st = status(repo)
    tracked = [p for p in paths if not _is_untracked(st, p)]
    untracked = [p for p in paths if _is_untracked(st, p)]
    if tracked:
        _run(repo, "checkout", "--", *tracked)
    for rel in untracked:
        target = Path(st["repo"]) / rel
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
    return status(repo)


def _is_untracked(st: dict, path: str) -> bool:
    return any(f["path"] == path and f["untracked"] for f in st["files"])


def commit(repo: str, message: str, amend: bool = False, stage_all: bool = False) -> dict:
    if not message.strip() and not amend:
        raise GitError("a commit message is required")
    args = ["commit", "-m", message]
    if amend:
        args.append("--amend")
    if stage_all:
        args.append("-a")
    out = _run(repo, *args)
    return {"output": out.strip(), "status": status(repo)}


def push(repo: str, set_upstream: bool = False) -> dict:
    st = status(repo)
    args = ["push"]
    if set_upstream or not st["upstream"]:
        args += ["--set-upstream", "origin", st["branch"]]
    return {"output": _run(repo, *args, timeout=120).strip(), "status": status(repo)}


def pull(repo: str) -> dict:
    return {"output": _run(repo, "pull", "--ff-only", timeout=120).strip(), "status": status(repo)}


def fetch(repo: str) -> dict:
    return {"output": _run(repo, "fetch", "--all", "--prune", timeout=120).strip(), "status": status(repo)}


def branches(repo: str) -> dict:
    def refs(namespace: str) -> list[dict]:
        out = _run(
            repo,
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname:short)\t%(upstream:short)\t%(committerdate:relative)",
            namespace,
        )
        rows = []
        for line in out.splitlines():
            name, _, rest = line.partition("\t")
            upstream, _, when = rest.partition("\t")
            if name.endswith("/HEAD"):
                continue
            rows.append({"name": name, "upstream": upstream, "when": when})
        return rows

    return {
        "current": status(repo)["branch"],
        "local": refs("refs/heads"),
        "remote": refs("refs/remotes"),
    }


def checkout(repo: str, name: str, create: bool = False) -> dict:
    _run(repo, "checkout", *(["-b"] if create else []), name)
    return status(repo)


def log(repo: str, limit: int = 40) -> dict:
    out = _run(
        repo,
        "log",
        f"-{limit}",
        "--date=relative",
        "--pretty=format:%h\t%an\t%ad\t%s",
    )
    commits = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            commits.append(dict(zip(("hash", "author", "when", "subject"), parts)))
    return {"commits": commits}


def init(repo: str) -> dict:
    _run(repo, "init")
    return status(repo)


# --------------------------------------------------------------------------
# gh
# --------------------------------------------------------------------------


def gh_available() -> bool:
    return shutil.which("gh") is not None


def gh(repo: str, *args: str, timeout: int = 30) -> str:
    if not gh_available():
        raise GitError("the GitHub CLI (gh) is not installed")
    proc = subprocess.run(
        ["gh", *args],
        cwd=Path(repo).expanduser(),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "GH_PAGER": "cat", "NO_COLOR": "1"},
    )
    if proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout or "gh failed").strip())
    return proc.stdout


def gh_overview(repo: str) -> dict:
    """What the source-control panel shows about GitHub, best effort.

    Every part is allowed to fail independently: no gh, not logged in, no
    remote, or a repo that simply has no pull requests are all normal states,
    not errors worth interrupting the panel for.
    """
    if not gh_available():
        return {"available": False, "reason": "gh is not installed"}
    info: dict = {"available": True, "authed": False, "repo": "", "prs": []}
    try:
        gh(repo, "auth", "status")
        info["authed"] = True
    except Exception as exc:
        info["reason"] = str(exc).splitlines()[0] if str(exc) else "not signed in"
        return info
    try:
        import json

        info["repo"] = json.loads(gh(repo, "repo", "view", "--json", "nameWithOwner"))[
            "nameWithOwner"
        ]
    except Exception:
        pass
    try:
        import json

        info["prs"] = json.loads(
            gh(repo, "pr", "list", "--limit", "10", "--json", "number,title,author,isDraft,url")
        )
    except Exception:
        pass
    return info
