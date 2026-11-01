import asyncio

from app.pipeline.chunking import Chunk
from app.static.findings import ToolFinding
from app.static.ruff_runner import run_ruff
from app.static.semgrep_runner import run_semgrep

# Files Ruff and our Semgrep rules understand.
PYTHON_SUFFIXES = (".py", ".pyi")
# Ruff processes at once, so a PR with many Python files doesn't spawn them all together.
MAX_PARALLEL_TOOLS = 4


async def collect_tool_findings(
    files: dict[str, str], added_lines: dict[str, set[int]], semgrep_enabled: bool
) -> dict[str, list[ToolFinding]]:
    """Run the tools on the reviewed Python files. Keeps only findings on lines this PR added:
    problems that were already there aren't the PR's doing."""
    python_files = {path: text for path, text in files.items() if path.endswith(PYTHON_SUFFIXES)}
    limit = asyncio.Semaphore(MAX_PARALLEL_TOOLS)

    async def ruff_one(path: str, text: str) -> list[ToolFinding]:
        async with limit:
            return await run_ruff(path, text)

    results = await asyncio.gather(*(ruff_one(path, text) for path, text in python_files.items()))
    by_file = dict(zip(python_files, results, strict=True))
    if semgrep_enabled:
        for path, findings in (await run_semgrep(python_files)).items():
            by_file.setdefault(path, []).extend(findings)

    return {
        path: [f for f in findings if f.line in added_lines.get(path, set())]
        for path, findings in by_file.items()
    }


def attach_hints(chunks: list[Chunk], findings_by_file: dict[str, list[ToolFinding]]) -> None:
    """Give each chunk the tool findings on its lines, grouped by the lane that should see them."""
    for chunk in chunks:
        for finding in findings_by_file.get(chunk.path, []):
            if chunk.start <= finding.line <= chunk.end:
                chunk.hints.setdefault(finding.lane, []).append(finding)
