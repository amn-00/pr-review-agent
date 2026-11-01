from shop.models import Customer
from shop.stats import average_order_value, total_spent


def customer_summary(customer: Customer) -> str:
    spent = total_spent(customer.orders)
    average = average_order_value(customer.orders)
    return f"{customer.name}: {spent:.2f} spent, {average:.2f} per order"
