from pathlib import Path


def test_privacy_is_not_a_moderation_rule() -> None:
    policy = Path("RULES.md").read_text().casefold()
    forbidden = (
        "privacy and sensitive material",
        "privacy-invasive",
        "private communications",
        "private contact details",
        "access credentials",
        "medical information",
        "private family details",
        "personal data",
        "cropped or redacted",
    )
    assert all(term not in policy for term in forbidden)
    assert "moderation_appeal" not in policy
    assert "we don't take appeals" in policy
    assert "feedback on the rules used for moderation" in policy
    assert "#discord-server-feedback" in policy


def test_evidence_policy_is_proportional_and_accepts_pinpoint_sources() -> None:
    policy = Path("RULES.md").read_text().casefold()
    assert "genuine question" in policy
    assert "explicitly tentative understanding" in policy
    assert "i have not found" in policy
    assert "informal, non-definitive views" in policy
    assert "specific accessible message" in policy
    assert "without claiming its contents were verified" in policy
    assert "optional non-blocking advisory" in policy
    assert "must not be used to launder" in policy
