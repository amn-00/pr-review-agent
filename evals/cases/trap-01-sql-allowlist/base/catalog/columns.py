SORTABLE_COLUMNS = {"name", "price", "created_at"}


def sort_column(requested: str) -> str:
    """The column to sort by: only known columns; anything else falls back to name."""
    return requested if requested in SORTABLE_COLUMNS else "name"
