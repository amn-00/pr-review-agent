"""Static-analysis results, as hints for the lanes (never posted directly)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolFinding:
    tool: str  # "ruff" or "semgrep"
    line: int  # new-file line number
    code: str  # rule ID, e.g. "F821" or "subprocess-shell-true"
    message: str
    lane: str  # the lane that sees it as a hint
