"""The agent's tools: read-only views of the repo at the PR's head SHA, and its CI results.

A tool never raises: bad arguments, missing files and GitHub errors come back as text for the
model. Every result is capped, because each step resends the whole conversation and Groq's
free tier allows 8K tokens per minute.
"""

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.agent.snapshot import Snapshot
from app.github.client import GitHubClient
from app.github.http import NonRetryableError, RetryableError

MAX_RESULT_CHARS = 2500
MAX_MATCHES = 15
MAX_READ_LINES = 80
MAX_LINE_CHARS = 200
MAX_QUERY_CHARS = 200
MAX_CHECK_TITLE_CHARS = 120
# Anchored: pydantic patterns are searched, not matched.
SYMBOL_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.]*$"


@dataclass
class ToolResult:
    text: str
    # (path, line) pairs shown to the model. Evidence must point at one of these.
    seen: set[tuple[str, int]] = field(default_factory=set)


class SearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)


class ReadFileArgs(BaseModel):
    # line_start/line_end, like the candidate finding the model is shown: with start/end, the
    # model kept writing line_start/line_end and Groq rejected the call (Phase 5 smoke run).
    path: str = Field(min_length=1)
    line_start: int
    line_end: int


class CallersArgs(BaseModel):
    symbol: str = Field(pattern=SYMBOL_PATTERN, max_length=100)


class Evidence(BaseModel):
    path: str
    line: int
    why: str


class VerdictArgs(BaseModel):
    verdict: Literal["confirmed", "dropped", "uncertain"]
    reason: str
    evidence: list[Evidence]


def _function(name: str, description: str, properties: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


VERDICT_TOOL = "submit_verdict"
TOOL_SCHEMAS = [
    _function(
        "search_repo",
        "Search every file of the repository for a piece of text (plain text, not a regex, "
        "case-sensitive). Returns up to 15 matching lines as path:line: text.",
        {"query": {"type": "string", "description": "The exact text to look for."}},
    ),
    _function(
        "read_file",
        "Read lines line_start..line_end (inclusive, at most 80) of a file in the repository, "
        "numbered.",
        {
            "path": {"type": "string", "description": "Path from the repository root."},
            "line_start": {"type": "integer"},
            "line_end": {"type": "integer"},
        },
    ),
    _function(
        "find_callers",
        "Find where a function, class or variable is used: lines containing the name as a "
        "whole word, excluding its own def/class line. Text-based, so it can miss dynamic "
        "calls and match unrelated names.",
        {"symbol": {"type": "string", "description": "e.g. average or Cart.total"}},
    ),
    _function(
        "get_ci_results",
        "The pull request's CI check runs (e.g. GitHub Actions jobs) for this commit: name, "
        "status and conclusion. CI may still be running.",
        {},
    ),
    _function(
        VERDICT_TOOL,
        "Finish: give your verdict on the candidate finding, with evidence.",
        {
            "verdict": {"type": "string", "enum": ["confirmed", "dropped", "uncertain"]},
            "reason": {"type": "string", "description": "One or two sentences."},
            "evidence": {
                "type": "array",
                "description": "Lines you read that support the verdict.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "line": {"type": "integer"},
                        "why": {"type": "string"},
                    },
                    "required": ["path", "line", "why"],
                    "additionalProperties": False,
                },
            },
        },
    ),
]


def normalize_path(path: str) -> str:
    """ "./app/x.py", "/app/x.py" and "app/x.py" all mean the same file."""
    return PurePosixPath(path.strip().lstrip("/")).as_posix()


class AgentTools:
    """The tools for one review attempt: the snapshot is shared by every finding's agent, and
    the CI results are fetched once."""

    def __init__(
        self, snapshot: Snapshot, github: GitHubClient, installation_id: int, repo: str, sha: str
    ):
        self._snapshot = snapshot
        self._github = github
        self._installation_id = installation_id
        self._repo = repo
        self._sha = sha
        self._ci: ToolResult | None = None

    async def run(self, name: str, arguments: str) -> ToolResult:
        """Run one tool call. `arguments` is the JSON string the model wrote."""
        try:
            if name == "search_repo":
                query = SearchArgs.model_validate_json(arguments).query
                return await asyncio.to_thread(search_repo, self._snapshot, query)
            if name == "read_file":
                args = ReadFileArgs.model_validate_json(arguments)
                return await asyncio.to_thread(
                    read_file, self._snapshot, args.path, args.line_start, args.line_end
                )
            if name == "find_callers":
                symbol = CallersArgs.model_validate_json(arguments).symbol
                return await asyncio.to_thread(find_callers, self._snapshot, symbol)
            if name == "get_ci_results":
                return await self._ci_results()
        except ValidationError as exc:
            return ToolResult(f"Invalid arguments for {name}: {_short_errors(exc)}")
        return ToolResult(f"Unknown tool: {name}")

    async def _ci_results(self) -> ToolResult:
        if self._ci is None:
            try:
                runs = await self._github.list_check_runs(
                    self._installation_id, self._repo, self._sha
                )
            except (RetryableError, NonRetryableError):
                return ToolResult("CI results are unavailable (GitHub returned an error).")
            self._ci = ToolResult(format_check_runs(runs))
        return self._ci


def format_check_runs(runs: list[dict]) -> str:
    if not runs:
        return "No CI check runs for this commit."
    lines = []
    for run in runs:
        title = ((run.get("output") or {}).get("title") or "")[:MAX_CHECK_TITLE_CHARS]
        lines.append(
            f"- {run['name']}: {run['status']}, conclusion {run.get('conclusion') or 'none yet'}"
            + (f" ({title})" if title else "")
        )
    return _cap("\n".join(lines))


def read_file(snapshot: Snapshot, path: str, start: int, end: int) -> ToolResult:
    if snapshot.root is None:
        return ToolResult(_unavailable(snapshot))
    relative = normalize_path(path)
    target = (snapshot.root / relative).resolve()
    if not target.is_relative_to(snapshot.root.resolve()) or not target.is_file():
        return ToolResult(f"No such file: {relative}")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(start, 1)
    end = min(end, start + MAX_READ_LINES - 1, len(lines))
    if start > end:
        return ToolResult(f"{relative} has {len(lines)} lines; nothing to show at {start}..")
    rows = [(relative, n, lines[n - 1]) for n in range(start, end + 1)]
    return _rows_result(f"{relative}, lines {start}-{end} of {len(lines)}:", rows, "{n:>5}  {text}")


def search_repo(snapshot: Snapshot, query: str) -> ToolResult:
    # Plain text, never a regex: a model-chosen pattern could take forever to match.
    return _grep(snapshot, lambda line: query in line, f"Lines containing {query!r}")


def find_callers(snapshot: Snapshot, symbol: str) -> ToolResult:
    name = symbol.rsplit(".", 1)[-1]
    word = re.compile(rf"\b{re.escape(name)}\b")
    definition = re.compile(rf"^\s*(async\s+def|def|class)\s+{re.escape(name)}\b")
    return _grep(
        snapshot,
        lambda line: bool(word.search(line)) and not definition.search(line),
        f"Uses of {name!r}",
    )


def _grep(snapshot: Snapshot, matches: Callable[[str], bool], header: str) -> ToolResult:
    if snapshot.root is None:
        return ToolResult(_unavailable(snapshot))
    rows: list[tuple[str, int, str]] = []
    total = 0
    for file in sorted(snapshot.root.rglob("*")):
        if not file.is_file():
            continue
        path = file.relative_to(snapshot.root).as_posix()
        text = file.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), start=1):
            if matches(line):
                total += 1
                if len(rows) < MAX_MATCHES:
                    rows.append((path, n, line.strip()))
    if not rows:
        return ToolResult(f"{header}: no matches." + _incomplete_note(snapshot))
    result = _rows_result(f"{header}:", rows, "{path}:{n}: {text}")
    if total > len(rows):
        result.text += f"\n... and {total - len(rows)} more"
    result.text += _incomplete_note(snapshot)
    return result


def _rows_result(header: str, rows: list[tuple[str, int, str]], row_format: str) -> ToolResult:
    """Lines until the result cap. Only lines actually shown count as seen."""
    result = ToolResult(header)
    for path, n, text in rows:
        row = row_format.format(path=path, n=n, text=text[:MAX_LINE_CHARS])
        if len(result.text) + len(row) + 1 > MAX_RESULT_CHARS:
            result.text += "\n... (truncated)"
            break
        result.text += "\n" + row
        result.seen.add((path, n))
    return result


def _cap(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[: MAX_RESULT_CHARS - 16] + "\n... (truncated)"


def _unavailable(snapshot: Snapshot) -> str:
    return (
        f"Repository files are unavailable: {snapshot.problem}. Use the excerpt in the task "
        "and get_ci_results."
    )


def _incomplete_note(snapshot: Snapshot) -> str:
    return "" if snapshot.complete else f"\n(Note: {snapshot.problem}.)"


def _short_errors(exc: ValidationError) -> str:
    errors = [f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors()]
    return "; ".join(errors)


def tool_call_key(name: str, arguments: str) -> str:
    """Same tool with the same arguments, however the JSON is spaced or ordered."""
    try:
        return name + json.dumps(json.loads(arguments), sort_keys=True)
    except ValueError:
        return name + arguments
