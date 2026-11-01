def truncate(text: str, limit: int) -> str:
    """Cut `text` to at most `limit` characters, ending with "…" if it was cut."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return text if len(text) <= limit else text[: limit - 1] + "…"
