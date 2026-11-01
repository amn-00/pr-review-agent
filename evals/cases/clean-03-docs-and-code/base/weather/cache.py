import time


class TTLCache:
    def __init__(self, ttl_seconds: float):
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at > self.ttl_seconds:
            del self._entries[key]
            return None
        return value

    def set(self, key: str, value) -> None:
        self._entries[key] = (time.monotonic(), value)
