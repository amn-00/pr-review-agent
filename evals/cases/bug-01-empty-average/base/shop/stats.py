from shop.models import Order


def total_spent(orders: list[Order]) -> float:
    return sum(order.total for order in orders if order.paid)
