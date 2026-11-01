import time

import httpx

from jobs.client import fetch_status


async def wait_until_ready(client: httpx.AsyncClient, url: str, attempts: int = 10) -> bool:
    """Poll a health URL until it answers 200, backing off between tries. Runs in the
    service's event loop, next to the request handlers."""
    for attempt in range(attempts):
        if await fetch_status(client, url) == 200:
            return True
        time.sleep(2**attempt)
    return False
