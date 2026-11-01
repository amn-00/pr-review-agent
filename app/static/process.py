import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def run_tool(
    args: list[str],
    *,
    limit_seconds: float,
    ok_codes: tuple[int, ...],
    stdin: bytes | None = None,
    cwd: str | None = None,
) -> bytes | None:
    """Run a static-analysis tool and return its stdout, or None if it failed.

    Failures are logged, never raised: the tools are hints for the lanes, and a tool problem
    must never fail a review.
    """
    name = Path(args[0]).name
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except OSError as exc:
        logger.warning("could not start %s: %s", name, exc)
        return None
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), limit_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()
        logger.warning("%s timed out after %ss", name, limit_seconds)
        return None
    if process.returncode not in ok_codes:
        logger.warning("%s exited with %s: %s", name, process.returncode, stderr[:300])
        return None
    return stdout
