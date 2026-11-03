RESET_TOKENS: dict[str, str] = {}


def save_reset_token(email: str, token: str) -> None:
    RESET_TOKENS[token] = email
