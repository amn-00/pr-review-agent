# Deploying to Render (free tier)

The production setup is one Render free web service that runs the API and the arq worker in one
process, plus Neon Postgres and Render Key Value. Everything is set up by hand in the dashboards.
These steps were followed on 2026-11-05. The first review from the deployed bot is
[pr-agent-playground#6](https://github.com/amn-00/pr-agent-playground/pull/6).

You need the GitHub App from [github-app-setup.md](github-app-setup.md), and a Groq API key.

**Pick one region for everything.** The Key Value's internal URL only works inside one Render
region, and every run claim and cache lookup is a round trip to Postgres. We use Render
**Virginia (US East)** with Neon **AWS US East (N. Virginia), `us-east-1`**.

## 1. Neon Postgres

1. **Create the project.** In [console.neon.tech](https://console.neon.tech), click **New Project**:
   - **Postgres version:** **16**, the same as local dev and CI.
   - **Region:** AWS US East (N. Virginia).
2. **Copy the direct connection string.** Click **Connect**, choose branch `main`, database
   `neondb` and role `neondb_owner`, and **turn Connection pooling off**, so the host has no
   `-pooler`. Click **Show password**, then the copy icon. Selecting the text while the password
   is masked copies the asterisks.
3. **Rewrite it for SQLAlchemy and asyncpg.** Change the scheme, and replace the whole query
   string:
   ```
   Neon:  postgresql://neondb_owner:<pw>@ep-xxxx.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require
   Ours:  postgresql+asyncpg://neondb_owner:<pw>@ep-xxxx.us-east-1.aws.neon.tech/neondb?ssl=verify-full
   ```
   Neon's string as-is crashes at startup: SQLAlchemy passes URL parameters to
   `asyncpg.connect()`, which has no `sslmode` or `channel_binding` arguments (`TypeError`).
   `ssl=verify-full` checks Neon's certificate and hostname. asyncpg needs a CA file for it, so
   `PGSSLROOTCERT` is set in step 3.
4. **Optional check from your machine.** This connects and reads, and changes nothing.
   Copy the rewritten URL first; it's read from the clipboard, so it doesn't land in
   PowerShell's history:
   ```powershell
   $env:DATABASE_URL = (Get-Clipboard -Raw).Trim()
   $env:PGSSLROOTCERT = (uv run python -m certifi)
   uv run alembic current
   Remove-Item Env:DATABASE_URL, Env:PGSSLROOTCERT
   ```
   *Expected:* `Context impl PostgresqlImpl.` and `Will assume transactional DDL.`, with no
   revision and no traceback. `password authentication failed` means the password was copied
   wrong.

Never put this URL in `.env`. That file is for the local Docker stack.

## 2. Render Key Value

**New → Key Value**:
- **Region:** Virginia (US East).
- **Maxmemory policy: `noeviction`.** Not the default `allkeys-lru`, and not `volatile-ttl`,
  which Render's form labels "recommended for queues". arq gives queued jobs and its
  in-progress keys an expiry, so `volatile-ttl` would evict them first when memory is full.
  With `noeviction`, a full Redis refuses writes: the webhook gets a 503, and the reconciler
  picks the PR up later.
- **Compute: $0 / month (Free),** 25 MB, no persistence. The form defaults to a paid plan, and
  compute can't be downgraded later, so check that the bottom bar says $0 before creating.
- **Access control:** no external IP rules.

Copy the **Internal Key Value URL** (`redis://red-…:6379`).

## 3. Render web service

**New → Web Service**, then pick this repo:
- **Language:** Docker (detected from the Dockerfile). **Branch:** `main`.
- **Region:** Virginia (US East). **Instance type:** Free. It also defaults to a paid plan.

**Environment variables.** Paste these with **Add from .env**, taking the four GitHub and Groq
lines from your local `.env` as they are:

| Key | Value |
|---|---|
| `ENVIRONMENT` | `prod` |
| `LOG_LEVEL` | `INFO` |
| `RUN_WORKER_IN_API` | `true` |
| `DATABASE_URL` | the rewritten Neon URL from step 1 |
| `PGSSLROOTCERT` | `/etc/ssl/certs/ca-certificates.crt` (the image's CA bundle) |
| `REDIS_URL` | the internal Key Value URL |
| `GITHUB_APP_ID` | from `.env` |
| `GITHUB_APP_PRIVATE_KEY` | from `.env`; the value must start with `-----BEGIN`, with no `"` |
| `GITHUB_WEBHOOK_SECRET` | from `.env`; it must match the secret on the GitHub App |
| `GROQ_API_KEY` | from `.env` |

Don't set `PORT`, because Render sets it. The rest keep their defaults: the agent on, Semgrep
off.

**Advanced:**
- **Health Check Path:** `/health`. It answers 503 if the in-process worker has stopped.
- **Docker Command,** with **no quotes**:
  ```
  sh -c alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port $PORT
  ```
  - **Why no quotes:** Render passes everything after `sh -c` to `sh` as one argument, and keeps
    quote characters literally. With `sh -c '…'`, the deploy fails with
    `sh: 1: alembic upgrade head && …: not found` and exit status 127.
  - **Why `exec`:** it makes uvicorn PID 1. Render's SIGTERM on a deploy or spin-down then
    reaches uvicorn, and in-flight runs are handed back as `queued`.
  - **Migrations:** they run at every start. A failed migration stops the start, and the old
    deploy keeps serving.
- **Pre-Deploy Command:** leave it empty. It's paid-only.

**Before deploying:** open PRs on installed repos updated in the last 24 hours have no run in the
new database. The startup reconcile reviews them, which posts reviews and uses Groq quota. Stop
any local `api` and `worker` containers too, so two bots don't review the same PR.

*Expected after the deploy:*
- **The runtime log:** 8 `Running upgrade …` lines, `Application startup complete.`, `Starting
  worker for 2 functions: review_pr, cron:reconcile`, then `← cron:reconcile ● N`.
- **`/health`** → `{"status":"ok"}`.
- **`/health/ready`** → `{"redis":"ok","postgres":"ok"}`.
- **`/`** → a redirect to this repo.

## 4. Point the GitHub App at Render

1. **The webhook URL.** In the App's settings → **General → Webhook**, keep **Active** ticked
   and set **Webhook URL** to `https://<service>.onrender.com/webhook`. Leave the secret as it
   is, then click **Save changes**. smee then stops receiving webhooks, so local dev needs a
   second App if it's needed again.
2. **Check it with a real PR.** Open a PR with a planted bug on a repo the App is installed on.
   - **Recent Deliveries** (App → **Advanced**): `pull_request.opened`, response **202**
     `{"status":"queued"}`. smee also answers 2xx, so check the response body, not just the
     green check.
   - **The Render logs:** `review job enqueued`, `job started`, then the review posted.

**First run (PR #6):**
- **The webhook** reached Render 3 s after the PR was opened.
- **The review** was posted 2 min 37 s after it was opened, mostly Groq free-tier waits.
- **The verdicts:** 2 confirmed and 1 dropped. It cost $0.0042 at list prices.

## Operating notes
- **Cold starts:** the service sleeps after 15 minutes without inbound traffic, and takes about
  a minute to wake. GitHub may record the waking webhook as timed out. The startup reconcile
  reviews that PR anyway.
- **A deploy:** every push to `main` redeploys (Auto-Deploy: On Commit). Runs in flight are
  handed back on SIGTERM, and the new instance picks them up.
- **The Groq quota is per account.** The local evals and prod share it, but their daily budget
  counters live in different databases, so don't run evals and prod on the same day.
