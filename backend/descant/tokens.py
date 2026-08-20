"""Token estimation.

No exact tokenizer is available offline (tiktoken cannot fetch its BPE files in
a sandbox, and it would be the wrong vocabulary for Claude anyway), so we use a
characters-per-token heuristic, calibrated against the *measured* usage numbers
that Claude Code records in every transcript.

Crucially the inspector never relies on the heuristic alone where a real number
exists: transcripts carry exact ``usage`` blocks, and we anchor the report to
those, using the heuristic only to apportion the measured totals across
categories.  Every reported field is tagged ``measured``, ``estimated`` or
``derived`` so it is obvious which is which.
"""

from __future__ import annotations

import json
from typing import Any

from . import config

# Content that is mostly prose vs mostly structured payload.  JSON and code
# tokenize denser (more tokens per character) than English.
_PROSE_KINDS = {"user_text", "assistant_text", "thinking"}


def estimate_text(text: str, dense: bool = False) -> int:
    if not text:
        return 0
    ratio = config.CHARS_PER_TOKEN_CODE if dense else config.CHARS_PER_TOKEN_TEXT
    return max(1, round(len(text) / ratio))


def estimate_json(obj: Any) -> int:
    if obj is None:
        return 0
    return estimate_text(json.dumps(obj, ensure_ascii=False), dense=True)


def estimate_event(event) -> int:
    """Token cost an event contributes to the context window."""
    dense = event.kind not in _PROSE_KINDS
    n = estimate_text(event.text, dense=dense)
    if event.tool_input is not None:
        n += estimate_json(event.tool_input)
    if event.tool_name:
        n += 3  # name + block scaffolding
    return n + config.MESSAGE_FRAMING_TOKENS


# --------------------------------------------------------------------------
# tool schema sizes
# --------------------------------------------------------------------------
# Tool *schemas* are never written to the transcript -- they live only in the
# API request.  These are measured-by-hand approximations of the JSON schema
# size of each built-in Claude Code tool, in tokens.  They are the one genuinely
# soft number in the report, which is why the CLI can print the preamble
# unsplit (`--no-split`) when you would rather see a measured number alone.
TOOL_SCHEMA_TOKENS = {
    "Task": 700,
    "Agent": 700,
    "Bash": 520,
    "Glob": 180,
    "Grep": 620,
    "Read": 320,
    "Edit": 220,
    "Write": 160,
    "NotebookEdit": 220,
    "WebFetch": 220,
    "WebSearch": 260,
    "TodoWrite": 260,
    "TaskCreate": 260,
    "TaskUpdate": 200,
    "ExitPlanMode": 120,
    "BashOutput": 110,
    "KillShell": 60,
    "Skill": 260,
    "SlashCommand": 150,
    "ToolSearch": 200,
}
#: Anything we do not recognise (MCP tools, plugin tools) gets this.
UNKNOWN_TOOL_SCHEMA_TOKENS = 260
#: Fixed cost of the tools array scaffolding itself.
TOOL_BLOCK_OVERHEAD = 40


def estimate_tool_schemas(tool_names) -> int:
    total = TOOL_BLOCK_OVERHEAD if tool_names else 0
    for name in tool_names:
        total += TOOL_SCHEMA_TOKENS.get(name, UNKNOWN_TOOL_SCHEMA_TOKENS)
    return total


# --------------------------------------------------------------------------
# money
# --------------------------------------------------------------------------


def usd_cost(
    input_tokens: int, output_tokens: int, cache_write: int, cache_read: int
) -> float:
    p = config.PRICING_USD_PER_MTOK
    return (
        input_tokens * p["input"]
        + output_tokens * p["output"]
        + cache_write * p["cache_write"]
        + cache_read * p["cache_read"]
    ) / 1_000_000


def fmt_tokens(n: float) -> str:
    n = int(round(n))
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)
