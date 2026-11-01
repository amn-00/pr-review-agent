from app_config.settings import load_settings


def server_address() -> str:
    settings = load_settings()
    return f"http://{settings.host}:{settings.port}"
