"""Failure tests (Phase 6b): kill workers, restart Redis, and make Groq and GitHub fail while
reviews run. Then check that every PR still ends with exactly one review, and how long that took.

    uv run python -m loadtest.failures K1        # --list shows every test
    uv run python -m loadtest.report

Each test starts the load-test stack fresh (`down -v`, then `up`) with the workers it needs, and
saves loadtest/results/<name>.json. The kill tests wait out the real 900 s job timeout, so they
take 15–30 minutes each. Expectations are from reading the code (docs/plans/phase-6.md); the
tests check them.
"""

import argparse
import asyncio
import json
import secrets
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

import httpx

from loadtest import stack
from loadtest.report import RESULTS_DIR, metrics
from loadtest.run import (
    API_URL,
    FAKES_URL,
    build_result,
    clock_offset,
    send_all,
    to_our_clock,
    webhook_secret,
)
from loadtest.scenarios import Event, mixed

# Seconds per fake Groq call in the kill tests: a review takes about 30 s, so a kill lands
# mid-job. The other tests use FAST, a quick real Groq call without quota waits.
SLOW = 12.0
FAST = 0.5
POLL_SECONDS = 2
GRACE_SECONDS = 15  # after the last run settles: a late duplicate review would land here
BUSY_WAIT_SECONDS = 180
TERMINAL = ("completed", "failed")
# app.worker.MAX_JOBS, not imported: importing app.worker loads the App's settings, which on
# the host would mean the real .env. A test keeps the two equal.
MAX_JOBS = 2
# app.reconciler.MAX_AUTO_RETRIES, kept equal by the same test.
MAX_AUTO_RETRIES = 3


def open_heads(records: dict) -> list[str]:
    return [p["head"] for p in records["pulls"] if p["state"] == "open" and not p["draft"]]


def is_settled(records: dict, runs: dict[str, dict]) -> bool:
    """Every open PR's head has a run that's done: completed, or failed for good. A transient
    failure with automatic retries left isn't done: the reconciler will try it again."""
    return all(_done(runs.get(head)) for head in open_heads(records))


def _done(run: dict | None) -> bool:
    if run is None or run["status"] not in TERMINAL:
        return False
    retry_left = run.get("failure_kind") == "transient" and run["auto_retries"] < MAX_AUTO_RETRIES
    return run["status"] == "completed" or not retry_left


def in_flight(records: dict) -> dict[str, list[str]]:
    """Per worker, the heads whose job has started and has no review yet."""
    reviewed = {r["sha"] for r in records["reviews"]}
    busy: dict[str, list[str]] = {}
    for sha, seen in records["first_seen"].items():
        if sha not in reviewed:
            busy.setdefault(seen["worker"], []).append(sha)
    return busy


def recovery_s(shas: list[str], runs: dict[str, dict], since: float) -> float | None:
    """Seconds from `since` until the last of these runs completed; None if one never did."""
    finished = [runs.get(sha, {}).get("finished") for sha in shas]
    if not shas or any(
        runs.get(sha, {}).get("status") != "completed" or t is None
        for sha, t in zip(shas, finished, strict=True)
    ):
        return None
    return round(max(finished) - since, 1)


def reviews_waited_behind(records: dict, sha: str, sent: float) -> int | None:
    """Reviews of other PRs posted after `sha`'s webhook was sent and before its own review: how
    many reviews it waited behind. None if it was never reviewed."""
    done = review_seconds(records, sha, sent)
    if done is None:
        return None
    return sum(sent < r["t"] < sent + done for r in records["reviews"] if r["sha"] != sha)


def review_seconds(records: dict, sha: str, sent: float) -> float | None:
    """Seconds from `sent` to `sha`'s first review; None if it has none."""
    own = [r["t"] for r in records["reviews"] if r["sha"] == sha]
    return round(min(own) - sent, 2) if own else None


def run_summary(runs: dict[str, dict]) -> dict:
    return {
        "run_statuses": dict(Counter(r["status"] for r in runs.values())),
        "failure_errors": dict(
            Counter(r["error"] for r in runs.values() if r["status"] == "failed")
        ),
    }


class Session:
    """One failure test in progress: the clients, the clock offset, the webhooks sent, and a
    timeline of what was done to the stack."""

    def __init__(self, api: httpx.AsyncClient, fakes: httpx.AsyncClient, offset: float, args):
        self.api = api
        self.fakes = fakes
        self.offset = offset
        self.args = args
        self.nonce = secrets.token_hex(4)
        self.webhooks: list[dict] = []
        self.timeline: list[dict] = []

    def note(self, what: str) -> float:
        now = time.time()
        self.timeline.append({"t": round(now, 3), "what": what})
        print(f"[{time.strftime('%H:%M:%S')}] {what}", flush=True)
        return now

    def prs(self, default: int) -> int:
        return self.args.prs or default

    def opened(self, n: int, installations: int = 5) -> list[Event]:
        return mixed(n, self.nonce, installations=installations, mix={"opened": 1})

    def fakes_time(self, t: float) -> float:
        return t + self.offset

    async def configure(self, **config) -> None:
        """Replaces the fakes' whole config: anything not given is back to its default."""
        (await self.fakes.put("/_admin/config", json=config)).raise_for_status()
        self.note(f"fakes config: {json.dumps(config)}")

    async def send(self, events: list[Event]) -> list[dict]:
        records = await send_all(self.api, self.fakes, events, webhook_secret(), concurrency=20)
        self.webhooks += records
        statuses = Counter(r["status"] for r in records)
        self.note(f"sent {len(events)} webhooks: {dict(statuses)}")
        return records

    async def records(self) -> dict:
        records = (await self.fakes.get("/_admin/records")).json()
        to_our_clock(records, self.offset)
        return records

    async def runs(self) -> dict[str, dict]:
        runs = await stack.runs()
        for run in runs.values():  # Postgres runs in Docker's VM, on the fakes' clock
            if run["finished"] is not None:
                run["finished"] -= self.offset
        return runs

    async def settled(self) -> bool:
        return is_settled(await self.records(), await self.runs())

    async def wait(self, condition: Callable[[], Awaitable[bool]], max_wait: float, what: str):
        deadline = time.time() + max_wait
        while time.time() < deadline:
            if await condition():
                self.note(f"done: {what}")
                return True
            await asyncio.sleep(POLL_SECONDS)
        self.note(f"gave up after {max_wait:.0f} s: {what}")
        return False

    async def busy_worker(self, jobs: int) -> tuple[str, list[str]]:
        """A worker with at least `jobs` jobs in flight, and those jobs' heads."""
        deadline = time.time() + BUSY_WAIT_SECONDS
        while time.time() < deadline:
            for worker, shas in in_flight(await self.records()).items():
                if len(shas) >= jobs:
                    return worker, shas
            await asyncio.sleep(0.5)
        raise RuntimeError(f"no worker had {jobs} jobs in flight within {BUSY_WAIT_SECONDS} s")


# --- Killing a worker -----------------------------------------------------------------------


async def k1(s: Session) -> dict:
    await s.configure(groq_latency=SLOW, agent_latency=SLOW)
    await s.send(s.opened(s.prs(10)))
    worker, shas = await s.busy_worker(2)
    name = await stack.worker_name(worker)
    await stack.kill(name, "KILL")
    killed = s.note(f"SIGKILL {name} with {len(shas)} jobs in flight; it stays down")
    settled = await s.wait(s.settled, 1800, "every run completed or failed")
    return {
        "settled": settled,
        "affected": shas,
        "recovery_s": recovery_s(shas, await s.runs(), killed),
    }


async def k1b(s: Session) -> dict:
    await s.configure(groq_latency=SLOW, agent_latency=SLOW)
    await s.send(s.opened(s.prs(10)))
    worker, shas = await s.busy_worker(2)
    name = await stack.worker_name(worker)
    await stack.kill(name, "KILL")
    killed = s.note(f"SIGKILL {name} with {len(shas)} jobs in flight")
    await stack.start(name)
    s.note(f"started {name} again")
    settled = await s.wait(s.settled, 1800, "every run completed or failed")
    return {
        "settled": settled,
        "affected": shas,
        "recovery_s": recovery_s(shas, await s.runs(), killed),
    }


async def k2(s: Session) -> dict:
    await s.configure(groq_latency=SLOW, agent_latency=SLOW)
    await s.send(s.opened(s.prs(10)))
    worker, shas = await s.busy_worker(2)
    name = await stack.worker_name(worker)
    stopped = s.note(f"docker stop {name} (SIGTERM, 10 s grace) with {len(shas)} jobs in flight")
    await stack.stop(name, grace_seconds=10)
    runs = await s.runs()
    after_stop = {sha: runs.get(sha, {}).get("status") for sha in shas}
    s.note(f"stopped; the in-flight runs are now {after_stop}")
    await stack.start(name)
    s.note(f"started {name} again")
    settled = await s.wait(s.settled, 2400, "every run completed or failed")
    return {
        "settled": settled,
        "affected": shas,
        "statuses_after_stop": after_stop,
        "recovery_s": recovery_s(shas, await s.runs(), stopped),
    }


async def k3(s: Session) -> dict:
    await s.configure(review_hold_seconds=30)
    [event] = s.opened(1)
    await s.send([event])
    if not await s.wait(
        partial(_has_reviews, s, 1), 120, "the review is stored and its answer held"
    ):
        raise RuntimeError("no review was posted")
    [review] = (await s.records())["reviews"]
    name = await stack.worker_name(review["worker"])
    await stack.kill(name, "KILL")
    killed = s.note(f"SIGKILL {name} after GitHub stored the review, before it answered")
    await s.configure()  # no more holding
    await stack.start(name)
    s.note(f"started {name} again")
    settled = await s.wait(s.settled, 1800, "the run completed")
    reviews = [r for r in (await s.records())["reviews"] if r["sha"] == event.sha]
    return {
        "settled": settled,
        "affected": [event.sha],
        "reviews_of_affected": len(reviews),
        "recovery_s": recovery_s([event.sha], await s.runs(), killed),
    }


async def _has_reviews(s: Session, n: int) -> bool:
    return len((await s.records())["reviews"]) >= n


# --- Groq 429 ---------------------------------------------------------------------------------


async def q1(s: Session) -> dict:
    fault = {"status": 429, "rate": 0.3, "retry_after": 2}
    await s.configure(groq_latency=FAST, agent_latency=FAST, groq_fault=fault)
    await s.send(s.opened(s.prs(30)))
    return {"settled": await s.wait(s.settled, 900, "every run completed or failed")}


async def q2(s: Session) -> dict:
    start = time.time()
    fault = {
        "status": 429,
        "rate": 1.0,
        "retry_after": 30,
        "start": s.fakes_time(start),
        "end": s.fakes_time(start + 120),
    }
    await s.configure(groq_latency=FAST, agent_latency=FAST, groq_fault=fault)
    s.note("every Groq call gets 429 for the next 120 s")
    await s.send(s.opened(s.prs(10)))
    return {"settled": await s.wait(s.settled, 900, "every run completed or failed")}


async def q3(s: Session) -> dict:
    await s.configure(groq_latency=FAST, agent_latency=FAST, groq_tpm=8000)
    await s.send(s.opened(s.prs(10)))
    return {"settled": await s.wait(s.settled, 2400, "every run completed or failed")}


# --- GitHub 5xx and rate limits ---------------------------------------------------------------


async def g1(s: Session) -> dict:
    fault = {"status": 502, "rate": s.args.fault_rate}
    await s.configure(groq_latency=FAST, agent_latency=FAST, github_fault=fault)
    await s.send(s.opened(s.prs(30)))
    return {"settled": await s.wait(s.settled, 900, "every run completed or failed")}


async def g2(s: Session) -> dict:
    start = time.time() + 10  # jobs are under way when GitHub goes down
    fault = {
        "status": 503,
        "rate": 1.0,
        "start": s.fakes_time(start),
        "end": s.fakes_time(start + s.args.outage),
    }
    await s.configure(groq_latency=2.0, agent_latency=2.0, github_fault=fault)
    s.note(f"GitHub answers 503 to everything from +10 s, for {s.args.outage:.0f} s")
    await s.send(s.opened(s.prs(20)))
    # Long enough for the reconciler's retries of transient failures (5, 10, 20 min apart).
    settled = await s.wait(s.settled, 2700, "every run completed or out of retries")
    return {"settled": settled}


async def g3(s: Session) -> dict:
    await s.configure(groq_latency=FAST, agent_latency=FAST, review_fail_status=502)
    await s.send(s.opened(s.prs(10)))
    return {"settled": await s.wait(s.settled, 900, "every run completed or failed")}


async def g4(s: Session) -> dict:
    await s.configure(
        groq_latency=FAST, agent_latency=FAST, github_rate_limit=200, github_rate_window=300
    )
    s.note("one installation, 200 GitHub requests per 300 s")
    await s.send(s.opened(s.prs(40), installations=1))
    return {"settled": await s.wait(s.settled, 1200, "every run completed or failed")}


# --- Redis restart ----------------------------------------------------------------------------


async def r1(s: Session) -> dict:
    """R1, then R2: replay every delivery once the lost work is recovered."""
    await s.configure(groq_latency=2.0, agent_latency=2.0)
    events = s.opened(s.prs(80))
    first, during = events[: len(events) * 3 // 4], events[len(events) * 3 // 4 :]
    await s.send(first)
    await s.wait(partial(_has_reviews, s, 5), 300, "5 reviews posted, the rest queued")

    s.note(f"restarting Redis, and sending {len(during)} more webhooks meanwhile")
    await asyncio.gather(stack.restart(stack.REDIS), s.send(during))
    await s.wait(stack.redis_up, 60, "Redis answers again")
    back = s.note("Redis is back")
    runs = await s.runs()
    lost = [
        h for h in open_heads(await s.records()) if runs.get(h, {}).get("status") not in TERMINAL
    ]
    s.note(f"{len(lost)} PRs not reviewed yet")
    worker = (await stack.containers("worker"))[0][1]
    restarts = 0

    async def settled_with_worker_up() -> bool:
        """If the worker exits over the lost connection, start it, as Render would."""
        nonlocal restarts
        if not await stack.is_running(worker):
            restarts += 1
            s.note(f"{worker} had exited; starting it again")
            await stack.start(worker)
        return await s.settled()

    settled = await s.wait(settled_with_worker_up, 1500, "every run completed or failed")
    recovery = recovery_s(lost, await s.runs(), back)

    # R2: Redis forgot every delivery ID, so each replay is accepted. Postgres must stop them.
    before = len((await s.records())["reviews"])
    replays = [
        Event(**{**vars(e), "kind": "replay", "expected": (200, 202), "push": False})
        for e in events
    ]
    replayed = await send_all(s.api, s.fakes, replays, webhook_secret(), concurrency=20)
    s.note(f"replayed all {len(replays)} deliveries")
    await asyncio.sleep(60)  # time for the replayed jobs to run (and find their runs completed)
    after = len((await s.records())["reviews"])
    return {
        "settled": settled,
        "affected": lost,
        "recovery_s": recovery,
        "worker_restarts": restarts,
        "replay_statuses": dict(Counter(r["status"] for r in replayed)),
        "replay_new_reviews": after - before,
    }


# --- Noisy neighbour (the per-installation limit decision) ----------------------------------


async def n1(s: Session) -> dict:
    """Installation B's one PR, first on an empty queue, then 10 s into a 100-PR burst from
    installation A."""
    await s.configure(groq_latency=FAST, agent_latency=FAST)

    def b_pull(number: int) -> Event:
        [event] = mixed(1, f"{s.nonce}-b{number}", installations=1, mix={"opened": 1})
        return replace(
            event, installation_id=2, repo_id=201, repo="load-org-2/repo-1", number=number
        )

    alone = b_pull(1)
    [record] = await s.send([alone])
    await s.wait(s.settled, 120, "B's PR reviewed on an empty queue")
    records = await s.records()
    alone_s = review_seconds(records, alone.sha, record["t"])

    burst = s.opened(s.prs(100), installations=1)  # all installation A
    sending = asyncio.create_task(s.send(burst))
    await asyncio.sleep(10)
    behind = b_pull(2)
    [record] = await s.send([behind])
    await sending
    settled = await s.wait(s.settled, 900, "every run completed or failed")
    records = await s.records()
    waited = reviews_waited_behind(records, behind.sha, record["t"])
    return {
        "settled": settled,
        "b_alone_s": alone_s,
        "b_behind_burst_s": review_seconds(records, behind.sha, record["t"]),
        "b_waited_behind_reviews": waited,
        # The rule in docs/plans/phase-6.md: a limit is worth building past max_jobs reviews.
        "limit_rule_triggered": waited is not None and waited > MAX_JOBS,
    }


@dataclass
class FailureTest:
    run: Callable[[Session], Awaitable[dict]]
    workers: int
    expect: str


TESTS = {
    "K1": FailureTest(
        k1, 2,
        "Queued jobs go to the other worker at once. The 2 killed jobs come back when arq's "
        "in-progress key expires (~910 s); claim_run takes over the stale run. 1 review each.",
    ),
    "K1b": FailureTest(
        k1b, 1,
        "Same ~15 min with the worker restarted at once: the startup reconcile leaves a fresh "
        "`running` run alone.",
    ),
    "K2": FailureTest(
        k2, 1,
        "Likely gap: arq re-queues the cancelled jobs at once, but their runs stay `running`, "
        "so the re-runs back off. Recovery only via the reconciler: 900 s + up to 5 min.",
    ),
    "K3": FailureTest(
        k3, 1,
        "The re-run (~910 s later) finds the run marker and posts nothing: 1 review, and the "
        "run completed.",
    ),
    "Q1": FailureTest(
        q1, 1,
        "Every review posted with its comment: since Phase 6c fix 3, 429s are waited out until "
        "the deadline instead of failing the call after 3 tries.",
    ),
    "Q2": FailureTest(
        q2, 1,
        "Lane calls wait out the 120 s storm (fix 3), so all 10 reviews post with their comment. "
        "A call that still fails makes its run fail as transient, for the reconciler to retry.",
    ),
    "Q3": FailureTest(
        q3, 1,
        "With 1 worker ~0 429s (the client waits on Groq's headers); with 2 (--workers 2) each "
        "keeps its own limiter, so 429s may appear.",
    ),
    "G1": FailureTest(
        g1, 1,
        "Any 5xx restarts the whole job (5 tries). At 10% about 1% of runs may end `failed`; "
        "retries re-run the agent.",
    ),
    "G2": FailureTest(
        g2, 1,
        "Likely gap: the 5 tries are used up in ~40–75 s, so jobs caught in a 180 s outage end "
        "`failed`, and the reconciler doesn't retry failed runs.",
    ),
    "G3": FailureTest(
        g3, 1,
        "The retry finds the marker: 1 review per PR, and no agent calls repeated.",
    ),
    "G4": FailureTest(
        g4, 1,
        "Jobs defer until the window resets (below 50 left), none fail, every PR reviewed.",
    ),
    "R1": FailureTest(
        r1, 1,
        "503s while Redis is down; queued jobs and delivery IDs lost; the reconciler re-enqueues "
        "every lost PR within ~5 min. R2: replays accepted (202) but 0 new reviews.",
    ),
    "N1": FailureTest(
        n1, 1,
        "arq's queue is FIFO, so B's one PR waits behind all of A's burst still queued when it "
        "arrives: far more than max_jobs (2) reviews.",
    ),
}  # fmt: skip


def default_name(key: str, args: argparse.Namespace, workers: int) -> str:
    if key == "G1":
        return f"G1-{round(args.fault_rate * 100)}pct"
    if key == "G2":
        return f"G2-{args.outage:.0f}s"
    if key in ("Q3", "G4"):
        return f"{key}-{workers}w"
    return key


async def run_test(key: str, args: argparse.Namespace, workers: int) -> dict:
    test = TESTS[key]
    async with (
        httpx.AsyncClient(base_url=args.fakes, timeout=30) as fakes,
        httpx.AsyncClient(base_url=args.api, timeout=30) as api,
    ):
        print(f"starting a fresh stack with {workers} worker(s)...", flush=True)
        await stack.fresh_stack(workers, api, fakes)
        offset = await clock_offset(fakes)
        s = Session(api, fakes, offset, args)
        stop_sampling = asyncio.Event()
        memory = asyncio.create_task(stack.sample_memory(stop_sampling))
        try:
            extra = await test.run(s)
            await asyncio.sleep(GRACE_SECONDS)
            records = await s.records()
            runs = await s.runs()
        finally:
            stop_sampling.set()
        peaks = await memory

    config = {
        "scenario": "failure",
        "test": key,
        "prs": args.prs,
        "fault_rate": args.fault_rate if key == "G1" else None,
        "outage_s": args.outage if key == "G2" else None,
    }
    settled = extra.pop("settled")
    result = build_result(
        args.name or default_name(key, args, workers),
        config, workers, offset, settled, s.webhooks, records, peaks,
    )  # fmt: skip
    result["failure"] = {
        "test": key,
        "expect": test.expect,
        "timeline": s.timeline,
        **run_summary(runs),
        **extra,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("test", nargs="?", choices=list(TESTS))
    parser.add_argument("--list", action="store_true", help="list the tests and what they expect")
    parser.add_argument("--name", help="result file name (default: from the test and options)")
    parser.add_argument("--workers", type=int, help="override the test's worker count")
    parser.add_argument("--prs", type=int, help="override the test's number of PRs")
    parser.add_argument("--fault-rate", type=float, default=0.10, help="G1: share of 502s")
    parser.add_argument("--outage", type=float, default=180, help="G2: outage length, seconds")
    parser.add_argument("--api", default=API_URL)
    parser.add_argument("--fakes", default=FAKES_URL)
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if args.list or not args.test:
        for key, test in TESTS.items():
            print(f"{key:4} ({test.workers} worker{'s' if test.workers > 1 else ''}) {test.expect}")
        return
    workers = args.workers or TESTS[args.test].workers
    path = args.out / f"{args.name or default_name(args.test, args, workers)}.json"
    if path.exists() and not args.overwrite:
        sys.exit(f"{path} exists; pass --overwrite to replace it")
    result = asyncio.run(run_test(args.test, args, workers))
    args.out.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({**metrics(result), "failure": result["failure"]}, indent=2))
    print(f"saved {path}")


if __name__ == "__main__":
    main()
