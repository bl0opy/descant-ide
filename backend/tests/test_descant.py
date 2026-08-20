"""Tests for the parts everything else stands on.

    cd backend && python3 -m tests.test_descant

Plain asserts, no pytest dependency -- this needs to run anywhere.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from descant import events, tokens  # noqa: E402
from descant.inspector import analyze  # noqa: E402
from descant.transcript import decode_project_dir, load_all, parse_lines  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "projects"

PASSED = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {extra}")
        raise AssertionError(label)


def _lines(*objs) -> list[str]:
    return [json.dumps(o) for o in objs]


# --------------------------------------------------------------------------


def test_usage_deduped_by_request_id():
    """One API request emits several assistant lines repeating one usage block.

    Summing per line triple-counts.  This is the single easiest thing to get
    wrong about this format, so it gets the first test.
    """
    usage = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 0,
        "output_tokens": 50,
    }
    msg = lambda blocks: {  # noqa: E731
        "type": "assistant",
        "requestId": "req_same",
        "uuid": "u",
        "timestamp": "2026-08-20T10:00:00Z",
        "message": {"role": "assistant", "content": blocks, "usage": usage, "model": "m"},
    }
    sess = parse_lines(
        _lines(
            {"type": "user", "message": {"role": "user", "content": "hi"},
             "timestamp": "2026-08-20T09:59:00Z", "cwd": "/tmp/x"},
            msg([{"type": "text", "text": "one"}]),
            msg([{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
            msg([{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pwd"}}]),
        ),
        "s",
        Path("s.jsonl"),
        "-tmp-x",
    )
    check("one request recorded, not three", len(sess.requests) == 1, str(sess.requests))
    m = analyze(sess).measured
    check("output tokens counted once", m.output_tokens == 50, str(m.output_tokens))
    check("context measured once", m.current_context_tokens == 1002)


def test_content_string_or_blocks():
    sess = parse_lines(
        _lines(
            {"type": "user", "message": {"role": "user", "content": "bare string"},
             "timestamp": "2026-08-20T09:00:00Z"},
            {"type": "user", "message": {"role": "user",
                                         "content": [{"type": "text", "text": "block form"}]},
             "timestamp": "2026-08-20T09:01:00Z"},
        ),
        "s", Path("s.jsonl"), "-x",
    )
    kinds = [e.kind for e in sess.events]
    check("both content shapes parse", kinds == ["user_text", "user_text"], str(kinds))


def test_unknown_line_types_are_survivable():
    sess = parse_lines(
        _lines(
            {"type": "queue-operation", "operation": "clear"},
            {"type": "atis-latch", "atis": "x"},
            {"type": "some-future-type-we-have-never-seen", "payload": {"a": 1}},
            {"type": "user", "message": {"role": "user", "content": "still here"},
             "timestamp": "2026-08-20T09:00:00Z"},
        )
        + ["{not json at all", ""],
        "s", Path("s.jsonl"), "-x",
    )
    check("unknown types skipped, not fatal", len(sess.events) == 1)
    check("malformed line counted", sess.malformed_lines == 1, str(sess.malformed_lines))


def test_tool_results_attributed_to_their_tool():
    sess = parse_lines(
        _lines(
            {"type": "assistant", "requestId": "r", "timestamp": "2026-08-20T09:00:00Z",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "id": "t1", "name": "Grep", "input": {"pattern": "x"}}]}},
            {"type": "user", "timestamp": "2026-08-20T09:00:01Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "t1", "content": "a" * 4000}]}},
        ),
        "s", Path("s.jsonl"), "-x",
    )
    result = [e for e in sess.events if e.kind == "tool_result"][0]
    check("result inherits its tool's name", result.tool_name == "Grep")
    costs = {t.name: t for t in analyze(sess).tool_costs}
    check("Grep's result counted against Grep", costs["Grep"].result_tokens > 1000)


def test_attachments_are_counted_as_context():
    """Injected context is invisible in most UIs and is not free."""
    sess = parse_lines(
        _lines(
            {"type": "attachment", "timestamp": "2026-08-20T09:00:00Z",
             "attachment": {"type": "skill_listing", "text": "x" * 8000}},
        ),
        "s", Path("s.jsonl"), "-x",
    )
    report = analyze(sess)
    check("attachment shows in the breakdown",
          report.conversation_breakdown["attachments"] > 1500)
    check("attachment typed in its own table",
          "skill_listing" in report.attachment_breakdown)


def test_cwd_beats_lossy_directory_name():
    sess = parse_lines(
        _lines({"type": "user", "message": {"role": "user", "content": "hi"},
                "cwd": "/Users/ayan/code/my-dashed-repo",
                "timestamp": "2026-08-20T09:00:00Z"}),
        "s", Path("s.jsonl"), "-Users-ayan-code-my-dashed-repo",
    )
    check("exact cwd wins over the encoded dir",
          sess.repo_path == "/Users/ayan/code/my-dashed-repo", sess.repo_path)
    check("repo name derived from it", sess.repo_name == "my-dashed-repo")
    # The fallback decoder is ambiguous by nature, so it disambiguates against
    # the filesystem: a real directory wins over the naive all-slashes reading.
    with tempfile.TemporaryDirectory() as td:
        real = Path(td) / "code" / "my-dashed-repo"
        real.mkdir(parents=True)
        encoded = str(real).replace("/", "-")
        check("decoder resolves dashes against disk",
              decode_project_dir(encoded) == str(real), decode_project_dir(encoded))
    check("decoder falls back when nothing exists",
          decode_project_dir("-no-such-place-anywhere")
          == "/no/such/place/anywhere")


def test_preamble_is_measured_not_guessed():
    """preamble = first request's measured total - the estimated first turn."""
    sess = parse_lines(
        _lines(
            {"type": "user", "message": {"role": "user", "content": "short"},
             "timestamp": "2026-08-20T09:00:00Z", "cwd": "/tmp/x"},
            {"type": "assistant", "requestId": "r1", "timestamp": "2026-08-20T09:00:01Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}],
                         "model": "m",
                         "usage": {"input_tokens": 0, "cache_creation_input_tokens": 20000,
                                   "cache_read_input_tokens": 0, "output_tokens": 5}}},
        ),
        "s", Path("s.jsonl"), "-tmp-x",
    )
    r = analyze(sess)
    check("preamble flagged measured", r.preamble_is_measured)
    check("preamble ≈ 20k minus a tiny turn", 19_800 < r.preamble_tokens <= 20_000,
          str(r.preamble_tokens))
    check("split adds back up",
          r.system_prompt_tokens + r.tool_schema_tokens == r.preamble_tokens)
    check("system prompt never negative", r.system_prompt_tokens >= 0)


def test_stream_json_permission_is_surfaced():
    """The event that makes a session go amber."""
    out = events.from_stream_json({
        "type": "system", "subtype": "permission_denied",
        "tool_name": "Bash", "tool_use_id": "t9", "message": "blocked",
    })
    check("permission event emitted", [e["kind"] for e in out] == ["permission"])
    check("carries the tool name", out[0]["tool_name"] == "Bash")

    out = events.from_stream_json({
        "type": "system", "subtype": "post_turn_summary",
        "status_category": "blocked", "status_detail": "d", "needs_action": "approve it",
    })
    check("needs_action carried through", out[0]["meta"]["needs_action"] == "approve it")


def test_stream_json_assistant_blocks():
    out = events.from_stream_json({
        "type": "assistant",
        "message": {"role": "assistant", "usage": {"output_tokens": 1}, "content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "t", "name": "Read", "input": {"file_path": "/a"}},
        ]},
    })
    kinds = [e["kind"] for e in out]
    check("blocks normalised in order",
          kinds == ["thinking", "assistant_text", "tool_use", "system"], str(kinds))


def test_fixtures_parse_and_analyze():
    if not FIXTURES.exists():
        print("  skip fixtures (run fixtures/generate.py first)")
        return
    sessions = load_all(FIXTURES)
    check("fixtures load", len(sessions) >= 6, str(len(sessions)))
    check("across 3 repos", len({s.repo_path for s in sessions}) == 3)
    for s in sessions:
        r = analyze(s)
        check(f"{s.repo_name}/{s.session_id[:8]} has a positive total",
              r.total_tokens > 0)
        check(f"{s.session_id[:8]} estimate within 25% of measured",
              abs(r.total_tokens - r.measured.current_context_tokens)
              / max(1, r.measured.current_context_tokens) < 0.25)


def test_token_helpers():
    check("empty text is free", tokens.estimate_text("") == 0)
    check("prose cheaper per char than json",
          tokens.estimate_text("a" * 1000) < tokens.estimate_text("a" * 1000, dense=True))
    check("unknown tools still cost something",
          tokens.estimate_tool_schemas(["NoSuchTool"]) > 0)
    check("formatting", tokens.fmt_tokens(1500) == "1.5k" and tokens.fmt_tokens(12) == "12")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\n{PASSED} checks passed across {len(tests)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
