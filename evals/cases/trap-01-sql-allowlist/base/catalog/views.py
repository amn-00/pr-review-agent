from catalog.columns import sort_column
from catalog.queries import list_products


def products_page(params: dict) -> list[dict]:
    column = sort_column(params.get("sort", "name"))
    return list_products(column)
