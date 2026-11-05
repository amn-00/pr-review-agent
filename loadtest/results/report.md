# Load test results

**What these numbers are.** Groq and GitHub are fakes, so this measures our own system: the
webhook, the queue, dedupe, Postgres, the worker's overhead and recovery. Real throughput is
set by Groq's free-tier quota (8K tokens/minute, about 5 agent-checked PRs a day; see
`evals/results/`), not by anything here. Everything runs on one machine in Docker, and enqueue
latency goes through Docker's port mapping. Each run is one sample.

## Load

| Run | Webhooks | Workers | Enqueue p95 (ms) | Review p95 (s) | Webhooks/s | Reviews/min | Duplicates | Lost | Stale posts |
|---|---|---|---|---|---|---|---|---|---|
| L1 | 100 | 1 | 259.1 | 50.94 | 77.9 | 76.2 | 0 | 0 | 0 |
| L2 | 250 | 1 | 377.6 | 131.62 | 66.7 | 76.1 | 0 | 0 | 0 |
| L3 | 500 | 1 | 375.2 | 259.21 | 68.6 | 75.6 | 0 | 0 | 0 |
| L4 | 500 | 1 | 56.2 | 222.35 | 10.0 | 75.7 | 0 | 0 | 0 |
| L5 | 500 | 2 | 396.3 | 126.41 | 67.6 | 151.4 | 0 | 0 | 0 |

## Failure tests

| Test | Workers | Settled | Reviews | Duplicates | Lost | Failed runs | Without comment | Recovery (s) |
|---|---|---|---|---|---|---|---|---|
| G1-10pct | 1 | True | 30 | 0 | 0 | 0 | 0 | – |
| G1-2pct | 1 | True | 30 | 0 | 0 | 0 | 0 | – |
| G2-180s-fixed | 1 | True | 20 | 0 | 0 | 0 | 0 | – |
| G2-180s | 1 | True | 4 | 0 | 16 | 16 | 0 | – |
| G2-60s-final | 1 | True | 20 | 0 | 0 | 0 | 0 | – |
| G2-60s-fixed | 1 | True | 20 | 0 | 0 | 0 | 0 | – |
| G2-60s | 1 | True | 11 | 0 | 9 | 9 | 0 | – |
| G3 | 1 | True | 10 | 0 | 0 | 0 | 0 | – |
| G4-1w | 1 | True | 40 | 0 | 0 | 0 | 0 | – |
| K1 | 2 | True | 10 | 0 | 0 | 0 | 0 | 933.5 |
| K1b | 1 | True | 10 | 0 | 0 | 0 | 0 | 933.4 |
| K2-fixed | 1 | True | 10 | 0 | 0 | 0 | 0 | 26.6 |
| K2 | 1 | True | 10 | 0 | 0 | 0 | 0 | 1035.2 |
| K3 | 1 | True | 1 | 0 | 0 | 0 | 0 | 907.9 |
| N1 | 1 | True | 102 | 0 | 0 | 0 | 0 | – |
| Q1-fixed | 1 | True | 30 | 0 | 0 | 0 | 0 | – |
| Q1 | 1 | True | 30 | 0 | 0 | 0 | 2 | – |
| Q2-fixed | 1 | True | 10 | 0 | 0 | 0 | 0 | – |
| Q2 | 1 | True | 10 | 0 | 0 | 0 | 2 | – |
| Q3-1w | 1 | True | 10 | 0 | 0 | 0 | 0 | – |
| Q3-2w | 2 | True | 10 | 0 | 0 | 0 | 0 | – |
| R1 | 1 | True | 80 | 0 | 0 | 0 | 0 | 217.6 |

### L1

- Config: `{"scenario": "mixed", "webhooks": 100, "seed": 1, "concurrency": 20, "rate": null, "fakes": {"groq_latency": 0.5, "agent_latency": 0.5}}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `29c0b6d`
- Settled before the timeout: True

```json
{
  "webhooks": 100,
  "enqueue_latency_ms": {
    "p50": 110.9,
    "p95": 259.1,
    "max": 341.5
  },
  "review_time_s": {
    "p50": 27.32,
    "p95": 50.94,
    "max": 53.87
  },
  "queue_wait_s": {
    "p50": 26.16,
    "p95": 49.74,
    "max": 52.69
  },
  "work_time_s": {
    "p50": 1.18,
    "p95": 1.24,
    "max": 1.28
  },
  "webhooks_per_s": 77.9,
  "reviews_per_min": 76.2,
  "reviews": 70,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 7.2,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 210,
    "agent 200": 70
  },
  "memory_peak_mib": {
    "worker-1": 68.7,
    "api-1": 66.8,
    "postgres-1": 52.6,
    "redis-1": 4.8,
    "fakes-1": 47.4
  }
}
```

### L2

- Config: `{"scenario": "mixed", "webhooks": 250, "seed": 1, "concurrency": 20, "rate": null, "fakes": {"groq_latency": 0.5, "agent_latency": 0.5}}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `29c0b6d`
- Settled before the timeout: True

```json
{
  "webhooks": 250,
  "enqueue_latency_ms": {
    "p50": 141.3,
    "p95": 377.6,
    "max": 641.3
  },
  "review_time_s": {
    "p50": 72.03,
    "p95": 131.62,
    "max": 138.92
  },
  "queue_wait_s": {
    "p50": 70.84,
    "p95": 130.46,
    "max": 137.79
  },
  "work_time_s": {
    "p50": 1.14,
    "p95": 1.19,
    "max": 1.32
  },
  "webhooks_per_s": 66.7,
  "reviews_per_min": 76.1,
  "reviews": 181,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 7.3,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 543,
    "agent 200": 181
  },
  "memory_peak_mib": {
    "api-1": 67.0,
    "worker-1": 72.3,
    "postgres-1": 52.9,
    "redis-1": 4.2,
    "fakes-1": 40.3
  }
}
```

### L3

- Config: `{"scenario": "mixed", "webhooks": 500, "seed": 1, "concurrency": 20, "rate": null, "fakes": {"groq_latency": 0.5, "agent_latency": 0.5}}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `29c0b6d`
- Settled before the timeout: True

```json
{
  "webhooks": 500,
  "enqueue_latency_ms": {
    "p50": 137.2,
    "p95": 375.2,
    "max": 710.8
  },
  "review_time_s": {
    "p50": 139.68,
    "p95": 259.21,
    "max": 271.93
  },
  "queue_wait_s": {
    "p50": 138.58,
    "p95": 258.06,
    "max": 270.78
  },
  "work_time_s": {
    "p50": 1.13,
    "p95": 1.18,
    "max": 1.24
  },
  "webhooks_per_s": 68.6,
  "reviews_per_min": 75.6,
  "reviews": 352,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 7.3,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 1059,
    "agent 200": 353
  },
  "memory_peak_mib": {
    "api-1": 67.1,
    "worker-1": 72.8,
    "postgres-1": 53.0,
    "redis-1": 8.3,
    "fakes-1": 40.4
  }
}
```

### L4

- Config: `{"scenario": "mixed", "webhooks": 500, "seed": 1, "concurrency": 20, "rate": 10.0, "fakes": {"groq_latency": 0.5, "agent_latency": 0.5}}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `29c0b6d`
- Settled before the timeout: True

```json
{
  "webhooks": 500,
  "enqueue_latency_ms": {
    "p50": 50.8,
    "p95": 56.2,
    "max": 61.5
  },
  "review_time_s": {
    "p50": 119.18,
    "p95": 222.35,
    "max": 232.98
  },
  "queue_wait_s": {
    "p50": 118.08,
    "p95": 221.24,
    "max": 231.86
  },
  "work_time_s": {
    "p50": 1.12,
    "p95": 1.18,
    "max": 1.25
  },
  "webhooks_per_s": 10.0,
  "reviews_per_min": 75.7,
  "reviews": 357,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 7.2,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 1074,
    "agent 200": 358
  },
  "memory_peak_mib": {
    "worker-1": 72.2,
    "api-1": 66.7,
    "postgres-1": 52.4,
    "redis-1": 4.8,
    "fakes-1": 40.2
  }
}
```

### L5

- Config: `{"scenario": "mixed", "webhooks": 500, "seed": 1, "concurrency": 20, "rate": null, "fakes": {"groq_latency": 0.5, "agent_latency": 0.5}}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 2}`, git `29c0b6d`
- Settled before the timeout: True

```json
{
  "webhooks": 500,
  "enqueue_latency_ms": {
    "p50": 153.4,
    "p95": 396.3,
    "max": 661.4
  },
  "review_time_s": {
    "p50": 68.31,
    "p95": 126.41,
    "max": 132.45
  },
  "queue_wait_s": {
    "p50": 67.14,
    "p95": 125.2,
    "max": 131.36
  },
  "work_time_s": {
    "p50": 1.13,
    "p95": 1.2,
    "max": 1.26
  },
  "webhooks_per_s": 67.6,
  "reviews_per_min": 151.4,
  "reviews": 353,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 7.2,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 1059,
    "agent 200": 353
  },
  "memory_peak_mib": {
    "worker-2": 69.8,
    "worker-1": 69.2,
    "api-1": 67.1,
    "postgres-1": 65.3,
    "redis-1": 5.3,
    "fakes-1": 40.5
  }
}
```

### G1-10pct

- Config: `{"scenario": "failure", "test": "G1", "prs": null, "fault_rate": 0.1, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `29c0b6d`
- Settled before the timeout: True
- Expected: Any 5xx restarts the whole job (5 tries). At 10% about 1% of runs may end `failed`; retries re-run the agent.

- +0 s: fakes config: {"groq_latency": 0.5, "agent_latency": 0.5, "github_fault": {"status": 502, "rate": 0.1}}
- +1 s: sent 30 webhooks: {202: 30}
- +82 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 30
  },
  "failure_errors": {}
}
```

```json
{
  "webhooks": 30,
  "enqueue_latency_ms": {
    "p50": 161.0,
    "p95": 185.4,
    "max": 197.5
  },
  "review_time_s": {
    "p50": 14.97,
    "p95": 73.33,
    "max": 79.84
  },
  "queue_wait_s": {
    "p50": 11.91,
    "p95": 29.63,
    "max": 31.67
  },
  "work_time_s": {
    "p50": 1.13,
    "p95": 43.7,
    "max": 57.25
  },
  "webhooks_per_s": 81.1,
  "reviews_per_min": 22.5,
  "reviews": 30,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 9.6,
  "agent_calls_per_review": 1.23,
  "github_errors": {
    "502": 24
  },
  "groq_calls": {
    "lane 200": 90,
    "agent 200": 37
  },
  "memory_peak_mib": {
    "api-1": 66.8,
    "worker-1": 67.1,
    "postgres-1": 51.9,
    "redis-1": 4.6,
    "fakes-1": 39.9
  }
}
```

### G1-2pct

- Config: `{"scenario": "failure", "test": "G1", "prs": null, "fault_rate": 0.02, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `29c0b6d`
- Settled before the timeout: True
- Expected: Any 5xx restarts the whole job (5 tries). At 10% about 1% of runs may end `failed`; retries re-run the agent.

- +0 s: fakes config: {"groq_latency": 0.5, "agent_latency": 0.5, "github_fault": {"status": 502, "rate": 0.02}}
- +1 s: sent 30 webhooks: {202: 30}
- +33 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 30
  },
  "failure_errors": {}
}
```

```json
{
  "webhooks": 30,
  "enqueue_latency_ms": {
    "p50": 204.1,
    "p95": 252.3,
    "max": 270.6
  },
  "review_time_s": {
    "p50": 11.96,
    "p95": 22.12,
    "max": 32.55
  },
  "queue_wait_s": {
    "p50": 10.82,
    "p95": 20.97,
    "max": 31.4
  },
  "work_time_s": {
    "p50": 1.14,
    "p95": 1.17,
    "max": 1.17
  },
  "webhooks_per_s": 56.5,
  "reviews_per_min": 55.1,
  "reviews": 30,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 7.5,
  "agent_calls_per_review": 1.0,
  "github_errors": {
    "502": 3
  },
  "groq_calls": {
    "lane 200": 90,
    "agent 200": 30
  },
  "memory_peak_mib": {
    "api-1": 66.8,
    "worker-1": 67.1,
    "postgres-1": 48.9,
    "redis-1": 4.0,
    "fakes-1": 39.9
  }
}
```

### G2-180s-fixed

- Config: `{"scenario": "failure", "test": "G2", "prs": null, "fault_rate": null, "outage_s": 180.0}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `6d9eb27`
- Settled before the timeout: True
- Expected: Likely gap: the 5 tries are used up in ~40–75 s, so jobs caught in a 180 s outage end `failed`, and the reconciler doesn't retry failed runs.

- +0 s: fakes config: {"groq_latency": 2.0, "agent_latency": 2.0, "github_fault": {"status": 503, "rate": 1.0, "start": 1793798455.8136752, "end": 1793798635.8136752}}
- +0 s: GitHub answers 503 to everything from +10 s, for 180 s
- +0 s: sent 20 webhooks: {202: 20}
- +591 s: done: every run completed or out of retries

```json
{
  "run_statuses": {
    "completed": 20
  },
  "failure_errors": {}
}
```

```json
{
  "webhooks": 20,
  "enqueue_latency_ms": {
    "p50": 103.2,
    "p95": 127.3,
    "max": 146.2
  },
  "review_time_s": {
    "p50": 566.28,
    "p95": 588.77,
    "max": 588.82
  },
  "queue_wait_s": {
    "p50": 562.09,
    "p95": 584.61,
    "max": 584.67
  },
  "work_time_s": {
    "p50": 4.17,
    "p95": 552.59,
    "max": 557.14
  },
  "webhooks_per_s": 145.6,
  "reviews_per_min": 2.0,
  "reviews": 20,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 13.7,
  "agent_calls_per_review": 1.1,
  "github_errors": {
    "503": 82
  },
  "groq_calls": {
    "lane 200": 60,
    "agent 200": 22
  },
  "memory_peak_mib": {
    "api-1": 67.3,
    "worker-1": 67.8,
    "postgres-1": 52.9,
    "redis-1": 4.4,
    "fakes-1": 39.8
  }
}
```

### G2-180s

- Config: `{"scenario": "failure", "test": "G2", "prs": null, "fault_rate": null, "outage_s": 180.0}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `29c0b6d`
- Settled before the timeout: True
- Expected: Likely gap: the 5 tries are used up in ~40–75 s, so jobs caught in a 180 s outage end `failed`, and the reconciler doesn't retry failed runs.

- +0 s: fakes config: {"groq_latency": 2.0, "agent_latency": 2.0, "github_fault": {"status": 503, "rate": 1.0, "start": 1793791532.0759096, "end": 1793791712.0759096}}
- +0 s: GitHub answers 503 to everything from +10 s, for 180 s
- +0 s: sent 20 webhooks: {202: 20}
- +84 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "failed": 16,
    "completed": 4
  },
  "failure_errors": {
    "gave up after 5 tries: GitHub POST /app/installations/5/access_tokens -> 503: {\"": 3,
    "gave up after 5 tries: GitHub GET /repos/load-org-2/repo-1/pulls/2 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-4/repo-4/pulls/2 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-1/repo-4/pulls/1 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-2/repo-3/pulls/1 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-4/repo-4/pulls/3 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub POST /app/installations/3/access_tokens -> 503: {\"": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-2/repo-4/pulls/1 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-4/repo-2/pulls/2 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-1/repo-1/pulls/1 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-4/repo-2/pulls/1 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-4/repo-4/pulls/4 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-1/repo-4/pulls/2 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-1/repo-4/pulls/3 -> 503: {\"mes": 1
  }
}
```

```json
{
  "webhooks": 20,
  "enqueue_latency_ms": {
    "p50": 120.3,
    "p95": 167.2,
    "max": 167.9
  },
  "review_time_s": {
    "p50": 4.84,
    "p95": 9.23,
    "max": 9.23
  },
  "queue_wait_s": {
    "p50": 0.72,
    "p95": 5.1,
    "max": 5.1
  },
  "work_time_s": {
    "p50": 4.14,
    "p95": 4.22,
    "max": 4.22
  },
  "webhooks_per_s": 94.9,
  "reviews_per_min": 25.7,
  "reviews": 4,
  "duplicates": 0,
  "lost": 16,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 30.8,
  "agent_calls_per_review": 1.5,
  "github_errors": {
    "503": 82
  },
  "groq_calls": {
    "lane 200": 18,
    "agent 200": 6
  },
  "memory_peak_mib": {
    "worker-1": 66.9,
    "api-1": 66.7,
    "postgres-1": 45.0,
    "redis-1": 4.1,
    "fakes-1": 39.6
  }
}
```

### G2-60s-final

- Config: `{"scenario": "failure", "test": "G2", "prs": null, "fault_rate": null, "outage_s": 60.0}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `08d73c5`
- Settled before the timeout: True
- Expected: Likely gap: the 5 tries are used up in ~40–75 s, so jobs caught in a 180 s outage end `failed`, and the reconciler doesn't retry failed runs.

- +0 s: fakes config: {"groq_latency": 2.0, "agent_latency": 2.0, "github_fault": {"status": 503, "rate": 1.0, "start": 1793896826.8380384, "end": 1793896886.8380384}}
- +0 s: GitHub answers 503 to everything from +10 s, for 60 s
- +1 s: sent 20 webhooks: {202: 20}
- +608 s: done: every run completed or out of retries

```json
{
  "run_statuses": {
    "completed": 20
  },
  "failure_errors": {}
}
```

```json
{
  "webhooks": 20,
  "enqueue_latency_ms": {
    "p50": 193.1,
    "p95": 273.3,
    "max": 281.5
  },
  "review_time_s": {
    "p50": 90.26,
    "p95": 605.37,
    "max": 605.46
  },
  "queue_wait_s": {
    "p50": 81.73,
    "p95": 600.93,
    "max": 600.97
  },
  "work_time_s": {
    "p50": 4.36,
    "p95": 585.18,
    "max": 590.0
  },
  "webhooks_per_s": 63.1,
  "reviews_per_min": 2.0,
  "reviews": 20,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 13.8,
  "agent_calls_per_review": 1.1,
  "github_errors": {
    "503": 82
  },
  "groq_calls": {
    "lane 200": 60,
    "agent 200": 22
  },
  "memory_peak_mib": {
    "worker-1": 67.4,
    "api-1": 66.7,
    "postgres-1": 54.2,
    "redis-1": 7.3,
    "fakes-1": 39.8
  }
}
```

### G2-60s-fixed

- Config: `{"scenario": "failure", "test": "G2", "prs": null, "fault_rate": null, "outage_s": 60.0}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `6d9eb27`
- Settled before the timeout: True
- Expected: Likely gap: the 5 tries are used up in ~40–75 s, so jobs caught in a 180 s outage end `failed`, and the reconciler doesn't retry failed runs.

- +0 s: fakes config: {"groq_latency": 2.0, "agent_latency": 2.0, "github_fault": {"status": 503, "rate": 1.0, "start": 1793798000.520968, "end": 1793798060.520968}}
- +0 s: GitHub answers 503 to everything from +10 s, for 60 s
- +0 s: sent 20 webhooks: {202: 20}
- +421 s: done: every run completed or out of retries

```json
{
  "run_statuses": {
    "completed": 20
  },
  "failure_errors": {}
}
```

```json
{
  "webhooks": 20,
  "enqueue_latency_ms": {
    "p50": 129.2,
    "p95": 156.1,
    "max": 173.7
  },
  "review_time_s": {
    "p50": 83.18,
    "p95": 414.0,
    "max": 418.53
  },
  "queue_wait_s": {
    "p50": 77.01,
    "p95": 409.84,
    "max": 414.41
  },
  "work_time_s": {
    "p50": 4.14,
    "p95": 62.75,
    "max": 74.28
  },
  "webhooks_per_s": 122.1,
  "reviews_per_min": 2.9,
  "reviews": 20,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 13.0,
  "agent_calls_per_review": 1.1,
  "github_errors": {
    "503": 69
  },
  "groq_calls": {
    "lane 200": 60,
    "agent 200": 22
  },
  "memory_peak_mib": {
    "api-1": 66.8,
    "worker-1": 67.5,
    "postgres-1": 51.2,
    "redis-1": 7.9,
    "fakes-1": 39.8
  }
}
```

### G2-60s

- Config: `{"scenario": "failure", "test": "G2", "prs": null, "fault_rate": null, "outage_s": 60.0}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `29c0b6d`
- Settled before the timeout: True
- Expected: Likely gap: the 5 tries are used up in ~40–75 s, so jobs caught in a 180 s outage end `failed`, and the reconciler doesn't retry failed runs.

- +0 s: fakes config: {"groq_latency": 2.0, "agent_latency": 2.0, "github_fault": {"status": 503, "rate": 1.0, "start": 1793791405.280387, "end": 1793791465.280387}}
- +0 s: GitHub answers 503 to everything from +10 s, for 60 s
- +0 s: sent 20 webhooks: {202: 20}
- +90 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 11,
    "failed": 9
  },
  "failure_errors": {
    "gave up after 5 tries: GitHub GET /repos/load-org-1/repo-1/pulls/1 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-1/repo-4/pulls/2 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-2/repo-3/pulls/1 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub POST /app/installations/5/access_tokens -> 503: {\"": 3,
    "gave up after 5 tries: GitHub GET /repos/load-org-1/repo-2/pulls/1 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-1/repo-4/pulls/3 -> 503: {\"mes": 1,
    "gave up after 5 tries: GitHub GET /repos/load-org-4/repo-2/pulls/2 -> 503: {\"mes": 1
  }
}
```

```json
{
  "webhooks": 20,
  "enqueue_latency_ms": {
    "p50": 126.8,
    "p95": 152.9,
    "max": 159.6
  },
  "review_time_s": {
    "p50": 76.65,
    "p95": 89.74,
    "max": 89.74
  },
  "queue_wait_s": {
    "p50": 72.52,
    "p95": 85.63,
    "max": 85.63
  },
  "work_time_s": {
    "p50": 4.14,
    "p95": 4.29,
    "max": 4.29
  },
  "webhooks_per_s": 116.6,
  "reviews_per_min": 7.3,
  "reviews": 11,
  "duplicates": 0,
  "lost": 9,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 15.9,
  "agent_calls_per_review": 1.0,
  "github_errors": {
    "503": 73
  },
  "groq_calls": {
    "lane 200": 33,
    "agent 200": 11
  },
  "memory_peak_mib": {
    "api-1": 67.0,
    "worker-1": 67.2,
    "postgres-1": 50.4,
    "redis-1": 4.6,
    "fakes-1": 39.8
  }
}
```

### G3

- Config: `{"scenario": "failure", "test": "G3", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `29c0b6d`
- Settled before the timeout: True
- Expected: The retry finds the marker: 1 review per PR, and no agent calls repeated.

- +0 s: fakes config: {"groq_latency": 0.5, "agent_latency": 0.5, "review_fail_status": 502}
- +0 s: sent 10 webhooks: {202: 10}
- +13 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 10
  },
  "failure_errors": {}
}
```

```json
{
  "webhooks": 10,
  "enqueue_latency_ms": {
    "p50": 60.7,
    "p95": 76.2,
    "max": 76.2
  },
  "review_time_s": {
    "p50": 4.35,
    "p95": 7.31,
    "max": 7.31
  },
  "queue_wait_s": {
    "p50": 3.16,
    "p95": 6.14,
    "max": 6.14
  },
  "work_time_s": {
    "p50": 1.18,
    "p95": 1.21,
    "max": 1.21
  },
  "webhooks_per_s": 159.3,
  "reviews_per_min": 81.4,
  "reviews": 10,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 11.0,
  "agent_calls_per_review": 1.0,
  "github_errors": {
    "502": 10
  },
  "groq_calls": {
    "lane 200": 30,
    "agent 200": 10
  },
  "memory_peak_mib": {
    "api-1": 66.9,
    "worker-1": 66.8,
    "postgres-1": 46.3,
    "redis-1": 4.1,
    "fakes-1": 39.8
  }
}
```

### G4-1w

- Config: `{"scenario": "failure", "test": "G4", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `29c0b6d`
- Settled before the timeout: True
- Expected: Jobs defer until the window resets (below 50 left), none fail, every PR reviewed.

- +0 s: fakes config: {"groq_latency": 0.5, "agent_latency": 0.5, "github_rate_limit": 200, "github_rate_window": 300}
- +0 s: one installation, 200 GitHub requests per 300 s
- +1 s: sent 40 webhooks: {202: 40}
- +316 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 40
  },
  "failure_errors": {}
}
```

```json
{
  "webhooks": 40,
  "enqueue_latency_ms": {
    "p50": 92.6,
    "p95": 152.5,
    "max": 154.4
  },
  "review_time_s": {
    "p50": 14.91,
    "p95": 313.62,
    "max": 315.01
  },
  "queue_wait_s": {
    "p50": 13.75,
    "p95": 312.25,
    "max": 313.79
  },
  "work_time_s": {
    "p50": 1.17,
    "p95": 1.24,
    "max": 298.42
  },
  "webhooks_per_s": 82.0,
  "reviews_per_min": 7.6,
  "reviews": 40,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 7.4,
  "agent_calls_per_review": 1.05,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 120,
    "agent 200": 42
  },
  "memory_peak_mib": {
    "api-1": 66.8,
    "worker-1": 68.2,
    "postgres-1": 50.6,
    "redis-1": 8.4,
    "fakes-1": 39.8
  }
}
```

### K1

- Config: `{"scenario": "failure", "test": "K1", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 2}`, git `ecce23d`
- Settled before the timeout: True
- Expected: Queued jobs go to the other worker at once. The 2 killed jobs come back when arq's in-progress key expires (~910 s); claim_run takes over the stale run. 1 review each.

- +0 s: fakes config: {"groq_latency": 12.0, "agent_latency": 12.0}
- +0 s: sent 10 webhooks: {202: 10}
- +1 s: SIGKILL pr-review-load-worker-2 with 2 jobs in flight; it stays down
- +936 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 10
  },
  "failure_errors": {},
  "affected": [
    "20a4afbf7ca168273c561af717d4bee5e28c7b12",
    "70da2e051361d4c1ee95f9c9020f75952ed6bd38"
  ],
  "recovery_s": 933.5
}
```

```json
{
  "webhooks": 10,
  "enqueue_latency_ms": {
    "p50": 54.2,
    "p95": 71.3,
    "max": 71.3
  },
  "review_time_s": {
    "p50": 73.78,
    "p95": 934.52,
    "max": 934.52
  },
  "queue_wait_s": {
    "p50": 25.06,
    "p95": 74.12,
    "max": 74.12
  },
  "work_time_s": {
    "p50": 24.2,
    "p95": 934.1,
    "max": 934.1
  },
  "webhooks_per_s": 221.5,
  "reviews_per_min": 0.6,
  "reviews": 10,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 12.7,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 36,
    "agent 200": 10
  },
  "memory_peak_mib": {
    "api-1": 66.7,
    "worker-1": 66.6,
    "postgres-1": 53.7,
    "redis-1": 8.0,
    "fakes-1": 39.8
  }
}
```

### K1b

- Config: `{"scenario": "failure", "test": "K1b", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `ecce23d`
- Settled before the timeout: True
- Expected: Same ~15 min with the worker restarted at once: the startup reconcile leaves a fresh `running` run alone.

- +0 s: fakes config: {"groq_latency": 12.0, "agent_latency": 12.0}
- +0 s: sent 10 webhooks: {202: 10}
- +2 s: SIGKILL pr-review-load-worker-1 with 2 jobs in flight
- +2 s: started pr-review-load-worker-1 again
- +936 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 10
  },
  "failure_errors": {},
  "affected": [
    "cd34bb59b6ad0562210a4a7900556fe22131c469",
    "36121fa3cc0be3be90a950fd1392ff5406dbae93"
  ],
  "recovery_s": 933.4
}
```

```json
{
  "webhooks": 10,
  "enqueue_latency_ms": {
    "p50": 58.8,
    "p95": 91.0,
    "max": 91.0
  },
  "review_time_s": {
    "p50": 76.22,
    "p95": 934.88,
    "max": 934.88
  },
  "queue_wait_s": {
    "p50": 27.51,
    "p95": 76.6,
    "max": 76.6
  },
  "work_time_s": {
    "p50": 24.19,
    "p95": 934.27,
    "max": 934.27
  },
  "webhooks_per_s": 219.7,
  "reviews_per_min": 0.6,
  "reviews": 10,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 14.1,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 36,
    "agent 200": 10
  },
  "memory_peak_mib": {
    "api-1": 66.7,
    "postgres-1": 53.2,
    "redis-1": 8.0,
    "fakes-1": 39.8,
    "worker-1": 66.9
  }
}
```

### K2-fixed

- Config: `{"scenario": "failure", "test": "K2", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `6d9eb27`
- Settled before the timeout: True
- Expected: Likely gap: arq re-queues the cancelled jobs at once, but their runs stay `running`, so the re-runs back off. Recovery only via the reconciler: 900 s + up to 5 min.

- +0 s: fakes config: {"groq_latency": 12.0, "agent_latency": 12.0}
- +0 s: sent 10 webhooks: {202: 10}
- +1 s: docker stop pr-review-load-worker-1 (SIGTERM, 10 s grace) with 2 jobs in flight
- +2 s: stopped; the in-flight runs are now {'176cd2e5a6c971f2db7cf315f859d426c3b5e835': 'queued', '154f8d99e345ceba8c652f17fe9cd8e736900776': 'queued'}
- +2 s: started pr-review-load-worker-1 again
- +126 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 10
  },
  "failure_errors": {},
  "affected": [
    "176cd2e5a6c971f2db7cf315f859d426c3b5e835",
    "154f8d99e345ceba8c652f17fe9cd8e736900776"
  ],
  "statuses_after_stop": {
    "176cd2e5a6c971f2db7cf315f859d426c3b5e835": "queued",
    "154f8d99e345ceba8c652f17fe9cd8e736900776": "queued"
  },
  "recovery_s": 26.6
}
```

```json
{
  "webhooks": 10,
  "enqueue_latency_ms": {
    "p50": 60.3,
    "p95": 82.2,
    "max": 82.2
  },
  "review_time_s": {
    "p50": 76.66,
    "p95": 125.82,
    "max": 125.82
  },
  "queue_wait_s": {
    "p50": 52.48,
    "p95": 101.59,
    "max": 101.59
  },
  "work_time_s": {
    "p50": 24.21,
    "p95": 27.19,
    "max": 27.19
  },
  "webhooks_per_s": 208.1,
  "reviews_per_min": 4.8,
  "reviews": 10,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 10.2,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 36,
    "agent 200": 10
  },
  "memory_peak_mib": {
    "api-1": 66.7,
    "postgres-1": 49.4,
    "redis-1": 4.1,
    "fakes-1": 39.8,
    "worker-1": 66.5
  }
}
```

### K2

- Config: `{"scenario": "failure", "test": "K2", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `ecce23d`
- Settled before the timeout: True
- Expected: Likely gap: arq re-queues the cancelled jobs at once, but their runs stay `running`, so the re-runs back off. Recovery only via the reconciler: 900 s + up to 5 min.

- +0 s: fakes config: {"groq_latency": 12.0, "agent_latency": 12.0}
- +0 s: sent 10 webhooks: {202: 10}
- +1 s: docker stop pr-review-load-worker-1 (SIGTERM, 10 s grace) with 2 jobs in flight
- +2 s: stopped; the in-flight runs are now {'970f06b1bce950be423102610773414dd31893ac': 'running', '957aea5486f838664256301fd59a0f747c6f7667': 'running'}
- +2 s: started pr-review-load-worker-1 again
- +1038 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 10
  },
  "failure_errors": {},
  "affected": [
    "970f06b1bce950be423102610773414dd31893ac",
    "957aea5486f838664256301fd59a0f747c6f7667"
  ],
  "statuses_after_stop": {
    "970f06b1bce950be423102610773414dd31893ac": "running",
    "957aea5486f838664256301fd59a0f747c6f7667": "running"
  },
  "recovery_s": 1035.2
}
```

```json
{
  "webhooks": 10,
  "enqueue_latency_ms": {
    "p50": 61.0,
    "p95": 71.2,
    "max": 71.2
  },
  "review_time_s": {
    "p50": 76.87,
    "p95": 1035.93,
    "max": 1035.93
  },
  "queue_wait_s": {
    "p50": 28.1,
    "p95": 77.22,
    "max": 77.22
  },
  "work_time_s": {
    "p50": 24.19,
    "p95": 1035.56,
    "max": 1035.56
  },
  "webhooks_per_s": 212.0,
  "reviews_per_min": 0.6,
  "reviews": 10,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 15.4,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 36,
    "agent 200": 10
  },
  "memory_peak_mib": {
    "api-1": 66.7,
    "postgres-1": 53.5,
    "redis-1": 8.2,
    "fakes-1": 39.8,
    "worker-1": 66.8
  }
}
```

### K3

- Config: `{"scenario": "failure", "test": "K3", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `ecce23d`
- Settled before the timeout: True
- Expected: The re-run (~910 s later) finds the run marker and posts nothing: 1 review, and the run completed.

- +0 s: fakes config: {"review_hold_seconds": 30}
- +0 s: sent 1 webhooks: {202: 1}
- +2 s: done: the review is stored and its answer held
- +3 s: SIGKILL pr-review-load-worker-1 after GitHub stored the review, before it answered
- +3 s: fakes config: {}
- +3 s: started pr-review-load-worker-1 again
- +911 s: done: the run completed

```json
{
  "run_statuses": {
    "completed": 1
  },
  "failure_errors": {},
  "affected": [
    "318f9dc20cfb0711400f027c9f4c0926d1d19537"
  ],
  "reviews_of_affected": 1,
  "recovery_s": 907.9
}
```

```json
{
  "webhooks": 1,
  "enqueue_latency_ms": {
    "p50": 50.6,
    "p95": 50.6,
    "max": 50.6
  },
  "review_time_s": {
    "p50": 0.44,
    "p95": 0.44,
    "max": 0.44
  },
  "queue_wait_s": {
    "p50": 0.28,
    "p95": 0.28,
    "max": 0.28
  },
  "work_time_s": {
    "p50": 0.17,
    "p95": 0.17,
    "max": 0.17
  },
  "webhooks_per_s": null,
  "reviews_per_min": 135.1,
  "reviews": 1,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 29.0,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 3,
    "agent 200": 1
  },
  "memory_peak_mib": {
    "worker-1": 65.5,
    "api-1": 66.3,
    "postgres-1": 43.3,
    "redis-1": 8.0,
    "fakes-1": 39.6
  }
}
```

### N1

- Config: `{"scenario": "failure", "test": "N1", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `6d9eb27`
- Settled before the timeout: True
- Expected: arq's queue is FIFO, so B's one PR waits behind all of A's burst still queued when it arrives: far more than max_jobs (2) reviews.

- +0 s: fakes config: {"groq_latency": 0.5, "agent_latency": 0.5}
- +0 s: sent 1 webhooks: {202: 1}
- +2 s: done: B's PR reviewed on an empty queue
- +4 s: sent 100 webhooks: {202: 100}
- +12 s: sent 1 webhooks: {202: 1}
- +81 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 102
  },
  "failure_errors": {},
  "b_alone_s": 1.79,
  "b_behind_burst_s": 66.98,
  "b_waited_behind_reviews": 88,
  "limit_rule_triggered": true
}
```

```json
{
  "webhooks": 102,
  "enqueue_latency_ms": {
    "p50": 99.8,
    "p95": 173.8,
    "max": 265.5
  },
  "review_time_s": {
    "p50": 37.22,
    "p95": 71.23,
    "max": 74.2
  },
  "queue_wait_s": {
    "p50": 36.06,
    "p95": 70.06,
    "max": 73.05
  },
  "work_time_s": {
    "p50": 1.15,
    "p95": 1.18,
    "max": 1.21
  },
  "webhooks_per_s": 8.3,
  "reviews_per_min": 77.1,
  "reviews": 102,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 7.1,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 306,
    "agent 200": 102
  },
  "memory_peak_mib": {
    "api-1": 69.0,
    "worker-1": 80.7,
    "postgres-1": 54.9,
    "redis-1": 4.9,
    "fakes-1": 51.1
  }
}
```

### Q1-fixed

- Config: `{"scenario": "failure", "test": "Q1", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `08d73c5`
- Settled before the timeout: True
- Expected: Every review posted with its comment: since Phase 6c fix 3, 429s are waited out until the deadline instead of failing the call after 3 tries.

- +0 s: fakes config: {"groq_latency": 0.5, "agent_latency": 0.5, "groq_fault": {"status": 429, "rate": 0.3, "retry_after": 2}}
- +1 s: sent 30 webhooks: {202: 30}
- +57 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 30
  },
  "failure_errors": {}
}
```

```json
{
  "webhooks": 30,
  "enqueue_latency_ms": {
    "p50": 283.9,
    "p95": 356.4,
    "max": 383.4
  },
  "review_time_s": {
    "p50": 31.17,
    "p95": 53.22,
    "max": 54.72
  },
  "queue_wait_s": {
    "p50": 27.22,
    "p95": 47.44,
    "max": 49.97
  },
  "work_time_s": {
    "p50": 3.26,
    "p95": 7.38,
    "max": 7.4
  },
  "webhooks_per_s": 41.0,
  "reviews_per_min": 32.5,
  "reviews": 30,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 7.2,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 429": 29,
    "lane 200": 90,
    "agent 429": 8,
    "agent 200": 30
  },
  "memory_peak_mib": {
    "worker-1": 67.2,
    "api-1": 66.7,
    "postgres-1": 53.4,
    "redis-1": 4.5,
    "fakes-1": 40.0
  }
}
```

### Q1

- Config: `{"scenario": "failure", "test": "Q1", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `29c0b6d`
- Settled before the timeout: True
- Expected: Every review posted. A call fails only after 3 straight 429s (~2.7% of calls).

- +0 s: fakes config: {"groq_latency": 0.5, "agent_latency": 0.5, "groq_fault": {"status": 429, "rate": 0.3, "retry_after": 2}}
- +1 s: sent 30 webhooks: {202: 30}
- +69 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 30
  },
  "failure_errors": {}
}
```

```json
{
  "webhooks": 30,
  "enqueue_latency_ms": {
    "p50": 130.1,
    "p95": 154.3,
    "max": 162.1
  },
  "review_time_s": {
    "p50": 32.73,
    "p95": 65.09,
    "max": 67.12
  },
  "queue_wait_s": {
    "p50": 27.61,
    "p95": 63.94,
    "max": 63.95
  },
  "work_time_s": {
    "p50": 3.17,
    "p95": 7.13,
    "max": 8.65
  },
  "webhooks_per_s": 87.6,
  "reviews_per_min": 26.7,
  "reviews": 30,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 2,
  "unexpected_statuses": {},
  "github_requests_per_review": 7.9,
  "agent_calls_per_review": 0.93,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 86,
    "agent 200": 28,
    "lane 429": 47,
    "agent 429": 9
  },
  "memory_peak_mib": {
    "worker-1": 67.1,
    "api-1": 66.8,
    "postgres-1": 49.8,
    "redis-1": 4.0,
    "fakes-1": 39.8
  }
}
```

### Q2-fixed

- Config: `{"scenario": "failure", "test": "Q2", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `08d73c5`
- Settled before the timeout: True
- Expected: Lane calls wait out the 120 s storm (fix 3), so all 10 reviews post with their comment. A call that still fails makes its run fail as transient, for the reconciler to retry.

- +0 s: fakes config: {"groq_latency": 0.5, "agent_latency": 0.5, "groq_fault": {"status": 429, "rate": 1.0, "retry_after": 30, "start": 1793895856.18228, "end": 1793895976.18228}}
- +0 s: every Groq call gets 429 for the next 120 s
- +0 s: sent 10 webhooks: {202: 10}
- +132 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 10
  },
  "failure_errors": {}
}
```

```json
{
  "webhooks": 10,
  "enqueue_latency_ms": {
    "p50": 110.2,
    "p95": 142.1,
    "max": 142.1
  },
  "review_time_s": {
    "p50": 125.7,
    "p95": 129.37,
    "max": 129.37
  },
  "queue_wait_s": {
    "p50": 124.34,
    "p95": 127.9,
    "max": 127.9
  },
  "work_time_s": {
    "p50": 1.36,
    "p95": 121.58,
    "max": 121.58
  },
  "webhooks_per_s": 66.9,
  "reviews_per_min": 4.6,
  "reviews": 10,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 8.9,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 429": 24,
    "lane 200": 30,
    "agent 200": 10
  },
  "memory_peak_mib": {
    "worker-1": 66.7,
    "api-1": 66.6,
    "postgres-1": 48.6,
    "redis-1": 4.3,
    "fakes-1": 39.5
  }
}
```

### Q2

- Config: `{"scenario": "failure", "test": "Q2", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `29c0b6d`
- Settled before the timeout: True
- Expected: Reviews still post within the 300 s lane deadline, listing skipped chunks if it hits. No run fails.

- +0 s: fakes config: {"groq_latency": 0.5, "agent_latency": 0.5, "groq_fault": {"status": 429, "rate": 1.0, "retry_after": 30, "start": 1793791045.8525567, "end": 1793791165.8525567}}
- +0 s: every Groq call gets 429 for the next 120 s
- +0 s: sent 10 webhooks: {202: 10}
- +127 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 10
  },
  "failure_errors": {}
}
```

```json
{
  "webhooks": 10,
  "enqueue_latency_ms": {
    "p50": 60.9,
    "p95": 79.9,
    "max": 79.9
  },
  "review_time_s": {
    "p50": 123.53,
    "p95": 126.51,
    "max": 126.51
  },
  "queue_wait_s": {
    "p50": 122.38,
    "p95": 125.36,
    "max": 125.36
  },
  "work_time_s": {
    "p50": 1.17,
    "p95": 90.13,
    "max": 90.13
  },
  "webhooks_per_s": 167.4,
  "reviews_per_min": 4.7,
  "reviews": 10,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 2,
  "unexpected_statuses": {},
  "github_requests_per_review": 7.5,
  "agent_calls_per_review": 0.8,
  "github_errors": {},
  "groq_calls": {
    "lane 429": 24,
    "lane 200": 24,
    "agent 200": 8
  },
  "memory_peak_mib": {
    "worker-1": 66.9,
    "api-1": 66.7,
    "postgres-1": 48.5,
    "redis-1": 4.7,
    "fakes-1": 39.4
  }
}
```

### Q3-1w

- Config: `{"scenario": "failure", "test": "Q3", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `ecce23d`
- Settled before the timeout: True
- Expected: With 1 worker ~0 429s (the client waits on Groq's headers); with 2 (--workers 2) each keeps its own limiter, so 429s may appear.

- +0 s: fakes config: {"groq_latency": 0.5, "agent_latency": 0.5, "groq_tpm": 8000}
- +0 s: sent 10 webhooks: {202: 10}
- +306 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 10
  },
  "failure_errors": {}
}
```

```json
{
  "webhooks": 10,
  "enqueue_latency_ms": {
    "p50": 66.9,
    "p95": 95.3,
    "max": 95.3
  },
  "review_time_s": {
    "p50": 122.39,
    "p95": 303.63,
    "max": 303.63
  },
  "queue_wait_s": {
    "p50": 62.26,
    "p95": 242.97,
    "max": 242.97
  },
  "work_time_s": {
    "p50": 60.13,
    "p95": 120.13,
    "max": 120.13
  },
  "webhooks_per_s": 168.4,
  "reviews_per_min": 2.0,
  "reviews": 10,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 8.9,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 30,
    "agent 429": 2,
    "agent 200": 10,
    "lane 429": 6
  },
  "memory_peak_mib": {
    "worker-1": 66.2,
    "api-1": 66.7,
    "postgres-1": 45.9,
    "redis-1": 4.5,
    "fakes-1": 39.5
  }
}
```

### Q3-2w

- Config: `{"scenario": "failure", "test": "Q3", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 2}`, git `ecce23d`
- Settled before the timeout: True
- Expected: With 1 worker ~0 429s (the client waits on Groq's headers); with 2 (--workers 2) each keeps its own limiter, so 429s may appear.

- +0 s: fakes config: {"groq_latency": 0.5, "agent_latency": 0.5, "groq_tpm": 8000}
- +0 s: sent 10 webhooks: {202: 10}
- +303 s: done: every run completed or failed

```json
{
  "run_statuses": {
    "completed": 10
  },
  "failure_errors": {}
}
```

```json
{
  "webhooks": 10,
  "enqueue_latency_ms": {
    "p50": 58.8,
    "p95": 63.5,
    "max": 63.5
  },
  "review_time_s": {
    "p50": 181.72,
    "p95": 302.54,
    "max": 302.54
  },
  "queue_wait_s": {
    "p50": 61.62,
    "p95": 182.42,
    "max": 182.42
  },
  "work_time_s": {
    "p50": 120.11,
    "p95": 180.66,
    "max": 180.66
  },
  "webhooks_per_s": 237.7,
  "reviews_per_min": 2.0,
  "reviews": 10,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {},
  "github_requests_per_review": 10.7,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 429": 13,
    "lane 200": 29,
    "agent 429": 7,
    "agent 200": 10
  },
  "memory_peak_mib": {
    "worker-2": 66.4,
    "worker-1": 66.0,
    "api-1": 66.7,
    "postgres-1": 61.2,
    "redis-1": 4.4,
    "fakes-1": 39.9
  }
}
```

### R1

- Config: `{"scenario": "failure", "test": "R1", "prs": null, "fault_rate": null, "outage_s": null}`
- Environment: `{"platform": "Windows-11-10.0.26300-SP0", "cpus": 16, "workers": 1}`, git `ecce23d`
- Settled before the timeout: True
- Expected: 503s while Redis is down; queued jobs and delivery IDs lost; the reconciler re-enqueues every lost PR within ~5 min. R2: replays accepted (202) but 0 new reviews.

- +0 s: fakes config: {"groq_latency": 2.0, "agent_latency": 2.0}
- +1 s: sent 60 webhooks: {202: 60}
- +15 s: done: 5 reviews posted, the rest queued
- +15 s: restarting Redis, and sending 20 more webhooks meanwhile
- +16 s: sent 20 webhooks: {503: 1, 202: 19}
- +16 s: done: Redis answers again
- +16 s: Redis is back
- +17 s: 74 PRs not reviewed yet
- +235 s: done: every run completed or failed
- +236 s: replayed all 80 deliveries

```json
{
  "run_statuses": {
    "completed": 80
  },
  "failure_errors": {},
  "affected": [
    "a9f2670497df09f76b63884d1b91a3d8eea5b122",
    "a91be9cc7e61955030fe273e2ed736759d050e38",
    "d8388655a1dba96042ccafd3a22ddd64c0ed3c0b",
    "ea3a9e971cd8109a47b8ff79967143de489ea37d",
    "8cdcf41a4b491a84d52219eef9c0066f5b9aca64",
    "6eddf58e3012a02e65bde33df434b40fe5a78cc8",
    "4a2debaaa847a81b056d3012fbed1c48d354817d",
    "d9c5d2cd4b489046e623b48da4c803497c468612",
    "dd1830546a61833e8a729c0f8627fdcc5e231989",
    "3b0cfe0064b008885927f4d339b84e06e5337dbb",
    "2090bdd51c4ef7093b1ed3249b203a3245287cfd",
    "84322f3f026503908496a5bc7286a37c9f97d547",
    "dc7890a3bede2f5e28dc4bd94d06db7823829ccd",
    "c1a0eb554b8604c651935f192ac89609a9851682",
    "41f76bf2a070049cab3a042060a933e5f8077b5f",
    "248359a91c72582538f145477918bb1c2eb4f0f6",
    "5c677afa74d8344cb371a397fa096da9acb9be0c",
    "56d32ab55afd92c14c26b72deac60cea0b7b590d",
    "93d459d01ec64c7227ad20ed54b12722feb61f9d",
    "fb3cd4002fe84519e2bcce2e83b9a718a2167d9a",
    "82f29844639d79edc3d17bf7feef37ecb8552fe2",
    "1fed34ff74709f2582eec91644407b2a5d8f116d",
    "0d0178a72ba7f3c70265cff8586840a26ff0b71e",
    "2bb443c9fd4643921a5364e9b33f62d1789a844c",
    "78793e8561dff77a7c08ddf72d736a58cfb4a48b",
    "e499bc7d6951d12d690da4f19ff9460c7ef86fde",
    "059fdaa65c762cd1b31b04107b535cbf968ffa8d",
    "962e7dfa641713d7dc0d8c51c2f5730358670785",
    "84ebc81de1f94474c6d583d86933114363a41023",
    "89fdbd44fab63affef1b137878ee702ad0972986",
    "9e053c22b822a2ba1c9e7acb519d547a9fd6c4c7",
    "5f8ceb60bcf1d59491ddcd976e00cc0f551f779c",
    "e9d4b2de99174957024ff664957d4e9fa7e91862",
    "e5712a3ccb538300bc189354f02c4fa958b8bff9",
    "9a7b4416a5fd41ef1230a6c90d58dc00094fe210",
    "233455fdf4928d0a0f259a8f2e7a987f557951f7",
    "ff95c6fee38df26f9e082db5d74500a6823c3a8f",
    "934a4045d6bfb868cd8af1321fb28212508bad2d",
    "6b78b098d0622df789a4d93db6da8469a52f725d",
    "73f5b97da7fae130b6b59c15cfb378ed10a2c634",
    "58dd9b0416d8228242d49526306f627c0302e48e",
    "e416a487b2eb2b11ae6da983b2905e60424331d6",
    "5a46d8bf65f4170438564186f882a5c68b1fc50c",
    "ce13e49c6a2bc836d456b0b62a40a97103e17951",
    "383ea52623c4908b612e04424a06bb6c1f587831",
    "9cce581eb4eff219512fdc78846f6c94a719a149",
    "68d5eced8b84aa9323ba909cbcc45214a9f8c937",
    "3816185a805ed440453948b6a3358dcfd4cdf811",
    "60f25443ae1691d5e426eb7eaa89ead33654c1ad",
    "b141f26b141dbb3864f0fce8a89aa0c793e7220d",
    "7031999df4b691d10a9b52bc06d9b4255557751c",
    "20d00569f1dcabb1fb83b6bddd9419f2a6d31291",
    "477b7b74a3f113f2deeb1e1c58b6a94f01376b3c",
    "9fefdaefd3cfcd5130b85028bea38007a40a9488",
    "60e789a3227f5d86ff2179045c6a3c1a8db4d1d1",
    "2ab498289cae8083170c93c417ac0a349623fb40",
    "d6c287e710f99bd6d26f73a3cde1558ca2ef1308",
    "b45393920eda9c9f92c93e4b7d59b8b05dcbd6d1",
    "f8ac98d011e1fc88e788dcc88032e3d28d2358d7",
    "2075aca90c8a7681c0a136fc858042681b87804f",
    "934c6be52278638c3b7c8f1745b019c7eb82e8e3",
    "99c21fb1cecd59ea5a41bd6749ad81f86e347598",
    "bdca99e1e3f76b143e8ef2822714d85d52d77b16",
    "fdd4a4d3c9bcba78db34cef8b386556a042be078",
    "b082de8b1ae067bf7750fc690eaee32a550199b1",
    "21548be3ccf920e4291eb4b3df4f55ca1a381008",
    "8e21b38a00c8bd7100819e9f216992523bfe450c",
    "a49f2747f445257a2ddb2f77c3f7ef542e337c6c",
    "c97d0fc97bbb21c7af63e0dc2568069bde20dbfc",
    "9db094e3d35d9529d2b848a1410e2f91848005af",
    "6b760ef781070af598513159641902e3bc68d1ea",
    "3957e762abcd4a4bdb81c8e6642fa2045387c0a9",
    "c4c0e2eab7e0aa8590e5c2cf34ada337535fad04",
    "93d0db48cdb31afe6e1ab0444e369eb9c8b35a2c"
  ],
  "recovery_s": 217.6,
  "worker_restarts": 0,
  "replay_statuses": {
    "202": 61,
    "200": 19
  },
  "replay_new_reviews": 0
}
```

```json
{
  "webhooks": 80,
  "enqueue_latency_ms": {
    "p50": 141.0,
    "p95": 1099.5,
    "max": 1160.1
  },
  "review_time_s": {
    "p50": 143.16,
    "p95": 224.61,
    "max": 233.47
  },
  "queue_wait_s": {
    "p50": 139.0,
    "p95": 220.44,
    "max": 229.31
  },
  "work_time_s": {
    "p50": 4.18,
    "p95": 4.24,
    "max": 4.24
  },
  "webhooks_per_s": 5.3,
  "reviews_per_min": 20.5,
  "reviews": 80,
  "duplicates": 0,
  "lost": 0,
  "stale_posts": 0,
  "reviews_without_comment": 0,
  "unexpected_statuses": {
    "opened -> 503": 1
  },
  "github_requests_per_review": 7.4,
  "agent_calls_per_review": 1.0,
  "github_errors": {},
  "groq_calls": {
    "lane 200": 240,
    "agent 200": 80
  },
  "memory_peak_mib": {
    "worker-1": 68.6,
    "api-1": 67.4,
    "postgres-1": 50.6,
    "redis-1": 8.7,
    "fakes-1": 40.0
  }
}
```
