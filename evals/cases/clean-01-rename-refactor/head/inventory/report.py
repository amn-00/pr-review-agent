from inventory.stock import Item, low_stock_items


def reorder_report(items: list[Item]) -> str:
    lines = [f"{item.sku}: {item.quantity} left" for item in low_stock_items(items)]
    return "\n".join(lines) or "Nothing to reorder"
