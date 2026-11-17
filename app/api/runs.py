"""A read-only page of the latest reviews, so the deployed service shows what it does."""

import html
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db.queries import recent_runs
from app.db.session import make_sessionmaker

router = APIRouter()

REPO_URL = "https://github.com/amn-00/pr-review-agent"
RUNS_SHOWN = 50

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PR Review Agent: recent reviews</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 82rem;
         padding: 0 1rem; color: #1f2328; }}
  p {{ max-width: 46rem; line-height: 1.5; }}
  .scroll {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.9rem; }}
  th, td {{ text-align: left; padding: 0.4rem 0.7rem; border-bottom: 1px solid #d0d7de;
            white-space: nowrap; }}
  td.title {{ white-space: normal; min-width: 12rem; }}
  th {{ background: #f6f8fa; }}
  a {{ color: #0969da; }}
</style>
</head>
<body>
<h1>PR Review Agent</h1>
<p>A GitHub App that reviews pull requests. A parallel LLM pipeline finds candidate issues, and
a tool-using agent checks each one against the repository before anything is posted. Only
confirmed findings become inline comments.</p>
<p>Code, design decisions and evaluation results: <a href="{repo}">{repo}</a></p>
<h2>Latest reviews</h2>
<p>Comments are the inline comments posted on the PR. Cost is at the LLM provider's list
prices; the service itself runs on free tiers.</p>
{body}
</body>
</html>
"""
HEADERS = ["Started (UTC)", "Repository", "Pull request", "Status", "Review time"]
HEADERS += ["Comments", "Agent verdicts", "Cost"]


def _review_time(started: datetime, finished: datetime | None) -> str:
    if finished is None:
        return "—"
    seconds = int((finished - started).total_seconds())
    return f"{seconds // 60}m {seconds % 60:02d}s"


def _verdicts(run) -> str:
    """Only the verdicts that occurred, e.g. "2 confirmed, 1 dropped": the column stays narrow."""
    counts = [(run.confirmed, "confirmed"), (run.dropped, "dropped"), (run.uncertain, "uncertain")]
    return ", ".join(f"{count} {name}" for count, name in counts if count) or "—"


def render(runs: list) -> str:
    """The page for `runs` (rows from `recent_runs`). Repo names, PR titles and statuses come
    from GitHub and the database, so each goes through html.escape before it's put in."""
    if not runs:
        return PAGE.format(repo=REPO_URL, body="<p>No reviews yet.</p>")
    rows = []
    for run in runs:
        repo = html.escape(run.full_name)
        cells = [
            f"{run.started_at.astimezone(UTC):%Y-%m-%d %H:%M}",
            repo,
            f'<a href="https://github.com/{repo}/pull/{run.number}">#{run.number}</a> '
            f"{html.escape(run.title)}",
            html.escape(run.status),
            _review_time(run.started_at, run.finished_at),
            str(run.comments),
            _verdicts(run),
            f"${run.cost_usd:.4f}",
        ]
        # The PR title (the third cell) may wrap; the other cells stay on one line.
        row = "".join(
            f'<td class="title">{cell}</td>' if index == 2 else f"<td>{cell}</td>"
            for index, cell in enumerate(cells)
        )
        rows.append(f"<tr>{row}</tr>")
    head = "".join(f"<th>{header}</th>" for header in HEADERS)
    table = f'<div class="scroll"><table><tr>{head}</tr>\n' + "\n".join(rows) + "\n</table></div>"
    return PAGE.format(repo=REPO_URL, body=table)


@router.get("/runs", response_class=HTMLResponse, include_in_schema=False)
async def runs_page(request: Request) -> HTMLResponse:
    """The latest runs, newest first. Read-only: one query, no forms, no JavaScript."""
    async with make_sessionmaker(request.app.state.engine)() as session:
        runs = await recent_runs(session, RUNS_SHOWN)
    return HTMLResponse(render(runs))
