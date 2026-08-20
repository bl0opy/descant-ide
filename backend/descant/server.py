"""Descant backend.

    python3 -m descant.server        # or: uvicorn descant.server:app

Serves the parsed transcript history, the context-cost analysis, and live
``claude`` runs over WebSockets.  The Electron shell is a thin client over this.
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import config, events
from .inspector import analyze
from .runner import MANAGER
from .transcript import Session, load_all, load_session

app = FastAPI(title="Descant", version="0.1.0")
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
        root = config.projects_dir()
        if not root.exists():
            return []
        sessions: list[Session] = []
        for project in sorted(p for p in root.iterdir() if p.is_dir()):
            for jsonl in sorted(project.glob("*.jsonl")):
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

    def one(self, session_id: str) -> Session | None:
        for s in self.all():
            if s.session_id == session_id or s.session_id.startswith(session_id):
                return s
        return None


CACHE = _Cache()


# --------------------------------------------------------------------------
# read endpoints
# --------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    root = config.projects_dir()
    return {
        "ok": True,
        "projects_dir": str(root),
        "projects_dir_exists": root.exists(),
        "claude_bin": config.CLAUDE_BIN,
        "live_runs": len(MANAGER.runs),
    }


@app.get("/api/sessions")
def list_sessions() -> dict:
    """Every historical session, grouped by repo, with its context breakdown."""
    reports = [analyze(s) for s in CACHE.all()]
    repos: dict[str, dict] = {}
    for r in reports:
        s = r.session
        bucket = repos.setdefault(
            s.repo_path,
            {"repo_path": s.repo_path, "repo_name": s.repo_name, "sessions": [], "total_tokens": 0},
        )
        bucket["sessions"].append(r.to_dict())
        bucket["total_tokens"] += r.total_tokens

    ordered = sorted(
        repos.values(),
        key=lambda b: b["sessions"][0]["last_activity"] or "",
        reverse=True,
    )
    return {
        "projects_dir": str(config.projects_dir()),
        "repos": ordered,
        "session_count": len(reports),
        "live": MANAGER.list(),
    }


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    sess = CACHE.one(session_id)
    if sess is None:
        raise HTTPException(404, f"no session {session_id}")
    report = analyze(sess)
    return {
        **report.to_dict(),
        "events": [events.from_transcript_event(e) for e in sess.events],
    }


@app.get("/api/sessions/{session_id}/context")
def get_context(session_id: str) -> dict:
    sess = CACHE.one(session_id)
    if sess is None:
        raise HTTPException(404, f"no session {session_id}")
    return analyze(sess).to_dict()


@app.get("/api/files")
def list_files(path: str, limit: int = 2000) -> dict:
    """Shallow-ish file tree for the editor panel (item 6)."""
    root = Path(path).expanduser()
    if not root.is_dir():
        raise HTTPException(404, f"not a directory: {root}")
    skip = {".git", "node_modules", "__pycache__", ".venv", "dist", "build", ".next"}
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip and not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            full = Path(dirpath) / name
            try:
                size = full.stat().st_size
            except OSError:
                continue
            out.append({"path": str(full), "rel": str(full.relative_to(root)), "size": size})
            if len(out) >= limit:
                return {"root": str(root), "files": out, "truncated": True}
    return {"root": str(root), "files": out, "truncated": False}


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


def main() -> None:
    import uvicorn

    uvicorn.run(
        app, host=config.HOST, port=config.PORT, log_level=os.environ.get("DESCANT_LOG", "warning")
    )


if __name__ == "__main__":
    main()
