"""Stands in for GitHubClient in evals: serves one PR built from a case's files, and keeps the
review the job posts instead of sending it. Only the methods the review job calls exist."""

import io
import tarfile
from difflib import unified_diff


def make_patch(old: str, new: str) -> str:
    """The unified diff GitHub returns as a file's `patch`: hunks with 3 lines of context,
    without the ---/+++ file header lines."""
    lines = unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=3)
    return "\n".join(list(lines)[2:])


def pull_files(base: dict[str, str], head: dict[str, str]) -> list[dict]:
    """GitHub's PR files list: every file that differs between base and head. Cases only add
    or change files, so there are no deletions."""
    return [
        {
            "filename": path,
            "status": "modified" if path in base else "added",
            "patch": make_patch(base.get(path, ""), head[path]),
        }
        for path in sorted(head)
        if base.get(path) != head[path]
    ]


def tarball(files: dict[str, str], top: str) -> bytes:
    """A gzipped tarball shaped like GitHub's: every file under one top folder."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, text in sorted(files.items()):
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeGitHub:
    def __init__(
        self, base: dict[str, str], head: dict[str, str], sha: str, check_runs: list[dict]
    ):
        self.base = base
        self.head = head
        self.sha = sha
        self.check_runs = check_runs
        self.reviews: list[dict] = []  # what the job posted: {"body", "comments"}

    async def get_pull(self, installation_id: int, repo: str, number: int) -> dict:
        return {"state": "open", "draft": False, "head": {"sha": self.sha}}

    async def list_pull_files(self, installation_id: int, repo: str, number: int) -> list[dict]:
        return pull_files(self.base, self.head)

    async def get_file_content(self, installation_id: int, repo: str, path: str, ref: str) -> str:
        return self.head[path]

    async def list_reviews(self, installation_id: int, repo: str, number: int) -> list[dict]:
        return []

    async def bot_login(self) -> str:
        return "eval[bot]"

    async def create_review(
        self,
        installation_id: int,
        repo: str,
        number: int,
        commit_id: str,
        body: str,
        comments: list[dict],
    ) -> dict:
        self.reviews.append({"body": body, "comments": comments})
        return {"id": len(self.reviews)}

    async def iter_tarball(self, installation_id: int, repo: str, ref: str):
        yield tarball(self.head, f"eval-{self.sha[:7]}")

    async def list_check_runs(self, installation_id: int, repo: str, sha: str) -> list[dict]:
        return self.check_runs
