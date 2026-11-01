from api.store import load_items


def list_items(page: int = 1, per_page: int = 20) -> dict:
    items = load_items()
    return {"items": items, "total": len(items)}
