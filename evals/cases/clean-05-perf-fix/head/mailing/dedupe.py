def unsubscribed_filter(recipients: list[str], unsubscribed: list[str]) -> list[str]:
    """Recipients who haven't unsubscribed, in their original order."""
    blocked = {address.lower() for address in unsubscribed}
    return [address for address in recipients if address.lower() not in blocked]
