def to_cents(amount: str) -> int:
    """'12.34' -> 1234."""
    whole, _, cents = amount.partition(".")
    return int(whole) * 100 + int(cents.ljust(2, "0")[:2])
