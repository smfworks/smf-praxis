from ...authority import AuthorityPolicy


def policy(jurisdiction: str) -> AuthorityPolicy:
    return AuthorityPolicy("legal", jurisdiction, ("binding", "persuasive", "secondary"), 3650)
