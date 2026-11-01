import sqlite3

DB_PATH = "orders.db"


def connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)
