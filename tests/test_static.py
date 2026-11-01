import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from app.pipeline.cache import cache_key
from app.pipeline.lanes import system_prompt, tool_hints, user_message
from app.static import collect, ruff_runner, semgrep_runner
from app.static.collect import attach_hints, collect_tool_findings
from app.static.findings import ToolFinding
from app.static.ruff_runner import run_ruff
from app.static.semgrep_runner import _write_files, run_semgrep
from tests.factories import make_chunk

# One problem per line, numbered:
CODE = """import sys
class A(object):
    pass
def f(x=[]):
    return undefined_name
if f:
    y = 1
else:
    y = 2
"""
SECURITY_CODE = """import subprocess

def run(cmd):
    subprocess.run(cmd, shell=True)
    API_KEY = "sk_live_1234567890abcdef"
    return API_KEY
"""

needs_semgrep = pytest.mark.skipif(
    shutil.which("semgrep") is None, reason="semgrep is not installed (it runs in CI)"
)


def by_code(findings: list[ToolFinding]) -> dict[str, ToolFinding]:
    return {f.code: f for f in findings}


async def test_ruff_findings_have_lines_codes_and_lanes():
    findings = by_code(await run_ruff("app/x.py", CODE))

    assert findings["F401"].line == 1  # unused import: likely a bug
    assert findings["F401"].lane == "correctness"
    assert findings["B006"].lane == "correctness"  # mutable default argument
    assert findings["F821"].line == 5
    assert findings["UP004"].lane == "maintainability"  # inherits from object
    assert findings["SIM108"].lane == "maintainability"  # if/else could be a ternary
    assert all(f.tool == "ruff" for f in findings.values())


async def test_syntax_errors_go_to_the_correctness_lane():
    [finding, *_] = await run_ruff("app/broken.py", "def f(:\n")

    assert (finding.code, finding.lane) == ("invalid-syntax", "correctness")


async def test_ruff_ignores_the_repos_own_config(tmp_path, monkeypatch):
    # A PR can't silence our rules with its own Ruff config: --isolated never reads it.
    # (per-file-ignores, because a CLI --select already overrides a config's plain `ignore`.)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff.lint.per-file-ignores]\n"x.py" = ["F401"]\n'
    )
    monkeypatch.chdir(tmp_path)

    findings = by_code(await run_ruff("x.py", CODE))

    assert "F401" in findings


async def test_ruff_failing_to_start_returns_nothing(monkeypatch, caplog):
    monkeypatch.setattr(ruff_runner, "find_ruff_bin", lambda: "no-such-ruff-binary")

    with caplog.at_level(logging.WARNING):
        assert await run_ruff("app/x.py", CODE) == []
    assert "could not start" in caplog.text


async def test_ruff_timing_out_returns_nothing(monkeypatch, caplog):
    monkeypatch.setattr(ruff_runner, "RUFF_TIMEOUT_SECONDS", 0.000001)

    with caplog.at_level(logging.WARNING):
        assert await run_ruff("app/x.py", CODE) == []
    assert "timed out" in caplog.text


async def test_ruff_output_that_isnt_json_returns_nothing(monkeypatch):
    async def garbage(*args, **kwargs):
        return b"not json"

    monkeypatch.setattr(ruff_runner, "run_tool", garbage)

    assert await run_ruff("app/x.py", CODE) == []


async def test_only_findings_on_added_lines_are_kept(monkeypatch):
    async def no_semgrep(files):
        raise AssertionError("semgrep must not run when disabled")

    monkeypatch.setattr(collect, "run_semgrep", no_semgrep)
    files = {"app/x.py": CODE, "README.md": "# not python\n"}

    found = await collect_tool_findings(files, {"app/x.py": {4, 5}}, semgrep_enabled=False)

    assert set(found) == {"app/x.py"}  # non-Python files aren't run
    assert {(f.line, f.code) for f in found["app/x.py"]} == {(4, "B006"), (5, "F821")}


async def test_semgrep_findings_are_merged_when_enabled(monkeypatch):
    async def fake_semgrep(files):
        return {"app/x.py": [ToolFinding("semgrep", 4, "eval-or-exec", "m", "security")]}

    monkeypatch.setattr(collect, "run_semgrep", fake_semgrep)

    found = await collect_tool_findings({"app/x.py": CODE}, {"app/x.py": {4}}, True)

    assert {(f.tool, f.code) for f in found["app/x.py"]} == {
        ("ruff", "B006"),
        ("semgrep", "eval-or-exec"),
    }


def test_hints_are_attached_to_the_chunk_and_lane_they_belong_to():
    chunk = make_chunk("app/x.py", CODE, added={4, 5})
    other_file = make_chunk("app/y.py", CODE, added={4})
    bug = ToolFinding("ruff", 5, "F821", "Undefined name", "correctness")
    style = ToolFinding("ruff", 2, "UP004", "Inherits from object", "maintainability")

    attach_hints([chunk, other_file], {"app/x.py": [bug, style]})

    assert chunk.hints == {"correctness": [bug], "maintainability": [style]}
    assert other_file.hints == {}


def test_each_lane_sees_only_its_own_hints_inside_the_untrusted_block():
    chunk = make_chunk("app/x.py", CODE, added={5})
    chunk.hints = {"correctness": [ToolFinding("ruff", 5, "F821", "Undefined name `x`", "c")]}

    correctness = user_message(chunk, "b0und4ry", "correctness")
    security = user_message(chunk, "b0und4ry", "security")

    block = correctness.split("BEGIN UNTRUSTED PR CONTENT b0und4ry\n", 1)[1]
    assert "- line 5: F821 Undefined name `x` (ruff)" in block
    assert block.index("F821") < block.index("END UNTRUSTED PR CONTENT b0und4ry")
    assert "Tool findings" not in security
    assert tool_hints(chunk, "security") == ""


def test_hints_are_part_of_the_cache_key():
    plain = make_chunk("app/x.py", CODE, added={5})
    hinted = make_chunk("app/x.py", CODE, added={5})
    hinted.hints = {"correctness": [ToolFinding("ruff", 5, "F821", "Undefined name", "c")]}

    assert cache_key(1, "correctness", "m", plain) != cache_key(1, "correctness", "m", hinted)
    # Lanes that see no hints keep the same key.
    assert cache_key(1, "security", "m", plain) == cache_key(1, "security", "m", hinted)


def test_prompt_explains_how_to_use_tool_findings():
    prompt = system_prompt("correctness")

    assert "## Tool findings" in prompt
    assert "Treat them as hints" in prompt


async def test_semgrep_missing_returns_nothing(monkeypatch, caplog):
    monkeypatch.setattr(semgrep_runner.shutil, "which", lambda name: None)

    with caplog.at_level(logging.WARNING):
        assert await run_semgrep({"app/x.py": SECURITY_CODE}) == {}
    assert "isn't installed" in caplog.text


def test_files_are_never_written_outside_the_temp_folder(tmp_path):
    folder = tmp_path / "scan"
    folder.mkdir()

    _write_files(str(folder), {"app/ok.py": "x = 1\n", "../escaped.py": "x = 2\n"})

    assert (folder / "app" / "ok.py").exists()
    assert not (tmp_path / "escaped.py").exists()


@needs_semgrep
async def test_real_semgrep_finds_our_rules():
    found = await run_semgrep({"app/run.py": SECURITY_CODE})

    assert {(f.line, f.code) for f in found["app/run.py"]} == {
        (4, "subprocess-shell-true"),
        (5, "hardcoded-secret"),
    }
    assert all(f.lane == "security" and f.tool == "semgrep" for f in found["app/run.py"])


@needs_semgrep
def test_rules_file_is_valid():
    result = subprocess.run(
        [
            shutil.which("semgrep"),
            "--validate",
            "--config",
            str(semgrep_runner.RULES_FILE),
            "--metrics=off",
            "--disable-version-check",
            "--quiet",
        ],
        capture_output=True,
        cwd=Path(semgrep_runner.RULES_FILE).parent,
    )

    assert result.returncode == 0, result.stderr
