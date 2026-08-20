"""Central configuration for Descant.

Everything that points at the outside world lives here so that swapping the
fixture data for a user's real ``~/.claude/projects`` needs zero code changes --
just an env var.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- data source -----------------------------------------------------------

#: Where Claude Code writes its session transcripts.
#: Override with DESCANT_PROJECTS_DIR to point at fixtures (or another machine's
#: exported history).  Default matches the real Claude Code location.
DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def projects_dir() -> Path:
    """Resolve the transcript root, honouring DESCANT_PROJECTS_DIR."""
    raw = os.environ.get("DESCANT_PROJECTS_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return DEFAULT_PROJECTS_DIR


# --- claude CLI ------------------------------------------------------------

#: Binary used to spawn live sessions.  Override for a pinned/dev build.
CLAUDE_BIN = os.environ.get("DESCANT_CLAUDE_BIN", "claude")

#: Where `descant run` creates its throwaway sandbox repo when none is given.
SANDBOX_DIR = Path(
    os.environ.get("DESCANT_SANDBOX_DIR", str(Path.home() / ".descant" / "sandbox"))
).expanduser()


# --- server ----------------------------------------------------------------

HOST = os.environ.get("DESCANT_HOST", "127.0.0.1")
PORT = int(os.environ.get("DESCANT_PORT", "8787"))


# --- token accounting ------------------------------------------------------
# See tokens.py for how these are used.  Calibrated against real transcripts in
# ~/.claude/projects (see PROGRESS.md "Token estimation").

#: Average characters per token for natural-language prose.
CHARS_PER_TOKEN_TEXT = 3.8
#: Average characters per token for JSON / code / tool payloads (denser).
CHARS_PER_TOKEN_CODE = 3.1
#: Per-message structural overhead the API charges beyond raw content.
MESSAGE_FRAMING_TOKENS = 5

#: USD per million tokens.  Only used for the informational $ column; edit here.
PRICING_USD_PER_MTOK = {
    "input": 5.00,
    "output": 25.00,
    "cache_write": 6.25,
    "cache_read": 0.50,
}
