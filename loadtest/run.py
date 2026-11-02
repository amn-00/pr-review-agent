"""Run the mixed webhook scenario against the load-test stack, and save the result.

    docker compose -f loadtest/docker-compose.yml up -d --build [--scale worker=2]
    uv run python -m loadtest.run --name L1 --webhooks 100
    uv run python -m loadtest.report

The stack has its own project, ports, database and Redis, and its workers only talk to the
fakes, so this never touches the dev stack, the eval database or the Groq quota. For a measured
run, start from a fresh stack (`docker compose ... down -v` first).
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import platform
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from loadtest.report import RESULTS_DIR, metrics
from loadtest.scenarios import Event, mixed
from loadtest.stack import sample_memory, worker_count

API_URL = "http://localhost:18000"
FAKES_URL = "http://localhost:19000"
ENV_FILE = Path(__file__).parent / ".env.loadtest"
POLL_SECONDS = 2


def webhook_secret() -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("GITHUB_WEBHOOK_SECRET="):
            return line.split("=", 1)[1]
    raise SystemExit(f"GITHUB_WEBHOOK_SECRET not found in {ENV_FILE}")


def signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def send_all(
    api: httpx.AsyncClient,
    fakes: httpx.AsyncClient,
    events: list[Event],
    secret: str,
    concurrency: int,
    rate: float | None = None,
) -> list[dict]:
    """Send every event with `concurrency` senders taking them in order: as fast as possible,
    or `rate` per second. Returns one record per webhook."""
    answered = [asyncio.Event() for _ in events]
    records: list[dict] = [{} for _ in events]
    queue = iter(enumerate(events))  # shared by the senders, so events go out in order
    start = time.time()

    async def sender() -> None:
        for i, event in queue:
            if rate:
                await asyncio.sleep(max(0.0, start + i / rate - time.time()))
            if event.after is not None:
                await answered[event.after].wait()
            if event.push:
                pull = {
                    "installation_id": event.installation_id,
                    "repo_id": event.repo_id,
                    "repo": event.repo,
                    "number": event.number,
                    "sha": event.sha,
                }
                (await fakes.post("/_admin/pulls", json=pull)).raise_for_status()
            body = json.dumps(event.payload()).encode()
            headers = {
                "Content-Type": "application/json",
                "X-GitHub-Event": event.event,
                "X-GitHub-Delivery": event.delivery,
                "X-Hub-Signature-256": signature(secret, body),
            }
            record = {
                "i": i,
                "kind": event.kind,
                "delivery": event.delivery,
                "repo": event.repo,
                "number": event.number,
                "sha": event.sha,
                "expected": list(event.expected),
                "status": None,
                "answer": None,
                "error": None,
            }
            sent = time.time()
            try:
                response = await api.post("/webhook", content=body, headers=headers)
                record["status"] = response.status_code
                record["answer"] = response.json().get("status")
            except (httpx.HTTPError, ValueError) as exc:
                record["error"] = repr(exc)
            record["t"] = sent
            record["latency_ms"] = round((time.time() - sent) * 1000, 2)
            records[i] = record
            answered[i].set()

    await asyncio.gather(*(sender() for _ in range(concurrency)))
    return records


async def clock_offset(fakes: httpx.AsyncClient) -> float:
    """The fakes' clock minus ours (Docker's VM clock can drift from the host's), from the
    fastest of a few round trips."""
    best: tuple[float, float] | None = None
    for _ in range(5):
        before = time.time()
        now = (await fakes.get("/_admin/clock")).json()["now"]
        after = time.time()
        if best is None or after - before < best[0]:
            best = (after - before, now - (before + after) / 2)
    return best[1]


async def wait_until_reviewed(fakes: httpx.AsyncClient, max_wait: float) -> bool:
    """True once every open PR's head has a review; False if `max_wait` seconds pass first."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        progress = (await fakes.get("/_admin/progress")).json()
        if progress["reviewed"] >= progress["heads"]:
            return True
        await asyncio.sleep(POLL_SECONDS)
    return False


def git_sha() -> str:
    out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip()


def to_our_clock(records: dict, offset: float) -> None:
    """Fakes' times -> the generator's clock, so they compare with the send times."""
    for review in records["reviews"]:
        review["t"] -= offset
    for seen in records["first_seen"].values():
        seen["t"] -= offset


async def run(args: argparse.Namespace) -> dict:
    nonce = secrets.token_hex(4)
    events = mixed(args.webhooks, nonce, args.seed)
    fakes_config = {"groq_latency": args.groq_latency, "agent_latency": args.agent_latency}
    async with (
        httpx.AsyncClient(base_url=args.fakes, timeout=30) as fakes,
        httpx.AsyncClient(base_url=args.api, timeout=30) as api,
    ):
        (await fakes.post("/_admin/reset")).raise_for_status()
        (await fakes.put("/_admin/config", json=fakes_config)).raise_for_status()
        offset = await clock_offset(fakes)
        stop = asyncio.Event()
        memory = asyncio.create_task(sample_memory(stop))

        webhooks = await send_all(api, fakes, events, webhook_secret(), args.concurrency, args.rate)
        settled = await wait_until_reviewed(fakes, args.settle)
        await asyncio.sleep(args.grace)  # a late duplicate review would land here
        stop.set()
        records = (await fakes.get("/_admin/records")).json()
        peaks = await memory
    to_our_clock(records, offset)

    config = {
        "scenario": "mixed",
        "webhooks": args.webhooks,
        "seed": args.seed,
        "concurrency": args.concurrency,
        "rate": args.rate,
        "fakes": fakes_config,
    }
    return build_result(
        args.name, config, worker_count(), offset, settled, webhooks, records, peaks
    )


def build_result(
    name: str,
    config: dict,
    workers: int,
    offset: float,
    settled: bool,
    webhooks: list[dict],
    records: dict,
    peaks: dict[str, float],
) -> dict:
    """One result file's content; `records` already on our clock. The failure tests add a
    "failure" section to it."""
    return {
        "name": name,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": git_sha(),
        "settled": settled,
        "config": config,
        "environment": {
            "platform": platform.platform(),
            "cpus": os.cpu_count(),
            "workers": workers,
        },
        "clock_offset_s": round(offset, 4),
        "webhooks": webhooks,
        "records": records,
        "memory_peak_mib": peaks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="result file name, e.g. L1")
    parser.add_argument("--webhooks", type=int, default=100)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=20, help="parallel senders")
    parser.add_argument("--rate", type=float, help="webhooks per second (default: a burst)")
    parser.add_argument("--groq-latency", type=float, default=0.5, help="seconds per lane call")
    parser.add_argument("--agent-latency", type=float, default=0.5, help="seconds per agent step")
    parser.add_argument(
        "--settle", type=float, default=1800, help="max seconds to wait for reviews"
    )
    parser.add_argument("--grace", type=float, default=15, help="seconds to wait after that")
    parser.add_argument("--api", default=API_URL)
    parser.add_argument("--fakes", default=FAKES_URL)
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    path = args.out / f"{args.name}.json"
    if path.exists() and not args.overwrite:
        sys.exit(f"{path} exists; pass --overwrite to replace it")
    result = asyncio.run(run(args))
    args.out.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(metrics(result), indent=2))
    print(f"saved {path}")


if __name__ == "__main__":
    main()
