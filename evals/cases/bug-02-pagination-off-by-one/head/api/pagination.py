def paginate(items: list, page: int, per_page: int) -> list:
    """Return one page of `items`. Pages are numbered from 1."""
    if page < 1 or per_page < 1:
        raise ValueError("page and per_page must be positive")
    start = (page - 1) * per_page
    end = start + per_page - 1
    return items[start:end]
