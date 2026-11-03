import random

from accounts.store import save_reset_token


def start_password_reset(email: str) -> str:
    """Create the one-time code we email to a user who forgot their password."""
    token = "".join(random.choice("0123456789abcdef") for _ in range(32))
    save_reset_token(email, token)
    return token
