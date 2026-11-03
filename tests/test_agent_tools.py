import io
import json
import tarfile

import httpx
import pytest
import respx

from app.agent import snapshot as snapshot_module
from app.agent import tools as tools_module
from app.agent.snapshot import Snapshot, download_snapshot, extract
from app.agent.tools import AgentTools, find_callers, read_file, search_repo
from app.github.client import GitHubClient
from tests.conftest import GITHUB_API, StubAuth
from tests.factories import INSTALLATION_ID, REPO, SHA_1

TOP = "octo-hello-aaaaaaa"  # GitHub's top folder in a tarball
CODELOAD = "https://codeload.github.com/octo/hello/legacy.tar.gz/aaaa"


def make_tarball(files: dict[str, bytes], links: dict[str, str] | None = None) -> bytes:
    """A gzipped tarball shaped like GitHub's: everything under one top folder."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name if name.startswith(("/", "..")) else f"{TOP}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(f"{TOP}/{name}")
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return buffer.getvalue()


def snapshot_of(tmp_path, files: dict[str, str]) -> Snapshot:
    root = tmp_path / "repo"
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    return Snapshot(root)


REPO_FILES = {
    "app/stats.py": "def average(values):\n    return sum(values) / len(values)\n",
    "app/report.py": (
        "from app.stats import average\n\n\ndef report(scores):\n"
        "    if not scores:\n        return 0\n    return average(scores)\n"
    ),
    "app/other.py": "def average_all(xs):\n    return xs\n",
}


# --- extraction ---


def test_extract_strips_the_top_folder_and_keeps_text_files(tmp_path):
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(make_tarball({"app/x.py": b"x = 1\n", "README.md": b"# hi\n"}))

    assert extract(archive, tmp_path / "out") is True

    assert (tmp_path / "out" / "app" / "x.py").read_text() == "x = 1\n"
    assert (tmp_path / "out" / "README.md").exists()


def test_extract_never_writes_outside_the_folder(tmp_path):
    archive = tmp_path / "repo.tar.gz"
    tarball = make_tarball(
        {"../../escaped.py": b"x\n", "/abs.py": b"x\n", "a/../../../up.py": b"x\n"},
        links={"link.py": "/etc/passwd"},
    )
    archive.write_bytes(tarball)
    root = tmp_path / "out" / "repo"

    extract(archive, root)

    written = [p for p in tmp_path.rglob("*") if p.is_file() and p != archive]
    # "/abs.py" loses its leading slash and lands inside; traversals and the symlink don't.
    assert written == [root / "abs.py"]


def test_extract_skips_binaries_and_big_files(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_module, "MAX_FILE_BYTES", 100)
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(
        make_tarball({"logo.png": b"\x89PNG\0\0data", "big.py": b"x" * 101, "ok.py": b"ok\n"})
    )

    assert extract(archive, tmp_path / "out") is True

    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["ok.py"]


def test_extract_reports_when_the_total_limit_cuts_it_short(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_module, "MAX_TOTAL_BYTES", 10)
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(make_tarball({"a.py": b"123456\n", "b.py": b"123456\n"}))

    assert extract(archive, tmp_path / "out") is False

    assert [p.name for p in (tmp_path / "out").iterdir()] == ["a.py"]


# --- download ---


@pytest.fixture
async def redirecting_github():
    """A client that follows redirects like the real one (make_http_client)."""
    client = GitHubClient(httpx.AsyncClient(base_url=GITHUB_API, follow_redirects=True), StubAuth())
    yield client
    await client.aclose()


async def test_download_follows_the_redirect_without_sending_our_token(
    redirecting_github, tmp_path
):
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GITHUB_API}/repos/{REPO}/tarball/{SHA_1}").mock(
            return_value=httpx.Response(302, headers={"Location": CODELOAD})
        )
        codeload = mock.get(CODELOAD).mock(
            return_value=httpx.Response(200, content=make_tarball({"app/x.py": b"x = 1\n"}))
        )

        snap = await download_snapshot(redirecting_github, INSTALLATION_ID, REPO, SHA_1, tmp_path)

    assert snap.complete and (snap.root / "app" / "x.py").exists()
    assert "Authorization" not in codeload.calls.last.request.headers
    assert not (tmp_path / "repo.tar.gz").exists()  # the archive is removed after extraction


async def test_a_tarball_over_the_size_cap_gives_no_snapshot(
    redirecting_github, tmp_path, monkeypatch
):
    monkeypatch.setattr(snapshot_module, "MAX_TARBALL_BYTES", 10)
    with respx.mock() as mock:
        mock.get(f"{GITHUB_API}/repos/{REPO}/tarball/{SHA_1}").mock(
            return_value=httpx.Response(200, content=b"x" * 1000)
        )

        snap = await download_snapshot(redirecting_github, INSTALLATION_ID, REPO, SHA_1, tmp_path)

    assert snap.root is None and "too large" in snap.problem


@pytest.mark.parametrize(
    "response",
    [httpx.Response(404), httpx.Response(200, content=b"not a tarball")],
    ids=["github-error", "corrupt-archive"],
)
async def test_download_problems_give_no_snapshot_instead_of_failing(
    redirecting_github, tmp_path, response
):
    with respx.mock() as mock:
        mock.get(f"{GITHUB_API}/repos/{REPO}/tarball/{SHA_1}").mock(return_value=response)

        snap = await download_snapshot(redirecting_github, INSTALLATION_ID, REPO, SHA_1, tmp_path)

    assert snap.root is None and "could not be downloaded" in snap.problem


# --- read_file, search_repo, find_callers ---


def test_read_file_returns_numbered_lines_and_marks_them_seen(tmp_path):
    snap = snapshot_of(tmp_path, REPO_FILES)

    result = read_file(snap, "./app/report.py", 4, 7)

    assert "app/report.py, lines 4-7 of 7:" in result.text
    assert "    7      return average(scores)" in result.text
    assert set(result.seen) == {("app/report.py", n) for n in range(4, 8)}


def test_read_file_caps_the_number_of_lines(tmp_path):
    snap = snapshot_of(tmp_path, {"long.py": "x = 1\n" * 500})

    result = read_file(snap, "long.py", 1, 500)

    assert len(result.seen) <= tools_module.MAX_READ_LINES
    assert len(result.text) <= tools_module.MAX_RESULT_CHARS + 20


@pytest.mark.parametrize("path", ["../secret.txt", "/../secret.txt", "app/../../secret.txt"])
def test_read_file_cannot_leave_the_snapshot(tmp_path, path):
    (tmp_path / "secret.txt").write_text("password")
    snap = snapshot_of(tmp_path, REPO_FILES)

    result = read_file(snap, path, 1, 10)

    assert "No such file" in result.text and "password" not in result.text


def test_read_file_reports_missing_files_and_bad_ranges(tmp_path):
    snap = snapshot_of(tmp_path, REPO_FILES)

    assert "No such file: app/nope.py" in read_file(snap, "app/nope.py", 1, 5).text
    assert "has 2 lines" in read_file(snap, "app/stats.py", 50, 60).text


def test_search_is_plain_text_not_a_regex(tmp_path):
    snap = snapshot_of(tmp_path, REPO_FILES)

    result = search_repo(snap, "sum(values) /")  # regex metacharacters, taken literally

    assert result.text.splitlines()[1] == "app/stats.py:2: return sum(values) / len(values)"
    assert result.seen == {("app/stats.py", 2): "return sum(values) / len(values)"}


def test_search_lists_at_most_15_matches_and_says_how_many_more(tmp_path):
    snap = snapshot_of(tmp_path, {"many.py": "hit = 1\n" * 40})

    result = search_repo(snap, "hit")

    assert len(result.seen) == 15
    assert "... and 25 more" in result.text


def test_find_callers_matches_whole_words_and_skips_the_definition(tmp_path):
    snap = snapshot_of(tmp_path, REPO_FILES)

    result = find_callers(snap, "stats.average")  # a dotted name uses its last part

    assert set(result.seen) == {("app/report.py", 1), ("app/report.py", 7)}
    assert "average_all" not in result.text


def test_tools_explain_a_missing_or_incomplete_snapshot(tmp_path):
    missing = Snapshot(None, problem="the repository is too large to download")
    partial = snapshot_of(tmp_path, REPO_FILES)
    partial.complete, partial.problem = False, "some files are missing"

    assert "unavailable: the repository is too large" in read_file(missing, "a.py", 1, 2).text
    assert "unavailable" in search_repo(missing, "x").text
    assert "Note: some files are missing" in search_repo(partial, "average").text


# --- AgentTools: argument checks and CI ---


async def test_bad_arguments_and_unknown_tools_come_back_as_text(tmp_path, github):
    tools = AgentTools(snapshot_of(tmp_path, REPO_FILES), github, INSTALLATION_ID, REPO, SHA_1)

    assert "Invalid arguments for read_file" in (await tools.run("read_file", '{"path": 1}')).text
    assert "Invalid arguments" in (await tools.run("search_repo", "not json")).text
    assert "Invalid arguments" in (await tools.run("find_callers", '{"symbol": "a b"}')).text
    assert "Unknown tool: rm_rf" in (await tools.run("rm_rf", "{}")).text


async def test_tools_run_with_valid_arguments(tmp_path, github):
    tools = AgentTools(snapshot_of(tmp_path, REPO_FILES), github, INSTALLATION_ID, REPO, SHA_1)

    result = await tools.run("find_callers", json.dumps({"symbol": "average"}))

    assert ("app/report.py", 7) in result.seen


async def test_read_file_takes_line_start_and_line_end(tmp_path, github):
    tools = AgentTools(snapshot_of(tmp_path, REPO_FILES), github, INSTALLATION_ID, REPO, SHA_1)

    args = {"path": "app/report.py", "line_start": 4, "line_end": 7}
    result = await tools.run("read_file", json.dumps(args))
    old_names = await tools.run(
        "read_file", json.dumps({"path": "app/report.py", "start": 4, "end": 7})
    )

    assert set(result.seen) == {("app/report.py", n) for n in range(4, 8)}
    assert "Invalid arguments for read_file" in old_names.text


async def test_ci_results_are_fetched_once_and_summarized(tmp_path, github, github_api):
    route = github_api.get(f"/repos/{REPO}/commits/{SHA_1}/check-runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 2,
                "check_runs": [
                    {
                        "name": "tests",
                        "status": "completed",
                        "conclusion": "failure",
                        "output": {"title": "2 failed"},
                    },
                    {"name": "lint", "status": "in_progress", "conclusion": None, "output": {}},
                ],
            },
        )
    )
    tools = AgentTools(snapshot_of(tmp_path, {}), github, INSTALLATION_ID, REPO, SHA_1)

    first = await tools.run("get_ci_results", "{}")
    await tools.run("get_ci_results", "{}")

    assert first.text == (
        "- tests: completed, conclusion failure (2 failed)\n"
        "- lint: in_progress, conclusion none yet"
    )
    assert route.call_count == 1


async def test_ci_errors_come_back_as_text(tmp_path, github, github_api):
    github_api.get(f"/repos/{REPO}/commits/{SHA_1}/check-runs").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible"})
    )
    tools = AgentTools(snapshot_of(tmp_path, {}), github, INSTALLATION_ID, REPO, SHA_1)

    result = await tools.run("get_ci_results", "{}")

    assert result.text == "CI results are unavailable (GitHub returned an error)."
