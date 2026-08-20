"""Follow a Claude Code session by watching the transcript it writes.

Descant runs live sessions in a real terminal rather than owning a headless
subprocess, so there is no stdout to read.  What there *is* -- always, for every
session, whether we started it or you did -- is the ``.jsonl`` Claude Code
appends to as it works.  Tailing that is a strictly better source: it survives
Descant restarting, it picks up sessions started outside the app, and it is the
same data the history view already knows how to render.

Polling rather than inotify on purpose: it is a handful of `stat` calls a
second, it behaves identically on macOS and Linux, and it cannot miss a write
the way a dropped watch event can.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, events
from .transcript import Session, parse_lines

POLL_SECONDS = 0.4
#: A session with no new lines for this long is treated as no longer running.
IDLE_AFTER_SECONDS = 12.0


def encode_project_dir(cwd: str) -> str:
    """Claude Code's path encoding: separators and dots become dashes."""
    return cwd.replace("/", "-").replace(".", "-").replace("_", "-")


def candidate_dirs(cwd: str) -> list[Path]:
    """Project directories that could hold transcripts for *cwd*."""
    key = encode_project_dir(str(Path(cwd).expanduser()))
    roots = [config.live_projects_dir(), *config.projects_dirs()]
    out: list[Path] = []
    for root in roots:
        d = root / key
        if d.is_dir() and d not in out:
            out.append(d)
    return out


def newest_transcript(cwd: str, after: float = 0.0) -> Path | None:
    """The most recently modified transcript for *cwd*, if any."""
    best: tuple[float, Path] | None = None
    for d in candidate_dirs(cwd):
        for jsonl in d.glob("*.jsonl"):
            try:
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            if mtime < after:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, jsonl)
    return best[1] if best else None


@dataclass
class TailState:
    path: Path
    offset: int = 0
    partial: str = ""
    last_change: float = 0.0


async def tail_session(
    ws,
    cwd: str,
    session_id: str | None = None,
    from_start: bool = True,
) -> None:
    """Stream a session's transcript over *ws* as normalised events.

    Waits for the file to appear if the session has not started yet, so the UI
    can attach the moment you hit Run and watch the conversation materialise.
    """
    started = time.time()
    state: TailState | None = None
    status = "waiting"

    async def send(payload: dict) -> None:
        await ws.send_json(payload)

    async def resolve() -> Path | None:
        if session_id:
            for d in candidate_dirs(cwd):
                p = d / f"{session_id}.jsonl"
                if p.is_file():
                    return p
            return None
        # No id means "follow the session that is about to start".  Only accept
        # a transcript touched since we attached -- never fall back to the most
        # recent one, or we latch onto yesterday's conversation and show it as
        # live.  Waiting is the correct behaviour until the session appears.
        return newest_transcript(cwd, after=started - 2.0)

    await send(
        {
            "kind": "meta",
            "run": {
                "cwd": cwd,
                "repo_name": os.path.basename(cwd.rstrip("/")) or cwd,
                "session_id": session_id,
                "status": status,
                "live": True,
                "source": "transcript",
            },
        }
    )

    while True:
        if state is None:
            found = await resolve()
            if found is None:
                await asyncio.sleep(POLL_SECONDS)
                if time.time() - started > 1.5 and status == "waiting":
                    await send(
                        events.make(
                            "status",
                            subtype="waiting",
                            text="waiting for a session to start in the terminal…",
                        )
                    )
                    status = "waiting-announced"
                continue
            state = TailState(path=found, last_change=time.time())
            await send(
                events.make(
                    "status",
                    subtype="attached",
                    text=f"following {found.stem[:8]}",
                    meta={"session_id": found.stem, "path": str(found)},
                )
            )
            if not from_start:
                try:
                    state.offset = found.stat().st_size
                except OSError:
                    pass

        try:
            size = state.path.stat().st_size
        except OSError:
            # Rotated or removed; look again.
            state = None
            continue

        if size < state.offset:  # truncated / replaced
            state.offset = 0
            state.partial = ""

        if size > state.offset:
            try:
                with open(state.path, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(state.offset)
                    chunk = fh.read()
                    state.offset = fh.tell()
            except OSError:
                await asyncio.sleep(POLL_SECONDS)
                continue

            state.partial += chunk
            # The last line may be a partial write; hold it back until it ends.
            lines = state.partial.split("\n")
            state.partial = lines.pop()
            complete = [ln for ln in lines if ln.strip()]
            if complete:
                state.last_change = time.time()
                sess = parse_lines(
                    complete, state.path.stem, state.path, state.path.parent.name
                )
                for ev in sess.events:
                    await send(events.from_transcript_event(ev))
                await send(_status_from(sess, "running"))
        else:
            if state.last_change and time.time() - state.last_change > IDLE_AFTER_SECONDS:
                await send(
                    events.make("status", subtype="state", text="idle", meta={"status": "idle"})
                )
                state.last_change = 0.0

        await asyncio.sleep(POLL_SECONDS)


def _status_from(sess: Session, default: str) -> dict:
    """Derive a status dot from what just landed in the transcript."""
    status = default
    detail = ""
    for ev in sess.events:
        if ev.kind == "tool_result" and ev.is_error:
            text = (ev.text or "").lower()
            if "permission" in text or "requires approval" in text or "blocked" in text:
                status = "needs_input"
                detail = ev.text[:200]
    return events.make(
        "status", subtype="state", text=detail or status, meta={"status": status}
    )
