"""Tests for the parts everything else stands on.

    cd backend && python3 -m tests.test_descant

Plain asserts, no pytest dependency -- this needs to run anywhere. What's
covered is the stuff a wrong answer would be dangerous or invisible: path
containment on every write, the fuzzy ranking Cmd+P lives on, and git porcelain
parsing against a real repo built in a temp dir.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from descant import git, search, server  # noqa: E402

# The endpoints are ordinary functions, so they are called directly rather than
# through a test client -- that would pull in httpx purely to exercise routing
# FastAPI already tests.

PASSED = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {extra}")
        raise AssertionError(label)


def _repo(tmp: str) -> str:
    """A real git repo with one commit, because parsing needs real output."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp, env=env, capture_output=True, check=True)
    run("init", "-q", "-b", "main")
    Path(tmp, "a.txt").write_text("one\ntwo\n")
    run("add", "-A")
    run("commit", "-q", "-m", "first")
    return tmp


# --------------------------------------------------------------------------
# path containment
# --------------------------------------------------------------------------


def test_guard_path_refuses_escapes():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp, "repo")
        (root / "sub").mkdir(parents=True)

        inside = server._guard_path("sub/new.py", str(root))
        check("a relative path resolves under the root", inside == (root / "sub" / "new.py").resolve())

        for attempt in ("../outside.py", "../../etc/passwd", "sub/../../escape"):
            try:
                server._guard_path(attempt, str(root))
                check(f"{attempt} was refused", False)
            except HTTPException:
                check(f"{attempt} was refused", True)

        # A symlink pointing out of the tree must be caught, not followed.
        target = Path(tmp, "elsewhere")
        target.mkdir()
        (root / "link").symlink_to(target)
        try:
            server._guard_path("link/x", str(root))
            check("a symlink out of the root was refused", False)
        except HTTPException:
            check("a symlink out of the root was refused", True)


# --------------------------------------------------------------------------
# fuzzy file matching
# --------------------------------------------------------------------------


def test_fuzzy_ranking():
    paths = [
        "app/renderer/app.js",
        "packages/adjacent/other.ts",
        "app/renderer/editor.js",
        "README.md",
    ]
    top = search.match_paths(paths, "appjs")[0]["path"]
    check("basename matches win", top == "app/renderer/app.js", top)

    hits = [r["path"] for r in search.match_paths(paths, "edit")]
    check("a substring of the basename matches", "app/renderer/editor.js" in hits, str(hits))

    check("nonsense matches nothing", search.match_paths(paths, "zzzqqq") == [])
    check("an empty query returns everything", len(search.match_paths(paths, "")) == 4)


def test_text_search_finds_and_skips():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "hit.py").write_text("alpha = 1\nbeta = alpha + 1\n")
        Path(tmp, "node_modules").mkdir()
        Path(tmp, "node_modules", "junk.py").write_text("alpha\n")

        res = search.grep(tmp, "alpha")
        paths = {f["path"] for f in res["files"]}
        check("the match was found", "hit.py" in paths, str(paths))
        check("node_modules was skipped", not any("node_modules" in p for p in paths), str(paths))
        check("both occurrences were counted", res["matches"] == 2, str(res["matches"]))

        none = search.grep(tmp, "ALPHA", case_sensitive=True)
        check("case-sensitive search respects case", none["matches"] == 0)


def test_replace_refuses_outside_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp, "root")
        root.mkdir()
        (root / "f.txt").write_text("cat cat\n")
        n = search.replace_in_file(str(root), "f.txt", "cat", "dog", regex=False, case_sensitive=True)
        check("both occurrences were replaced", n == 2)
        check("the file was rewritten", (root / "f.txt").read_text() == "dog dog\n")
        try:
            search.replace_in_file(str(root), "../escape.txt", "a", "b", regex=False, case_sensitive=True)
            check("replace refuses to escape the root", False)
        except ValueError:
            check("replace refuses to escape the root", True)


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------


def test_git_status_parsing():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp)
        check("a repo is recognised", git.is_repo(repo))
        check("a bare directory is not", not git.is_repo(tempfile.gettempdir() + "/definitely-not-a-repo"))

        st = git.status(repo)
        check("a clean repo reports clean", st["clean"], str(st["files"]))
        check("the branch was read", st["branch"] == "main", st["branch"])

        Path(repo, "a.txt").write_text("one\ntwo\nthree\n")
        Path(repo, "new file.txt").write_text("x\n")  # a space, on purpose
        st = git.status(repo)
        by_path = {f["path"]: f for f in st["files"]}
        check("the edit shows as unstaged", by_path["a.txt"]["unstaged"], str(by_path))
        check("the new file shows as untracked", by_path["new file.txt"]["untracked"])
        check("a filename with a space survives parsing", "new file.txt" in by_path)

        git.stage(repo, ["a.txt"])
        st = git.status(repo)
        check("staging moves it to staged", st["staged"][0]["path"] == "a.txt", str(st["staged"]))

        sides = git.diff_sides(repo, "a.txt", staged=True)
        check("the staged diff has both sides", sides["before"] == "one\ntwo\n" and "three" in sides["after"])

        git.unstage(repo, ["a.txt"])
        check("unstaging moves it back", not git.status(repo)["staged"])

        git.stage(repo, ["a.txt"])
        git.commit(repo, "second")
        log = git.log(repo)
        check("the commit landed", log["commits"][0]["subject"] == "second", str(log["commits"][:1]))

        branches = git.branches(repo)
        check("the current branch is named", branches["current"] == "main")
        git.checkout(repo, "feature", create=True)
        check("a new branch is checked out", git.status(repo)["branch"] == "feature")


def test_git_discard():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp)
        Path(repo, "a.txt").write_text("ruined\n")
        Path(repo, "junk.txt").write_text("junk\n")
        git.discard(repo, ["a.txt", "junk.txt"])
        check("a tracked edit was reverted", Path(repo, "a.txt").read_text() == "one\ntwo\n")
        check("an untracked file was removed", not Path(repo, "junk.txt").exists())


# --------------------------------------------------------------------------
# the HTTP surface
# --------------------------------------------------------------------------


def test_file_endpoints_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        made = server.create_entry(server.CreateBody(root=tmp, path="src/main.py"))
        check("create makes parent directories", Path(made["path"]).is_file(), str(made))

        try:
            server.create_entry(server.CreateBody(root=tmp, path="src/main.py"))
            check("creating twice conflicts", False)
        except HTTPException as exc:
            check("creating twice conflicts", exc.status_code == 409, str(exc.detail))

        server.write_file(server.WriteBody(root=tmp, path=made["path"], text="print('hi')\n"))
        check(
            "the write round-trips",
            server.read_file(made["path"])["text"] == "print('hi')\n",
        )

        renamed = server.rename_entry(server.RenameBody(root=tmp, path=made["path"], to="app.py"))
        check("rename keeps the file in place", renamed["name"] == "app.py", str(renamed))

        copy = server.duplicate_entry(server.DuplicateBody(root=tmp, path=renamed["path"]))
        check("duplicate names the copy", copy["name"] == "app copy.py", str(copy))

        try:
            server.delete_entry(server.DeleteBody(root=tmp, path=str(Path(tmp, "src"))))
            check("a non-empty folder needs confirming", False)
        except HTTPException as exc:
            check("a non-empty folder needs confirming", exc.status_code == 409)

        server.delete_entry(server.DeleteBody(root=tmp, path=str(Path(tmp, "src")), recursive=True))
        check("recursive delete works", not Path(tmp, "src").exists())

        try:
            server.delete_entry(server.DeleteBody(root=tmp, path=tmp))
            check("the open folder itself cannot be deleted", False)
        except HTTPException:
            check("the open folder itself cannot be deleted", True)


def test_binary_and_truncation_flags():
    with tempfile.TemporaryDirectory() as tmp:
        blob = Path(tmp, "x.bin")
        blob.write_bytes(bytes([0xFF, 0xFE, 0x00, 0x01]))
        check("a binary file is flagged", server.read_file(str(blob))["binary"])

        big = Path(tmp, "big.txt")
        big.write_text("x" * 5000)
        got = server.read_file(str(big), max_bytes=100)
        check("a partial read is flagged truncated", got["truncated"] and len(got["text"]) == 100)


def test_listing_hides_noise():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "node_modules").mkdir()
        Path(tmp, ".hidden").write_text("x")
        Path(tmp, "visible.py").write_text("x")
        names = {e["name"] for e in server.list_files(tmp)["entries"]}
        check("node_modules never appears", "node_modules" not in names, str(names))
        check("dotfiles appear by default", ".hidden" in names, str(names))
        shown = {e["name"] for e in server.list_files(tmp, hidden=False)["entries"]}
        check("and can be hidden on request", ".hidden" not in shown and "visible.py" in shown)


def test_git_endpoints():
    with tempfile.TemporaryDirectory() as tmp:
        plain = server.git_status(tmp)
        check("a non-repo answers rather than erroring", plain["is_repo"] is False, str(plain))

        repo = _repo(tmp)
        st = server.git_status(repo)
        check("a repo reports its branch", st["branch"] == "main" and st["is_repo"], str(st))


def test_runner_detection():
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp, "go.py")
        script.write_text("print(1)\n")
        cmd = server.runner_for(str(script), tmp)
        check("python files get a python command", cmd["kind"] == "python3", str(cmd))

        venv = Path(tmp, ".venv", "bin")
        venv.mkdir(parents=True)
        (venv / "python3").write_text("#!/bin/sh\n")
        (venv / "python3").chmod(0o755)
        cmd = server.runner_for(str(script), tmp)
        check("a local venv wins over PATH", ".venv/bin/python3" in cmd["command"], cmd["command"])

        odd = Path(tmp, "notes.xyz")
        odd.write_text("")
        check("an unknown type reports no runner", server.runner_for(str(odd), tmp)["kind"] == "none")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\n{PASSED} checks passed across {len(tests)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
