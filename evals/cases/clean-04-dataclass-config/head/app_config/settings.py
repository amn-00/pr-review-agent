import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False


def load_settings() -> Settings:
    return Settings(
        host=os.environ.get("APP_HOST", Settings.host),
        port=int(os.environ.get("APP_PORT", Settings.port)),
        debug=os.environ.get("APP_DEBUG", "false").lower() == "true",
    )
