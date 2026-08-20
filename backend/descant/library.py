"""A searchable skill library across every project.

The idea behind on-demand tool discovery: instead of declaring every skill and
MCP server up front and paying for all of them on every turn, keep one index and
*find* the right tool when a task needs it. The index is the cheap part; the
expensive part is what you stop loading.

**On the word "semantic".** This is lexical retrieval — BM25 over name,
description and body, with field boosting and a small hand-built synonym map —
not embeddings. That is a deliberate, and slightly reluctant, choice: embeddings
would need either a network call per query or a local model, and this sandbox has
neither. The honest framing is that it behaves semantically for the cases a
synonym map covers ("test" finding "pytest", "docs" finding "documentation") and
lexically everywhere else. Swapping in a real embedding backend means replacing
:func:`score_query` and nothing else — the index, the API and the UI are all
agnostic to how a query turns into scores.

BM25 is implemented directly rather than pulled in, both to keep the backend
dependency-free and because the corpus here is small enough that the ranking
quality difference is noise.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import mcp, skills

# BM25 tuning. k1 controls term-frequency saturation, b the length penalty.
K1 = 1.4
B = 0.72

# Field weights: a match in the name is worth far more than one in the body.
W_NAME = 6.0
W_DESCRIPTION = 3.0
W_BODY = 1.0

_WORD = re.compile(r"[a-z0-9]+")

# Deliberately broad. A query is a sentence ("turn a csv into a chart"), so
# without this the filler words match everything and drown the real signal --
# "into" was scoring hits on half the corpus before it was added here.
_STOP = {
    "a", "an", "and", "any", "are", "as", "at", "be", "been", "both", "but", "by",
    "can", "could", "do", "does", "each", "for", "from", "get", "give", "has",
    "have", "how", "if", "in", "into", "is", "it", "its", "just", "make", "makes",
    "many", "may", "me", "might", "more", "most", "my", "need", "needs", "new",
    "not", "of", "on", "one", "only", "or", "other", "out", "over", "own",
    "please", "put", "same", "set", "should", "so", "some", "such", "take",
    "than", "that", "the", "their", "them", "then", "there", "these", "they",
    "this", "those", "to", "turn", "under", "up", "us", "use", "used", "using",
    "very", "want", "wants", "was", "way", "we", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "would", "you", "your",
}

# The "semantic" layer, such as it is: query terms expand to related terms so a
# search for "test" finds a skill that only ever says "pytest". Small and
# hand-built on purpose — it is inspectable, which an embedding is not.
_SYNONYMS = {
    "test": ["pytest", "vitest", "jest", "spec", "unittest", "testing", "suite"],
    "lint": ["eslint", "ruff", "flake8", "clippy", "format", "prettier", "style"],
    "docs": ["documentation", "readme", "doc", "guide", "manual"],
    "deploy": ["release", "ship", "publish", "rollout", "deployment"],
    "db": ["database", "sql", "postgres", "mysql", "sqlite", "query"],
    "spreadsheet": ["xlsx", "excel", "csv", "sheet", "tabular"],
    "document": ["docx", "word", "pdf", "report"],
    "slides": ["pptx", "powerpoint", "deck", "presentation"],
    "weather": ["forecast", "temperature", "climate", "meteorology"],
    "commit": ["git", "vcs", "branch", "diff", "stage"],
    "review": ["audit", "inspect", "critique", "feedback"],
    "search": ["find", "grep", "lookup", "query"],
    "build": ["compile", "bundle", "make", "webpack"],
    "chart": ["graph", "plot", "visualisation", "visualization", "dataviz"],
}
# Expansion works both ways: searching "pytest" should also match "test".
_REVERSE: dict[str, list[str]] = {}
for _root, _kids in _SYNONYMS.items():
    for _k in _kids:
        _REVERSE.setdefault(_k, []).append(_root)


def tokenize(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1]


def expand(terms: list[str]) -> Counter:
    """Query terms plus related terms at reduced weight."""
    weighted: Counter = Counter()
    for t in terms:
        weighted[t] += 1.0
        for syn in _SYNONYMS.get(t, []):
            weighted[syn] += 0.55
        for root in _REVERSE.get(t, []):
            weighted[root] += 0.55
            for sibling in _SYNONYMS.get(root, []):
                if sibling != t:
                    weighted[sibling] += 0.3
    return weighted


@dataclass
class Entry:
    """One searchable capability: a skill, or a tool on an MCP server."""

    key: str
    kind: str  # "skill" | "mcp_tool"
    name: str
    description: str
    source: str
    repo_path: str = ""
    path: str = ""
    server: str = ""
    always_on_tokens: int = 0
    body: str = ""
    name_terms: Counter = field(default_factory=Counter)
    desc_terms: Counter = field(default_factory=Counter)
    body_terms: Counter = field(default_factory=Counter)

    @property
    def length(self) -> float:
        return sum(self.name_terms.values()) * W_NAME + sum(
            self.desc_terms.values()
        ) * W_DESCRIPTION + sum(self.body_terms.values()) * W_BODY

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "repo_path": self.repo_path,
            "path": self.path,
            "server": self.server,
            "always_on_tokens": self.always_on_tokens,
        }


class Library:
    """An in-memory BM25 index over every skill and MCP tool we can see."""

    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self._df: Counter = Counter()
        self._avg_len: float = 1.0

    # -- building --------------------------------------------------------

    def add_skill(self, skill: skills.Skill, repo_path: str = "") -> None:
        body = ""
        try:
            body = Path(skill.path).read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            pass
        self.entries.append(
            Entry(
                key=f"skill:{skill.source}:{skill.name}",
                kind="skill",
                name=skill.name,
                description=skill.description,
                source=skill.source,
                repo_path=repo_path,
                path=str(skill.path),
                always_on_tokens=skill.listing_tokens,
                body=body,
            )
        )

    def add_mcp_tool(self, server: mcp.McpServer, tool: mcp.McpTool, repo_path: str) -> None:
        self.entries.append(
            Entry(
                key=f"mcp:{server.name}:{tool.name}",
                kind="mcp_tool",
                name=tool.name,
                description=tool.description,
                source=server.source,
                repo_path=repo_path,
                server=server.name,
                always_on_tokens=tool.token_cost,
            )
        )

    def finalize(self) -> "Library":
        self._df = Counter()
        total_len = 0.0
        for e in self.entries:
            e.name_terms = Counter(tokenize(e.name.replace("_", " ").replace("-", " ")))
            e.desc_terms = Counter(tokenize(e.description))
            e.body_terms = Counter(tokenize(e.body))
            e.body = ""  # drop the raw text once indexed
            seen = set(e.name_terms) | set(e.desc_terms) | set(e.body_terms)
            for t in seen:
                self._df[t] += 1
            total_len += e.length
        self._avg_len = (total_len / len(self.entries)) if self.entries else 1.0
        return self

    # -- querying --------------------------------------------------------

    def _idf(self, term: str) -> float:
        n = len(self.entries)
        df = self._df.get(term, 0)
        # BM25's probabilistic idf, floored so a term in every document still
        # contributes a little rather than going negative.
        return max(0.05, math.log(1 + (n - df + 0.5) / (df + 0.5)))

    def score_query(self, query: str, limit: int = 20) -> list[dict]:
        terms = expand(tokenize(query))
        if not terms:
            return []
        results = []
        for e in self.entries:
            score = 0.0
            matched: list[str] = []
            for term, weight in terms.items():
                tf = (
                    e.name_terms.get(term, 0) * W_NAME
                    + e.desc_terms.get(term, 0) * W_DESCRIPTION
                    + e.body_terms.get(term, 0) * W_BODY
                )
                if not tf:
                    continue
                norm = 1 - B + B * (e.length / self._avg_len if self._avg_len else 1)
                score += weight * self._idf(term) * (tf * (K1 + 1)) / (tf + K1 * norm)
                if e.name_terms.get(term) or e.desc_terms.get(term):
                    matched.append(term)
            if score > 0:
                results.append({**e.to_dict(), "score": round(score, 3), "matched": matched[:6]})
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]


async def build(repo_paths: list[str], probe_mcp: bool = True) -> Library:
    """Index every skill and MCP tool across the given repos."""
    lib = Library()
    seen_skills: set[str] = set()

    for repo in repo_paths:
        for s in skills.discover(repo):
            key = f"{s.source}:{s.name}"
            if key in seen_skills:
                continue
            seen_skills.add(key)
            lib.add_skill(s, repo_path=repo if s.source == "project" else "")
        if probe_mcp:
            for server in await mcp.discover_and_probe(repo, do_probe=True):
                for tool in server.tools:
                    lib.add_mcp_tool(server, tool, repo)
    return lib.finalize()


def stats(lib: Library) -> dict:
    skills_n = sum(1 for e in lib.entries if e.kind == "skill")
    tools_n = sum(1 for e in lib.entries if e.kind == "mcp_tool")
    return {
        "entry_count": len(lib.entries),
        "skill_count": skills_n,
        "mcp_tool_count": tools_n,
        "indexed_tokens": sum(e.always_on_tokens for e in lib.entries),
        "retrieval": "bm25+synonyms",
    }
