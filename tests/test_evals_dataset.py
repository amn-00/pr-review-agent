"""The eval dataset and the fake GitHub that serves it (no LLM, no database)."""

import pytest

from app.agent.snapshot import download_snapshot
from app.github.diff import parse_patch
from evals.dataset import Case, Label, load_case, load_cases, validate
from evals.fake_github import FakeGitHub, make_patch, pull_files


def write_case(folder, toml: str, base: dict, head: dict, push2: dict | None = None) -> None:
    folder.mkdir()
    (folder / "case.toml").write_text(toml, encoding="utf-8")
    for part, files in (("base", base), ("head", head), ("push2", push2 or {})):
        for path, text in files.items():
            target = folder / part / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")


BUG_TOML = """kind = "bug"
title = "t"

[[expect]]
file = "app/calc.py"
lines = [3, 3]
note = "the bug"
"""


def test_every_case_in_the_dataset_is_valid():
    cases = load_cases()

    assert len(cases) >= 20
    assert {case.id: validate(case) for case in cases} == {case.id: [] for case in cases}
    assert len({case.repo_id for case in cases}) == len(cases)  # no shared lane cache
    assert {case.kind for case in cases} == {"bug", "clean", "trap", "injection"}


def test_patch_of_a_modified_file_parses_to_the_added_lines():
    old = "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n"
    new = "a\nB\nc\nd\ne\nf\ng\nh\ni\nj\nk\n"  # line 2 changed, line 11 added

    hunks = parse_patch(make_patch(old, new))

    assert set().union(*(h.added for h in hunks)) == {2, 11}


def test_patch_of_a_new_file_covers_every_line():
    [hunk] = parse_patch(make_patch("", "x = 1\ny = 2\n"))

    assert (hunk.start, hunk.end, hunk.added) == (1, 2, {1, 2})


def test_patch_of_a_one_line_file():
    [hunk] = parse_patch(make_patch("x = 1\n", "x = 2\n"))

    assert (hunk.start, hunk.end, hunk.added) == (1, 1, {1})


def test_pull_files_lists_only_changed_files():
    files = pull_files(
        {"same.py": "x\n", "old.py": "a\n"}, {"same.py": "x\n", "old.py": "b\n", "new.py": "c\n"}
    )

    assert [(f["filename"], f["status"]) for f in files] == [
        ("new.py", "added"),
        ("old.py", "modified"),
    ]


def test_head_overlays_base_and_push2_overlays_head(tmp_path):
    base = {"app/calc.py": "v1\n", "app/other.py": "o\n"}
    write_case(
        tmp_path / "c", BUG_TOML, base, {"app/calc.py": "1\n2\n3\n"}, {"app/other.py": "p\n"}
    )

    case = load_case(tmp_path / "c")

    assert case.head == {"app/calc.py": "1\n2\n3\n", "app/other.py": "o\n"}
    assert case.push2 == {"app/calc.py": "1\n2\n3\n", "app/other.py": "p\n"}
    # Each commit gets its own stand-in SHA; the head's changes whenever a case file does.
    assert case.target("pipeline")[1] != case.target("repush")[1]
    case.head["app/calc.py"] = "1\n2\nchanged\n"
    assert case.target("pipeline")[1] != load_case(tmp_path / "c").target("pipeline")[1]


def test_typo_in_case_toml_is_an_error(tmp_path):
    toml = BUG_TOML.replace("lines = [3, 3]", "line = [3, 3]")
    write_case(tmp_path / "c", toml, {}, {"app/calc.py": "1\n2\n3\n"})

    with pytest.raises(ValueError, match=r"unknown keys \['line'\], missing keys \['lines'\]"):
        load_case(tmp_path / "c")


def case_with(labels: list[Label], kind: str = "bug") -> Case:
    base = {"app/calc.py": "1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n"}
    head = {"app/calc.py": base["app/calc.py"].replace("12\n", "twelve\n")}  # hunk: lines 9-12
    return Case("c", kind, "t", base, head, None, labels, [], [], [])


def label(first: int, last: int, file: str = "app/calc.py") -> Label:
    return Label(file, (first, last), "n", ("bug",))


def test_labels_must_be_on_lines_a_comment_can_go_on():
    assert validate(case_with([label(12, 12)])) == []
    assert validate(case_with([label(2, 3)])) == [
        "app/calc.py:2-3: not inside a diff hunk, so it can never be matched"
    ]
    assert validate(case_with([label(12, 13)])) == ["app/calc.py:12-13: lines out of range"]
    assert validate(case_with([label(1, 1, file="nope.py")])) == [
        "nope.py:1-1: no such file at head"
    ]


def test_kind_rules():
    assert validate(case_with([label(12, 12)], kind="clean")) == ["a clean case has no [[expect]]"]
    assert validate(case_with([], kind="bug")) == ["a bug case needs at least one [[expect]]"]
    assert validate(case_with([], kind="trap")) == ["a trap case needs at least one [[trap]]"]


def test_label_matching():
    bug = Label("a.py", (10, 12), "n", ("bug",))

    def finding(start, end, category="bug", file="a.py"):
        return {"file": file, "line_start": start, "line_end": end, "category": category}

    assert bug.matches(finding(12, 14))
    assert bug.matches(finding(8, 10))
    assert not bug.matches(finding(13, 13))  # no overlap
    assert not bug.matches(finding(11, 11, category="readability"))
    assert not bug.matches(finding(11, 11, file="b.py"))

    # Reported at a call site listed in also_at: still the same bug.
    bug.also_at = (("b.py", (5, 5)),)
    assert bug.matches(finding(5, 5, file="b.py"))
    assert not bug.matches(finding(6, 6, file="b.py"))


def test_also_at_places_are_parsed_and_validated(tmp_path):
    toml = BUG_TOML + 'also_at = ["app/calc.py:1", "app/calc.py:2-3"]\n'
    write_case(tmp_path / "c", toml, {"app/calc.py": "a\n"}, {"app/calc.py": "1\n2\n3\n"})

    case = load_case(tmp_path / "c")

    assert case.expect[0].also_at == (("app/calc.py", (1, 1)), ("app/calc.py", (2, 3)))
    assert validate(case) == []
    outside = Label("app/calc.py", (12, 12), "n", ("bug",), also_at=(("app/calc.py", (1, 1)),))
    assert validate(case_with([outside])) == [
        "app/calc.py:1-1: not inside a diff hunk, so it can never be matched"
    ]


def test_malformed_also_at_is_an_error(tmp_path):
    toml = BUG_TOML + 'also_at = ["app/calc.py"]\n'
    write_case(tmp_path / "c", toml, {}, {"app/calc.py": "1\n2\n3\n"})

    with pytest.raises(ValueError, match="isn't path:line"):
        load_case(tmp_path / "c")


async def test_fake_github_serves_the_head_as_a_repo_snapshot(tmp_path):
    github = FakeGitHub({"a.py": "old\n"}, {"a.py": "new\n", "pkg/b.py": "b\n"}, "f" * 40, [])

    snapshot = await download_snapshot(github, 1, "evals/c", "f" * 40, tmp_path)

    assert snapshot.complete
    assert (snapshot.root / "a.py").read_text() == "new\n"
    assert (snapshot.root / "pkg" / "b.py").read_text() == "b\n"
