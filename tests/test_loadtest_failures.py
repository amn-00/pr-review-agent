"""The failure-test drivers' bookkeeping (no Docker): when a test counts as settled, which jobs
a worker holds, recovery time, parsing what Docker and psql print, and the report section."""

import asyncio

import httpx
import pytest

from app import reconciler, worker
from loadtest import failures
from loadtest.failures import (
    TESTS,
    default_name,
    in_flight,
    is_settled,
    recovery_s,
    review_seconds,
    reviews_waited_behind,
    run_summary,
)
from loadtest.fakes import create_app
from loadtest.report import build_report
from loadtest.scenarios import mixed
from loadtest.stack import container_for, parse_mib, parse_runs


def _records(pulls: list[dict], reviews=(), first_seen=None) -> dict:
    return {
        "pulls": [{"state": "open", "draft": False} | p for p in pulls],
        "reviews": list(reviews),
        "first_seen": first_seen or {},
        "github_requests": {},
        "github_errors": {},
        "groq_calls": {},
    }


def _run(
    status: str,
    finished: float | None = None,
    error: str | None = None,
    failure_kind: str | None = None,
    auto_retries: int = 0,
) -> dict:
    return {
        "status": status,
        "finished": finished,
        "error": error,
        "failure_kind": failure_kind,
        "auto_retries": auto_retries,
    }


def test_settled_when_every_open_head_is_completed_or_failed():
    records = _records(
        [
            {"head": "a"},
            {"head": "b"},
            {"head": "c", "state": "closed"},
            {"head": "d", "draft": True},
        ]
    )

    assert is_settled(records, {"a": _run("completed"), "b": _run("failed")})
    assert not is_settled(records, {"a": _run("completed"), "b": _run("running")})
    assert not is_settled(records, {"a": _run("completed")})  # b has no run yet


def test_a_transient_failure_with_retries_left_is_not_settled_yet():
    records = _records([{"head": "a"}])

    assert not is_settled(records, {"a": _run("failed", failure_kind="transient")})
    assert is_settled(records, {"a": _run("failed", failure_kind="transient", auto_retries=3)})
    assert is_settled(records, {"a": _run("failed", failure_kind="permanent")})


def test_in_flight_is_started_and_not_reviewed_per_worker():
    records = _records(
        [],
        reviews=[{"sha": "a"}],
        first_seen={"a": {"worker": "w1"}, "b": {"worker": "w1"}, "c": {"worker": "w2"}},
    )

    assert in_flight(records) == {"w1": ["b"], "w2": ["c"]}


def test_recovery_is_until_the_last_affected_run_completed():
    runs = {"a": _run("completed", 130.0), "b": _run("completed", 160.0), "c": _run("running")}

    assert recovery_s(["a", "b"], runs, since=100.0) == 60.0
    assert recovery_s(["a", "c"], runs, since=100.0) is None  # c never completed
    assert recovery_s(["a", "x"], runs, since=100.0) is None  # x has no run
    assert recovery_s([], runs, since=100.0) is None


def test_run_summary_counts_statuses_and_failure_errors():
    runs = {
        "a": _run("completed"),
        "b": _run("failed", error="gave up after 5 tries: 503"),
        "c": _run("failed", error="gave up after 5 tries: 503"),
    }

    assert run_summary(runs) == {
        "run_statuses": {"completed": 1, "failed": 2},
        "failure_errors": {"gave up after 5 tries: 503": 2},
    }


def test_container_is_found_from_the_hostname():
    rows = [
        ("3f2a9c1b7d4e", "pr-review-load-worker-1"),
        ("9b8c7d6e5f4a", "pr-review-load-worker-2"),
    ]

    assert container_for("9b8c7d6e5f4a", rows) == "pr-review-load-worker-2"
    with pytest.raises(LookupError):
        container_for("000000000000", rows)


def test_psql_rows_and_docker_memory_are_parsed():
    out = (
        "abc\tcompleted\t\t1700000000.5\t\t0\n"
        "def\tfailed\tgave up\t1700000001\ttransient\t2\n"
        "ghi\tqueued\t\t\t\t0\n"
    )

    assert parse_runs(out) == {
        "abc": _run("completed", 1700000000.5),
        "def": _run("failed", 1700000001.0, "gave up", "transient", 2),
        "ghi": _run("queued"),
    }
    assert (parse_mib("85.3MiB"), parse_mib("1.5GiB"), parse_mib("512KiB")) == (85.3, 1536, 0.5)
    assert parse_mib("0B") == 0


def test_every_test_has_an_expectation_and_a_name_per_option():
    class Args:
        fault_rate, outage = 0.02, 60

    assert all(test.expect and test.workers >= 1 for test in TESTS.values())
    assert default_name("G1", Args, 1) == "G1-2pct"
    assert default_name("G2", Args, 1) == "G2-60s"
    assert default_name("Q3", Args, 2) == "Q3-2w"
    assert default_name("K1", Args, 2) == "K1"


def test_opened_only_mix_sends_new_prs_only():
    events = mixed(30, "n", installations=1, mix={"opened": 1})

    assert {e.kind for e in events} == {"opened"}
    assert all(e.push and e.after is None and e.installation_id == 1 for e in events)


async def test_rate_limit_window_resets_after_its_length():
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://fakes") as fakes:
        # A 1 s window leaves slow CI runners plenty of time for the first two requests to
        # land in the same window.
        config = {"github_rate_limit": 50, "github_rate_window": 1.0}
        (await fakes.put("/_admin/config", json=config)).raise_for_status()
        auth = {"Authorization": "Bearer inst-1"}

        first = await fakes.get("/repos/o/r/commits/abc/check-runs", headers=auth)
        second = await fakes.get("/repos/o/r/commits/abc/check-runs", headers=auth)
        await asyncio.sleep(1.1)  # past the window, which started at the first request
        after_reset = await fakes.get("/repos/o/r/commits/abc/check-runs", headers=auth)

    remaining = [int(r.headers["x-ratelimit-remaining"]) for r in (first, second, after_reset)]
    assert remaining == [49, 48, 49]


def test_report_has_a_failure_table_and_timeline():
    result = {
        "name": "K1",
        "config": {"scenario": "failure"},
        "environment": {"workers": 2},
        "git_sha": "abc",
        "settled": True,
        "webhooks": [],
        "records": _records([], reviews=[{"repo": "o/r", "number": 1, "sha": "a", "t": 5.0,
                                          "stale": False, "comments": 1}]),
        "memory_peak_mib": {},
        "failure": {
            "test": "K1",
            "expect": "recovers in ~910 s",
            "timeline": [{"t": 100.0, "what": "SIGKILL"}, {"t": 1012.0, "what": "done"}],
            "run_statuses": {"completed": 1},
            "failure_errors": {},
            "recovery_s": 912.0,
        },
    }  # fmt: skip

    report = build_report([result])

    assert "## Failure tests" in report and "## Load" not in report
    assert "| K1 | 2 | True | 1 | 0 | 0 | 0 | 0 | 912.0 |" in report
    assert "- +912 s: done" in report
    assert "- Expected: recovers in ~910 s" in report


def test_reviews_waited_behind_counts_other_reviews_until_its_own():
    records = _records(
        [],
        reviews=[
            {"sha": "a1", "t": 99.0},  # before B was sent
            {"sha": "a2", "t": 101.0},
            {"sha": "a3", "t": 102.0},
            {"sha": "b", "t": 103.0},
            {"sha": "a4", "t": 104.0},  # after B's review
        ],
    )

    assert review_seconds(records, "b", sent=100.0) == 3.0
    assert reviews_waited_behind(records, "b", sent=100.0) == 2
    assert reviews_waited_behind(records, "x", sent=100.0) is None


def test_the_drivers_constants_match_the_app():
    assert failures.MAX_JOBS == worker.MAX_JOBS
    assert failures.MAX_AUTO_RETRIES == reconciler.MAX_AUTO_RETRIES
