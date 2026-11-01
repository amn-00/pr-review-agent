def mean_rating(ratings: list[int]) -> float:
    """Average star rating, rounded to one decimal."""
    return round(sum(ratings) / len(ratings), 1)
