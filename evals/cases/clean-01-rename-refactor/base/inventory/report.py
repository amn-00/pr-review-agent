from inventory.stock import Item, items_to_reorder


def reorder_report(items: list[Item]) -> str:
    lines = [f"{item.sku}: {item.quantity} left" for item in items_to_reorder(items)]
    return "\n".join(lines) or "Nothing to reorder"
