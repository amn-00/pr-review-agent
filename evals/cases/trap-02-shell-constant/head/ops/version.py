import subprocess

APP_NAME = "inventory"


def build_label() -> str:
    """App name plus the short git commit, e.g. "inventory@3f9a1c7", for the /health page."""
    commit = subprocess.run(
        "git rev-parse --short HEAD", shell=True, capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"{APP_NAME}@{commit}"
