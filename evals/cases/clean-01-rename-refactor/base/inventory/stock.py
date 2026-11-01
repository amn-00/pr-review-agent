from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    quantity: int
    reorder_level: int


def needs_reorder(item: Item) -> bool:
    return item.quantity <= item.reorder_level


def items_to_reorder(items: list[Item]) -> list[Item]:
    result = []
    for item in items:
        if needs_reorder(item):
            result.append(item)
    return result
