def mean_rating(ratings: list[int]) -> float:
    total = 0
    for rating in ratings:
        total += rating
    return round(total / len(ratings), 1)
