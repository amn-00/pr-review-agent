from reviews.stats import mean_rating


def rating_badge(product: dict) -> str:
    ratings = product.get("ratings", [])
    if not ratings:
        return "No ratings yet"
    return f"{mean_rating(ratings)} / 5 ({len(ratings)} ratings)"
