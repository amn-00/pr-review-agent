# weather-cache

Caches weather API responses for a few minutes.

## Usage

```python
from weather.cache import TTLCache

cache = TTLCache(ttl_seconds=300)
cache.set("london", forecast)
cache.get("london")  # None once 5 minutes have passed
cache.clear()        # e.g. after switching weather providers
```
