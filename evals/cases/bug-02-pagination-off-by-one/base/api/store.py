ITEMS = [{"id": n, "name": f"item {n}"} for n in range(1, 101)]


def load_items() -> list[dict]:
    return list(ITEMS)
