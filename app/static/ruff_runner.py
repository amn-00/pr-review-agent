"""Ruff on one file, read from stdin: no checkout needed."""

import json
import logging

from ruff import find_ruff_bin

from app.static.findings import ToolFinding
from app.static.process import run_tool

logger = logging.getLogger(__name__)

# Our rules only. --isolated means the repo's own Ruff config is never read.
RUFF_RULES = "F,B,UP,SIM"
RUFF_TIMEOUT_SECONDS = 10


def ruff_lane(code: str) -> str:
    """Pyflakes (F), bugbear (B) and syntax errors point at bugs; the rest is about style."""
    if code.startswith(("F", "B")) or code == "invalid-syntax":
        return "correctness"
    return "maintainability"


async def run_ruff(path: str, content: str) -> list[ToolFinding]:
    args = [
        find_ruff_bin(),
        "check",
        "--isolated",
        "--select",
        RUFF_RULES,
        "--output-format",
        "json",
        "--stdin-filename",
        path,
        "-",
    ]
    # Exit code 1 just means "found something".
    output = await run_tool(
        args, stdin=content.encode(), limit_seconds=RUFF_TIMEOUT_SECONDS, ok_codes=(0, 1)
    )
    if output is None:
        return []
    try:
        items = json.loads(output)
    except ValueError:
        logger.warning("ruff returned output that isn't JSON for %s", path)
        return []
    findings = []
    for item in items:
        code = item["code"] or "invalid-syntax"
        findings.append(
            ToolFinding("ruff", item["location"]["row"], code, item["message"], ruff_lane(code))
        )
    return findings
