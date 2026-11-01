import httpx


async def fetch_status(client: httpx.AsyncClient, url: str) -> int:
    response = await client.get(url)
    return response.status_code
