"""Controls the load-test stack through Docker: start it fresh, kill, stop and start
containers, read the database, and sample memory."""

import asyncio
import subprocess
import time
from pathlib import Path

import httpx

COMPOSE_PROJECT = "pr-review-load"
COMPOSE = ["docker", "compose", "-f", str(Path(__file__).parent / "docker-compose.yml")]
POSTGRES = f"{COMPOSE_PROJECT}-postgres-1"
REDIS = f"{COMPOSE_PROJECT}-redis-1"
READY_SECONDS = 180
MEMORY_SAMPLE_SECONDS = 2
MEMORY_UNITS = {"B": 1 / 2**20, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024}


async def sh(*args: str, check: bool = True) -> str:
    """Run a command, return its stdout. Raises if it fails and `check` is set."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {err.decode().strip()}")
    return out.decode()


async def fresh_stack(workers: int, api: httpx.AsyncClient, fakes: httpx.AsyncClient) -> None:
    """Remove the stack with its data, start it again with `workers` workers, and wait until
    the API is ready and every worker has started."""
    await sh(*COMPOSE, "down", "-v")
    await sh(*COMPOSE, "up", "-d", "--build", "--scale", f"worker={workers}")
    deadline = time.time() + READY_SECONDS
    while time.time() < deadline:
        try:
            api_ready = (await api.get("/health/ready")).status_code == 200
            fakes_up = (await fakes.get("/_admin/clock")).status_code == 200
        except httpx.HTTPError:
            api_ready = fakes_up = False
        logs = await sh(*COMPOSE, "logs", "worker")
        if api_ready and fakes_up and logs.count("Starting worker for") >= workers:
            return
        await asyncio.sleep(1)
    raise RuntimeError(f"the stack wasn't ready within {READY_SECONDS} s")


def container_for(hostname: str, rows: list[tuple[str, str]]) -> str:
    """A container's name from its hostname, which Docker sets to the short container ID."""
    for container_id, name in rows:
        if container_id.startswith(hostname):
            return name
    raise LookupError(f"no container with hostname {hostname}")


async def containers(service: str) -> list[tuple[str, str]]:
    """(ID, name) of the service's containers, running or not."""
    out = await sh(
        "docker", "ps", "-a",
        "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}",
        "--filter", f"label=com.docker.compose.service={service}",
        "--format", "{{.ID}}\t{{.Names}}",
    )  # fmt: skip
    return [tuple(line.split("\t")) for line in out.splitlines()]


async def worker_name(hostname: str) -> str:
    return container_for(hostname, await containers("worker"))


async def kill(name: str, signal: str) -> None:
    await sh("docker", "kill", "--signal", signal, name)


async def stop(name: str, grace_seconds: int) -> None:
    """SIGTERM, then SIGKILL after the grace period: what a deploy or restart does."""
    await sh("docker", "stop", "--time", str(grace_seconds), name)


async def start(name: str) -> None:
    await sh("docker", "start", name)


async def restart(name: str) -> None:
    await sh("docker", "restart", name)


async def is_running(name: str) -> bool:
    out = await sh("docker", "inspect", "--format", "{{.State.Running}}", name)
    return out.strip() == "true"


async def redis_up() -> bool:
    out = await sh("docker", "exec", REDIS, "redis-cli", "ping", check=False)
    return out.strip() == "PONG"


def parse_runs(out: str) -> dict[str, dict]:
    """psql's tab-separated rows -> {head SHA: {status, error, finished}}."""
    runs = {}
    for line in out.splitlines():
        sha, status, error, finished = line.split("\t")
        runs[sha] = {
            "status": status,
            "error": error or None,
            "finished": float(finished) if finished else None,
        }
    return runs


async def runs() -> dict[str, dict]:
    """Every review run, by head SHA. `finished` is on the containers' clock, like the fakes'."""
    sql = (
        "SELECT head_sha, status, coalesce(left(error, 80), ''), "
        "coalesce(extract(epoch FROM finished_at)::text, '') FROM review_runs"
    )
    out = await sh(
        "docker", "exec", POSTGRES,
        "psql", "-U", "postgres", "-d", "pr_review_load", "-At", "-F", "\t", "-c", sql,
    )  # fmt: skip
    return parse_runs(out)


def parse_mib(usage: str) -> float:
    """'85.3MiB' -> 85.3"""
    for unit in sorted(MEMORY_UNITS, key=len, reverse=True):
        if usage.endswith(unit):
            return float(usage.removesuffix(unit)) * MEMORY_UNITS[unit]
    raise ValueError(f"unknown memory unit: {usage}")


async def sample_memory(stop_sampling: asyncio.Event) -> dict[str, float]:
    """Peak memory per container of the stack, from `docker stats` every few seconds."""
    peaks: dict[str, float] = {}
    prefix = f"{COMPOSE_PROJECT}-"
    while not stop_sampling.is_set():
        out = await sh(
            "docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}", check=False
        )
        for line in out.splitlines():
            name, usage = line.split("\t")
            if name.startswith(prefix):
                service = name.removeprefix(prefix)
                mib = round(parse_mib(usage.split(" / ")[0]), 1)
                peaks[service] = max(peaks.get(service, 0.0), mib)
        try:
            await asyncio.wait_for(stop_sampling.wait(), MEMORY_SAMPLE_SECONDS)
        except TimeoutError:
            pass
    return peaks


def worker_count() -> int:
    """Running worker containers."""
    out = subprocess.run(
        [
            "docker", "ps", "-q",
            "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}",
            "--filter", "label=com.docker.compose.service=worker",
        ],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return len(out.split())
