from shop.models import Order


def total_spent(orders: list[Order]) -> float:
    return sum(order.total for order in orders if order.paid)


def average_order_value(orders: list[Order]) -> float:
    paid = [order for order in orders if order.paid]
    return sum(order.total for order in paid) / len(paid)
