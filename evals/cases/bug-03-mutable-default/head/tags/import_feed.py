from tags.registry import Article, normalize_tags


def import_articles(feed: list[dict]) -> list[Article]:
    return [Article(entry["title"], normalize_tags(entry.get("tags", []))) for entry in feed]
