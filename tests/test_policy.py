from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "policy_path",
    [Path("RULES.md"), Path("resources/moderation-agent-prompt.md")],
)
def test_privacy_is_not_a_moderation_rule(policy_path: Path) -> None:
    policy = policy_path.read_text().casefold()
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
