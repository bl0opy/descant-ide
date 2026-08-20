"""``descant`` context-cost inspector CLI.

    python -m descant.cli list
    python -m descant.cli show <session-id-prefix>
    python -m descant.cli list --json

Reads transcripts from ``$DESCANT_PROJECTS_DIR`` (default ~/.claude/projects).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # allow `python cli.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "descant"

from . import config, tokens  # noqa: E402
from .inspector import CONVERSATION_BUCKETS, analyze  # noqa: E402
from .transcript import load_all  # noqa: E402

# --- tiny ANSI helpers (no dependency) ------------------------------------

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


BOLD = lambda s: _c("1", s)  # noqa: E731
DIM = lambda s: _c("2", s)  # noqa: E731
CYAN = lambda s: _c("36", s)  # noqa: E731
GREEN = lambda s: _c("32", s)  # noqa: E731
YELLOW = lambda s: _c("33", s)  # noqa: E731
RED = lambda s: _c("31", s)  # noqa: E731

BAR_COLORS = ["35", "36", "33", "32", "34", "31", "37"]


def human_age(ts: datetime | None) -> str:
    if ts is None:
        return "unknown"
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    secs = (now - ts).total_seconds()
    if secs < 0:
        return "just now"
    for limit, div, unit in ((60, 1, "s"), (3600, 60, "m"), (86400, 3600, "h"), (86400 * 30, 86400, "d")):
        if secs < limit:
            return f"{int(secs // div)}{unit} ago"
    return f"{int(secs // (86400 * 30))}mo ago"


def bar(fraction: float, width: int = 28, color: str = "36") -> str:
    filled = int(round(max(0.0, min(1.0, fraction)) * width))
    return _c(color, "█" * filled) + DIM("·" * (width - filled))


def _load(args) -> list:
    root = Path(args.projects_dir).expanduser() if args.projects_dir else config.projects_dir()
    if not root.exists():
        print(f"error: transcript directory not found: {root}", file=sys.stderr)
        print(
            "hint: set DESCANT_PROJECTS_DIR (e.g. to ./fixtures/projects) or pass --projects-dir",
            file=sys.stderr,
        )
        raise SystemExit(2)
    sessions = load_all(root)
    if args.repo:
        needle = args.repo.lower()
        sessions = [s for s in sessions if needle in s.repo_path.lower()]
    return sessions


# --------------------------------------------------------------------------


def cmd_list(args) -> int:
    sessions = _load(args)
    reports = [analyze(s) for s in sessions]

    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2))
        return 0

    if not reports:
        print(DIM("no sessions found"))
        return 0

    rows = []
    for r in reports:
        s = r.session
        rows.append(
            (
                s.repo_name,
                s.session_id[:8],
                human_age(s.last_activity),
                str(s.message_count),
                tokens.fmt_tokens(r.total_tokens),
                tokens.fmt_tokens(r.measured.current_context_tokens)
                if r.measured.available
                else "-",
                f"${r.measured.usd:.2f}" if r.measured.available else "-",
                s.title[: args.width],
            )
        )

    headers = ("REPO", "SESSION", "LAST", "MSGS", "EST CTX", "MEASURED", "COST", "TITLE")
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    print(BOLD("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))))
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            cell = cell.ljust(widths[i])
            if i == 0:
                cell = CYAN(cell)
            elif i == 7:
                cell = DIM(cell)
            cells.append(cell)
        print("  ".join(cells))

    total_est = sum(r.total_tokens for r in reports)
    total_usd = sum(r.measured.usd for r in reports)
    print()
    print(
        DIM(
            f"{len(reports)} session(s) across "
            f"{len({r.session.repo_path for r in reports})} repo(s) · "
            f"{tokens.fmt_tokens(total_est)} est. context · ${total_usd:.2f} spent"
        )
    )
    print(DIM(f"source: {args.projects_dir or config.projects_dir()}"))
    return 0


def cmd_show(args) -> int:
    sessions = _load(args)
    matches = [s for s in sessions if s.session_id.startswith(args.session_id)]
    if not matches:
        matches = [s for s in sessions if args.session_id in s.session_id]
    if not matches:
        print(f"error: no session matching {args.session_id!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: {args.session_id!r} is ambiguous:", file=sys.stderr)
        for s in matches[:10]:
            print(f"  {s.session_id}  {s.repo_name}", file=sys.stderr)
        return 1

    r = analyze(matches[0])
    if args.json:
        print(json.dumps(r.to_dict(), indent=2))
        return 0

    s = r.session
    print()
    print(BOLD(f"  {s.repo_name}") + DIM(f"  {s.repo_path}"))
    print(DIM(f"  session {s.session_id}"))
    print(f"  {s.title}")
    meta = [
        f"{s.message_count} messages",
        f"{s.turn_count} turns",
        f"{s.tool_call_count} tool calls",
        f"last activity {human_age(s.last_activity)}",
    ]
    if s.git_branch:
        meta.append(f"branch {s.git_branch}")
    if s.model:
        meta.append(s.model)
    print(DIM("  " + " · ".join(meta)))
    print()

    # --- top-level split -------------------------------------------------
    print(BOLD("  CONTEXT COST"))
    tag = GREEN("measured") if r.preamble_is_measured else YELLOW("estimated")
    top = [
        ("System prompt", r.system_prompt_tokens, "35"),
        ("Tool schemas", r.tool_schema_tokens, "33"),
        ("Conversation", r.conversation_tokens, "36"),
    ]
    total = r.total_tokens or 1
    label_w = max(len(n) for n, _, _ in top)
    for name, val, color in top:
        print(
            f"  {name.ljust(label_w)}  {bar(val / total, color=color)} "
            f"{tokens.fmt_tokens(val).rjust(7)}  {DIM(f'{100 * val / total:5.1f}%')}"
        )
    print(f"  {'TOTAL'.ljust(label_w)}  {' ' * 28} {BOLD(tokens.fmt_tokens(total).rjust(7))}")
    print(DIM(f"  (system prompt + tool schemas derived from a {tag} preamble)"))
    print()

    # --- conversation breakdown ------------------------------------------
    print(BOLD("  CONVERSATION BREAKDOWN"))
    conv_total = r.conversation_tokens or 1
    label_w = max(len(lbl) for _, lbl in CONVERSATION_BUCKETS)
    for i, (key, label) in enumerate(CONVERSATION_BUCKETS):
        val = r.conversation_breakdown.get(key, 0)
        print(
            f"  {label.ljust(label_w)}  {bar(val / conv_total, color=BAR_COLORS[i % len(BAR_COLORS)])} "
            f"{tokens.fmt_tokens(val).rjust(7)}  {DIM(f'{100 * val / conv_total:5.1f}%')}"
        )
    print()

    # --- per-tool --------------------------------------------------------
    if r.tool_costs:
        print(BOLD("  COST BY TOOL") + DIM("  (call inputs + results)"))
        top_tools = r.tool_costs[: args.top]
        tw = max(len(t.name) for t in top_tools)
        biggest = max(t.total for t in top_tools) or 1
        for t in top_tools:
            print(
                f"  {t.name.ljust(tw)}  {bar(t.total / biggest, width=20, color='36')} "
                f"{tokens.fmt_tokens(t.total).rjust(7)}  "
                + DIM(f"{t.calls} call{'s' if t.calls != 1 else ''}")
            )
        if len(r.tool_costs) > args.top:
            print(DIM(f"  … {len(r.tool_costs) - args.top} more"))
        print()

    # --- injected context ------------------------------------------------
    if r.attachment_breakdown:
        print(BOLD("  INJECTED CONTEXT") + DIM("  (attachments you never typed)"))
        aw = max(len(k) for k in r.attachment_breakdown)
        for k, v in list(r.attachment_breakdown.items())[: args.top]:
            print(f"  {k.ljust(aw)}  {tokens.fmt_tokens(v).rjust(7)}")
        print()

    # --- measured --------------------------------------------------------
    m = r.measured
    if m.available:
        print(BOLD("  MEASURED USAGE") + DIM("  (exact, from transcript)"))
        pct_full = 100.0 * m.current_context_tokens / 200_000
        warn = RED if pct_full > 80 else (YELLOW if pct_full > 60 else GREEN)
        print(f"  API requests        {m.request_count}")
        print(
            f"  Context now         {tokens.fmt_tokens(m.current_context_tokens)}  "
            + warn(f"{pct_full:.0f}% of a 200k window")
        )
        print(f"  Peak context        {tokens.fmt_tokens(m.peak_context_tokens)}")
        print(f"  Output tokens       {tokens.fmt_tokens(m.output_tokens)}")
        print(
            f"  Cache               {tokens.fmt_tokens(m.cache_read_tokens)} read · "
            f"{tokens.fmt_tokens(m.cache_creation_tokens)} written"
        )
        if m.thinking_tokens:
            print(f"  Thinking            {tokens.fmt_tokens(m.thinking_tokens)}")
        print(f"  Estimated spend     ${m.usd:.4f}")
        print()

    if r.notes and not args.quiet:
        print(BOLD("  NOTES"))
        for n in r.notes:
            print(DIM(f"  · {n}"))
        print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="descant", description="Claude Code context-cost inspector")
    p.add_argument("--projects-dir", help="override $DESCANT_PROJECTS_DIR")
    p.add_argument("--repo", help="filter to repos whose path contains this substring")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd")

    lp = sub.add_parser("list", help="one line per session")
    lp.add_argument("--width", type=int, default=60, help="max title width")
    lp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="full breakdown for one session")
    sp.add_argument("session_id", help="session id or unique prefix")
    sp.add_argument("--top", type=int, default=10)
    sp.add_argument("-q", "--quiet", action="store_true", help="hide methodology notes")
    sp.set_defaults(func=cmd_show)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args((argv or sys.argv[1:]) + ["list"])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
