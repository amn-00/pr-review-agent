from payments.client import charge
from payments.settings import PAYMENTS_API_KEY


def checkout(cart_total_cents: int, card_token: str) -> dict:
    return charge(cart_total_cents, card_token, PAYMENTS_API_KEY)
