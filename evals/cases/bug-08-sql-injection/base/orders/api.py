from orders.db import connect


def order_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
