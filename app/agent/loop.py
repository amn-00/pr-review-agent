"""The verification agent: a hand-written tool-calling loop that checks one candidate finding.

Groq can't combine strict structured output with tool calling, so the verdict is itself a tool
(submit_verdict). Every step must call a tool, and the last allowed step is forced to call
submit_verdict, so the answer is never guessed from prose. If that forced step brings no
verdict, there is one more forced call, then one strict-JSON call without tools: at most
MAX_AGENT_STEPS + 2 calls (see docs/decisions.md).
"""

import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from pydantic import ValidationError

from app.agent.tools import (
    MAX_RESULT_CHARS,
    TOOL_SCHEMAS,
    VERDICT_TOOL,
    AgentTools,
    ToolResult,
    VerdictArgs,
    normalize_path,
    tool_call_key,
)
from app.llm.groq import (
    EXPECTED_TOOL_CALL_OUTPUT_TOKENS,
    DeadlineExceeded,
    GroqClient,
    LLMError,
    ToolCallFailed,
    estimate_tool_call_tokens,
)
from app.pipeline.chunking import Chunk
from app.pipeline.lanes import Candidate

# v2: no callers found is not evidence of safety; "dropped" needs a guard. v1 is kept for
# comparison (the Phase 5 baseline ran on it).
AGENT_PROMPT_VERSION = "v2"
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts" / "agent"

MAX_AGENT_STEPS = 6
FINDING_TOKEN_LIMIT = 12_000
# Working time per finding: LLM calls and tools, not waits for Groq's rate limits (those come
# from the shared quota, not from this finding). Past it, the next step is the forced verdict.
# The agent's overall deadline still counts waits, and is the hard limit.
FINDING_TIME_LIMIT_SECONDS = 120
RESULT_SUMMARY_CHARS = 300
# Lines of the chunk shown around the finding. A whole chunk can be ~2000 tokens, resent on
# every step; the tools can read more when needed.
EXCERPT_CONTEXT_LINES = 20
FORCE_VERDICT = {"type": "function", "function": {"name": VERDICT_TOOL}}
# The last-resort verdict call answers with JSON in submit_verdict's shape (strict mode).
VERDICT_SCHEMA = next(
    t["function"]["parameters"] for t in TOOL_SCHEMAS if t["function"]["name"] == VERDICT_TOOL
)
# agent_steps.tool for that call, which has no tools.
VERDICT_JSON = "verdict_json"
ASK_FOR_JSON_VERDICT = (
    "Give your verdict now as JSON with verdict, reason and evidence, exactly as you would "
    "pass them to submit_verdict. Evidence must point at lines shown above."
)
# How much one more step adds to the next request, at most: a full tool result plus the call.
STEP_GROWTH_TOKENS = MAX_RESULT_CHARS // 4 + EXPECTED_TOOL_CALL_OUTPUT_TOKENS
NOT_RUN = "Not run: one tool call per step."
TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]
# Summary prefix of a verdict the model sent under another tool name (see misnamed_call).
CALLED_AS = "called as"


@dataclass
class AgentStep:
    """One LLM call and the tool it called. Field names match the agent_steps columns."""

    step_no: int
    tool: str | None  # None when the call failed or called no tool
    args_json: dict | None
    result_chars: int
    result_summary: str
    tokens_in: int
    tokens_out: int
    latency_ms: int


@dataclass
class Verdict:
    verdict: str  # confirmed | dropped | uncertain
    reason: str
    evidence: list[dict] = field(default_factory=list)
    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


@cache
def system_prompt() -> str:
    return (PROMPTS_DIR / f"{AGENT_PROMPT_VERSION}.md").read_text(encoding="utf-8")


def excerpt(candidate: Candidate, chunk: Chunk) -> tuple[int, int, str]:
    """The finding's lines plus EXCERPT_CONTEXT_LINES around them, from the chunk's numbered
    text (one line per file line). Returns the first and last line shown, and the text."""
    first = max(chunk.start, candidate.line_start - EXCERPT_CONTEXT_LINES)
    last = min(chunk.end, candidate.line_end + EXCERPT_CONTEXT_LINES)
    lines = chunk.text.splitlines()[first - chunk.start : last - chunk.start + 1]
    return first, last, "\n".join(lines)


def task_message(candidate: Candidate, chunk: Chunk, boundary: str) -> str:
    """The finding and its excerpt, inside one untrusted block: the finding's text comes from a
    model that read the PR, so it can repeat planted text."""
    lines = f"line {candidate.line_start}"
    if candidate.line_end != candidate.line_start:
        lines = f"lines {candidate.line_start}-{candidate.line_end}"
    return (
        "Verify this candidate finding. Numbers on the left of the excerpt are new-file line "
        'numbers; "+" marks lines added or changed in this pull request.\n\n'
        f"BEGIN UNTRUSTED PR CONTENT {boundary}\n"
        f"Finding: {candidate.severity} {candidate.category} in {candidate.file}, {lines}\n"
        f"Title: {candidate.title}\n"
        f"Explanation: {candidate.explanation}\n\n"
        f"Excerpt of {chunk.path}:\n"
        f"{excerpt(candidate, chunk)[2]}\n"
        f"END UNTRUSTED PR CONTENT {boundary}"
    )


def wrap_result(text: str) -> str:
    """A new random ID per result, so file content can't fake the end marker."""
    boundary = secrets.token_hex(8)
    return f"BEGIN UNTRUSTED TOOL RESULT {boundary}\n{text}\nEND UNTRUSTED TOOL RESULT {boundary}"


async def verify_finding(
    groq: GroqClient,
    model: str,
    tools: AgentTools,
    candidate: Candidate,
    chunk: Chunk,
    deadline: float,
    on_step: Callable[[AgentStep], Awaitable[None]],
    clock: Callable[[], float] = time.monotonic,
) -> Verdict:
    """Check one candidate. `deadline` is the agent's overall deadline, waits included.
    `on_step` is awaited after every LLM call (to store the step and its usage). Raises
    NonRetryableError for a rejected API key; anything else ends as a verdict."""
    began = clock()
    waited = 0.0  # seconds Groq spent waiting on rate limits and backoff
    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": task_message(candidate, chunk, secrets.token_hex(8))},
    ]
    first, last, _ = excerpt(candidate, chunk)
    seen = {(chunk.path, n) for n in range(first, last + 1)}
    already_called: set[str] = set()
    result = Verdict("uncertain", f"no verdict within {MAX_AGENT_STEPS} steps")

    async def step_done(step_no, started, tool=None, arguments=None, text="", tokens=(0, 0)):
        result.steps = step_no
        result.tokens_in += tokens[0]
        result.tokens_out += tokens[1]
        args = _args(arguments) if arguments is not None else None
        summary = text[:RESULT_SUMMARY_CHARS]
        latency_ms = round((clock() - started) * 1000)
        await on_step(AgentStep(step_no, tool, args, len(text), summary, *tokens, latency_ms))

    def done(verdict: Verdict) -> Verdict:
        verdict.steps = result.steps
        verdict.tokens_in, verdict.tokens_out = result.tokens_in, result.tokens_out
        return verdict

    def fits(estimate: int) -> bool:
        return result.tokens_in + result.tokens_out + estimate <= FINDING_TOKEN_LIMIT

    async def failed_call(exc: ToolCallFailed, step_no: int, started: float) -> Verdict | None:
        """A call Groq rejected. If the model sent a verdict under a tool name that doesn't
        exist ("json", "commentary": 23 of 158 baseline steps), it's used as submit_verdict,
        evidence rules included. Otherwise the step is lost; for an unknown tool name, the
        model is told the real ones."""
        misnamed = misnamed_call(exc.failed_generation)
        if misnamed:
            name, arguments = misnamed
            verdict, _ = _parse_verdict(arguments, seen)
            if verdict is not None:
                text = f"{CALLED_AS} {name!r}: {verdict.verdict}: {verdict.reason}"
                await step_done(step_no, started, VERDICT_TOOL, arguments, text)
                return verdict
            messages.append({"role": "user", "content": unknown_tool_message(name)})
        await step_done(step_no, started, text=str(exc))
        return None

    async def forced_retry() -> Verdict | None:
        """One more call forced to submit_verdict. None if it brings no valid verdict."""
        step_no, started = result.steps + 1, clock()
        try:
            completion = await groq.complete_tools(
                model, messages, TOOL_SCHEMAS, FORCE_VERDICT, deadline
            )
        except ToolCallFailed as exc:  # e.g. the model called another tool again
            return await failed_call(exc, step_no, started)
        tokens = (completion.tokens_in, completion.tokens_out)
        calls = completion.message.get("tool_calls") or []
        name = calls[0]["function"]["name"] if calls else None
        if name != VERDICT_TOOL:
            await step_done(step_no, started, name, text="no verdict", tokens=tokens)
            return None
        arguments = calls[0]["function"]["arguments"] or "{}"
        verdict, error = _parse_verdict(arguments, seen)
        text = f"{verdict.verdict}: {verdict.reason}" if verdict else error
        await step_done(step_no, started, name, arguments, text, tokens)
        return verdict

    async def json_verdict(plain: list[dict]) -> Verdict | None:
        """Last resort: the conversation as plain text, answered in strict JSON. There are no
        tools, so the model can't call one instead. The same evidence rules apply."""
        step_no, started = result.steps + 1, clock()
        completion = await groq.complete_json(model, plain, VERDICT_TOOL, VERDICT_SCHEMA, deadline)
        tokens = (completion.tokens_in, completion.tokens_out)
        verdict, error = _parse_verdict(completion.content, seen)
        text = f"{verdict.verdict}: {verdict.reason}" if verdict else error
        await step_done(step_no, started, VERDICT_JSON, completion.content, text, tokens)
        return verdict

    for step_no in range(1, MAX_AGENT_STEPS + 1):
        estimate = estimate_tool_call_tokens(messages, TOOL_SCHEMAS)
        used = result.tokens_in + result.tokens_out
        if not fits(estimate):
            result.reason = "token limit"
            return result
        # Force the verdict on the last step, while there's still room for it (the call after
        # this one would not fit the token limit), or once the working time is used up.
        last_chance = (
            step_no == MAX_AGENT_STEPS
            or used + 2 * estimate + STEP_GROWTH_TOKENS > FINDING_TOKEN_LIMIT
            or clock() - began - waited > FINDING_TIME_LIMIT_SECONDS
        )
        tool_choice = FORCE_VERDICT if last_chance else "required"
        started = clock()
        try:
            completion = await groq.complete_tools(
                model, messages, TOOL_SCHEMAS, tool_choice, deadline
            )
        except ToolCallFailed as exc:  # a malformed tool call: this step is lost, ask again
            verdict = await failed_call(exc, step_no, started)
            if verdict is not None:
                return done(verdict)
            if last_chance:
                break  # the forced verdict didn't come: recover below
            continue
        except DeadlineExceeded:
            result.reason = "time limit"
            return result
        except LLMError as exc:
            result.reason = f"agent error: {str(exc)[:200]}"
            return result

        waited += completion.waited_seconds
        tokens = (completion.tokens_in, completion.tokens_out)
        content = completion.message.get("content")
        calls = completion.message.get("tool_calls") or []
        if not calls:
            # `required` should prevent this; nudge the model, and the step still counts.
            messages.append({"role": "assistant", "content": content or ""})
            messages.append({"role": "user", "content": "Call one of the tools."})
            await step_done(step_no, started, text="no tool call", tokens=tokens)
            if last_chance:
                break
            continue

        messages.append({"role": "assistant", "content": content, "tool_calls": calls})
        call, extra_calls = calls[0], calls[1:]
        name = call["function"]["name"]
        arguments = call["function"]["arguments"] or "{}"

        if name == VERDICT_TOOL:
            verdict, error = _parse_verdict(arguments, seen)
            if verdict is not None:
                text = f"{verdict.verdict}: {verdict.reason}"
                await step_done(step_no, started, name, arguments, text, tokens)
                return done(verdict)
            tool_result = ToolResult(error)
        elif tool_call_key(name, arguments) in already_called:
            tool_result = ToolResult("Already called with these arguments; the result is above.")
        else:
            already_called.add(tool_call_key(name, arguments))
            tool_result = await tools.run(name, arguments)
            seen |= tool_result.seen

        messages.append(
            {"role": "tool", "tool_call_id": call["id"], "content": wrap_result(tool_result.text)}
        )
        # Every tool call needs an answer, even the ones we don't run.
        for extra in extra_calls:
            messages.append({"role": "tool", "tool_call_id": extra["id"], "content": NOT_RUN})
        await step_done(step_no, started, name, arguments, tool_result.text, tokens)
        if last_chance:
            break  # a forced step that brought no valid verdict

    # The forced verdict didn't come. Groq rejects a call to another tool when one is forced,
    # and the model sometimes makes it anyway (Phase 5 smoke run). Ask once more, forcing it;
    # then once for plain JSON, where no tool can be called instead.
    try:
        if not fits(estimate_tool_call_tokens(messages, TOOL_SCHEMAS)):
            result.reason = "token limit"
            return result
        verdict = await forced_retry()
        if verdict is not None:
            return done(verdict)
        plain = _plain_messages(messages)
        if not fits(estimate_tool_call_tokens(plain, [])):
            result.reason = "token limit"
            return result
        verdict = await json_verdict(plain)
        if verdict is not None:
            return done(verdict)
    except DeadlineExceeded:
        result.reason = "time limit"
        return result
    except LLMError as exc:
        result.reason = f"agent error: {str(exc)[:200]}"
        return result
    result.reason = "no verdict, even when forced and asked for JSON"
    return result


def _parse_verdict(arguments: str, seen: set[tuple[str, int]]) -> tuple[Verdict | None, str]:
    """A verdict, or the error to send back so the model can fix its call.

    Evidence must point at lines the agent was shown. A confirmed or dropped verdict with no
    such evidence becomes uncertain: that catches invented evidence, and means an injection
    that wants a real finding dropped has to point at real code.
    """
    try:
        args = VerdictArgs.model_validate_json(arguments)
    except ValidationError as exc:
        first_error = exc.errors()[0]
        where = ".".join(map(str, first_error["loc"])) or "arguments"
        return None, f"Invalid {VERDICT_TOOL} arguments: {where}: {first_error['msg']}"
    evidence = [
        {"path": normalize_path(e.path), "line": e.line, "why": e.why}
        for e in args.evidence
        if (normalize_path(e.path), e.line) in seen
    ]
    if args.verdict != "uncertain" and not evidence:
        reason = f"{args.verdict} without evidence from code it read: {args.reason}"
        return Verdict("uncertain", reason), ""
    return Verdict(args.verdict, args.reason, evidence), ""


def misnamed_call(failed_generation: str | None) -> tuple[str, str] | None:
    """(name, arguments as JSON) of a rejected call to a tool that doesn't exist, from Groq's
    failed_generation, e.g. {"name": "json", "arguments": {...}}. None for anything else: no
    generation, not a call, or a call to one of our tools (that failed for another reason)."""
    if not failed_generation:
        return None
    try:
        call = json.loads(failed_generation)
    except ValueError:
        return None
    if not isinstance(call, dict):
        return None
    name, arguments = call.get("name"), call.get("arguments")
    if not isinstance(name, str) or name in TOOL_NAMES:
        return None
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments)
    if not isinstance(arguments, str):
        return None
    return name, arguments


def unknown_tool_message(name: str) -> str:
    return (
        f"There is no tool named {name[:50]!r}. The tools are: {', '.join(TOOL_NAMES)}. "
        f"Give your verdict only with {VERDICT_TOOL}."
    )


def _plain_messages(messages: list[dict]) -> list[dict]:
    """The conversation for a call without tools: each tool call becomes a line of assistant
    text, and each tool result (still wrapped as untrusted) a user message."""
    plain = []
    for message in messages:
        if message["role"] == "tool":
            plain.append({"role": "user", "content": message["content"]})
        elif message["role"] == "assistant":
            lines = [message.get("content") or ""]
            for call in message.get("tool_calls") or []:
                function = call["function"]
                lines.append(f"Called {function['name']}({function['arguments']})")
            plain.append({"role": "assistant", "content": "\n".join(lines).strip()})
        else:
            plain.append(message)
    plain.append({"role": "user", "content": ASK_FOR_JSON_VERDICT})
    return plain


def _args(arguments: str) -> dict:
    """The call's arguments for args_json. Kept even when they don't parse, for debugging."""
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return {"unparsed": arguments[:1000]}
    return parsed if isinstance(parsed, dict) else {"value": parsed}
