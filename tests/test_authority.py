"""Authority policy must filter applicability before similarity ranking."""

from hybridagent.authority import AuthorityCandidate, AuthorityPolicy, filter_authority


def test_wrong_jurisdiction_is_rejected_before_high_similarity():
    policy = AuthorityPolicy(
        vertical="legal", jurisdiction="US-CA", accepted_tiers=("binding", "persuasive"),
        max_age_days=3650)
    wrong = AuthorityCandidate(
        source_id="wrong", authority_tier="binding", jurisdiction="US-NY",
        age_days=1, similarity=0.99)
    right = AuthorityCandidate(
        source_id="right", authority_tier="binding", jurisdiction="US-CA",
        age_days=100, similarity=0.50)
    result = filter_authority(policy, [wrong, right])
    assert [item.candidate.source_id for item in result.accepted] == ["right"]
    assert result.rejected[0].reasons == ("jurisdiction_mismatch",)


def test_stale_retracted_and_superseded_sources_are_rejected():
    policy = AuthorityPolicy(
        vertical="medical", jurisdiction="US", accepted_tiers=("guideline",),
        max_age_days=365, population="adult")
    candidates = [
        AuthorityCandidate("stale", "guideline", "US", 366, 0.9, population="adult"),
        AuthorityCandidate("retracted", "guideline", "US", 1, 0.9,
                           population="adult", retracted=True),
        AuthorityCandidate("old", "guideline", "US", 1, 0.9,
                           population="adult", superseded=True),
        AuthorityCandidate("child", "guideline", "US", 1, 0.9,
                           population="pediatric"),
    ]
    result = filter_authority(policy, candidates)
    assert not result.accepted
    assert {reason for item in result.rejected for reason in item.reasons} == {
        "stale", "retracted", "superseded", "population_mismatch"}


def test_accepted_sources_rank_by_authority_then_similarity():
    policy = AuthorityPolicy(
        vertical="architecture", jurisdiction="US", accepted_tiers=("code", "standard"),
        max_age_days=1000)
    result = filter_authority(policy, [
        AuthorityCandidate("standard", "standard", "US", 10, 0.99),
        AuthorityCandidate("code", "code", "US", 20, 0.50),
    ])
    assert [item.candidate.source_id for item in result.accepted] == ["code", "standard"]
    assert all(item.reasons == ("applicable",) for item in result.accepted)
