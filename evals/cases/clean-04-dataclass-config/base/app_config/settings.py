import os


def load_settings() -> dict:
    return {
        "host": os.environ.get("APP_HOST", "127.0.0.1"),
        "port": int(os.environ.get("APP_PORT", "8000")),
        "debug": os.environ.get("APP_DEBUG", "false").lower() == "true",
    }
