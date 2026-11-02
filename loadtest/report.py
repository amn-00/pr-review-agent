"""Metrics, and report.md, built from the load test's result files alone.

uv run python -m loadtest.report
"""

import argparse
import json
import math
from collections import Counter
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"

LIMITS = """\
**What these numbers are.** Groq and GitHub are fakes, so this measures our own system: the
webhook, the queue, dedupe, Postgres, the worker's overhead and recovery. Real throughput is
set by Groq's free-tier quota (8K tokens/minute, about 5 agent-checked PRs a day; see
`evals/results/`), not by anything here. Everything runs on one machine in Docker, and enqueue
latency goes through Docker's port mapping. Each run is one sample."""


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least p% of values at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(p / 100 * len(ordered)) - 1, 0)]


def _spread(values: list[float], digits: int) -> dict:
    return {
        name: None if (v := percentile(values, p)) is None else round(v, digits)
        for name, p in (("p50", 50), ("p95", 95), ("max", 100))
    }


def metrics(result: dict) -> dict:
    webhooks = result["webhooks"]
    records = result["records"]
    reviews = sorted(records["reviews"], key=lambda r: r["t"])

    # A review's clock starts at the first webhook that enqueued its head (202). Heads only the
    # reconciler enqueued have no start, and are left out of the timings.
    sent: dict[str, float] = {}
    for w in webhooks:
        if w["status"] == 202:
            sent[w["sha"]] = min(w["t"], sent.get(w["sha"], w["t"]))
    first_review: dict[str, float] = {}
    for r in reviews:
        first_review.setdefault(r["sha"], r["t"])
    review_s, wait_s, work_s = [], [], []
    for sha, t in first_review.items():
        if sha not in sent:
            continue
        review_s.append(t - sent[sha])
        if seen := records["first_seen"].get(sha):
            wait_s.append(seen["t"] - sent[sha])
            work_s.append(t - seen["t"])

    per_commit = Counter((r["repo"], r["number"], r["sha"]) for r in reviews)
    open_heads = [p["head"] for p in records["pulls"] if p["state"] == "open" and not p["draft"]]
    send_times = [w["t"] for w in webhooks]
    send_span = max(send_times) - min(send_times) if send_times else 0
    review_span = reviews[-1]["t"] - min(send_times) if reviews and send_times else 0
    github_requests = sum(records["github_requests"].values())

    return {
        "webhooks": len(webhooks),
        "enqueue_latency_ms": _spread(
            [w["latency_ms"] for w in webhooks if w["status"] is not None], 1
        ),
        "review_time_s": _spread(review_s, 2),
        "queue_wait_s": _spread(wait_s, 2),
        "work_time_s": _spread(work_s, 2),
        "webhooks_per_s": round(len(webhooks) / send_span, 1) if send_span else None,
        "reviews_per_min": round(len(first_review) / review_span * 60, 1) if review_span else None,
        "reviews": len(reviews),
        "duplicates": sum(n - 1 for n in per_commit.values() if n > 1),
        "lost": sum(head not in first_review for head in open_heads),
        "stale_posts": sum(r["stale"] for r in reviews),
        "unexpected_statuses": dict(
            Counter(
                f"{w['kind']} -> {w['status']}"
                for w in webhooks
                if w["status"] not in w["expected"]
            )
        ),
        "github_requests_per_review": round(github_requests / len(reviews), 1) if reviews else None,
        "github_errors": records["github_errors"],
        "groq_calls": records["groq_calls"],
        "memory_peak_mib": result["memory_peak_mib"],
    }


def _fmt(value) -> str:
    return "–" if value is None else str(value)


def build_report(results: list[dict]) -> str:
    lines = ["# Load test results", "", LIMITS, ""]
    lines += [
        "| Run | Webhooks | Workers | Enqueue p95 (ms) | Review p95 (s) | Webhooks/s "
        "| Reviews/min | Duplicates | Lost | Stale posts |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        m = metrics(result)
        lines.append(
            f"| {result['name']} | {m['webhooks']} | {_fmt(result['environment']['workers'])} "
            f"| {_fmt(m['enqueue_latency_ms']['p95'])} | {_fmt(m['review_time_s']['p95'])} "
            f"| {_fmt(m['webhooks_per_s'])} | {_fmt(m['reviews_per_min'])} "
            f"| {m['duplicates']} | {m['lost']} | {m['stale_posts']} |"
        )
    for result in results:
        lines += [
            "",
            f"## {result['name']}",
            "",
            f"- Config: `{json.dumps(result['config'])}`",
            f"- Environment: `{json.dumps(result['environment'])}`, git `{result['git_sha']}`",
            f"- Settled (every open head reviewed before the timeout): {result['settled']}",
            "",
            "```json",
            json.dumps(metrics(result), indent=2),
            "```",
        ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=RESULTS_DIR)
    args = parser.parse_args()
    paths = sorted(args.results.glob("*.json"))
    results = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    (args.results / "report.md").write_text(build_report(results), encoding="utf-8")
    print(f"report.md written from {len(results)} result(s)")


if __name__ == "__main__":
    main()
