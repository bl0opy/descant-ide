"""Parse Claude Code ``.jsonl`` session transcripts.

Format notes (observed on disk from a real ~/.claude/projects, 2026-08):

* One JSON object per line.  Files live at
  ``<projects_dir>/<encoded-repo-path>/<session-uuid>.jsonl``.
* The directory name encodes the repo path with ``/`` replaced by ``-``, which
  is lossy.  Every conversation line also carries an exact ``cwd`` field, so we
  prefer that and only fall back to decoding the directory name.
* Line ``type`` values seen in the wild:
  ``user``, ``assistant``, ``attachment``, ``summary``, ``last-prompt``,
  ``atis-latch``, ``queue-operation``, ``system``, ``file-history-snapshot``.
  We treat anything we do not recognise as inert metadata rather than failing.
* ``message.content`` is either a plain string (simple user turn) or a list of
  content blocks: ``text``, ``thinking``, ``tool_use``, ``tool_result``,
  ``image``.
* Assistant lines carry ``message.usage`` with the *real* token counts.  One
  API request fans out into several assistant lines that all repeat the same
  ``requestId`` and the same ``usage`` object -- dedupe by ``requestId`` before
  summing or you will multiply-count.
* ``attachment`` lines are injected context (system reminders, skill listings,
  tool listings).  They cost real tokens and are invisible in most UIs, which
  is exactly what the context inspector exists to surface.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

# Line types that carry no conversational content.
META_TYPES = {
    "summary",
    "last-prompt",
    "atis-latch",
    "queue-operation",
    "file-history-snapshot",
    "diagnostics",
}


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def decode_project_dir(name: str) -> str:
    """Best-effort reverse of Claude Code's path encoding.

    The encoding replaces ``/`` with ``-``, which is ambiguous: given
    ``-home-user-descant-ide`` there is no way to know from the string alone
    whether the repo is ``descant-ide`` or ``descant/ide``.

    So we disambiguate against the filesystem where we can, walking the segments
    and greedily preferring the longest joining that actually exists on disk.
    That resolves the common case exactly.  When nothing matches -- history from
    another machine -- we fall back to the naive all-slashes reading.

    Only reached when no transcript line carries ``cwd``; that field is exact
    and always preferred.
    """
    if not name.startswith("-"):
        return name
    segments = name[1:].split("-")
    naive = "/" + "/".join(segments)

    resolved = Path("/")
    i = 0
    while i < len(segments):
        # Longest first, so `descant-ide` beats `descant`.
        for j in range(len(segments), i, -1):
            candidate = resolved / "-".join(segments[i:j])
            if candidate.exists():
                resolved = candidate
                i = j
                break
        else:
            return naive  # this segment matches nothing; stop guessing
    return str(resolved)


@dataclass
class Event:
    """One renderable thing that happened in a session."""

    kind: str  # user_text | assistant_text | thinking | tool_use | tool_result | attachment | meta
    index: int = 0
    ts: datetime | None = None
    uuid: str | None = None
    parent_uuid: str | None = None
    text: str = ""
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_use_id: str | None = None
    is_error: bool = False
    is_sidechain: bool = False
    request_id: str | None = None
    usage: dict | None = None
    subtype: str | None = None  # attachment type, etc.

    @property
    def char_len(self) -> int:
        n = len(self.text)
        if self.tool_input is not None:
            n += len(json.dumps(self.tool_input, ensure_ascii=False))
        if self.tool_name:
            n += len(self.tool_name)
        return n

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "index": self.index,
            "ts": self.ts.isoformat() if self.ts else None,
            "uuid": self.uuid,
            "text": self.text,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "tool_use_id": self.tool_use_id,
            "is_error": self.is_error,
            "is_sidechain": self.is_sidechain,
            "subtype": self.subtype,
        }


@dataclass
class Session:
    session_id: str
    path: Path
    project_key: str  # the encoded directory name
    cwd: str = ""
    git_branch: str = ""
    version: str = ""
    model: str = ""
    started_at: datetime | None = None
    last_activity: datetime | None = None
    first_prompt: str = ""
    summary: str = ""
    events: list[Event] = field(default_factory=list)
    #: usage dicts deduped by requestId, in order.
    requests: list[dict] = field(default_factory=list)
    malformed_lines: int = 0

    # -- derived ------------------------------------------------------------

    @property
    def repo_path(self) -> str:
        return self.cwd or decode_project_dir(self.project_key)

    @property
    def repo_name(self) -> str:
        return os.path.basename(self.repo_path.rstrip("/")) or self.repo_path

    @property
    def message_count(self) -> int:
        """User + assistant turns, not counting injected attachments."""
        return sum(
            1
            for e in self.events
            if e.kind in ("user_text", "assistant_text", "tool_use", "tool_result")
        )

    @property
    def turn_count(self) -> int:
        return sum(1 for e in self.events if e.kind == "user_text")

    @property
    def tool_call_count(self) -> int:
        return sum(1 for e in self.events if e.kind == "tool_use")

    @property
    def title(self) -> str:
        base = self.summary or self.first_prompt or "(no prompt)"
        base = " ".join(base.split())
        return base[:100] + ("…" if len(base) > 100 else "")

    def meta_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "project_key": self.project_key,
            "repo_path": self.repo_path,
            "repo_name": self.repo_name,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "version": self.version,
            "model": self.model,
            "title": self.title,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_activity": self.last_activity.isoformat() if self.last_activity else None,
            "message_count": self.message_count,
            "turn_count": self.turn_count,
            "tool_call_count": self.tool_call_count,
            "path": str(self.path),
        }


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def _blocks(content: Any) -> Iterable[dict]:
    if isinstance(content, str):
        yield {"type": "text", "text": content}
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                yield b


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for sub in c:
            if isinstance(sub, dict):
                if sub.get("type") == "text":
                    parts.append(sub.get("text", ""))
                elif sub.get("type") == "image":
                    parts.append("[image]")
            elif isinstance(sub, str):
                parts.append(sub)
        return "\n".join(parts)
    if c is None:
        return ""
    return json.dumps(c, ensure_ascii=False)


def parse_lines(lines: Iterable[str], session_id: str, path: Path, project_key: str) -> Session:
    sess = Session(session_id=session_id, path=path, project_key=project_key)
    seen_requests: set[str] = set()
    idx = 0

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            sess.malformed_lines += 1
            continue
        if not isinstance(d, dict):
            sess.malformed_lines += 1
            continue

        ltype = d.get("type")
        ts = _parse_ts(d.get("timestamp"))
        if ts:
            if sess.started_at is None or ts < sess.started_at:
                sess.started_at = ts
            if sess.last_activity is None or ts > sess.last_activity:
                sess.last_activity = ts

        # Session-level metadata, wherever it shows up.
        for src, dest in (("cwd", "cwd"), ("gitBranch", "git_branch"), ("version", "version")):
            val = d.get(src)
            if val and not getattr(sess, dest):
                setattr(sess, dest, val)

        if ltype in ("summary", "last-prompt"):
            if not sess.summary:
                sess.summary = d.get("summary") or d.get("lastPrompt") or ""
            continue
        if ltype in META_TYPES:
            continue

        sidechain = bool(d.get("isSidechain"))

        if ltype == "attachment":
            att = d.get("attachment") or {}
            text = att.get("text") or att.get("content") or ""
            if not text:
                # Listing-style attachments: their payload is structured.
                text = json.dumps(
                    {k: v for k, v in att.items() if k != "type"}, ensure_ascii=False
                )
            sess.events.append(
                Event(
                    kind="attachment",
                    index=idx,
                    ts=ts,
                    uuid=d.get("uuid"),
                    parent_uuid=d.get("parentUuid"),
                    text=text,
                    subtype=att.get("type") or "attachment",
                    is_sidechain=sidechain,
                )
            )
            idx += 1
            continue

        msg = d.get("message")
        if not isinstance(msg, dict):
            continue

        if ltype == "assistant":
            if msg.get("model") and not sess.model:
                sess.model = msg["model"]
            rid = d.get("requestId") or msg.get("id")
            usage = msg.get("usage")
            # One request emits several lines that repeat the same usage block.
            if usage and rid and rid not in seen_requests:
                seen_requests.add(rid)
                sess.requests.append({"request_id": rid, "ts": ts, **usage})

        for b in _blocks(msg.get("content")):
            btype = b.get("type")
            ev: Event | None = None
            if btype == "text":
                txt = b.get("text", "")
                if ltype == "user":
                    ev = Event(kind="user_text", text=txt)
                    if not sess.first_prompt and txt and not sidechain:
                        sess.first_prompt = txt
                else:
                    ev = Event(kind="assistant_text", text=txt)
            elif btype == "thinking":
                ev = Event(kind="thinking", text=b.get("thinking", ""))
            elif btype == "tool_use":
                ev = Event(
                    kind="tool_use",
                    tool_name=b.get("name"),
                    tool_input=b.get("input") if isinstance(b.get("input"), dict) else {},
                    tool_use_id=b.get("id"),
                )
            elif btype == "tool_result":
                ev = Event(
                    kind="tool_result",
                    text=_result_text(b),
                    tool_use_id=b.get("tool_use_id"),
                    is_error=bool(b.get("is_error")),
                )
            elif btype == "image":
                ev = Event(kind="tool_result", text="[image]")
            if ev is None:
                continue
            ev.index = idx
            ev.ts = ts
            ev.uuid = d.get("uuid")
            ev.parent_uuid = d.get("parentUuid")
            ev.is_sidechain = sidechain
            ev.request_id = d.get("requestId")
            sess.events.append(ev)
            idx += 1

    # Name each tool_result after the tool_use it answers, so per-tool cost
    # attribution works.
    by_id = {e.tool_use_id: e for e in sess.events if e.kind == "tool_use" and e.tool_use_id}
    for e in sess.events:
        if e.kind == "tool_result" and e.tool_use_id in by_id:
            e.tool_name = by_id[e.tool_use_id].tool_name

    return sess


def load_session(path: Path, project_key: str | None = None) -> Session:
    key = project_key if project_key is not None else path.parent.name
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return parse_lines(fh, session_id=path.stem, path=path, project_key=key)


def iter_transcript_paths(root: Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(project_key, jsonl_path)`` for every transcript under *root*."""
    if not root.exists():
        return
    for project in sorted(p for p in root.iterdir() if p.is_dir()):
        for jsonl in sorted(project.glob("*.jsonl")):
            yield project.name, jsonl


def load_all(root: Path) -> list[Session]:
    sessions = []
    for key, path in iter_transcript_paths(root):
        try:
            sessions.append(load_session(path, key))
        except OSError:
            continue
    sessions.sort(key=lambda s: s.last_activity or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return sessions
