def unsubscribed_filter(recipients: list[str], unsubscribed: list[str]) -> list[str]:
    """Recipients who haven't unsubscribed, in their original order."""
    result = []
    for address in recipients:
        if address.lower() not in [u.lower() for u in unsubscribed]:
            result.append(address)
    return result
