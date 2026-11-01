"""Parse the unified-diff `patch` GitHub returns for each changed file."""

import re
from dataclasses import dataclass, field

# "@@ -12,5 +14,7 @@ optional section heading". A missing count means 1.
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    """One hunk, in new-file line numbers.

    Every line from `start` to `end` (inclusive) is part of the hunk on the new side (added or
    unchanged context), and GitHub accepts inline comments on exactly those lines.
    """

    start: int
    end: int
    added: set[int] = field(default_factory=set)

    def contains(self, line: int) -> bool:
        return self.start <= line <= self.end


def parse_patch(patch: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    current: Hunk | None = None
    line_no = 0
    for line in patch.splitlines():
        match = HUNK_HEADER.match(line)
        if match:
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            line_no = start
            # A pure deletion has no new-side lines, so nothing in it can be commented on.
            current = Hunk(start, start + count - 1) if count > 0 else None
            if current:
                hunks.append(current)
            continue
        if current is None:
            continue
        if line.startswith("+"):
            current.added.add(line_no)
            line_no += 1
        elif line.startswith("-") or line.startswith("\\"):  # removed, or "\ No newline..."
            continue
        else:  # unchanged context line
            line_no += 1
    return hunks


def hunk_for_line(hunks: list[Hunk], line: int) -> Hunk | None:
    return next((hunk for hunk in hunks if hunk.contains(line)), None)
