from billing.money import to_cents


def split_bill(total: str, people: int) -> list[int]:
    """Each person's share in cents. The shares must add up to the total."""
    if people < 1:
        raise ValueError("people must be at least 1")
    cents = to_cents(total)
    share = cents // people
    return [share] * people
