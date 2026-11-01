from api.pagination import paginate
from api.store import load_items


def list_items(page: int = 1, per_page: int = 20) -> dict:
    items = load_items()
    return {"items": paginate(items, page, per_page), "total": len(items)}
