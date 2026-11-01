from pathlib import Path

UPLOADS = Path("/srv/uploads")


def upload_path(name: str) -> Path:
    return UPLOADS / Path(name).name
