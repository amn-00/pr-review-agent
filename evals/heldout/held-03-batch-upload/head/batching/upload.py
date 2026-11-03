from batching.chunks import chunked

BATCH_SIZE = 100


def upload_all(client, records: list[dict]) -> int:
    for batch in chunked(records, BATCH_SIZE):
        client.post("/records/batch", json=list(batch))
    return len(records)
