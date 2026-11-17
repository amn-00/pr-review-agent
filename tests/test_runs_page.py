"""The page of recent reviews: the query behind it, the HTML, and the routes."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx

from app.api.runs import REPO_URL, render
from app.db.models import RunStatus
from app.db.queries import (
    add_run_usage,
    finish_run,
    get_or_create_run,
    mark_posted,
    recent_runs,
    record_findings,
    set_verdict,
)
from app.db.session import make_engine
from app.github.webhooks import PullRequestPayload
from app.main import create_app
from app.pipeline.usage import Usage
from tests.conftest import TEST_DATABASE_URL
from tests.factories import REPO, SHA_1, SHA_2, pr_payload
from tests.test_db import candidate


async def reviewed_run(sessionmaker, sha: str, verdicts: list[str], posted: int, title: str) -> int:
    """A completed run with one finding per verdict, the first `posted` of them posted."""
    payload = PullRequestPayload.model_validate(pr_payload(sha=sha, title=title))
    async with sessionmaker() as session:
        run = await get_or_create_run(session, payload)
        candidates = [candidate(f"finding {n}") for n in range(len(verdicts))]
        ids = await record_findings(session, run.id, candidates, True)
        for finding_id, verdict in zip(ids, verdicts, strict=True):
            await set_verdict(session, finding_id, verdict, "checked", [])
        await mark_posted(session, ids[:posted])
        await add_run_usage(session, run.id, Usage(cost_usd=Decimal("0.0059")))
        await finish_run(session, run.id, RunStatus.COMPLETED)
    return run.id


async def test_recent_runs_are_newest_first_with_their_counts(sessionmaker):
    older = await reviewed_run(sessionmaker, SHA_1, ["confirmed", "confirmed", "dropped"], 1, "One")
    newer = await reviewed_run(sessionmaker, SHA_2, [], 0, "Two")  # a clean PR: no findings

    async with sessionmaker() as session:
        runs = await recent_runs(session, 50)
        only_latest = await recent_runs(session, 1)

    assert [run.id for run in runs] == [newer, older]
    assert [run.id for run in only_latest] == [newer]
    clean, reviewed = runs
    assert (clean.comments, clean.confirmed, clean.dropped, clean.uncertain) == (0, 0, 0, 0)
    # 2 confirmed, but only 1 posted inline (the other is a collapsed style suggestion, say).
    assert (reviewed.comments, reviewed.confirmed, reviewed.dropped, reviewed.uncertain) == (
        1,
        2,
        1,
        0,
    )
    assert (reviewed.full_name, reviewed.title, reviewed.status) == (REPO, "Two", "completed")
    assert reviewed.cost_usd == Decimal("0.0059") and reviewed.finished_at > reviewed.started_at


def row(**fields) -> SimpleNamespace:
    """A row shaped like recent_runs' rows."""
    defaults = {
        "full_name": "octo/hello",
        "number": 7,
        "title": "Add feature",
        "status": "completed",
        "started_at": datetime(2026, 11, 16, 17, 42, 36, tzinfo=UTC),
        "finished_at": datetime(2026, 11, 16, 17, 46, 25, tzinfo=UTC),
        "cost_usd": Decimal("0.005912345"),
        "comments": 2,
        "confirmed": 2,
        "dropped": 1,
        "uncertain": 0,
    }
    return SimpleNamespace(**(defaults | fields))


def test_a_row_shows_the_review_with_a_link_to_its_pr():
    page = render([row()])

    assert "<td>2026-11-16 17:42</td>" in page
    assert '<a href="https://github.com/octo/hello/pull/7">#7</a> Add feature</td>' in page
    assert "<td>3m 49s</td><td>2</td><td>2 confirmed, 1 dropped</td>" in page
    assert "<td>$0.0059</td>" in page
    assert f'<a href="{REPO_URL}">' in page


def test_text_from_github_cannot_inject_html():
    hostile = row(title='<script>alert(1)</script> "x"', full_name='a/b"><img src=x>')

    page = render([hostile])

    assert "<script>" not in page and "<img" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &quot;x&quot;" in page
    assert 'href="https://github.com/a/b&quot;&gt;&lt;img src=x&gt;/pull/7"' in page


def test_a_run_without_a_review_and_an_empty_page():
    running = row(status="running", finished_at=None, comments=0, confirmed=0, dropped=0)

    page = render([running])

    assert "<td>running</td><td>—</td><td>0</td><td>—</td>" in page
    assert "<p>No reviews yet.</p>" in render([]) and "<table>" not in render([])


async def get(app, url: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(url)


async def test_the_page_is_served_from_the_database_and_the_root_leads_to_it(sessionmaker):
    await reviewed_run(sessionmaker, SHA_1, ["confirmed"], 1, "Fix <b>bold</b> claims")
    app = create_app()
    app.state.engine = make_engine(TEST_DATABASE_URL)

    page = await get(app, "/runs")
    root = await get(app, "/")
    await app.state.engine.dispose()

    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert (
        f'<a href="https://github.com/{REPO}/pull/7">#7</a> Fix &lt;b&gt;bold&lt;/b&gt; claims'
        in page.text
    )
    assert "<td>1</td><td>1 confirmed</td><td>$0.0059</td>" in page.text
    assert (root.status_code, root.headers["location"]) == (307, "/runs")
