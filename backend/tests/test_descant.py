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




# ==========================================================================
# Feature modules: skills, MCP, mining, library
# ==========================================================================


def test_skill_frontmatter_parsing():
    """Frontmatter is parsed without a YAML dependency, tolerantly."""
    from descant import skills

    meta, body = skills.parse_frontmatter(
        '---\nname: thing\ndescription: "Quoted, with: a colon"\n---\n# Body\ntext\n'
    )
    check("name parsed", meta["name"] == "thing")
    check(meta["description"] == "Quoted, with: a colon", "quotes stripped, colon kept")
    check("body separated", body.startswith("# Body"))

    meta2, body2 = skills.parse_frontmatter("no frontmatter here")
    check("missing frontmatter is not fatal", meta2 == {} and body2 == "no frontmatter here")

    # Block scalars fold onto one line.
    meta3, _ = skills.parse_frontmatter("---\ndescription: |\n  line one\n  line two\n---\nx")
    check("block scalar folded", "line one line two" == meta3["description"])


def test_mcp_conversion_shrinks_and_is_runnable():
    """Converting a server produces a smaller listing and a real client."""
    from descant import mcp

    server = mcp.McpServer(
        name="weather",
        config={"command": "python3", "args": ["/tmp/x.py"]},
        source=".mcp.json",
        probed=True,
        tools=[
            mcp.McpTool("get_forecast", "Get a forecast for a place.", {"type": "object"}),
            mcp.McpTool("get_current", "Get current conditions.", {"type": "object"}),
        ],
    )
    saving = mcp.conversion_savings(server)
    check("conversion reduces cost", saving["before_tokens"] > saving["after_tokens"])
    rendered = mcp.render_skill(server)
    check("SKILL.md rendered", "weather/SKILL.md" in rendered["files"])
    check("client rendered", "weather/mcp_call.py" in rendered["files"])
    check(
        "tools/call" in rendered["files"]["weather/mcp_call.py"],
        "client actually speaks MCP rather than being a stub",
    )
    check(
        "get_forecast" in rendered["files"]["weather/SKILL.md"],
        "tool names listed for discoverability",
    )


def test_mining_collapses_cycles_and_rotations():
    """The de-noising rules are the whole value; pin them."""
    from descant import mining

    check(
        mining._base_period(("A", "B", "C", "A", "B")) == ("A", "B", "C"),
        "partial cycle collapses (must not require exact division)",
    )
    check(mining._base_period(("A", "B", "A", "B")) == ("A", "B"), "full cycle collapses")
    check(mining._base_period(("A", "B", "C")) == ("A", "B", "C"), "non-cycle left alone")
    check(
        mining._canonical_rotation(("B", "C", "A")) == ("A", "B", "C"),
        "rotations share a representative",
    )


def test_mining_signature_normalisation():
    from descant import mining

    check(
        mining.signature("tool_use", "Bash", {"command": "pytest -x tests/foo.py"})
        == mining.signature("tool_use", "Bash", {"command": "pytest -x tests/bar.py"}),
        "paths generalised away so sequences can repeat",
    )
    check(
        mining.signature("tool_use", "Bash", {"command": "git commit -m 'x'"}) == "Bash(git commit)",
        "subcommand kept",
    )
    check("noise dropped", mining.signature("tool_use", "Bash", {"command": "cd /tmp"}) is None)
    check(
        mining.signature("tool_use", "Read", {"file_path": "/a/b/c.py"}) == "Read(.py)",
        "file type is the signal, not the path",
    )


def test_mining_requires_a_command():
    """Read-then-edit is what every session does; it is never a workflow."""
    from descant import mining
    from descant.transcript import Event, Session
    from pathlib import Path

    def make(sid):
        s = Session(session_id=sid, path=Path("/x"), project_key="p", cwd="/repo")
        for _ in range(4):
            s.events.append(Event(kind="tool_use", tool_name="Read", tool_input={"file_path": "a.py"}))
            s.events.append(Event(kind="tool_use", tool_name="Edit", tool_input={"file_path": "a.py"}))
        return s

    found = mining.mine([make("s1"), make("s2")])
    check("pure read/edit repetition yields no candidates", not found)


def test_library_ranks_by_meaning_where_synonyms_reach():
    from descant import library

    lib = library.Library()
    lib.entries = [
        library.Entry(
            key="a", kind="skill", name="xlsx", source="user",
            description="Read and write spreadsheets and csv files.",
        ),
        library.Entry(
            key="b", kind="skill", name="docx", source="user",
            description="Create and edit Word documents.",
        ),
    ]
    lib.finalize()
    top = lib.score_query("turn a csv into a chart")
    check("csv query finds the spreadsheet skill", top and top[0]["name"] == "xlsx")
    check(
        all(m not in ("into", "turn", "a") for r in top for m in r["matched"]),
        "filler words do not count as matches",
    )
    top2 = lib.score_query("edit a word document")
    check("document query finds docx", top2 and top2[0]["name"] == "docx")


def test_inspector_carves_measured_attribution_from_preamble():
    """MCP/skill rows come out of the preamble; the system prompt keeps the rest."""
    from descant import inspector
    from descant.transcript import Event, Session
    from pathlib import Path

    s = Session(session_id="s", path=Path("/x"), project_key="p", cwd="/repo")
    s.events.append(Event(kind="user_text", text="hello"))
    s.events.append(Event(kind="assistant_text", text="hi"))
    s.requests.append(
        {"request_id": "r1", "ts": None, "input_tokens": 1,
         "cache_creation_input_tokens": 20000, "cache_read_input_tokens": 0,
         "output_tokens": 10}
    )

    plain = inspector.analyze(s)
    with_attr = inspector.analyze(
        s, attribution={"mcp_tokens": 3000, "skill_tokens": 1000, "source": "test"}
    )
    check("mcp carved out", with_attr.mcp_schema_tokens == 3000)
    check("skills carved out", with_attr.skill_listing_tokens == 1000)
    check(
        with_attr.system_prompt_tokens < plain.system_prompt_tokens,
        "system prompt shrinks by exactly what was attributed elsewhere",
    )
    check(
        abs(with_attr.preamble_tokens - plain.preamble_tokens) < 2,
        "the measured preamble itself is unchanged — only its split",
    )


def test_mcp_config_normalisation():
    """A pasted command line is the common case; argv is an implementation detail."""
    from descant import mcp

    cfg = mcp.normalise_config({"command": "uvx mcp-server-weather --verbose"})
    check("command split from argv", cfg == {"command": "uvx", "args": ["mcp-server-weather", "--verbose"]})

    url = mcp.normalise_config({"url": "https://example.com/mcp"})
    check("url transport typed", url == {"url": "https://example.com/mcp", "type": "http"})

    env = mcp.normalise_config({"command": "srv", "env": {"K": "v"}})
    check("env preserved", env["env"] == {"K": "v"})

    for bad in ({}, {"command": "   "}):
        try:
            mcp.normalise_config(bad)
        except ValueError:
            pass
        else:
            check("empty config rejected", False)
    check("empty config rejected", True)

    for bad_name in ("", "has space", "semi;colon"):
        try:
            mcp.validate_name(bad_name)
        except ValueError:
            pass
        else:
            check(f"name {bad_name!r} rejected", False)
    check("bad names rejected", True)


def test_mcp_saved_per_repo_is_discovered_and_enabled():
    """Saving must land where discovery looks -- and .mcp.json needs enabling too."""
    from descant import mcp

    with tempfile.TemporaryDirectory() as td:
        home, repo = Path(td) / "home", Path(td) / "repo"
        home.mkdir()
        repo.mkdir()
        real_home = Path.home
        Path.home = staticmethod(lambda: home)  # type: ignore[assignment]
        try:
            mcp.save_server(str(repo), "weather", {"command": "srv --flag"}, scope="project")
            written = json.loads((repo / ".mcp.json").read_text())
            check("project scope writes .mcp.json", "weather" in written["mcpServers"])

            found = {s.name: s for s in mcp.discover(str(repo))}
            check("saved server is discovered", "weather" in found)
            check(
                "a .mcp.json save also enables it, or it would appear to do nothing",
                found["weather"].enabled,
            )

            mcp.save_server(str(repo), "local-one", {"command": "other"}, scope="local")
            check(
                "local scope stays out of the repo",
                "local-one" not in json.loads((repo / ".mcp.json").read_text())["mcpServers"],
            )
            check("local scope is still discovered", "local-one" in {s.name for s in mcp.discover(str(repo))})

            mcp.delete_server(str(repo), "weather")
            check("deleted server disappears", "weather" not in {s.name for s in mcp.discover(str(repo))})

            try:
                mcp.delete_server(str(repo), "weather")
            except ValueError:
                check("deleting what this repo does not define is refused", True)
            else:
                check("deleting what this repo does not define is refused", False)
        finally:
            Path.home = real_home  # type: ignore[assignment]


def test_skill_scope_decides_which_agents_get_it():
    from descant import loadout, skills

    md = "---\nname: t\ndescription: d\n---\n\nbody\n"
    with tempfile.TemporaryDirectory() as td:
        home, repo, other = Path(td) / "home", Path(td) / "repo", Path(td) / "other"
        for d in (home, repo, other):
            d.mkdir()
        real_home = Path.home
        Path.home = staticmethod(lambda: home)  # type: ignore[assignment]
        try:
            r = loadout.write_mined_skill(str(repo), "Test Then Lint!", md, scope="repo")
            check("slug is filesystem-safe", r["slug"] == "test-then-lint")
            check("repo scope lands in the repo", Path(r["path"]).is_relative_to(repo))

            try:
                loadout.write_mined_skill(str(repo), "test-then-lint", md, scope="repo")
            except FileExistsError:
                check("an existing skill is not silently clobbered", True)
            else:
                check("an existing skill is not silently clobbered", False)
            check(
                "overwrite is possible when asked for",
                loadout.write_mined_skill(str(repo), "test-then-lint", md, scope="repo", overwrite=True)["overwritten"],
            )

            u = loadout.copy_skill(r["path"], scope="user")
            check("user scope lands in ~/.claude/skills", Path(u["path"]).is_relative_to(home))
            check(
                "a user-scope skill is visible with no repo at all",
                "t" in {s.name for s in skills.discover(None)},
            )

            c = loadout.copy_skill(u["path"], scope="repo", repo_path=str(other))
            check("a skill can be handed to another repo", Path(c["path"]).is_relative_to(other))
            check("the whole directory travels, not just SKILL.md", (Path(c["path"]) / "SKILL.md").is_file())

            loadout.delete_skill(c["path"])
            check("delete removes the directory", not Path(c["path"]).exists())
            try:
                loadout.delete_skill(str(other))
            except ValueError:
                check("delete refuses anything outside a .claude/skills tree", True)
            else:
                check("delete refuses anything outside a .claude/skills tree", False)
        finally:
            Path.home = real_home  # type: ignore[assignment]


def test_permission_rules_are_narrow():
    """Approving one call must not hand over the whole tool."""
    from descant.chat import _permission_rule

    check("bash scoped to the command", _permission_rule("Bash", {"command": "rm -rf /tmp/x"}) == "Bash(rm:*)")
    check(
        "subcommand kept where it is the useful unit",
        _permission_rule("Bash", {"command": "git commit -m 'x'"}) == "Bash(git commit:*)",
    )
    check(
        "leading env assignments ignored",
        _permission_rule("Bash", {"command": "FOO=1 pytest -x"}) == "Bash(pytest:*)",
    )
    check("unquotable input does not explode", _permission_rule("Bash", {"command": "echo 'unclosed"}) .startswith("Bash"))
    check("non-bash tools scope to the tool", _permission_rule("Write", {"file_path": "/a"}) == "Write")


def test_permission_request_borrows_args_from_the_tool_call():
    """The denial names the tool but not what it wanted -- correlate it back.

    Without this a blocked ``rm -rf`` and a blocked ``git status`` would offer
    the same approval, and that approval would be all of Bash.
    """
    import asyncio

    from descant import chat as chat_mod

    c = chat_mod.Chat(chat_id="t", cwd=".", session_id="s")

    async def drive():
        await c._handle(
            events.make(
                "tool_use",
                tool_name="Bash",
                tool_input={"command": "rm -rf build/"},
                tool_use_id="tu_1",
            )
        )
        await c._handle(
            events.make(
                "permission", subtype="denied", tool_name="Bash", tool_use_id="tu_1", is_error=True
            )
        )

    asyncio.run(drive())
    pending = c.pending["tu_1"]
    check("the blocked call's arguments are recovered", pending["tool_input"] == {"command": "rm -rf build/"})
    check("so the offered rule is specific", pending["rule"] == "Bash(rm:*)")
    check("and the chat knows it is waiting on a human", c.status == chat_mod.NEEDS_INPUT)


def test_chat_argv_resumes_rather_than_restarting():
    """Approval restarts the process; the conversation must survive that."""
    from descant import chat as chat_mod

    c = chat_mod.Chat(chat_id="t", cwd=".", session_id="abc-123")
    first = c._argv()
    check("first launch names the session", "--session-id" in first and "abc-123" in first)
    check("no permissions are granted up front", "--allowedTools" not in first)
    check("never skips permission checks", "--dangerously-skip-permissions" not in first)

    c._started_once = True
    c.allowed_tools = ["Bash(git status:*)"]
    second = c._argv()
    check("later launches rejoin the same session", "--resume" in second and "abc-123" in second)
    check("and only ever name it once", "--session-id" not in second)
    check("carrying the approvals forward", "Bash(git status:*)" in second)


def test_remembered_rules_land_where_claude_code_reads_them():
    from descant.chat import remember_rule

    with tempfile.TemporaryDirectory() as td:
        remember_rule(td, "Bash(pytest:*)")
        remember_rule(td, "Write")
        remember_rule(td, "Bash(pytest:*)")  # twice on purpose
        data = json.loads((Path(td) / ".claude" / "settings.local.json").read_text())
        allow = data["permissions"]["allow"]
        check("rules persist for the next session", "Write" in allow)
        check("and are not duplicated", allow.count("Bash(pytest:*)") == 1)


def test_file_tree_lists_one_level_with_dirs_first():
    from descant import server

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "src").mkdir()
        (root / "src" / "deep").mkdir()
        (root / "node_modules").mkdir()
        (root / "zebra.py").write_text("x")
        (root / "alpha.md").write_text("y")

        listing = server.list_files(str(root))
        names = [e["name"] for e in listing["entries"]]
        check("directories come first", names[0] == "src")
        check("files follow, alphabetically", names[1:] == ["alpha.md", "zebra.py"])
        check("node_modules is not listed", "node_modules" not in names)
        check(
            "nothing below this level is walked",
            "deep" not in names and all(e["name"] != "deep" for e in listing["entries"]),
        )
        nested = server.list_files(str(root / "src"))
        check("expanding a directory lists its children", [e["name"] for e in nested["entries"]] == ["deep"])


def test_file_writes_cannot_escape_the_repo():
    """Every create/delete goes through one guard; it has to fail closed."""
    from fastapi import HTTPException

    from descant import server

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        root.mkdir()
        (Path(td) / "secret.txt").write_text("do not touch")

        for escape in ("../secret.txt", "/etc/passwd", "sub/../../secret.txt"):
            try:
                server.create_entry(server.CreateFileBody(root=str(root), path=escape))
            except HTTPException:
                pass
            else:
                check(f"{escape} refused", False)
        check("traversal and absolute escapes are refused", True)
        check("the file outside was untouched", (Path(td) / "secret.txt").read_text() == "do not touch")

        made = server.create_entry(
            server.CreateFileBody(root=str(root), path="src/main.py", content="print(1)\n")
        )
        check("nested creates make their parents", Path(made["path"]).is_file())
        check("any extension is allowed", made["name"] == "main.py")

        try:
            server.create_entry(server.CreateFileBody(root=str(root), path="src/main.py"))
        except HTTPException:
            check("creating over an existing file is refused", True)
        else:
            check("creating over an existing file is refused", False)

        try:
            server.delete_entry(server.DeleteFileBody(root=str(root), path="src"))
        except HTTPException:
            check("a non-empty folder is not deleted by accident", True)
        else:
            check("a non-empty folder is not deleted by accident", False)

        server.delete_entry(server.DeleteFileBody(root=str(root), path="src", recursive=True))
        check("recursive delete removes it", not (root / "src").exists())

        try:
            server.delete_entry(server.DeleteFileBody(root=str(root), path="."))
        except HTTPException:
            check("the repo itself cannot be deleted", True)
        else:
            check("the repo itself cannot be deleted", False)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\n{PASSED} checks passed across {len(tests)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
