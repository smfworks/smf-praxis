from ...authority import AuthorityPolicy


def policy(jurisdiction: str, population: str) -> AuthorityPolicy:
    return AuthorityPolicy("medical", jurisdiction, ("guideline", "systematic_review", "study"), 730, population)
