from collections.abc import Iterator, Sequence


def chunked(items: Sequence, size: int) -> Iterator[Sequence]:
    """Consecutive slices of `items`, each at most `size` long."""
    if size < 1:
        raise ValueError("size must be at least 1")
    for start in range(0, len(items), size):
        yield items[start : start + size]
