# GitHub App setup (local development)

How to get a real PR on a test repo to reach your local stack. Takes about 10 minutes.

GitHub can't reach `localhost`, so we use [smee.io](https://smee.io): GitHub sends webhooks to
a public smee URL, and a small client on your machine forwards them to `localhost:8000/webhook`.
The smee URL is stable, so you configure the App once.

## 1. Create a smee channel

Open https://smee.io and click **Start a new channel**. Copy the URL
(e.g. `https://smee.io/AbC123xyz`). Anyone with the URL can see the payloads sent to it, so use
it only for test repos.

## 2. Create the GitHub App

GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**.

| Field | Value |
|---|---|
| GitHub App name | anything unique, e.g. `pr-review-agent-dev-<you>` |
| Homepage URL | your repo URL (any URL works) |
| Webhook → Active | checked |
| Webhook URL | your smee URL |
| Webhook secret | a long random string, e.g. `python -c "import secrets; print(secrets.token_hex(32))"` |

**Repository permissions:**

| Permission | Access | Needed for |
|---|---|---|
| Pull requests | Read & write | reading PRs, posting comments and reviews |
| Contents | Read-only | reading files and the diff (Phase 2+) |
| Checks | Read-only | the agent's `get_ci_results` tool (Phase 4) |
| Metadata | Read-only | mandatory, selected automatically |

**Subscribe to events:** Pull request.

**Where can this GitHub App be installed?** Only on this account.

Click **Create GitHub App**. On the next page:
- Note the **App ID** near the top.
- Scroll to **Private keys** → **Generate a private key**. A `.pem` file downloads. Keep it
  out of the repo (`*.pem` is in `.gitignore`).

## 3. Install the App on a test repo

Create a throwaway repo (e.g. `pr-review-sandbox`) with a commit on `main`. Then in the App's
settings → **Install App** → install on your account → **Only select repositories** → pick the
test repo.

## 4. Fill in `.env`

```sh
cp .env.example .env
```

Set `GITHUB_APP_ID` and `GITHUB_WEBHOOK_SECRET`. `GITHUB_APP_PRIVATE_KEY` must be on one line,
with newlines written as `\n`:

```powershell
# PowerShell (single quotes keep \n literal)
(Get-Content path\to\key.pem) -join '\n'
```

```sh
# bash
awk 'NF {sub(/\r/, ""); printf "%s\\n", $0}' path/to/key.pem
```

Paste the output between double quotes:

```
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\n"
```

## 5. Run it

```sh
docker compose up --build                     # terminal 1
npx smee-client --url https://smee.io/AbC123xyz --target http://localhost:8000/webhook   # terminal 2
```

Open a PR on the test repo. Within a few seconds a comment appears:
"Review started for commit `abc1234`."

## Checking it worked

- **Webhook deliveries:** App settings → Advanced → Recent Deliveries shows each delivery and
  the response. You can **Redeliver** one from there. A redelivery reuses the delivery ID, so
  the API should answer `{"status": "duplicate"}` and no second comment appears.
- **Logs:** every line is JSON with `delivery_id`, `repo`, `pr`, `sha` (and `run_id` in the
  worker). To follow one PR: `docker compose logs -f api worker | grep '"pr": 7'`.
- **Database:** `docker compose exec postgres psql -U postgres -d pr_review -c "select * from review_runs"`

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 401 `invalid signature` | `GITHUB_WEBHOOK_SECRET` doesn't match the App's webhook secret. smee re-serializes the JSON body, so if only PRs with non-ASCII titles (e.g. emoji) fail, that's smee; use plain titles for local testing. |
| Run `failed`, error `-> 404 ... Integration not found` | Wrong `GITHUB_APP_ID`, or the key belongs to a different App. |
| Run `failed`, error `-> 403 ... Resource not accessible` | App permissions are missing, or you changed them and haven't accepted the new permissions on the installation. |
| Nothing in the API logs | The smee client isn't running, or the App's webhook URL isn't your smee URL. |
| Draft PR gets no comment | Expected: drafts are skipped until marked "Ready for review". |
