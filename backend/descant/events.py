"""One normalised event shape for both replayed transcripts and live streams.

The UI has a single renderer.  Whether an event came out of a ``.jsonl`` file on
disk or off a live ``claude -p --output-format stream-json`` pipe, it arrives at
the frontend looking the same.
"""

from __future__ import annotations

import time
from typing import Any


def make(
    kind: str,
    *,
    text: str = "",
    tool_name: str | None = None,
    tool_input: Any = None,
    tool_use_id: str | None = None,
    is_error: bool = False,
    subtype: str | None = None,
    meta: dict | None = None,
    ts: float | None = None,
) -> dict:
    return {
        "kind": kind,
        "ts": ts if ts is not None else time.time(),
        "text": text,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
        "is_error": is_error,
        "subtype": subtype,
        "meta": meta or {},
    }


# Kinds the frontend knows how to render.
KINDS = {
    "user_text",
    "assistant_text",
    "thinking",
    "tool_use",
    "tool_result",
    "permission",  # a tool was blocked / needs a decision from the human
    "status",  # lifecycle: started, turn summary, finished
    "system",  # informational, rendered dim
    "error",
}


def from_stream_json(raw: dict) -> list[dict]:
    """Translate one line of ``claude --output-format stream-json`` output.

    Event vocabulary observed from claude-code 2.1.237 (the brief described a
    simpler shape; this follows what the binary actually emits):

    ``system/init``               session id, cwd, model, tool list
    ``system/permission_denied``  a tool call was blocked -- surfaced, never auto-approved
    ``system/post_turn_summary``  status_category + needs_action, drives the status dot
    ``system/task_summary``       short human label for what is happening now
    ``assistant``                 message with text / thinking / tool_use blocks
    ``user``                      message with tool_result blocks
    ``result``                    terminal: cost, usage, permission_denials
    """
    t = raw.get("type")
    out: list[dict] = []

    if t == "system":
        sub = raw.get("subtype")
        if sub == "init":
            out.append(
                make(
                    "status",
                    subtype="init",
                    text=f"session started · {raw.get('model', 'unknown model')}",
                    meta={
                        "session_id": raw.get("session_id"),
                        "cwd": raw.get("cwd"),
                        "model": raw.get("model"),
                        "permission_mode": raw.get("permissionMode"),
                        "tools": raw.get("tools") or [],
                    },
                )
            )
        elif sub == "permission_denied":
            out.append(
                make(
                    "permission",
                    subtype="denied",
                    text=raw.get("message") or "Tool call blocked pending permission.",
                    tool_name=raw.get("tool_name"),
                    tool_use_id=raw.get("tool_use_id"),
                    is_error=True,
                    meta={"tool_input": raw.get("tool_input")},
                )
            )
        elif sub == "post_turn_summary":
            out.append(
                make(
                    "status",
                    subtype="turn_summary",
                    text=raw.get("status_detail") or "",
                    meta={
                        "status_category": raw.get("status_category"),
                        "needs_action": raw.get("needs_action") or "",
                    },
                )
            )
        elif sub == "task_summary":
            if raw.get("detail"):
                out.append(make("system", subtype="task", text=raw["detail"]))
        # commands_changed / thinking_tokens etc. are noise for our purposes.
        return out

    if t == "assistant":
        msg = raw.get("message") or {}
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and b.get("text"):
                out.append(make("assistant_text", text=b["text"]))
            elif bt == "thinking":
                out.append(make("thinking", text=b.get("thinking") or "(thinking)"))
            elif bt == "tool_use":
                out.append(
                    make(
                        "tool_use",
                        tool_name=b.get("name"),
                        tool_input=b.get("input"),
                        tool_use_id=b.get("id"),
                    )
                )
        usage = msg.get("usage")
        if usage:
            out.append(make("system", subtype="usage", meta={"usage": usage}))
        return out

    if t == "user":
        msg = raw.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            out.append(make("user_text", text=content))
            return out
        for b in content or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(
                        x.get("text", "") if isinstance(x, dict) else str(x) for x in c
                    )
                out.append(
                    make(
                        "tool_result",
                        text=c if isinstance(c, str) else str(c),
                        tool_use_id=b.get("tool_use_id"),
                        is_error=bool(b.get("is_error")),
                    )
                )
            elif b.get("type") == "text":
                out.append(make("user_text", text=b.get("text", "")))
        return out

    if t == "result":
        denials = raw.get("permission_denials") or []
        out.append(
            make(
                "status",
                subtype="result",
                text=raw.get("result") or "",
                is_error=bool(raw.get("is_error")),
                meta={
                    "subtype": raw.get("subtype"),
                    "cost_usd": raw.get("total_cost_usd"),
                    "duration_ms": raw.get("duration_ms"),
                    "num_turns": raw.get("num_turns"),
                    "usage": raw.get("usage"),
                    "permission_denials": denials,
                },
            )
        )
        return out

    if t == "rate_limit_event":
        info = raw.get("rate_limit_info") or {}
        if info.get("status") not in (None, "allowed"):
            out.append(
                make("system", subtype="rate_limit", text=f"rate limit: {info.get('status')}")
            )
        return out

    return out


def from_transcript_event(ev) -> dict:
    """Adapt a parsed ``transcript.Event`` into the same wire shape."""
    return make(
        ev.kind if ev.kind != "attachment" else "system",
        text=ev.text,
        tool_name=ev.tool_name,
        tool_input=ev.tool_input,
        tool_use_id=ev.tool_use_id,
        is_error=ev.is_error,
        subtype=ev.subtype if ev.kind == "attachment" else None,
        meta={"sidechain": ev.is_sidechain, "index": ev.index},
        ts=ev.ts.timestamp() if ev.ts else 0.0,
    )
