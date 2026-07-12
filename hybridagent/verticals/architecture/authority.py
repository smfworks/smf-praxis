from ...authority import AuthorityPolicy


def policy(jurisdiction: str) -> AuthorityPolicy:
    return AuthorityPolicy("architecture", jurisdiction, ("code", "standard", "guidance"), 1825)
