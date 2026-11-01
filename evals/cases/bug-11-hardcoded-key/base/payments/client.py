import httpx

API_URL = "https://payments.example.com/v1"


def charge(amount_cents: int, token: str, api_key: str) -> dict:
    response = httpx.post(
        f"{API_URL}/charges",
        json={"amount": amount_cents, "source": token},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    response.raise_for_status()
    return response.json()
