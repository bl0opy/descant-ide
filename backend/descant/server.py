"""Descant backend.

    python3 -m descant.server        # or: uvicorn descant.server:app

Serves the parsed transcript history, the context-cost analysis, and live
``claude`` runs over WebSockets.  The Electron shell is a thin client over this.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import chat, config, events, library, loadout, mcp, mining, skills
from .inspector import analyze
from .runner import MANAGER
from .tailer import newest_transcript, tail_session
from .transcript import Session, load_all, load_session

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
    """Live chats own subprocesses; a server exit must not orphan them."""
    watchdog = asyncio.create_task(_exit_with_parent())
    yield
    watchdog.cancel()
    await chat.REGISTRY.close_all()


app = FastAPI(title="Descant", version="0.1.0", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local-only desktop app; the Electron origin is file://
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# transcript cache
# --------------------------------------------------------------------------


class _Cache:
    """Re-parse a transcript only when its file actually changed.

    Keyed on (mtime, size) so a real ~/.claude/projects with thousands of files
    stays responsive after the first load.
    """

    def __init__(self) -> None:
        self._by_path: dict[str, tuple[tuple[float, int], Session]] = {}

    def all(self) -> list[Session]:
        sessions: list[Session] = []
        seen: set[str] = set()
        for root in config.projects_dirs():
            if not root.exists():
                continue
            for project in sorted(p for p in root.iterdir() if p.is_dir()):
                for jsonl in sorted(project.glob("*.jsonl")):
                    if str(jsonl) in seen:
                        continue
                    seen.add(str(jsonl))
                    try:
                        st = jsonl.stat()
                    except OSError:
                        continue
                    stamp = (st.st_mtime, st.st_size)
                    hit = self._by_path.get(str(jsonl))
                    if hit and hit[0] == stamp:
                        sessions.append(hit[1])
                        continue
                    try:
                        sess = load_session(jsonl, project.name)
                    except OSError:
                        continue
                    self._by_path[str(jsonl)] = (stamp, sess)
                    sessions.append(sess)
        sessions.sort(key=lambda s: s.last_activity.timestamp() if s.last_activity else 0, reverse=True)
        return sessions

    def forget(self, path: str) -> None:
        self._by_path.pop(path, None)

    def one(self, session_id: str) -> Session | None:
        for s in self.all():
            if s.session_id == session_id or s.session_id.startswith(session_id):
                return s
        return None


CACHE = _Cache()

#: Probing MCP servers spawns processes, so per-repo attribution is memoised for
#: the life of the server. Toggling a server clears it.
_ATTRIBUTION_CACHE: dict[str, dict] = {}


# --------------------------------------------------------------------------
# read endpoints
# --------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    roots = config.projects_dirs()
    return {
        "ok": True,
        "projects_dir": str(roots[0]),
        "projects_dirs": [str(r) for r in roots],
        "live_projects_dir": str(config.live_projects_dir()),
        "projects_dir_exists": any(r.exists() for r in roots),
        "claude_bin": config.CLAUDE_BIN,
        "live_runs": len(MANAGER.runs),
    }


async def _attribution_for(repo_path: str) -> dict:
    """Measured MCP + skill cost for a repo, for the inspector's preamble split.

    Cached per repo because probing MCP servers spawns processes; a session list
    over twenty repos must not spawn twenty server handshakes on every refresh.
    """
    if repo_path in _ATTRIBUTION_CACHE:
        return _ATTRIBUTION_CACHE[repo_path]
    result = {"mcp_tokens": 0, "skill_tokens": 0, "source": "not measured"}
    if Path(repo_path).is_dir():
        try:
            servers = await mcp.discover_and_probe(repo_path, do_probe=True)
            found = skills.discover(repo_path)
            result = {
                "mcp_tokens": sum(s.token_cost for s in servers if s.enabled),
                "skill_tokens": skills.listing_cost(found),
                "source": "MCP probe + SKILL.md frontmatter",
            }
        except Exception:
            pass
    _ATTRIBUTION_CACHE[repo_path] = result
    return result


@app.get("/api/sessions")
async def list_sessions() -> dict:
    """Every historical session, grouped by repo, with its context breakdown."""
    sessions = CACHE.all()
    attributions = {}
    for repo in {s.repo_path for s in sessions}:
        attributions[repo] = await _attribution_for(repo)
    reports = [analyze(s, attribution=attributions.get(s.repo_path)) for s in sessions]
    repos: dict[str, dict] = {}
    for r in reports:
        s = r.session
        bucket = repos.setdefault(
            s.repo_path,
            {
                "repo_path": s.repo_path,
                "repo_name": s.repo_name,
                # Transcripts routinely refer to repos that are not on *this*
                # machine (a fixture, or history synced from another laptop).
                # The UI needs to know before offering to run a live session there.
                "exists": Path(s.repo_path).is_dir(),
                "sessions": [],
                "total_tokens": 0,
            },
        )
        bucket["sessions"].append(r.to_dict())
        bucket["total_tokens"] += r.total_tokens

    ordered = sorted(
        repos.values(),
        key=lambda b: b["sessions"][0]["last_activity"] or "",
        reverse=True,
    )
    return {
        "projects_dir": os.pathsep.join(str(r) for r in config.projects_dirs()),
        "repos": ordered,
        "session_count": len(reports),
        "live": MANAGER.list(),
    }


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    sess = CACHE.one(session_id)
    if sess is None:
        raise HTTPException(404, f"no session {session_id}")
    report = analyze(sess, attribution=await _attribution_for(sess.repo_path))
    return {
        **report.to_dict(),
        "events": [events.from_transcript_event(e) for e in sess.events],
    }


@app.get("/api/sessions/{session_id}/context")
async def get_context(session_id: str) -> dict:
    sess = CACHE.one(session_id)
    if sess is None:
        raise HTTPException(404, f"no session {session_id}")
    return analyze(sess, attribution=await _attribution_for(sess.repo_path)).to_dict()


@app.get("/api/files")
def list_files(path: str, limit: int = 4000) -> dict:
    """One directory, not the whole tree.

    The explorer used to flatten every file under a repo into one list of
    relative paths, which loses the structure people actually navigate by — and
    hits a truncation limit on any real project. This lists a single level and
    lets the UI expand what it needs, so a big repo costs nothing until you look
    inside it.
    """
    root = Path(path).expanduser()
    if not root.is_dir():
        raise HTTPException(404, f"not a directory: {root}")

    skip = {".git", "node_modules", "__pycache__", ".venv", "dist", "build", ".next"}
    dirs, files = [], []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        raise HTTPException(400, str(exc))

    for entry in entries:
        if entry.name in skip:
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
            files.append(
                {"path": str(entry), "name": entry.name, "dir": False, "size": size}
            )
        if len(dirs) + len(files) >= limit:
            break

    # Directories first, the convention every file tree uses.
    return {
        "root": str(root),
        "parent": str(root.parent),
        "entries": dirs + files,
        "truncated": len(dirs) + len(files) >= limit,
    }


# --------------------------------------------------------------------------
# creating and deleting files
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
    # Resolve the target the same way as the base, or a repo living under a
    # symlinked directory (/tmp and /var are symlinks on macOS) would fail the
    # containment check even though it is plainly inside. Resolving also means a
    # symlink pointing out of the repo is caught rather than followed.
    resolved = path.resolve()
    if resolved != base and not resolved.is_relative_to(base):
        raise HTTPException(400, f"refusing to touch {resolved}: outside {base}")
    return resolved


class CreateFileBody(BaseModel):
    root: str
    path: str          # relative to root, or absolute inside it
    directory: bool = False
    content: str = ""


@app.post("/api/fs/create")
def create_entry(body: CreateFileBody) -> dict:
    """Create a file (any extension) or a folder."""
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


class DeleteFileBody(BaseModel):
    root: str
    path: str
    recursive: bool = False


@app.post("/api/fs/delete")
def delete_entry(body: DeleteFileBody) -> dict:
    """Delete a file, or a folder you have confirmed you meant."""
    target = _guard_path(body.path, body.root)
    base = Path(body.root).expanduser().resolve()
    if target == base:
        raise HTTPException(400, "refusing to delete the repo itself")
    if not target.exists():
        raise HTTPException(404, f"{target} does not exist")
    try:
        if target.is_dir():
            if not body.recursive and any(target.iterdir()):
                raise HTTPException(409, f"{target.name} is not empty")
            shutil.rmtree(target) if body.recursive else target.rmdir()
        else:
            target.unlink()
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(400, f"could not delete: {exc}")
    return {"deleted": str(target), "name": target.name}


@app.get("/api/file")
def read_file(path: str, max_bytes: int = 512_000) -> dict:
    p = Path(path).expanduser()
    if not p.is_file():
        raise HTTPException(404, f"not a file: {p}")
    data = p.read_bytes()[:max_bytes]
    try:
        text = data.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        text = ""
        binary = True
    return {"path": str(p), "text": text, "binary": binary, "size": p.stat().st_size}


class WriteFileBody(BaseModel):
    path: str
    text: str
    root: str = ""


@app.put("/api/file")
def write_file(body: WriteFileBody) -> dict:
    """Save the editor's contents.

    Written through a temp file in the same directory and moved into place, so
    an interrupted save cannot leave a half-written source file behind. When a
    repo root is given the path is guarded against it, the same as every other
    write in this file.
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
    return {"path": str(target), "size": target.stat().st_size}


# --------------------------------------------------------------------------
# live runs
# --------------------------------------------------------------------------


class StartRun(BaseModel):
    prompt: str
    cwd: str
    resume: str | None = None
    model: str | None = None
    permission_mode: str = "default"


@app.get("/api/runs")
def list_runs() -> dict:
    return {"runs": MANAGER.list()}


@app.post("/api/runs")
async def start_run(body: StartRun) -> dict:
    try:
        run = await MANAGER.start(
            body.prompt,
            body.cwd,
            resume=body.resume,
            model=body.model,
            permission_mode=body.permission_mode,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))
    return run.meta()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = MANAGER.get(run_id)
    if run is None:
        raise HTTPException(404, f"no run {run_id}")
    return {**run.meta(), "events": run.log}


@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str) -> dict:
    return {"stopped": await MANAGER.stop(run_id)}


@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str) -> None:
    """Live stream for one run.  Replays the backlog, then follows."""
    await ws.accept()
    run = MANAGER.get(run_id)
    if run is None:
        await ws.send_json({"kind": "error", "text": f"no run {run_id}"})
        await ws.close()
        return

    queue = run.subscribe()
    try:
        await ws.send_json({"kind": "meta", "run": run.meta()})
        # Backlog first so a late attach still sees the whole conversation.
        for ev in list(run.log):
            await ws.send_json(ev)
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=20.0)
            except asyncio.TimeoutError:
                await ws.send_json({"kind": "ping", "ts": 0})
                continue
            await ws.send_json(ev)
            if ev.get("kind") == "status" and ev.get("subtype") == "closed":
                await ws.send_json({"kind": "meta", "run": run.meta()})
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        pass  # socket closed under us
    finally:
        run.unsubscribe(queue)


@app.websocket("/ws/tail")
async def ws_tail(
    ws: WebSocket, cwd: str, session_id: str | None = None, from_start: bool = True
) -> None:
    """Follow a session running in the terminal by tailing its transcript.

    This is how the agent panel watches live work now that Run launches an
    interactive `claude` in the pty rather than a headless subprocess.
    """
    await ws.accept()
    try:
        await tail_session(ws, cwd, session_id=session_id, from_start=from_start)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as exc:  # never let one bad tail take the server down
        try:
            await ws.send_json(events.make("error", text=f"tail failed: {exc}"))
        except RuntimeError:
            pass


@app.get("/api/transcripts/latest")
def latest_transcript(cwd: str) -> dict:
    """Newest transcript for a repo — used to jump to a just-started session."""
    p = newest_transcript(cwd)
    return {"cwd": cwd, "session_id": p.stem if p else None, "path": str(p) if p else None}


@app.websocket("/ws/replay/{session_id}")
async def ws_replay(ws: WebSocket, session_id: str, delay: float = 0.0) -> None:
    """Replay a historical transcript over the same wire format as a live run.

    Useful for demoing the agent panel without burning tokens; `delay` paces it.
    """
    await ws.accept()
    sess = CACHE.one(session_id)
    if sess is None:
        await ws.send_json({"kind": "error", "text": f"no session {session_id}"})
        await ws.close()
        return
    try:
        await ws.send_json(
            {"kind": "meta", "run": {**sess.meta_dict(), "status": "idle", "live": False}}
        )
        for ev in sess.events:
            await ws.send_json(events.from_transcript_event(ev))
            if delay:
                await asyncio.sleep(delay)
        await ws.send_json(events.make("status", subtype="closed", text="replay complete"))
    except (WebSocketDisconnect, RuntimeError):
        pass


# --------------------------------------------------------------------------
# loadout, conversion, mining, library
# --------------------------------------------------------------------------


@app.get("/api/loadout")
async def get_loadout(repo: str, probe: bool = True) -> dict:
    """What is attached to a repo and what each item costs."""
    try:
        return await loadout.get(repo, probe=probe)
    except OSError as exc:
        raise HTTPException(400, str(exc))


class ToggleBody(BaseModel):
    repo: str
    name: str
    enabled: bool


@app.post("/api/loadout/mcp")
def toggle_mcp(body: ToggleBody) -> dict:
    _ATTRIBUTION_CACHE.pop(body.repo, None)
    return loadout.set_mcp_enabled(body.repo, body.name, body.enabled)


@app.post("/api/loadout/skill")
def toggle_skill(body: ToggleBody) -> dict:
    _ATTRIBUTION_CACHE.pop(body.repo, None)
    return loadout.set_skill_enabled(body.repo, body.name, body.enabled)


class ConvertBody(BaseModel):
    repo: str
    name: str
    disable_after: bool = True


@app.post("/api/mcp/preview")
async def preview_conversion(body: ConvertBody) -> dict:
    """Show the generated skill and its saving before writing anything."""
    servers = await mcp.discover_and_probe(body.repo, do_probe=True)
    server = next((s for s in servers if s.name == body.name), None)
    if server is None:
        raise HTTPException(404, f"no MCP server named {body.name!r}")
    return {
        "server": server.to_dict(),
        "skill": mcp.render_skill(server),
        **mcp.conversion_savings(server),
    }


@app.post("/api/mcp/convert")
async def convert(body: ConvertBody) -> dict:
    try:
        return await loadout.convert_mcp_to_skill(
            body.repo, body.name, disable_after=body.disable_after
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class SaveMcpBody(BaseModel):
    repo: str
    name: str
    config: dict
    scope: str = "local"


@app.post("/api/loadout/mcp/test")
async def test_mcp(body: SaveMcpBody) -> dict:
    """Probe a candidate server without saving it, so its cost is known first."""
    try:
        return await mcp.probe_config(body.name, body.config, body.repo)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/loadout/mcp/save")
def save_mcp(body: SaveMcpBody) -> dict:
    _ATTRIBUTION_CACHE.pop(body.repo, None)
    try:
        return mcp.save_server(body.repo, body.name, body.config, scope=body.scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class DeleteMcpBody(BaseModel):
    repo: str
    name: str


@app.post("/api/loadout/mcp/delete")
def delete_mcp(body: DeleteMcpBody) -> dict:
    _ATTRIBUTION_CACHE.pop(body.repo, None)
    try:
        return mcp.delete_server(body.repo, body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/mining")
def get_mining(repo: str | None = None, limit: int = 20) -> dict:
    """Repeated tool sequences worth turning into skills."""
    return mining.summary(CACHE.all(), repo_path=repo, limit=limit)


class AcceptBody(BaseModel):
    repo: str
    slug: str
    skill_md: str
    scope: str = "repo"
    overwrite: bool = False


@app.post("/api/mining/accept")
def accept_mined(body: AcceptBody) -> dict:
    try:
        return loadout.write_mined_skill(
            body.repo, body.slug, body.skill_md, scope=body.scope, overwrite=body.overwrite
        )
    except FileExistsError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class CopySkillBody(BaseModel):
    source_path: str
    scope: str = "user"
    repo: str | None = None
    slug: str | None = None
    overwrite: bool = False


@app.post("/api/skills/copy")
def copy_skill(body: CopySkillBody) -> dict:
    """Share a skill: promote it to every repo, or hand it to one other repo."""
    try:
        return loadout.copy_skill(
            body.source_path,
            body.scope,
            repo_path=body.repo,
            slug=body.slug,
            overwrite=body.overwrite,
        )
    except FileExistsError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/repos")
def list_repos() -> dict:
    """Repos Descant knows about — the targets a skill can be copied into."""
    seen: dict[str, dict] = {}
    for sess in CACHE.all():
        if not sess.repo_path or sess.repo_path in seen:
            continue
        seen[sess.repo_path] = {
            "path": sess.repo_path,
            "name": Path(sess.repo_path).name,
            "exists": Path(sess.repo_path).is_dir(),
        }
    return {"repos": sorted(seen.values(), key=lambda r: r["name"].lower())}


@app.get("/api/library")
async def get_library(q: str = "", limit: int = 20, probe: bool = True) -> dict:
    """Search every skill and MCP tool across all known repos."""
    repos = sorted({s.repo_path for s in CACHE.all() if Path(s.repo_path).is_dir()})
    lib = await library.build(repos, probe_mcp=probe)
    entries = (
        lib.score_query(q, limit=limit) if q else [e.to_dict() for e in lib.entries][:limit]
    )
    return {"query": q, "repos": repos, "stats": library.stats(lib), "results": entries}


# --------------------------------------------------------------------------
# chat: a two-way conversation, in the app rather than in the terminal
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
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        pass
    finally:
        c.unsubscribe(queue)




# --------------------------------------------------------------------------
# managing the session list
# --------------------------------------------------------------------------


class NewSessionBody(BaseModel):
    repo: str


@app.post("/api/sessions/new")
async def new_session(body: NewSessionBody) -> dict:
    """Start a fresh conversation for a repo.

    A session is a chat: Claude Code writes the transcript once the first turn
    lands, so what this really creates is somewhere to type. It shows up in the
    sidebar as soon as it has said anything.
    """
    try:
        c = await chat.REGISTRY.create(body.repo)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return c.meta()


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> dict:
    """Delete a session's transcript.

    This removes the file Claude Code wrote, which is the only record of the
    conversation — there is no undo, so the UI asks first. We refuse anything
    that is not a ``.jsonl`` under a configured projects directory, so a bad id
    can never turn into an arbitrary unlink.
    """
    sess = CACHE.one(session_id)
    if sess is None:
        raise HTTPException(404, f"no session {session_id}")

    path = Path(sess.path).resolve()
    roots = [r.resolve() for r in config.projects_dirs()]
    if path.suffix != ".jsonl" or not any(path.is_relative_to(root) for root in roots):
        raise HTTPException(400, f"refusing to delete {path}: not a transcript we manage")

    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise HTTPException(400, f"could not delete: {exc}")

    CACHE.forget(str(path))
    return {"deleted": sess.session_id, "path": str(path), "title": sess.title}


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

    Detection rather than a fixed table where it matters: a repo with a
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

    # Prefer the repo's own interpreter over whatever is on PATH.
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


def main() -> None:
    import uvicorn

    uvicorn.run(
        app, host=config.HOST, port=config.PORT, log_level=os.environ.get("DESCANT_LOG", "warning")
    )


if __name__ == "__main__":
    main()
