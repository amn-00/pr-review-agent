from products.repo import load_product, save_price


def get_product(product_id: int) -> dict | None:
    return load_product(product_id)


def update_price(product_id: int, price: float) -> None:
    if price <= 0:
        raise ValueError("price must be positive")
    save_price(product_id, price)
