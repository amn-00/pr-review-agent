"""Semgrep with our own rules, behind SEMGREP_ENABLED (off by default: it needs a few hundred MB
of memory, and the free tier has 512 MB).

Semgrep is installed as a separate tool, not a project dependency: current releases pin library
versions that conflict with FastAPI's (see docs/decisions.md). We only ever run its CLI.
"""

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path, PurePath

from app.static.findings import ToolFinding
from app.static.process import run_tool

logger = logging.getLogger(__name__)

RULES_FILE = Path(__file__).with_name("semgrep_rules.yml")
SEMGREP_TIMEOUT_SECONDS = 120


def _write_files(folder: str, files: dict[str, str]) -> None:
    root = Path(folder).resolve()
    for path, content in files.items():
        target = (root / path).resolve()
        if not target.is_relative_to(root):  # never write outside the temp folder
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


async def run_semgrep(files: dict[str, str]) -> dict[str, list[ToolFinding]]:
    """Scan the files (path -> content) in one run. Returns findings per path."""
    binary = shutil.which("semgrep")
    if binary is None:
        logger.warning("SEMGREP_ENABLED is set but the semgrep command isn't installed")
        return {}

    with tempfile.TemporaryDirectory() as tmp:
        await asyncio.to_thread(_write_files, tmp, files)
        # Only the PR's Python files are in the folder, so there's no config of the PR's for
        # semgrep to pick up.
        args = [
            binary,
            "scan",
            "--config",
            str(RULES_FILE),
            "--json",
            "--metrics=off",
            "--disable-version-check",
            "--quiet",
            ".",
        ]
        output = await run_tool(
            args, limit_seconds=SEMGREP_TIMEOUT_SECONDS, ok_codes=(0, 1), cwd=tmp
        )
    if output is None:
        return {}

    try:
        results = json.loads(output)["results"]
    except (ValueError, KeyError):
        logger.warning("semgrep returned output we couldn't read")
        return {}
    by_path: dict[str, list[ToolFinding]] = {}
    for result in results:
        path = PurePath(result["path"]).as_posix().removeprefix("./")
        rule = result["check_id"].rsplit(".", 1)[-1]  # drop the rules-file prefix
        message = result["extra"]["message"]
        finding = ToolFinding("semgrep", result["start"]["line"], rule, message, "security")
        by_path.setdefault(path, []).append(finding)
    return by_path
