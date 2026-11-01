from dataclasses import dataclass


@dataclass
class User:
    id: int
    is_admin: bool = False


@dataclass
class Document:
    id: int
    owner_id: int
    body: str
