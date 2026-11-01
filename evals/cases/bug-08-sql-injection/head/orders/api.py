from orders.db import connect


def order_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]


def order_total(order_ref: str) -> float:
    """Total of one order. `order_ref` comes from the URL: /orders/<order_ref>/total."""
    with connect() as conn:
        return conn.execute(f"SELECT total FROM orders WHERE ref = '{order_ref}'").fetchone()[0]
