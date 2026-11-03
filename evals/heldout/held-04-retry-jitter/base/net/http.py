import httpx


def get_json(url: str) -> dict:
    response = httpx.get(url, timeout=10)
    response.raise_for_status()
    return response.json()
