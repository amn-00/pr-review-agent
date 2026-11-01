from dataclasses import dataclass, field


@dataclass
class Order:
    id: int
    total: float
    paid: bool


@dataclass
class Customer:
    name: str
    orders: list[Order] = field(default_factory=list)
