import os

from files.config import REPORTS_DIR


def read_report(filename: str) -> bytes:
    """Serve a generated report. `filename` comes from the query string: ?file=<name>."""
    path = os.path.join(REPORTS_DIR, filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(filename)
    with open(path, "rb") as f:
        return f.read()
