"""Webhook sequences for the load test, with the HTTP status each webhook should get."""

import hashlib
import random
from dataclasses import dataclass

# Share of each kind of webhook in the mixed scenario (see docs/plans/phase-6.md).
MIX = {
    "opened": 70,  # a new PR
    "synchronize": 10,  # a new commit on an open PR
    "replay": 10,  # the same delivery again
    "same_head": 5,  # a new delivery for a head that already has one (e.g. reopened)
    "ignored": 5,  # a draft PR, or an event we don't handle
}


@dataclass
class Event:
    delivery: str
    event: str  # the X-GitHub-Event header
    action: str
    installation_id: int
    repo_id: int
    repo: str
    number: int
    sha: str
    kind: str
    expected: tuple[int, ...]  # acceptable HTTP statuses
    draft: bool = False
    push: bool = False  # tell the fakes about this head before sending (PR opened or pushed to)
    # Index of an earlier event that must have been answered before this one is sent: replays
    # and pushes only make sense after what they refer to, even with many senders in parallel.
    after: int | None = None

    def payload(self) -> dict:
        """The fields of GitHub's pull_request webhook that the API reads."""
        return {
            "action": self.action,
            "installation": {"id": self.installation_id},
            "repository": {
                "id": self.repo_id,
                "full_name": self.repo,
                "owner": {"login": self.repo.split("/")[0]},
            },
            "pull_request": {
                "number": self.number,
                "title": f"Load test PR {self.number}",
                "draft": self.draft,
                "head": {"sha": self.sha},
                "user": {"login": "loadtest-user"},
            },
        }


def make_sha(nonce: str, repo: str, number: int, push: int) -> str:
    return hashlib.sha1(f"{nonce}:{repo}:{number}:{push}".encode()).hexdigest()


def mixed(
    n: int,
    nonce: str,
    seed: int = 1,
    installations: int = 5,
    repos: int = 4,
    mix: dict[str, int] = MIX,
) -> list[Event]:
    """`n` webhooks spread over installations × repos, in the `mix` shares. The failure
    tests use {"opened": 1}: new PRs only.

    `nonce` goes into every SHA and delivery ID, so runs against the same stack never collide
    with an earlier run's reviews or dedupe keys.
    """
    rng = random.Random(seed)
    events: list[Event] = []
    last_event: dict[tuple[str, int], int] = {}  # open PR -> index of its latest event
    pushes: dict[tuple[str, int], int] = {}
    next_number: dict[str, int] = {}

    def new_pull(draft: bool) -> Event:
        installation = rng.randint(1, installations)
        repo_index = rng.randint(1, repos)
        repo = f"load-org-{installation}/repo-{repo_index}"
        number = next_number.get(repo, 0) + 1
        next_number[repo] = number
        return Event(
            delivery=f"{nonce}-{len(events)}",
            event="pull_request",
            action="opened",
            installation_id=installation,
            repo_id=installation * 100 + repo_index,
            repo=repo,
            number=number,
            sha=make_sha(nonce, repo, number, 0),
            kind="opened",
            expected=(202,),
            draft=draft,
        )

    while len(events) < n:
        kind = rng.choices(list(mix), weights=list(mix.values()))[0]
        if kind in ("synchronize", "replay", "same_head") and not last_event:
            kind = "opened"
        delivery = f"{nonce}-{len(events)}"

        if kind == "opened":
            event = new_pull(draft=False)
            event.push = True
        elif kind == "ignored":
            event = new_pull(draft=True)
            event.kind, event.expected = "ignored", (200,)
            if rng.random() < 0.5:
                event.event = "issues"  # not a pull_request event at all
        elif kind == "replay":
            index = rng.randrange(len(events))
            original = events[index]
            event = Event(**{**vars(original), "kind": "replay", "expected": (200,)})
            event.push, event.after = False, index
        else:  # synchronize or same_head: an event for a PR that's already open
            key = rng.choice(list(last_event))
            previous = events[last_event[key]]
            event = Event(**{**vars(previous), "delivery": delivery, "after": last_event[key]})
            event.kind = kind
            if kind == "synchronize":
                pushes[key] = pushes.get(key, 0) + 1
                event.sha = make_sha(nonce, key[0], key[1], pushes[key])
                event.action, event.expected, event.push = "synchronize", (202,), True
            else:
                # Its first job may still be queued (arq refuses the same job ID: 200) or done
                # (arq accepts it, Postgres stops a second review: 202).
                event.action, event.expected, event.push = "reopened", (200, 202), False

        if kind in ("opened", "synchronize", "same_head"):
            last_event[(event.repo, event.number)] = len(events)
        events.append(event)
    return events
