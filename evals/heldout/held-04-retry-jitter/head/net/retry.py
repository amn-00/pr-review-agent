import random
import time

import httpx

from net.http import get_json


def get_json_with_retry(url: str, attempts: int = 4) -> dict:
    """get_json, retrying timeouts with exponential backoff plus random jitter, so clients
    that failed together don't retry together."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(attempts):
        try:
            return get_json(url)
        except httpx.TimeoutException:
            if attempt == attempts - 1:
                raise
            time.sleep(2**attempt + random.uniform(0, 1))
    raise AssertionError("unreachable")
