"""Descant backend.

    python3 -m descant.server        # or: uvicorn descant.server:app

An editor's worth of filesystem, search and git, plus one optional Claude Code
conversation per folder. The Electron shell is a thin client over this; nothing
in the renderer knows how to touch a file or run a command by itself.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import chat, config, git, search


async def _exit_with_parent() -> None:
    """Follow the app down.

    Chats own live ``claude`` processes. If Electron dies without getting to
    SIGTERM us -- a crash, a kill -9 -- nothing else would ever stop them, and an
    orphaned agent keeps working (and spending) with no window to show for it.
    """
    ppid = os.environ.get("DESCANT_PARENT_PID")
    if not ppid or not ppid.isdigit():
        return
    parent = int(ppid)
    while True:
        await asyncio.sleep(2)
        try:
            os.kill(parent, 0)
        except (ProcessLookupError, PermissionError):
            await chat.REGISTRY.close_all()
            os._exit(0)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    watchdog = asyncio.create_task(_exit_with_parent())
    yield
    watchdog.cancel()
    await chat.REGISTRY.close_all()


app = FastAPI(title="Descant", version="0.2.0", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local-only desktop app; the Electron origin is file://
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "claude": shutil.which(config.CLAUDE_BIN) or "",
        "git": shutil.which("git") or "",
        "gh": shutil.which("gh") or "",
        "ripgrep": search.RG or "",
    }


# --------------------------------------------------------------------------
# the filesystem
# --------------------------------------------------------------------------


def _guard_path(target: str, root: str) -> Path:
    """Resolve *target* and refuse anything that escapes *root*.

    Every write path in this file goes through here. A traversal in a name
    (``../../.ssh/authorized_keys``) has to fail closed, not merely look wrong.
    """
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise HTTPException(400, f"{base} is not a directory")
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = base / path
    # Resolve the target the same way as the base, or a folder living under a
    # symlinked directory (/tmp and /var are symlinks on macOS) would fail the
    # containment check even though it is plainly inside. Resolving also means a
    # symlink pointing out of the folder is caught rather than followed.
    resolved = path.resolve()
    if resolved != base and not resolved.is_relative_to(base):
        raise HTTPException(400, f"refusing to touch {resolved}: outside {base}")
    return resolved


@app.get("/api/files")
def list_files(path: str, limit: int = 4000, hidden: bool = True) -> dict:
    """One directory, not the whole tree.

    Listing a single level and letting the UI expand what it needs means a big
    repo costs nothing until you look inside it.
    """
    root = Path(path).expanduser()
    if not root.is_dir():
        raise HTTPException(404, f"not a directory: {root}")

    dirs, files = [], []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        raise HTTPException(400, str(exc))

    for entry in entries:
        if entry.name in search.SKIP_DIRS:
            continue
        if not hidden and entry.name.startswith("."):
            continue
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if is_dir:
            dirs.append({"path": str(entry), "name": entry.name, "dir": True})
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            files.append({"path": str(entry), "name": entry.name, "dir": False, "size": size})
        if len(dirs) + len(files) >= limit:
            break

    # Directories first, the convention every file tree uses.
    return {
        "root": str(root),
        "parent": str(root.parent),
        "name": root.name,
        "entries": dirs + files,
        "truncated": len(dirs) + len(files) >= limit,
        "git": git.is_repo(str(root)),
    }


class CreateBody(BaseModel):
    root: str
    path: str  # relative to root, or absolute inside it
    directory: bool = False
    content: str = ""


@app.post("/api/fs/create")
def create_entry(body: CreateBody) -> dict:
    """Create a file (any extension) or a folder, parents included."""
    target = _guard_path(body.path, body.root)
    if not target.name:
        raise HTTPException(400, "a name is required")
    if target.exists():
        raise HTTPException(409, f"{target.name} already exists")
    try:
        if body.directory:
            target.mkdir(parents=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body.content, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(400, f"could not create: {exc}")
    return {"path": str(target), "name": target.name, "dir": body.directory}


class RenameBody(BaseModel):
    root: str
    path: str
    to: str  # a bare name, or a path relative to root


@app.post("/api/fs/rename")
def rename_entry(body: RenameBody) -> dict:
    """Rename or move. A bare name stays put; a path with a slash moves."""
    source = _guard_path(body.path, body.root)
    if not source.exists():
        raise HTTPException(404, f"{source} does not exist")
    dest_raw = body.to if "/" in body.to else str(source.parent / body.to)
    dest = _guard_path(dest_raw, body.root)
    if dest == source:
        return {"path": str(dest), "name": dest.name}
    if dest.exists():
        raise HTTPException(409, f"{dest.name} already exists")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        source.rename(dest)
    except OSError as exc:
        raise HTTPException(400, f"could not rename: {exc}")
    return {"path": str(dest), "name": dest.name, "from": str(source)}


class DuplicateBody(BaseModel):
    root: str
    path: str


@app.post("/api/fs/duplicate")
def duplicate_entry(body: DuplicateBody) -> dict:
    source = _guard_path(body.path, body.root)
    if not source.exists():
        raise HTTPException(404, f"{source} does not exist")
    stem, suffix = (source.stem, source.suffix) if source.is_file() else (source.name, "")
    for n in range(1, 100):
        candidate = source.with_name(f"{stem} copy{'' if n == 1 else f' {n}'}{suffix}")
        if not candidate.exists():
            break
    else:
        raise HTTPException(409, "too many copies already")
    try:
        if source.is_dir():
            shutil.copytree(source, candidate)
        else:
            shutil.copy2(source, candidate)
    except OSError as exc:
        raise HTTPException(400, f"could not duplicate: {exc}")
    return {"path": str(candidate), "name": candidate.name, "dir": candidate.is_dir()}


class DeleteBody(BaseModel):
    root: str
    path: str
    recursive: bool = False


@app.post("/api/fs/delete")
def delete_entry(body: DeleteBody) -> dict:
    """Delete a file, or a folder you have confirmed you meant."""
    target = _guard_path(body.path, body.root)
    base = Path(body.root).expanduser().resolve()
    if target == base:
        raise HTTPException(400, "refusing to delete the folder you have open")
    if not target.exists():
        raise HTTPException(404, f"{target} does not exist")
    try:
        if target.is_dir():
            if not body.recursive and any(target.iterdir()):
                raise HTTPException(409, f"{target.name} is not empty")
            if body.recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
        else:
            target.unlink()
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(400, f"could not delete: {exc}")
    return {"deleted": str(target), "name": target.name}


@app.get("/api/file")
def read_file(path: str, max_bytes: int = 2_000_000) -> dict:
    """Read a file for the editor.

    ``truncated`` matters more than it looks: a partially-read file that the
    editor then saves would silently delete everything past the cut. The flag
    exists so the UI can refuse to write one back.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise HTTPException(404, f"not a file: {p}")
    size = p.stat().st_size
    raw = p.read_bytes()
    data = raw[:max_bytes]
    truncated = len(raw) > len(data)
    try:
        text = data.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        # A cut mid-codepoint is not evidence of a binary file, so retry on a
        # boundary before deciding.
        try:
            text = data.decode("utf-8", errors="ignore")
            raw[:1].decode("utf-8")
            binary = False
            truncated = True
        except UnicodeDecodeError:
            text = ""
            binary = True
    return {
        "path": str(p),
        "name": p.name,
        "text": text,
        "binary": binary,
        "size": size,
        "truncated": truncated,
        "mtime": p.stat().st_mtime,
    }


class WriteBody(BaseModel):
    path: str
    text: str
    root: str = ""


@app.put("/api/file")
def write_file(body: WriteBody) -> dict:
    """Save the editor's contents.

    Written through a temp file in the same directory and moved into place, so
    an interrupted save cannot leave a half-written source file behind.
    """
    target = _guard_path(body.path, body.root) if body.root else Path(body.path).expanduser()
    if target.exists() and target.is_dir():
        raise HTTPException(400, f"{target} is a directory")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".descant-tmp")
        tmp.write_text(body.text, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise HTTPException(400, f"could not save: {exc}")
    st = target.stat()
    return {"path": str(target), "size": st.st_size, "mtime": st.st_mtime}


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


@app.get("/api/search/files")
def search_files(root: str, q: str = "", limit: int = 50) -> dict:
    """The quick-open index, filtered and ranked server-side."""
    paths = _file_index(root)
    return {"root": root, "results": search.match_paths(paths, q, limit), "indexed": len(paths)}


#: Listing a big tree costs real time, so hold it until the caller says stale.
_INDEX: dict[str, list[str]] = {}


def _file_index(root: str) -> list[str]:
    key = str(Path(root).expanduser())
    if key not in _INDEX:
        _INDEX[key] = search.files(key)
    return _INDEX[key]


@app.post("/api/search/reindex")
def reindex(body: dict) -> dict:
    _INDEX.pop(str(Path(body.get("root", "")).expanduser()), None)
    return {"ok": True}


@app.get("/api/search/text")
def search_text(
    root: str,
    q: str,
    regex: bool = False,
    case: bool = False,
    word: bool = False,
    include: str = "",
    limit: int = 2000,
) -> dict:
    return search.grep(
        root,
        q,
        regex=regex,
        case_sensitive=case,
        whole_word=word,
        include=include,
        max_results=limit,
    )


class ReplaceBody(BaseModel):
    root: str
    paths: list[str] = []
    query: str
    replacement: str
    regex: bool = False
    case: bool = False


@app.post("/api/search/replace")
def search_replace(body: ReplaceBody) -> dict:
    changed = 0
    touched = []
    for rel in body.paths:
        try:
            n = search.replace_in_file(
                body.root,
                rel,
                body.query,
                body.replacement,
                regex=body.regex,
                case_sensitive=body.case,
            )
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            raise HTTPException(400, f"{rel}: {exc}")
        if n:
            changed += n
            touched.append(rel)
    return {"replaced": changed, "files": touched}


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------


def _git(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except git.GitError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/git/status")
def git_status(repo: str) -> dict:
    if not git.is_repo(repo):
        return {"repo": repo, "is_repo": False, "files": [], "staged": [], "changes": []}
    return {**_git(git.status, repo), "is_repo": True}


@app.get("/api/git/diff")
def git_diff(repo: str, path: str, staged: bool = False) -> dict:
    return _git(git.diff_sides, repo, path, staged)


@app.get("/api/git/log")
def git_log(repo: str, limit: int = 40) -> dict:
    return _git(git.log, repo, limit)


@app.get("/api/git/branches")
def git_branches(repo: str) -> dict:
    return _git(git.branches, repo)


@app.get("/api/git/github")
def git_github(repo: str) -> dict:
    return git.gh_overview(repo)


class RepoPaths(BaseModel):
    repo: str
    paths: list[str] = []


@app.post("/api/git/stage")
def git_stage(body: RepoPaths) -> dict:
    return _git(git.stage, body.repo, body.paths)


@app.post("/api/git/unstage")
def git_unstage(body: RepoPaths) -> dict:
    return _git(git.unstage, body.repo, body.paths)


@app.post("/api/git/discard")
def git_discard(body: RepoPaths) -> dict:
    if not body.paths:
        raise HTTPException(400, "name the paths to discard")
    return _git(git.discard, body.repo, body.paths)


class CommitBody(BaseModel):
    repo: str
    message: str
    amend: bool = False
    stage_all: bool = False


@app.post("/api/git/commit")
def git_commit(body: CommitBody) -> dict:
    return _git(git.commit, body.repo, body.message, body.amend, body.stage_all)


class RepoBody(BaseModel):
    repo: str


@app.post("/api/git/push")
def git_push(body: RepoBody) -> dict:
    return _git(git.push, body.repo)


@app.post("/api/git/pull")
def git_pull(body: RepoBody) -> dict:
    return _git(git.pull, body.repo)


@app.post("/api/git/fetch")
def git_fetch(body: RepoBody) -> dict:
    return _git(git.fetch, body.repo)


@app.post("/api/git/init")
def git_init(body: RepoBody) -> dict:
    return _git(git.init, body.repo)


class CheckoutBody(BaseModel):
    repo: str
    name: str
    create: bool = False


@app.post("/api/git/checkout")
def git_checkout(body: CheckoutBody) -> dict:
    return _git(git.checkout, body.repo, body.name, body.create)


# --------------------------------------------------------------------------
# running the open file
# --------------------------------------------------------------------------

#: extension -> argv prefix. The value is a list so nothing depends on shell
#: word-splitting; the file path is appended and quoted by the caller.
_RUNNERS = {
    ".py": ["python3"],
    ".js": ["node"],
    ".mjs": ["node"],
    ".cjs": ["node"],
    ".ts": ["npx", "tsx"],
    ".sh": ["bash"],
    ".bash": ["bash"],
    ".zsh": ["zsh"],
    ".rb": ["ruby"],
    ".php": ["php"],
    ".lua": ["lua"],
    ".pl": ["perl"],
    ".go": ["go", "run"],
    ".java": ["java"],
    ".swift": ["swift"],
    ".r": ["Rscript"],
    ".jl": ["julia"],
}


@app.get("/api/runner")
def runner_for(path: str, repo: str = "") -> dict:
    """How would you run this file?

    Detection rather than a fixed table where it matters: a folder with a
    ``.venv`` means ``python3`` is the wrong python, and a Rust file belongs to
    its crate rather than to itself. Returning the command (instead of running
    it) keeps the decision visible — the UI types it into the terminal, where
    you can see and edit it before it goes.
    """
    target = Path(path).expanduser()
    if not target.is_file():
        raise HTTPException(404, f"not a file: {target}")

    root = Path(repo).expanduser() if repo else target.parent
    suffix = target.suffix.lower()

    # A crate is built, not interpreted; the same is true of a Go module.
    for parent in [target.parent, *target.parents]:
        if not str(parent).startswith(str(root)):
            break
        if suffix == ".rs" and (parent / "Cargo.toml").is_file():
            return {"command": "cargo run", "cwd": str(parent), "kind": "cargo"}

    if suffix == ".rs":
        return {
            "command": f"rustc {shlex.quote(str(target))} -o /tmp/descant-run && /tmp/descant-run",
            "cwd": str(root),
            "kind": "rustc",
        }

    argv = _RUNNERS.get(suffix)
    if not argv:
        return {
            "command": "",
            "cwd": str(root),
            "kind": "none",
            "reason": f"no runner for {suffix or 'this file type'}",
        }

    # Prefer the folder's own interpreter over whatever is on PATH.
    if argv[0] == "python3":
        for candidate in (root / ".venv" / "bin" / "python3", root / "venv" / "bin" / "python3"):
            if candidate.is_file():
                argv = [str(candidate)]
                break

    return {
        "command": " ".join([*argv, shlex.quote(str(target))]),
        "cwd": str(root),
        "kind": argv[0],
    }


# --------------------------------------------------------------------------
# chat: one optional conversation per open folder
# --------------------------------------------------------------------------


class NewChatBody(BaseModel):
    repo: str
    permission_mode: str = "manual"
    resume_session: str | None = None


@app.post("/api/chats")
async def new_chat(body: NewChatBody) -> dict:
    try:
        c = await chat.REGISTRY.create(
            body.repo, permission_mode=body.permission_mode, resume_session=body.resume_session
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return c.meta()


@app.get("/api/chats")
def list_chats() -> dict:
    return {"chats": [c.meta() for c in chat.REGISTRY.all()]}


def _chat_or_404(chat_id: str) -> chat.Chat:
    try:
        return chat.REGISTRY.get(chat_id)
    except KeyError:
        raise HTTPException(404, f"no chat {chat_id}")


class SendBody(BaseModel):
    text: str


@app.post("/api/chats/{chat_id}/send")
async def chat_send(chat_id: str, body: SendBody) -> dict:
    c = _chat_or_404(chat_id)
    try:
        await c.send(body.text)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))
    return c.meta()


class PermissionBody(BaseModel):
    tool_use_id: str
    decision: str  # "allow" | "deny"
    remember: bool = False
    reason: str = ""


@app.post("/api/chats/{chat_id}/permission")
async def chat_permission(chat_id: str, body: PermissionBody) -> dict:
    c = _chat_or_404(chat_id)
    try:
        if body.decision == "allow":
            return await c.approve(body.tool_use_id, remember=body.remember)
        if body.decision == "deny":
            return await c.deny(body.tool_use_id, reason=body.reason)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    raise HTTPException(400, "decision must be 'allow' or 'deny'")


class ModeBody(BaseModel):
    permission_mode: str


@app.post("/api/chats/{chat_id}/mode")
async def chat_mode(chat_id: str, body: ModeBody) -> dict:
    c = _chat_or_404(chat_id)
    try:
        return await c.set_permission_mode(body.permission_mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/chats/{chat_id}/interrupt")
async def chat_interrupt(chat_id: str) -> dict:
    return await _chat_or_404(chat_id).interrupt()


@app.post("/api/chats/{chat_id}/close")
async def chat_close(chat_id: str) -> dict:
    await chat.REGISTRY.close(chat_id)
    return {"closed": chat_id}


@app.websocket("/ws/chats/{chat_id}")
async def ws_chat(ws: WebSocket, chat_id: str) -> None:
    """Follow one conversation. Replays what was said, then streams the rest."""
    await ws.accept()
    try:
        c = chat.REGISTRY.get(chat_id)
    except KeyError:
        await ws.send_json({"kind": "error", "text": f"no chat {chat_id}"})
        await ws.close()
        return

    queue = c.subscribe()
    try:
        await ws.send_json({"kind": "meta", "chat": c.meta()})
        for ev in list(c.history):
            await ws.send_json({"event": ev, "meta": c.meta()})
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=20.0)
            except asyncio.TimeoutError:
                await ws.send_json({"kind": "ping"})
                continue
            await ws.send_json(payload)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        c.unsubscribe(queue)


def main() -> None:
    import uvicorn

    uvicorn.run(
        app, host=config.HOST, port=config.PORT, log_level=os.environ.get("DESCANT_LOG", "warning")
    )


if __name__ == "__main__":
    main()
