"""The repo's files at the PR's head SHA, on disk, for the agent's read-only tools.

GitHub's code search only indexes the default branch, so we download a tarball of the head SHA
instead (see docs/decisions.md). Everything is streamed and capped, so a huge repo can't use up
memory (512 MB on the free tier) or disk.
"""

import asyncio
import logging
import tarfile
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

from app.github.client import GitHubClient
from app.github.http import NonRetryableError, RetryableError

logger = logging.getLogger(__name__)

MAX_TARBALL_BYTES = 50 * 1024 * 1024  # compressed
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_FILES = 20_000
BINARY_SNIFF_BYTES = 8192


@dataclass
class Snapshot:
    """`root` is None when the files couldn't be downloaded; `complete` is False when a limit
    cut the extraction short, so some files are missing."""

    root: Path | None
    complete: bool = True
    problem: str = ""  # why root is None or the snapshot is incomplete, for the model


async def download_snapshot(
    github: GitHubClient, installation_id: int, repo: str, sha: str, folder: Path
) -> Snapshot:
    """Download and extract the repo into `folder` (which the caller deletes). Never raises
    for GitHub or archive problems: the agent then works without repo files."""
    archive = folder / "repo.tar.gz"
    root = folder / "repo"
    try:
        if not await _download(github, installation_id, repo, sha, archive):
            return Snapshot(None, problem="the repository is too large to download")
        complete = await asyncio.to_thread(extract, archive, root)
    except (RetryableError, NonRetryableError, tarfile.TarError, OSError) as exc:
        logger.warning("could not download the repository snapshot: %s", exc)
        return Snapshot(None, problem="the repository could not be downloaded")
    finally:
        await asyncio.to_thread(archive.unlink, missing_ok=True)
    if not complete:
        problem = "the repository is too large; some files are missing"
        return Snapshot(root, complete=False, problem=problem)
    return Snapshot(root)


async def _download(
    github: GitHubClient, installation_id: int, repo: str, sha: str, archive: Path
) -> bool:
    """Stream the tarball to `archive`. False if it's bigger than MAX_TARBALL_BYTES."""
    size = 0
    file = await asyncio.to_thread(archive.open, "wb")
    try:
        async with aclosing(github.iter_tarball(installation_id, repo, sha)) as pieces:
            async for piece in pieces:
                size += len(piece)
                if size > MAX_TARBALL_BYTES:
                    return False
                await asyncio.to_thread(file.write, piece)
    finally:
        await asyncio.to_thread(file.close)
    return True


def extract(archive: Path, root: Path) -> bool:
    """Extract text files from the tarball into `root`. Returns False if a limit was hit.

    Never tarfile.extractall: each member is checked, and only regular files are written, by
    us, at a path that must stay inside `root`. Links, devices and binaries are skipped.
    """
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    files = total = 0
    # "r|gz" reads the archive as a stream, one member at a time.
    with tarfile.open(archive, mode="r|gz") as tar:
        for member in tar:
            if not member.isfile() or member.size > MAX_FILE_BYTES:
                continue
            # GitHub puts everything under one top folder, "owner-repo-sha/".
            _, _, relative = member.name.partition("/")
            target = (root / relative).resolve()
            if not relative or not target.is_relative_to(root) or target == root:
                continue
            if files >= MAX_FILES or total + member.size > MAX_TOTAL_BYTES:
                return False
            data = tar.extractfile(member).read()
            if b"\0" in data[:BINARY_SNIFF_BYTES]:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files += 1
            total += len(data)
    return True
