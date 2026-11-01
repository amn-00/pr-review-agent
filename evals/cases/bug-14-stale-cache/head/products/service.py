from products.repo import load_product, save_price

# Product pages are read far more often than prices change.
_cache: dict[int, dict | None] = {}


def get_product(product_id: int) -> dict | None:
    if product_id not in _cache:
        _cache[product_id] = load_product(product_id)
    return _cache[product_id]


def update_price(product_id: int, price: float) -> None:
    if price <= 0:
        raise ValueError("price must be positive")
    save_price(product_id, round(price, 2))
