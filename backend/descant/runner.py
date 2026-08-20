"""Spawn and supervise live ``claude`` subprocesses.

One :class:`Run` per live session.  Each run owns a subprocess reading
``--output-format stream-json`` on stdout; lines are normalised through
``events.from_stream_json`` and fanned out to every attached WebSocket.

Permission handling: we deliberately do **not** pass
``--dangerously-skip-permissions``.  When Claude Code blocks a tool it emits
``system/permission_denied``, which becomes a ``permission`` event that the UI
surfaces in place (amber), and the run's status flips to ``needs_input``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config, events

# Status values the UI's dot understands.
RUNNING = "running"
IDLE = "idle"
NEEDS_INPUT = "needs_input"
FAILED = "failed"


@dataclass
class Run:
    run_id: str
    cwd: str
    prompt: str
    session_id: str
    resumed_from: str | None = None
    status: str = "starting"
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    exit_code: int | None = None
    model: str = ""
    last_status_detail: str = ""
    needs_action: str = ""
    cost_usd: float = 0.0
    log: list[dict] = field(default_factory=list)
    pending_permissions: list[dict] = field(default_factory=list)
    _proc: asyncio.subprocess.Process | None = None
    _subscribers: set[asyncio.Queue] = field(default_factory=set)
    _stderr_tail: list[str] = field(default_factory=list)

    def meta(self) -> dict:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "resumed_from": self.resumed_from,
            "cwd": self.cwd,
            "repo_name": os.path.basename(self.cwd.rstrip("/")) or self.cwd,
            "prompt": self.prompt,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "model": self.model,
            "status_detail": self.last_status_detail,
            "needs_action": self.needs_action,
            "cost_usd": self.cost_usd,
            "event_count": len(self.log),
            "pending_permissions": self.pending_permissions,
            "live": True,
        }

    # -- fan-out ---------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2048)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _emit(self, ev: dict) -> None:
        self.log.append(ev)
        for q in list(self._subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                # A slow client must not stall the process reader.
                self._subscribers.discard(q)

    def _set_status(self, status: str) -> None:
        if status == self.status:
            return
        self.status = status
        self._emit(events.make("status", subtype="state", text=status, meta={"status": status}))


class RunManager:
    """Owns every live run.  Item 7's multi-session support falls out of this."""

    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}

    # -- lifecycle -------------------------------------------------------

    def list(self) -> list[dict]:
        return [r.meta() for r in sorted(self.runs.values(), key=lambda r: -r.started_at)]

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    async def start(
        self,
        prompt: str,
        cwd: str,
        *,
        resume: str | None = None,
        model: str | None = None,
        permission_mode: str = "default",
    ) -> Run:
        cwd_path = Path(cwd).expanduser()
        if not cwd_path.is_dir():
            raise ValueError(f"working directory does not exist: {cwd_path}")
        if shutil.which(config.CLAUDE_BIN) is None:
            raise RuntimeError(
                f"`{config.CLAUDE_BIN}` not found on PATH; set DESCANT_CLAUDE_BIN"
            )

        run_id = uuid.uuid4().hex[:12]
        # Always pin an explicit session id.  Without it the child can inherit
        # CLAUDE_SESSION_ID from the environment and write into somebody else's
        # transcript -- observed in this sandbox.
        session_id = resume or str(uuid.uuid4())

        argv = [
            config.CLAUDE_BIN,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            permission_mode,
        ]
        if resume:
            argv += ["--resume", resume]
        else:
            argv += ["--session-id", session_id]
        if model:
            argv += ["--model", model]

        run = Run(
            run_id=run_id,
            cwd=str(cwd_path),
            prompt=prompt,
            session_id=session_id,
            resumed_from=resume,
        )
        self.runs[run_id] = run

        env = dict(os.environ)
        # Do not let the parent session's identity leak into the child.
        for k in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            env.pop(k, None)

        run._proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            limit=8 * 1024 * 1024,  # single stream-json lines can be very large
        )
        run._set_status(RUNNING)
        run._emit(
            events.make(
                "status",
                subtype="spawn",
                text=f"$ {config.CLAUDE_BIN} -p … (cwd: {cwd_path})",
                meta={"argv": argv, "cwd": str(cwd_path)},
            )
        )
        run._emit(events.make("user_text", text=prompt))

        asyncio.create_task(self._pump(run))
        asyncio.create_task(self._drain_stderr(run))
        return run

    async def stop(self, run_id: str) -> bool:
        run = self.runs.get(run_id)
        if not run or not run._proc or run._proc.returncode is not None:
            return False
        try:
            run._proc.terminate()
        except ProcessLookupError:
            return False
        run._emit(events.make("status", subtype="stopped", text="stopped by user"))
        return True

    # -- io --------------------------------------------------------------

    async def _pump(self, run: Run) -> None:
        proc = run._proc
        assert proc and proc.stdout
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    run._emit(
                        events.make("error", text="dropped an oversized stream-json line")
                    )
                    continue
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    raw = json.loads(text)
                except json.JSONDecodeError:
                    # Non-JSON on stdout is almost always a startup warning.
                    run._emit(events.make("system", subtype="stdout", text=text[:2000]))
                    continue
                for ev in events.from_stream_json(raw):
                    self._apply(run, ev)
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception as exc:  # keep one bad run from killing the server
            run._emit(events.make("error", text=f"stream reader failed: {exc}"))
        finally:
            rc = await proc.wait()
            run.exit_code = rc
            run.ended_at = time.time()
            if rc != 0 and run.status != NEEDS_INPUT:
                tail = "\n".join(run._stderr_tail[-8:])
                run._emit(
                    events.make(
                        "error",
                        text=f"claude exited with code {rc}" + (f"\n{tail}" if tail else ""),
                    )
                )
                run._set_status(FAILED)
            elif run.status != NEEDS_INPUT:
                run._set_status(IDLE)
            run._emit(events.make("status", subtype="closed", text="stream closed"))

    async def _drain_stderr(self, run: Run) -> None:
        proc = run._proc
        if not proc or not proc.stderr:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            run._stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())
            del run._stderr_tail[:-40]

    def _apply(self, run: Run, ev: dict) -> None:
        """Update run state from an event, then broadcast it."""
        kind, sub = ev["kind"], ev.get("subtype")
        meta = ev.get("meta") or {}

        if kind == "status" and sub == "init":
            run.model = meta.get("model") or run.model
            reported = meta.get("session_id")
            if reported:
                run.session_id = reported

        elif kind == "permission":
            run.pending_permissions.append(
                {
                    "tool_name": ev.get("tool_name"),
                    "tool_use_id": ev.get("tool_use_id"),
                    "message": ev.get("text"),
                    "tool_input": meta.get("tool_input"),
                }
            )
            run._set_status(NEEDS_INPUT)

        elif kind == "status" and sub == "turn_summary":
            run.last_status_detail = ev.get("text") or run.last_status_detail
            run.needs_action = meta.get("needs_action") or ""
            if run.needs_action or meta.get("status_category") == "blocked":
                run._set_status(NEEDS_INPUT)

        elif kind == "status" and sub == "result":
            run.cost_usd = float(meta.get("cost_usd") or 0.0)
            if meta.get("permission_denials"):
                run._set_status(NEEDS_INPUT)

        elif kind == "system" and sub == "usage":
            pass  # kept in the log for the inspector, no state change

        run._emit(ev)


MANAGER = RunManager()
