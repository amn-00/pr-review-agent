import sqlite3


def list_products(order_by: str) -> list[dict]:
    with sqlite3.connect("catalog.db") as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, name, price FROM products ORDER BY name").fetchall()
    return [dict(row) for row in rows]
