"""A real two-way conversation with Claude Code, not a transcript you watch.

Descant already had two halves of this and neither was a chat: the terminal runs
the interactive CLI (you type there, the GUI only hosts a shell), and the agent
panel tails the transcript (readable, but strictly past tense). This module is
the missing middle — you type in the app, the reply streams back into the app,
and the tool approvals happen in the app.

How it talks to Claude Code
---------------------------
One long-lived ``claude -p`` per chat, wired for streaming in *both* directions::

    claude -p --input-format stream-json --output-format stream-json --verbose

Each user turn is a JSON line on stdin; everything coming back is a JSON line on
stdout, normalised through :mod:`descant.events` so the renderer that draws
replayed transcripts draws live ones too. The process outlives the turn, so the
conversation keeps its context — turn three remembers turn one.

The permission problem, and what we do about it
-----------------------------------------------
Headless Claude Code has no way to answer "may I?" mid-stream: this CLI exposes
no permission-prompt tool, so a call needing approval comes back as
``system/permission_denied`` and the tool simply does not run. A chat window
that could only report "blocked, go use the terminal" would not be a chat
window.

So approval is a *restart*: sessions resume by id, which means Descant can stop
the process, bring it back with ``--resume <session-id> --allowedTools <rule>``,
and nudge it to continue. The conversation survives intact — it is the same
session, with one more thing permitted — and the human decided, in the GUI,
which is the point. "Always" additionally writes the rule to the repo's
``.claude/settings.local.json`` so the next session starts already knowing.

What we still refuse to do: ``--dangerously-skip-permissions`` is never passed.
Approval here is always a specific rule a person clicked.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config, events

RUNNING = "running"          # a turn is in flight
IDLE = "idle"                # process up, waiting on you
NEEDS_INPUT = "needs_input"  # blocked on a permission decision
FAILED = "failed"
CLOSED = "closed"

#: Keep memory bounded on very long conversations; the transcript on disk is
#: always the complete record.
MAX_HISTORY = 4000

PERMISSION_MODES = ("manual", "acceptEdits", "plan", "auto", "dontAsk")


def _permission_rule(tool_name: str, tool_input: dict | None) -> str:
    """The narrowest ``--allowedTools`` rule that unblocks this specific call.

    Granularity matters: approving a ``git status`` should not silently approve
    ``rm -rf``. Bash is scoped to the command's head, everything else to the
    tool.
    """
    if tool_name == "Bash":
        command = ((tool_input or {}).get("command") or "").strip()
        try:
            parts = [p for p in shlex.split(command) if not re.match(r"^[A-Za-z_]\w*=", p)]
        except ValueError:
            parts = command.split()
        if parts:
            head = parts[0]
            # `git commit` is a more useful unit than all of `git`.
            if len(parts) > 1 and head in {"git", "npm", "yarn", "pnpm", "cargo", "go", "uv"}:
                head = f"{head} {parts[1]}"
            return f"Bash({head}:*)"
        return "Bash"
    return tool_name or "Bash"


def _settings_local(cwd: str) -> Path:
    return Path(cwd).expanduser() / ".claude" / "settings.local.json"


def remember_rule(cwd: str, rule: str) -> str:
    """Persist an approval the way Claude Code itself would, so it outlives us."""
    path = _settings_local(cwd)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    perms = data.setdefault("permissions", {})
    allow = list(perms.get("allow") or [])
    if rule not in allow:
        allow.append(rule)
    perms["allow"] = allow
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.descant-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return str(path)


@dataclass
class Chat:
    chat_id: str
    cwd: str
    session_id: str
    permission_mode: str = "manual"
    model: str = ""
    #: A chat is ready before it has a process: the first turn spawns one, so a
    #: conversation nobody types into never costs a subprocess.
    status: str = IDLE
    allowed_tools: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    pending: dict[str, dict] = field(default_factory=dict)
    cost_usd: float = 0.0
    started_at: float = field(default_factory=time.time)
    error: str = ""
    _proc: asyncio.subprocess.Process | None = None
    _reader: asyncio.Task | None = None
    _subscribers: set[asyncio.Queue] = field(default_factory=set)
    _started_once: bool = False
    _stderr_tail: list[str] = field(default_factory=list)
    _recent_tool_use: dict[str, dict] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ----------------------------------------------------------------- meta
    def meta(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "repo_name": Path(self.cwd).name,
            "status": self.status,
            "model": self.model,
            "permission_mode": self.permission_mode,
            "allowed_tools": list(self.allowed_tools),
            "pending": list(self.pending.values()),
            "cost_usd": round(self.cost_usd, 4),
            "started_at": self.started_at,
            "error": self.error,
            "alive": bool(self._proc and self._proc.returncode is None),
        }

    # ------------------------------------------------------------ lifecycle
    def _argv(self) -> list[str]:
        argv = [
            config.CLAUDE_BIN,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            self.permission_mode,
        ]
        # First launch names the session; later launches rejoin it.
        if self._started_once:
            argv += ["--resume", self.session_id]
        else:
            argv += ["--session-id", self.session_id]
        if self.allowed_tools:
            argv += ["--allowedTools", *self.allowed_tools]
        return argv

    async def start(self) -> None:
        argv = self._argv()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "descant"},
            )
        except FileNotFoundError:
            self.status = FAILED
            self.error = (
                f"{config.CLAUDE_BIN!r} is not on PATH — set DESCANT_CLAUDE_BIN to point at it"
            )
            await self._emit(events.make("error", text=self.error, is_error=True))
            return
        self._started_once = True
        self.status = IDLE
        self._reader = asyncio.create_task(self._read_loop())
        asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                self._stderr_tail.append(text)
                del self._stderr_tail[:-20]

    async def _read_loop(self) -> None:
        proc = self._proc
        assert proc and proc.stdout
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    raw = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                for ev in events.from_stream_json(raw):
                    await self._handle(ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a dead pipe must not take the server down
            await self._emit(events.make("error", text=str(exc)[:300], is_error=True))
        finally:
            code = proc.returncode
            if self.status not in (CLOSED, FAILED) and code not in (0, None):
                self.status = FAILED
                detail = " · ".join(self._stderr_tail[-3:])
                self.error = f"claude exited with code {code}{': ' + detail if detail else ''}"
                await self._emit(events.make("error", text=self.error, is_error=True))

    async def _handle(self, ev: dict) -> None:
        """Track the bits of state the UI needs, then fan the event out."""
        kind, sub = ev.get("kind"), ev.get("subtype")

        if kind == "status" and sub == "init":
            self.model = ev["meta"].get("model") or self.model
            # Claude Code may hand back a different session id than we asked for
            # (a resume that forked, say); follow it or the next resume misses.
            if ev["meta"].get("session_id"):
                self.session_id = ev["meta"]["session_id"]
        elif kind == "assistant_text" or kind == "thinking":
            self.status = RUNNING
        elif kind == "tool_use":
            self.status = RUNNING
            # Remember the arguments: the permission event that may follow names
            # the tool but not what it wanted to do, and "allow Bash" is a very
            # different decision from "allow `git status`".
            if ev.get("tool_use_id"):
                self._recent_tool_use[ev["tool_use_id"]] = {
                    "tool_name": ev.get("tool_name"),
                    "tool_input": ev.get("tool_input"),
                }
                if len(self._recent_tool_use) > 200:
                    self._recent_tool_use.pop(next(iter(self._recent_tool_use)))
        elif kind == "permission":
            tuid = ev.get("tool_use_id") or uuid.uuid4().hex
            prior = self._recent_tool_use.get(tuid) or {}
            tool_name = ev.get("tool_name") or prior.get("tool_name") or ""
            tool_input = (ev.get("meta") or {}).get("tool_input")
            if tool_input is None:
                tool_input = prior.get("tool_input")
            self.pending[tuid] = {
                "tool_use_id": tuid,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "text": ev.get("text") or "",
                "rule": _permission_rule(tool_name, tool_input),
            }
            self.status = NEEDS_INPUT
            ev["meta"] = {
                **(ev.get("meta") or {}),
                "rule": self.pending[tuid]["rule"],
                "tool_input": tool_input,
            }
        elif kind == "status" and sub == "result":
            cost = (ev.get("meta") or {}).get("cost_usd")
            if cost:
                self.cost_usd = cost
            # A turn that ended while something is still pending is waiting on a
            # human, not finished.
            self.status = NEEDS_INPUT if self.pending else IDLE

        await self._emit(ev)

    async def _emit(self, ev: dict) -> None:
        self.history.append(ev)
        del self.history[:-MAX_HISTORY]
        payload = {"event": ev, "meta": self.meta()}
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    # -------------------------------------------------------------- talking
    async def send(self, text: str) -> None:
        """One user turn."""
        text = (text or "").strip()
        if not text:
            raise ValueError("nothing to send")
        async with self._lock:
            if not self._proc or self._proc.returncode is not None:
                await self.start()
            if self.status == FAILED:
                raise RuntimeError(self.error or "chat is not running")
            await self._emit(events.make("user_text", text=text))
            self.status = RUNNING
            await self._write(
                {
                    "type": "user",
                    "message": {"role": "user", "content": [{"type": "text", "text": text}]},
                }
            )

    async def _write(self, payload: dict) -> None:
        proc = self._proc
        if not proc or not proc.stdin or proc.returncode is not None:
            raise RuntimeError("claude is not running")
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        await proc.stdin.drain()

    # ---------------------------------------------------------- permissions
    async def approve(self, tool_use_id: str, remember: bool = False) -> dict:
        """Grant one blocked call and pick the conversation back up.

        Restarting is not a workaround for a missing feature so much as the only
        honest way to do it here: the permission set is fixed when the process
        starts, so a new permission means a new process — resumed into the same
        session, so nothing is lost but a second.
        """
        req = self.pending.get(tool_use_id)
        if req is None:
            raise ValueError("that permission request is no longer pending")
        rule = req["rule"]
        written = ""
        if rule not in self.allowed_tools:
            self.allowed_tools.append(rule)
        if remember:
            written = remember_rule(self.cwd, rule)

        del self.pending[tool_use_id]
        await self._emit(
            events.make(
                "status",
                subtype="approved",
                text=f"you allowed {rule}" + (" · saved for next time" if remember else ""),
                meta={"rule": rule, "remembered": written},
            )
        )
        await self._restart_and_continue(
            f"I approved {req['tool_name']}. Please continue from where you were stopped."
        )
        return {"rule": rule, "remembered": written, "meta": self.meta()}

    async def deny(self, tool_use_id: str, reason: str = "") -> dict:
        req = self.pending.pop(tool_use_id, None)
        if req is None:
            raise ValueError("that permission request is no longer pending")
        self.status = IDLE if not self.pending else NEEDS_INPUT
        await self._emit(
            events.make(
                "status",
                subtype="denied",
                text=f"you declined {req['tool_name']}" + (f" — {reason}" if reason else ""),
                meta={"rule": req["rule"]},
            )
        )
        return {"meta": self.meta()}

    async def _restart_and_continue(self, nudge: str) -> None:
        await self._stop_process()
        await self.start()
        if self.status == FAILED:
            return
        self.status = RUNNING
        await self._write(
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": nudge}]}}
        )

    async def set_permission_mode(self, mode: str) -> dict:
        if mode not in PERMISSION_MODES:
            raise ValueError(f"permission mode must be one of {PERMISSION_MODES}")
        self.permission_mode = mode
        if self._started_once and self._proc and self._proc.returncode is None:
            await self._stop_process()
            await self.start()
        await self._emit(
            events.make("system", subtype="permission_mode", text=f"permission mode: {mode}")
        )
        return self.meta()

    # ------------------------------------------------------------- stopping
    async def interrupt(self) -> dict:
        """Stop the current turn without losing the conversation."""
        await self._stop_process()
        self.status = IDLE
        await self._emit(events.make("status", subtype="interrupted", text="you stopped the turn"))
        return self.meta()

    async def _stop_process(self) -> None:
        proc, self._proc = self._proc, None
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if not proc or proc.returncode is not None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(proc.wait(), timeout=3)

    async def close(self) -> None:
        self.status = CLOSED
        await self._stop_process()

    # ---------------------------------------------------------- subscribers
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)


class ChatRegistry:
    """Every live chat, keyed by id. One per conversation, not one per repo."""

    def __init__(self) -> None:
        self._chats: dict[str, Chat] = {}

    async def create(self, cwd: str, permission_mode: str = "manual",
                     resume_session: str | None = None) -> Chat:
        path = Path(cwd).expanduser()
        if not path.is_dir():
            raise ValueError(f"{cwd} is not a directory on this machine")
        if permission_mode not in PERMISSION_MODES:
            raise ValueError(f"permission mode must be one of {PERMISSION_MODES}")
        chat = Chat(
            chat_id=uuid.uuid4().hex[:12],
            cwd=str(path),
            session_id=resume_session or str(uuid.uuid4()),
            permission_mode=permission_mode,
        )
        # Resuming an existing conversation means the next launch must --resume.
        chat._started_once = bool(resume_session)
        self._chats[chat.chat_id] = chat
        return chat

    def get(self, chat_id: str) -> Chat:
        chat = self._chats.get(chat_id)
        if chat is None:
            raise KeyError(chat_id)
        return chat

    def all(self) -> list[Chat]:
        return list(self._chats.values())

    async def close(self, chat_id: str) -> None:
        chat = self._chats.pop(chat_id, None)
        if chat:
            await chat.close()

    async def close_all(self) -> None:
        for chat in list(self._chats.values()):
            await chat.close()
        self._chats.clear()


REGISTRY = ChatRegistry()
