import re
import unicodedata


def truncate(text: str, limit: int) -> str:
    """Cut `text` to at most `limit` characters, ending with "…" if it was cut."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def slugify(text: str) -> str:
    """URL-friendly form of `text`: lower-case ASCII letters and digits, words joined by "-".

    >>> slugify("Héllo, World!")
    'hello-world'
    """
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_text.lower())
    return "-".join(words)
