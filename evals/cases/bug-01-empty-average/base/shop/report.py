from shop.models import Customer
from shop.stats import total_spent


def customer_summary(customer: Customer) -> str:
    spent = total_spent(customer.orders)
    return f"{customer.name}: {spent:.2f} spent"
