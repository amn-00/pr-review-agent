import base64
import hashlib
import hmac
import json
import time

from auth.settings import TOKEN_SECRET


def sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    signature = hmac.new(TOKEN_SECRET, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify(token: str) -> dict | None:
    """The token's payload if its signature is valid and it hasn't expired, else None."""
    body, _, signature = token.partition(".")
    expected = hmac.new(TOKEN_SECRET, body.encode(), hashlib.sha256).hexdigest()
    if signature != expected:
        return None
    payload = json.loads(base64.urlsafe_b64decode(body))
    if payload.get("exp", 0) < time.time():
        return None
    return payload
