"""Eval cases: a small repo before and after a PR, and labels saying what a review should find.

    evals/cases/<id>/
      case.toml   the labels (format below)
      base/       the repo before the PR
      head/       the files the PR changes or adds, laid over base/
      push2/      optional: files a second push changes, laid over head/ (for the cache metric)

case.toml:

    kind = "bug"              # bug | clean | trap | injection
    title = "Add average order value to the customer report"

    [[expect]]                # a bug the review should report (bug and injection cases)
    file = "shop/stats.py"
    lines = [7, 7]            # first and last line, new-file numbering; any overlap counts
    note = "division by zero when the customer has no paid orders"
    categories = ["bug"]      # optional; default bug, performance, security
    also_at = ["shop/report.py:7", "shop/report.py:9-10"]
                              # optional: other places where reporting it counts (a call site)

    [[trap]]                  # looks wrong but isn't: a finding here is a false positive
    [[acceptable]]            # real but not planted: neither a catch nor a false positive
                              # (same keys; categories default to all; add
                              # added_after_run = true when added after seeing model output)

    [[check_run]]             # optional CI results the agent can read
    name = "tests"
    status = "completed"
    conclusion = "failure"
    title = "1 failed"        # optional
"""

import hashlib
import tomllib
import zlib
from dataclasses import dataclass
from pathlib import Path

from app.github.diff import parse_patch
from evals.fake_github import pull_files

CASES_DIR = Path(__file__).parent / "cases"
KINDS = ("bug", "clean", "trap", "injection")
CATEGORIES = ("bug", "performance", "security", "readability", "idiom")
EXPECT_CATEGORIES = ("bug", "performance", "security")
LABEL_KEYS = {"file", "lines", "note", "categories", "added_after_run", "also_at"}
CASE_KEYS = {"kind", "title", "expect", "trap", "acceptable", "check_run"}
CHECK_RUN_KEYS = {"name", "status", "conclusion", "title"}


@dataclass
class Label:
    file: str
    lines: tuple[int, int]
    note: str
    categories: tuple[str, ...]
    added_after_run: bool = False
    also_at: tuple[tuple[str, tuple[int, int]], ...] = ()  # other (file, lines) that count

    @property
    def places(self) -> list[tuple[str, tuple[int, int]]]:
        return [(self.file, self.lines), *self.also_at]

    def matches(self, finding: dict) -> bool:
        """Same file, overlapping lines (at any of its places), and an allowed category."""
        return finding["category"] in self.categories and any(
            finding["file"] == file
            and finding["line_start"] <= last
            and first <= finding["line_end"]
            for file, (first, last) in self.places
        )


@dataclass
class Case:
    id: str
    kind: str
    title: str
    base: dict[str, str]  # path -> text
    head: dict[str, str]  # the whole repo at the PR's head
    push2: dict[str, str] | None  # the whole repo after the second push
    expect: list[Label]
    traps: list[Label]
    acceptable: list[Label]
    check_runs: list[dict]  # in GitHub's shape

    @property
    def repo_id(self) -> int:
        """One repo ID per case, so the lane cache never answers one case with another's."""
        return zlib.crc32(self.id.encode())

    def target(self, mode: str) -> tuple[dict[str, str], str]:
        """The repo tree and commit SHA a run reviews: push2 for "repush", else the head."""
        tree = self.push2 if mode == "repush" else self.head
        return tree, tree_sha(self.base, tree)


def tree_sha(base: dict[str, str], head: dict[str, str]) -> str:
    """A stand-in commit SHA: a hash of both trees, so editing a case gives a new SHA (and
    makes its saved results stale)."""
    digest = hashlib.sha1()
    for name, tree in (("base", base), ("head", head)):
        for path in sorted(tree):
            digest.update(f"{name}\0{path}\0{tree[path]}\0".encode())
    return digest.hexdigest()


def load_cases(cases_dir: Path = CASES_DIR) -> list[Case]:
    return [load_case(folder) for folder in sorted(cases_dir.iterdir()) if folder.is_dir()]


def load_case(folder: Path) -> Case:
    """Raises ValueError for a case.toml with unknown or missing keys."""
    data = tomllib.loads((folder / "case.toml").read_text(encoding="utf-8"))
    _check_keys(data, CASE_KEYS, {"kind", "title"}, folder.name)
    base = _read_tree(folder / "base")
    head = {**base, **_read_tree(folder / "head")}
    push2 = {**head, **_read_tree(folder / "push2")} if (folder / "push2").is_dir() else None
    return Case(
        id=folder.name,
        kind=data["kind"],
        title=data["title"],
        base=base,
        head=head,
        push2=push2,
        expect=[_label(raw, EXPECT_CATEGORIES, folder.name) for raw in data.get("expect", [])],
        traps=[_label(raw, CATEGORIES, folder.name) for raw in data.get("trap", [])],
        acceptable=[_label(raw, CATEGORIES, folder.name) for raw in data.get("acceptable", [])],
        check_runs=[_check_run(raw, folder.name) for raw in data.get("check_run", [])],
    )


def validate(case: Case) -> list[str]:
    """Problems that would make the case unfair or unscorable; empty if it's fine."""
    problems = []
    if case.kind not in KINDS:
        problems.append(f"kind must be one of {KINDS}, not {case.kind!r}")
    if case.kind == "clean" and case.expect:
        problems.append("a clean case has no [[expect]]")
    if case.kind in ("bug", "injection") and not case.expect:
        problems.append(f"a {case.kind} case needs at least one [[expect]]")
    if case.kind == "trap" and not case.traps:
        problems.append("a trap case needs at least one [[trap]]")

    # A finding can only be posted on lines inside a diff hunk, so a label elsewhere could
    # never be matched.
    hunks = {f["filename"]: parse_patch(f["patch"]) for f in pull_files(case.base, case.head)}
    for label in case.expect + case.traps + case.acceptable:
        bad = set(label.categories) - set(CATEGORIES)
        if bad:
            problems.append(f"{label.file}: unknown categories {sorted(bad)}")
        for file, (first, last) in label.places:
            where = f"{file}:{first}-{last}"
            if file not in case.head:
                problems.append(f"{where}: no such file at head")
                continue
            if not 1 <= first <= last <= len(case.head[file].splitlines()):
                problems.append(f"{where}: lines out of range")
            if not any(h.start <= last and first <= h.end for h in hunks.get(file, [])):
                problems.append(f"{where}: not inside a diff hunk, so it can never be matched")
    return problems


def _read_tree(folder: Path) -> dict[str, str]:
    if not folder.is_dir():
        return {}
    # read_text turns Windows line endings (git's autocrlf) back into "\n".
    return {
        path.relative_to(folder).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def _label(raw: dict, default_categories: tuple[str, ...], case_id: str) -> Label:
    _check_keys(raw, LABEL_KEYS, {"file", "lines", "note"}, case_id)
    first, last = raw["lines"]
    return Label(
        file=raw["file"],
        lines=(first, last),
        note=raw["note"],
        categories=tuple(raw.get("categories", default_categories)),
        added_after_run=raw.get("added_after_run", False),
        also_at=tuple(_place(text, case_id) for text in raw.get("also_at", [])),
    )


def _place(text: str, case_id: str) -> tuple[str, tuple[int, int]]:
    """ "shop/report.py:7" or "shop/report.py:7-9" -> ("shop/report.py", (7, 9))."""
    file, _, lines = text.rpartition(":")
    first, _, last = lines.partition("-")
    if not file or not first.isdigit() or not (last or first).isdigit():
        raise ValueError(f"{case_id}/case.toml: also_at entry {text!r} isn't path:line[-line]")
    return file, (int(first), int(last or first))


def _check_run(raw: dict, case_id: str) -> dict:
    _check_keys(raw, CHECK_RUN_KEYS, {"name", "status"}, case_id)
    return {
        "name": raw["name"],
        "status": raw["status"],
        "conclusion": raw.get("conclusion"),
        "output": {"title": raw.get("title")},
    }


def _check_keys(raw: dict, allowed: set[str], required: set[str], case_id: str) -> None:
    unknown = set(raw) - allowed
    missing = required - set(raw)
    if unknown or missing:
        raise ValueError(
            f"{case_id}/case.toml: unknown keys {sorted(unknown)}, missing keys {sorted(missing)}"
        )
