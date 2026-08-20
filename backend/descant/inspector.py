"""Context-cost inspector.

Answers: *where did this session's context window actually go?*

Strategy
--------
Claude Code records exact ``usage`` on every assistant turn, so the size of the
context window at each request is a **measured** fact.  What the usage block
does *not* tell you is the breakdown -- how much is the system prompt, how much
is tool schemas, how much is the conversation, and which tool is quietly eating
40% of your window.  That split is what we compute here.

The anchor trick: at the *first* request of a session the context window
contains the system prompt + tool schemas + exactly one user turn.  We know the
measured total, and we can estimate the user turn, so

    preamble = first_request_prompt_tokens - estimate(first user turn)

is a *measured-by-difference* number for "everything Claude Code injects before
you have said anything".  Splitting the preamble into system prompt vs tool
schemas is the only place a soft table is used, and it is flagged as such.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from . import tokens
from .transcript import Session

#: Conversation buckets, in report order.
CONVERSATION_BUCKETS = [
    ("user_messages", "Your messages"),
    ("assistant_text", "Assistant replies"),
    ("thinking", "Thinking"),
    ("tool_calls", "Tool call inputs"),
    ("tool_results", "Tool results"),
    ("attachments", "Injected context"),
]

_KIND_TO_BUCKET = {
    "user_text": "user_messages",
    "assistant_text": "assistant_text",
    "thinking": "thinking",
    "tool_use": "tool_calls",
    "tool_result": "tool_results",
    "attachment": "attachments",
}


@dataclass
class Measured:
    """Exact numbers lifted straight out of the transcript's usage blocks."""

    request_count: int = 0
    first_prompt_tokens: int = 0
    #: Size of the context window at the most recent request.  This is the
    #: number that matters when you are wondering how close you are to a
    #: compaction.
    current_context_tokens: int = 0
    peak_context_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    uncached_input_tokens: int = 0
    thinking_tokens: int = 0
    usd: float = 0.0

    @property
    def available(self) -> bool:
        return self.request_count > 0


@dataclass
class ToolCost:
    name: str
    calls: int = 0
    input_tokens: int = 0
    result_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.result_tokens


@dataclass
class ContextReport:
    session: Session
    measured: Measured
    #: Top-level split the user asked for.
    system_prompt_tokens: int = 0
    tool_schema_tokens: int = 0
    conversation_tokens: int = 0
    preamble_tokens: int = 0
    preamble_is_measured: bool = False
    conversation_breakdown: dict[str, int] = field(default_factory=dict)
    tool_costs: list[ToolCost] = field(default_factory=list)
    attachment_breakdown: dict[str, int] = field(default_factory=dict)
    tools_seen: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.system_prompt_tokens + self.tool_schema_tokens + self.conversation_tokens

    def pct(self, n: int) -> float:
        t = self.total_tokens
        return (100.0 * n / t) if t else 0.0

    def to_dict(self) -> dict:
        m = self.measured
        return {
            **self.session.meta_dict(),
            "context": {
                "total_tokens": self.total_tokens,
                "system_prompt_tokens": self.system_prompt_tokens,
                "tool_schema_tokens": self.tool_schema_tokens,
                "conversation_tokens": self.conversation_tokens,
                "preamble_tokens": self.preamble_tokens,
                "preamble_is_measured": self.preamble_is_measured,
                "conversation_breakdown": self.conversation_breakdown,
                "attachment_breakdown": self.attachment_breakdown,
                "tools_seen": self.tools_seen,
                "tool_costs": [
                    {
                        "name": t.name,
                        "calls": t.calls,
                        "input_tokens": t.input_tokens,
                        "result_tokens": t.result_tokens,
                        "total_tokens": t.total,
                    }
                    for t in self.tool_costs
                ],
                "notes": self.notes,
            },
            "measured": {
                "available": m.available,
                "request_count": m.request_count,
                "first_prompt_tokens": m.first_prompt_tokens,
                "current_context_tokens": m.current_context_tokens,
                "peak_context_tokens": m.peak_context_tokens,
                "output_tokens": m.output_tokens,
                "cache_creation_tokens": m.cache_creation_tokens,
                "cache_read_tokens": m.cache_read_tokens,
                "uncached_input_tokens": m.uncached_input_tokens,
                "thinking_tokens": m.thinking_tokens,
                "usd": round(m.usd, 4),
            },
        }


def _measure(session: Session) -> Measured:
    m = Measured()
    for r in session.requests:
        prompt = (
            int(r.get("input_tokens") or 0)
            + int(r.get("cache_creation_input_tokens") or 0)
            + int(r.get("cache_read_input_tokens") or 0)
        )
        if m.request_count == 0:
            m.first_prompt_tokens = prompt
        m.request_count += 1
        m.current_context_tokens = prompt
        m.peak_context_tokens = max(m.peak_context_tokens, prompt)
        m.output_tokens += int(r.get("output_tokens") or 0)
        m.cache_creation_tokens += int(r.get("cache_creation_input_tokens") or 0)
        m.cache_read_tokens += int(r.get("cache_read_input_tokens") or 0)
        m.uncached_input_tokens += int(r.get("input_tokens") or 0)
        details = r.get("output_tokens_details") or {}
        if isinstance(details, dict):
            m.thinking_tokens += int(details.get("thinking_tokens") or 0)
    m.usd = tokens.usd_cost(
        m.uncached_input_tokens, m.output_tokens, m.cache_creation_tokens, m.cache_read_tokens
    )
    return m


def _first_turn_tokens(session: Session) -> int:
    """Estimated tokens present in the window at the very first API request:
    everything up to (but excluding) the first assistant output."""
    total = 0
    for ev in session.events:
        if ev.kind in ("assistant_text", "thinking", "tool_use"):
            break
        total += tokens.estimate_event(ev)
    return total


def analyze(session: Session) -> ContextReport:
    measured = _measure(session)
    report = ContextReport(session=session, measured=measured)

    # --- conversation, bucketed ------------------------------------------
    buckets: dict[str, int] = {k: 0 for k, _ in CONVERSATION_BUCKETS}
    per_tool: dict[str, ToolCost] = {}
    per_attachment: dict[str, int] = defaultdict(int)

    for ev in session.events:
        bucket = _KIND_TO_BUCKET.get(ev.kind)
        if bucket is None:
            continue
        cost = tokens.estimate_event(ev)
        buckets[bucket] += cost
        if ev.kind == "attachment":
            per_attachment[ev.subtype or "attachment"] += cost
        if ev.tool_name:
            tc = per_tool.setdefault(ev.tool_name, ToolCost(name=ev.tool_name))
            if ev.kind == "tool_use":
                tc.calls += 1
                tc.input_tokens += cost
            elif ev.kind == "tool_result":
                tc.result_tokens += cost

    # --- replace estimates with measured numbers where they exist -----------
    # Everything the assistant produced was billed as output, and those totals
    # are exact.  Thinking is the big one: transcripts store a redacted stub, so
    # estimating it from characters undercounts badly on a long session (it cost
    # ~20% of drift on a real 250k-token transcript).  The rest of the output --
    # reply text and tool-call inputs -- is apportioned across those two buckets
    # by their character ratio, since only their sum is known exactly.
    if measured.available and measured.output_tokens:
        buckets["thinking"] = measured.thinking_tokens
        non_thinking = max(0, measured.output_tokens - measured.thinking_tokens)
        est_pair = buckets["assistant_text"] + buckets["tool_calls"]
        if est_pair > 0 and non_thinking > 0:
            share = non_thinking / est_pair
            buckets["assistant_text"] = round(buckets["assistant_text"] * share)
            buckets["tool_calls"] = round(buckets["tool_calls"] * share)
            for tc in per_tool.values():
                tc.input_tokens = round(tc.input_tokens * share)
        report.notes.append(
            "Assistant-side rows (replies, thinking, tool call inputs) use the "
            "transcript's measured output tokens; user messages, tool results and "
            "injected context are estimated."
        )

    report.conversation_breakdown = buckets
    report.conversation_tokens = sum(buckets.values())
    report.tool_costs = sorted(per_tool.values(), key=lambda t: t.total, reverse=True)
    report.attachment_breakdown = dict(
        sorted(per_attachment.items(), key=lambda kv: kv[1], reverse=True)
    )

    # --- preamble: system prompt + tool schemas ---------------------------
    tools_seen = sorted({t.name for t in report.tool_costs})
    report.tools_seen = tools_seen

    if measured.available and measured.first_prompt_tokens > 0:
        first_turn = _first_turn_tokens(session)
        preamble = measured.first_prompt_tokens - first_turn
        if preamble > 0:
            report.preamble_tokens = preamble
            report.preamble_is_measured = True
        else:
            report.notes.append(
                "First-request usage is smaller than the estimated first turn; "
                "the transcript is probably a resume/compaction continuation, so "
                "the preamble below is estimated rather than measured."
            )
    if not report.preamble_is_measured:
        # Fall back: assume the schemas we can name, plus a typical system prompt.
        report.preamble_tokens = tokens.estimate_tool_schemas(tools_seen) + 3000
        report.notes.append("No usable first-request usage data; preamble is a rough estimate.")

    schema_est = tokens.estimate_tool_schemas(tools_seen)
    # Only tools that were actually *called* appear in the transcript, so this
    # under-counts the schemas that were loaded but unused.  Clamp so the system
    # prompt never goes negative.
    report.tool_schema_tokens = min(schema_est, max(0, report.preamble_tokens - 500))
    report.system_prompt_tokens = max(0, report.preamble_tokens - report.tool_schema_tokens)
    if tools_seen:
        report.notes.append(
            "Tool-schema figure covers the %d tool(s) this session actually called; "
            "schemas loaded but never used are folded into the system prompt line."
            % len(tools_seen)
        )

    # --- reconcile estimate against the measured window -------------------
    if measured.available and measured.current_context_tokens:
        drift = report.total_tokens - measured.current_context_tokens
        pct = 100.0 * drift / measured.current_context_tokens
        report.notes.append(
            "Estimated total is %s vs %s measured at the last request (%+.0f%% drift)."
            % (
                tokens.fmt_tokens(report.total_tokens),
                tokens.fmt_tokens(measured.current_context_tokens),
                pct,
            )
        )

    return report


def analyze_all(sessions) -> list[ContextReport]:
    return [analyze(s) for s in sessions]
