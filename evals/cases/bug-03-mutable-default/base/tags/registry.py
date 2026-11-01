class Article:
    def __init__(self, title: str, tags: list[str] | None = None):
        self.title = title
        self.tags = tags or []
