#!/usr/bin/env python3
"""Generate synthetic Claude Code transcripts in the real stream-json format.

The sandbox this was built in has no access to a real ~/.claude/projects, so
these fixtures stand in for it.  They mirror the on-disk format exactly (see
backend/descant/transcript.py for the format notes), including the parts that
are easy to get wrong:

* several assistant lines per API request, all repeating one ``requestId`` and
  one ``usage`` block
* ``attachment`` lines for injected context
* a ``last-prompt`` line, ``queue-operation`` noise, and a sidechain subagent
* usage numbers that grow consistently with the conversation, so the inspector's
  measured-vs-estimated reconciliation is meaningful rather than random

Run:  python3 fixtures/generate.py            (writes fixtures/projects/)
"""

from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / "projects"
VERSION = "2.1.14"
MODEL = "claude-opus-5"

# The preamble a real session pays before anyone types anything: system prompt,
# env block, CLAUDE.md, tool schemas.  Fixed per fixture so the inspector's
# measured-by-difference maths lands on a sensible number.
PREAMBLE_TOKENS = 14_200

rng = random.Random(20260820)


def enc(path: str) -> str:
    """Claude Code's project-directory encoding: separators become dashes."""
    return path.replace("/", "-").replace(".", "-").replace("_", "-")


def est(text: str, dense: bool = False) -> int:
    return max(1, round(len(text) / (3.1 if dense else 3.8))) + 5


class Builder:
    def __init__(self, cwd: str, branch: str, started: datetime):
        self.cwd = cwd
        self.branch = branch
        self.t = started
        self.session_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        self.lines: list[dict] = []
        self.parent: str | None = None
        self.context = PREAMBLE_TOKENS
        self.pending = 0  # tokens added since the last API request
        self.request_id: str | None = None
        self.first_prompt = ""

    # -- plumbing --------------------------------------------------------

    def _tick(self, secs: float = 0.0) -> str:
        self.t += timedelta(seconds=secs or rng.uniform(0.6, 9.0))
        return self.t.isoformat().replace("+00:00", "Z")

    def _base(self, **kw) -> dict:
        u = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        d = {
            "parentUuid": self.parent,
            "isSidechain": kw.pop("sidechain", False),
            "type": kw.pop("type"),
            "uuid": u,
            "timestamp": kw.pop("ts", None) or self._tick(),
            "userType": "external",
            "entrypoint": "cli",
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "version": VERSION,
            "gitBranch": self.branch,
        }
        d.update(kw)
        self.parent = u
        self.lines.append(d)
        return d

    # -- content ---------------------------------------------------------

    def user(self, text: str):
        if not self.first_prompt:
            self.first_prompt = text
        self.pending += est(text)
        self._base(
            type="user",
            promptId=f"prompt_{rng.getrandbits(32):08x}",
            message={"role": "user", "content": text},
            permissionMode="default",
            origin={"kind": "cli"},
            promptSource="user",
        )

    def attachment(self, atype: str, text: str):
        self.pending += est(text, dense=True)
        self._base(type="attachment", attachment={"type": atype, "text": text})

    def queue_noise(self):
        self.lines.append(
            {
                "type": "queue-operation",
                "operation": "clear",
                "timestamp": self._tick(0.1),
                "sessionId": self.session_id,
            }
        )

    def _usage(self) -> dict:
        """Roll the context forward by whatever accumulated since last request."""
        prev = self.context
        self.context += self.pending
        creation = self.pending
        self.pending = 0
        out = rng.randint(180, 900)
        return {
            "input_tokens": rng.choice([1, 2, 3]),
            "cache_creation_input_tokens": creation if prev != PREAMBLE_TOKENS else self.context,
            "cache_read_input_tokens": prev if prev != PREAMBLE_TOKENS else 0,
            "output_tokens": out,
            "output_tokens_details": {"thinking_tokens": rng.choice([0, 0, 45, 210, 480])},
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            "service_tier": "standard",
            "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": creation},
            "inference_geo": "us",
        }

    def begin_request(self):
        self.request_id = f"req_{rng.getrandbits(48):012x}"
        self._current_usage = self._usage()

    def _assistant_line(self, block: dict):
        self.pending += est(json.dumps(block), dense=block.get("type") != "text")
        self._base(
            type="assistant",
            requestId=self.request_id,
            effort="high",
            message={
                "model": MODEL,
                "id": f"msg_{rng.getrandbits(48):012x}",
                "type": "message",
                "role": "assistant",
                "content": [block],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": self._current_usage,
            },
        )

    def thinking(self, text: str):
        self._assistant_line({"type": "thinking", "thinking": text, "signature": "fixture"})

    def say(self, text: str):
        self._assistant_line({"type": "text", "text": text})

    def tool(self, name: str, tool_input: dict, result: str, is_error: bool = False):
        tid = f"toolu_{rng.getrandbits(48):012x}"
        self._assistant_line({"type": "tool_use", "id": tid, "name": name, "input": tool_input})
        self.pending += est(result, dense=True)
        self._base(
            type="user",
            promptId=f"prompt_{rng.getrandbits(32):08x}",
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": result,
                        "is_error": is_error,
                    }
                ],
            },
            toolUseResult={"stdout": result, "stderr": "", "interrupted": False, "isImage": False},
        )

    def _reconcile_output_tokens(self) -> None:
        """Make each request's ``output_tokens`` match what it actually emitted.

        The inspector treats output tokens as ground truth for the
        assistant-side rows, so a fixture with invented output counts would make
        its own report look wrong.  Assistant lines within a request share one
        usage object, so setting it once per request is enough.
        """
        by_request: dict[str, list[dict]] = {}
        for line in self.lines:
            if line.get("type") != "assistant":
                continue
            by_request.setdefault(line["requestId"], []).append(line)

        for lines in by_request.values():
            total = 0
            thinking = 0
            for line in lines:
                for block in line["message"]["content"]:
                    n = est(json.dumps(block), dense=block.get("type") != "text")
                    total += n
                    if block.get("type") == "thinking":
                        thinking += n
            usage = lines[0]["message"]["usage"]  # shared object
            usage["output_tokens"] = total
            usage["output_tokens_details"]["thinking_tokens"] = thinking

    def finish(self) -> str:
        self._reconcile_output_tokens()
        self.lines.append(
            {
                "type": "last-prompt",
                "lastPrompt": self.first_prompt[:200],
                "leafUuid": self.parent,
                "sessionId": self.session_id,
            }
        )
        d = OUT / enc(self.cwd)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{self.session_id}.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for line in self.lines:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        return str(path)


# --------------------------------------------------------------------------
# canned content
# --------------------------------------------------------------------------

SYS_REMINDER = (
    "<system-reminder>Your todo list is currently empty. Do not mention this to "
    "the user explicitly; they are aware. If you are working on tasks that would "
    "benefit from a todo list please use the TodoWrite tool to create one.</system-reminder>"
)

SKILL_LISTING = (
    "The following skills are available:\n"
    + "\n".join(
        f"- {n}: {d}"
        for n, d in [
            ("pdf", "Read, create, merge, split and fill PDF files."),
            ("xlsx", "Read and write spreadsheets; clean messy tabular data."),
            ("docx", "Create and edit Word documents and templates."),
            ("code-review", "Review the current diff for correctness bugs."),
            ("security-review", "Complete a security review of pending changes."),
            ("skill-creator", "Create, modify and evaluate skills."),
        ]
    )
)

FILE_BLOB = """     1\timport { createServer } from 'node:http'
     2\timport { Router } from './router'
     3\timport { withAuth } from './middleware/auth'
     4\timport { metrics } from './telemetry'
     5\t
     6\tconst router = new Router()
     7\t
     8\trouter.get('/healthz', (_req, res) => {
     9\t  res.writeHead(200, { 'content-type': 'application/json' })
    10\t  res.end(JSON.stringify({ ok: true }))
    11\t})
    12\t
    13\trouter.post('/v1/ingest', withAuth(async (req, res) => {
    14\t  const body = await readJson(req)
    15\t  const t0 = performance.now()
    16\t  const result = await pipeline.submit(body)
    17\t  metrics.observe('ingest.latency', performance.now() - t0)
    18\t  res.writeHead(202)
    19\t  res.end(JSON.stringify({ id: result.id }))
    20\t}))
"""

TEST_OUTPUT = """
> harbor@0.4.1 test
> vitest run --reporter=basic

 ✓ src/router.test.ts (12 tests) 41ms
 ✓ src/middleware/auth.test.ts (8 tests) 22ms
 ❯ src/ingest.test.ts (6 tests | 1 failed) 118ms
   × rejects oversize payloads
     → expected 413 but got 202

 Test Files  1 failed | 2 passed (3)
      Tests  1 failed | 25 passed (26)
"""


def make_harbor_auth() -> Builder:
    b = Builder("/Users/ayan/code/harbor", "feat/rate-limit", datetime(2026, 8, 18, 9, 12, tzinfo=timezone.utc))
    b.user(
        "The /v1/ingest endpoint accepts payloads of any size and we're getting OOMs in "
        "staging. Add a size limit that returns 413, and make sure the existing tests "
        "still pass."
    )
    b.attachment("skill_listing", SKILL_LISTING)
    b.attachment("system_reminder", SYS_REMINDER)
    b.begin_request()
    b.thinking(
        "I should find the ingest handler first, then look at how readJson buffers the "
        "body. A limit has to be enforced during read, not after."
    )
    b.say("Let me look at the ingest path and how the body is read.")
    b.tool("Read", {"file_path": "/Users/ayan/code/harbor/src/server.ts"}, FILE_BLOB)
    b.tool(
        "Grep",
        {"pattern": "readJson", "path": "src", "output_mode": "content", "-n": True},
        "src/server.ts:14:  const body = await readJson(req)\n"
        "src/util/body.ts:7:export async function readJson(req: IncomingMessage) {\n"
        "src/util/body.ts:9:  for await (const chunk of req) chunks.push(chunk)\n",
    )
    b.begin_request()
    b.thinking("body.ts buffers unconditionally. The limit belongs there.")
    b.say("`readJson` buffers the whole stream with no ceiling. I'll add a limit there and surface it as a 413.")
    b.tool(
        "Edit",
        {
            "file_path": "/Users/ayan/code/harbor/src/util/body.ts",
            "old_string": "  for await (const chunk of req) chunks.push(chunk)",
            "new_string": (
                "  let seen = 0\n"
                "  for await (const chunk of req) {\n"
                "    seen += chunk.length\n"
                "    if (seen > MAX_BODY_BYTES) throw new PayloadTooLarge(seen)\n"
                "    chunks.push(chunk)\n"
                "  }"
            ),
        },
        "The file /Users/ayan/code/harbor/src/util/body.ts has been updated.",
    )
    b.tool("Bash", {"command": "npm test", "description": "Run the test suite"}, TEST_OUTPUT)
    b.attachment("system_reminder", SYS_REMINDER)
    b.begin_request()
    b.say(
        "One failure, and it's the assertion I want: `rejects oversize payloads` expects 413 "
        "but the handler still returns 202 because the error isn't mapped yet. Wiring that up."
    )
    b.tool(
        "Edit",
        {
            "file_path": "/Users/ayan/code/harbor/src/server.ts",
            "old_string": "  const body = await readJson(req)",
            "new_string": (
                "  let body\n"
                "  try { body = await readJson(req) }\n"
                "  catch (e) { if (e instanceof PayloadTooLarge) return send(res, 413, { error: 'payload too large' }); throw e }"
            ),
        },
        "The file /Users/ayan/code/harbor/src/server.ts has been updated.",
    )
    b.tool(
        "Bash",
        {"command": "npm test", "description": "Re-run the test suite"},
        TEST_OUTPUT.replace("1 failed | 2 passed (3)", "3 passed (3)")
        .replace("1 failed | 25 passed (26)", "26 passed (26)")
        .replace("   × rejects oversize payloads\n     → expected 413 but got 202\n", "")
        .replace("❯ src/ingest.test.ts (6 tests | 1 failed)", "✓ src/ingest.test.ts (6 tests)"),
    )
    b.begin_request()
    b.say(
        "Green — 26 passing. `readJson` now stops at `MAX_BODY_BYTES` (1 MiB) and the handler "
        "maps `PayloadTooLarge` to a 413 instead of letting it bubble into a 500."
    )
    return b


def make_harbor_flaky() -> Builder:
    b = Builder("/Users/ayan/code/harbor", "main", datetime(2026, 8, 19, 16, 40, tzinfo=timezone.utc))
    b.user("CI has been flaky on the auth tests for two days. Figure out why.")
    b.attachment("system_reminder", SYS_REMINDER)
    b.begin_request()
    b.say("I'll start by looking at what the auth tests actually assert.")
    b.tool(
        "Bash",
        {"command": "npx vitest run src/middleware/auth.test.ts --repeat 20", "description": "Stress the auth tests"},
        "\n".join(
            f" {'✓' if i not in (7, 13) else '×'} run {i + 1}/20"
            + ("" if i not in (7, 13) else "  → token expired mid-assertion")
            for i in range(20)
        )
        + "\n\n 2 failed | 18 passed (20)",
    )
    b.begin_request()
    b.thinking("Two failures out of twenty, both about expiry. Smells like a real clock in the test.")
    b.tool(
        "Grep",
        {"pattern": "Date\\.now|new Date\\(\\)", "path": "src/middleware", "output_mode": "content"},
        "src/middleware/auth.ts:22:  const now = Date.now()\n"
        "src/middleware/auth.test.ts:15:  const token = sign({ exp: Date.now() + 1000 })\n",
    )
    b.say(
        "Found it. The test mints a token with a 1-second expiry against the real clock, so any "
        "run where the suite stalls past a second fails. That's a test bug, not an auth bug — "
        "it needs fake timers."
    )
    return b


def make_tidepool_backfill() -> Builder:
    b = Builder("/Users/ayan/code/tidepool", "backfill/2026-q2", datetime(2026, 8, 19, 11, 5, tzinfo=timezone.utc))
    b.user(
        "I need to backfill the events table for Q2. Write a script that pages through S3, "
        "dedupes on event_id, and writes parquet. It has to be resumable — the last run died "
        "at 60% and I had to start over."
    )
    b.attachment("skill_listing", SKILL_LISTING)
    b.attachment("system_reminder", SYS_REMINDER)
    b.begin_request()
    b.thinking(
        "Resumability is the real requirement. A checkpoint file keyed on the S3 prefix "
        "that's already been consumed is the simplest thing that survives a crash."
    )
    b.say("The resumability requirement drives the design. Let me see what's already in the repo.")
    b.tool(
        "Glob",
        {"pattern": "**/*.py", "path": "/Users/ayan/code/tidepool"},
        "/Users/ayan/code/tidepool/tidepool/__init__.py\n"
        "/Users/ayan/code/tidepool/tidepool/io.py\n"
        "/Users/ayan/code/tidepool/tidepool/schema.py\n"
        "/Users/ayan/code/tidepool/scripts/daily_rollup.py\n"
        "/Users/ayan/code/tidepool/tests/test_io.py\n",
    )
    b.tool(
        "Read",
        {"file_path": "/Users/ayan/code/tidepool/tidepool/io.py"},
        "     1\timport boto3\n     2\timport pyarrow.parquet as pq\n     3\t\n"
        "     4\tdef iter_keys(bucket, prefix):\n"
        "     5\t    paginator = s3.get_paginator('list_objects_v2')\n"
        "     6\t    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):\n"
        "     7\t        for obj in page.get('Contents', []):\n"
        "     8\t            yield obj['Key']\n",
    )
    b.begin_request()
    b.say("`iter_keys` already pages correctly, so I can build the backfill on top of it.")
    b.tool(
        "Write",
        {
            "file_path": "/Users/ayan/code/tidepool/scripts/backfill_events.py",
            "content": (
                "import json, pathlib\n"
                "from tidepool.io import iter_keys, read_ndjson, write_parquet\n\n"
                "CHECKPOINT = pathlib.Path('.tidepool/backfill.json')\n\n"
                "def load_checkpoint():\n"
                "    if CHECKPOINT.exists():\n"
                "        return set(json.loads(CHECKPOINT.read_text())['done'])\n"
                "    return set()\n"
            ),
        },
        "File created successfully at /Users/ayan/code/tidepool/scripts/backfill_events.py",
    )
    b.tool(
        "Bash",
        {"command": "python -m scripts.backfill_events --dry-run --quarter 2026Q2", "description": "Dry run"},
        "scanning s3://tidepool-raw/events/2026/04..06\n"
        "  found 8,412 objects (191.4 GiB)\n"
        "  checkpoint: 0 done, 8412 pending\n"
        "  estimated 41 min at observed throughput\n"
        "dry run - nothing written",
    )
    b.attachment("system_reminder", SYS_REMINDER)
    b.begin_request()
    b.say(
        "Dry run looks right: 8,412 objects, ~41 minutes. The checkpoint is written after each "
        "prefix completes, so a crash resumes from the last finished prefix instead of zero."
    )
    return b


def make_tidepool_schema() -> Builder:
    b = Builder("/Users/ayan/code/tidepool", "main", datetime(2026, 8, 20, 6, 30, tzinfo=timezone.utc))
    b.user("Why does test_io fail only on Python 3.13?")
    b.begin_request()
    b.tool(
        "Bash",
        {"command": "python3.13 -m pytest tests/test_io.py -x", "description": "Reproduce on 3.13"},
        "tests/test_io.py::test_roundtrip FAILED\n\n"
        "E   TypeError: 'datetime.datetime' object cannot be interpreted as an integer\n"
        "tidepool/schema.py:44: in coerce\n"
        "    return int(value)\n\n"
        "1 failed in 0.44s",
        is_error=True,
    )
    b.begin_request()
    b.thinking("3.13 tightened int() on datetime subclasses. The coercion was always wrong; 3.12 was lenient.")
    b.say(
        "`schema.coerce` calls `int()` on a datetime. 3.12 tolerated it through a deprecated path "
        "that 3.13 removed — so the bug was always there, 3.13 just stopped hiding it."
    )
    return b


def make_descant_spike() -> Builder:
    b = Builder("/Users/ayan/projects/descant", "spike/transcript-parser", datetime(2026, 8, 20, 8, 15, tzinfo=timezone.utc))
    b.user("Sketch out how you'd parse the Claude Code transcript format. Don't write it yet, just think it through.")
    b.attachment("system_reminder", SYS_REMINDER)
    b.queue_noise()
    b.begin_request()
    b.thinking(
        "The format is line-delimited JSON with heterogeneous line types. The trap is that one "
        "API request produces several assistant lines that all repeat the same usage block."
    )
    b.say(
        "The shape is one JSON object per line with a `type` discriminator. Three things make it "
        "trickier than it looks:\n\n"
        "1. `message.content` is sometimes a bare string and sometimes a block list.\n"
        "2. Assistant lines repeat `usage` per request — dedupe on `requestId` or you triple-count.\n"
        "3. `attachment` lines are injected context nobody typed, and they are not cheap."
    )
    b.tool(
        "Bash",
        {"command": "ls ~/.claude/projects | head", "description": "List project dirs"},
        "-Users-ayan-code-harbor\n-Users-ayan-code-tidepool\n-Users-ayan-projects-descant\n",
    )
    return b


def make_descant_stalled() -> Builder:
    """A session whose last event is a tool error — the UI shows this as 'needs input'."""
    b = Builder("/Users/ayan/projects/descant", "main", datetime(2026, 8, 20, 7, 55, tzinfo=timezone.utc))
    b.user("Bump the electron dependency and see if the app still boots.")
    b.begin_request()
    b.tool(
        "Bash",
        {"command": "npm install electron@latest", "description": "Upgrade electron"},
        "npm error code EACCES\nnpm error syscall mkdir\nnpm error path /usr/local/lib/node_modules\n"
        "npm error errno -13\nnpm error Error: EACCES: permission denied",
        is_error=True,
    )
    b.begin_request()
    b.say(
        "That needs elevated permissions I don't have, and `sudo npm install -g` would change "
        "something outside this repo — I'd rather you make that call. Want me to switch the "
        "project to a local prefix instead?"
    )
    return b


def make_harbor_migration() -> Builder:
    """A long, expensive session.

    The interesting cases for a context inspector are the big ones -- a session
    that has been running for hours and is closing in on its window. Without one
    of these in the fixtures the tool looks like it has nothing to say.
    """
    b = Builder(
        "/Users/ayan/code/harbor", "chore/ts-strict", datetime(2026, 8, 19, 13, 20, tzinfo=timezone.utc)
    )
    b.user(
        "Turn on TypeScript strict mode across the whole repo and fix every error it "
        "surfaces. Work file by file, run the typechecker as you go, and don't change "
        "runtime behaviour."
    )
    b.attachment("skill_listing", SKILL_LISTING)
    b.attachment("system_reminder", SYS_REMINDER)
    b.begin_request()
    b.thinking(
        "Whole-repo strict mode is a long grind. The right order is: flip the flag, get "
        "the full error list, then fix by directory so the count only ever goes down."
    )
    b.say("Flipping the flag first to see the true size of this.")
    b.tool(
        "Edit",
        {
            "file_path": "/Users/ayan/code/harbor/tsconfig.json",
            "old_string": '"strict": false',
            "new_string": '"strict": true',
        },
        "The file /Users/ayan/code/harbor/tsconfig.json has been updated.",
    )

    modules = [
        "router", "middleware/auth", "middleware/cors", "util/body", "util/time",
        "telemetry", "ingest", "storage/pg", "storage/redis", "queue/worker",
        "queue/scheduler", "config", "errors", "server",
    ]
    remaining = 214
    for i, mod in enumerate(modules):
        errs = max(2, remaining // (len(modules) - i))
        remaining -= errs
        b.begin_request()
        if i % 4 == 0:
            b.thinking(
                f"{remaining} errors left after {mod}. Most are implicit-any on callback "
                "params, which are mechanical. The nullable ones need real thought."
            )
        b.tool(
            "Read",
            {"file_path": f"/Users/ayan/code/harbor/src/{mod}.ts"},
            "\n".join(
                f"{n + 1:6}\t{line}"
                for n, line in enumerate(
                    (FILE_BLOB.split("\n") * 3)[: 30 + (i * 7) % 40]
                )
            ),
        )
        b.tool(
            "Edit",
            {
                "file_path": f"/Users/ayan/code/harbor/src/{mod}.ts",
                "old_string": f"export function handle{i}(req, res) {{",
                "new_string": f"export function handle{i}(req: Request, res: Response): void {{",
            },
            f"The file /Users/ayan/code/harbor/src/{mod}.ts has been updated.",
        )
        b.tool(
            "Bash",
            {"command": "npx tsc --noEmit", "description": f"Typecheck after {mod}"},
            f"src/{mod}.ts:12:18 - error TS7006: Parameter 'req' implicitly has an 'any' type.\n"
            * min(6, errs)
            + f"\nFound {remaining} errors in {max(1, len(modules) - i - 1)} files.",
            is_error=remaining > 0,
        )
        if i % 3 == 2:
            b.attachment("system_reminder", SYS_REMINDER)

    b.begin_request()
    b.tool(
        "Bash",
        {"command": "npx tsc --noEmit && npm test", "description": "Final check"},
        "Found 0 errors.\n\n Test Files  12 passed (12)\n      Tests  184 passed (184)",
    )
    b.say(
        "Strict mode is on and the repo typechecks clean — 214 errors down to zero across "
        "14 modules, with all 184 tests still passing. The bulk were implicit-any callback "
        "params; the ones worth reviewing are in `storage/pg.ts`, where four columns were "
        "typed non-null but the schema allows NULL. I widened those types rather than "
        "asserting, so nothing changes at runtime."
    )
    return b


BUILDERS = [
    make_harbor_migration,
    make_harbor_auth,
    make_harbor_flaky,
    make_tidepool_backfill,
    make_tidepool_schema,
    make_descant_spike,
    make_descant_stalled,
]


def main() -> None:
    if OUT.exists():
        for d in OUT.iterdir():
            if d.is_dir():
                for f in d.glob("*.jsonl"):
                    f.unlink()
    for fn in BUILDERS:
        path = fn().finish()
        print("wrote", path)


if __name__ == "__main__":
    main()
