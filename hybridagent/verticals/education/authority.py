from ...authority import AuthorityPolicy


def policy(jurisdiction: str, population: str = "") -> AuthorityPolicy:
    return AuthorityPolicy("education", jurisdiction, ("regulation", "standard", "research"), 1825, population)
