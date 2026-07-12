from ...authority import AuthorityPolicy


def policy(jurisdiction: str) -> AuthorityPolicy:
    return AuthorityPolicy("forensic_engineering", jurisdiction, ("code", "standard", "technical_literature"), 1825)
