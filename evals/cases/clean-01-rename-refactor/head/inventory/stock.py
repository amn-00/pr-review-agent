from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    quantity: int
    reorder_level: int


def needs_reorder(item: Item) -> bool:
    return item.quantity <= item.reorder_level


def low_stock_items(items: list[Item]) -> list[Item]:
    """Items at or below their reorder level, lowest stock first."""
    return sorted((item for item in items if needs_reorder(item)), key=lambda item: item.quantity)
