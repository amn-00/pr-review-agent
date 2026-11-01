import sqlite3

DB_PATH = "products.db"


def load_product(product_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, name, price FROM products WHERE id = ?", (product_id,)
        ).fetchone()
    return dict(row) if row else None


def save_price(product_id: int, price: float) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE products SET price = ? WHERE id = ?", (price, product_id))
