class Article:
    def __init__(self, title: str, tags: list[str] | None = None):
        self.title = title
        self.tags = tags or []


def normalize_tags(raw: list[str], seen: set[str] = set()) -> list[str]:
    """Lower-case and de-duplicate tags, keeping their order."""
    result = []
    for tag in raw:
        tag = tag.strip().lower()
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result
