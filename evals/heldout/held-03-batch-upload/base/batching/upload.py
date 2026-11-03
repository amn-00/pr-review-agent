def upload_all(client, records: list[dict]) -> int:
    for record in records:
        client.post("/records", json=record)
    return len(records)
