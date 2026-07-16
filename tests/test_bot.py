from coronetbot.bot import CoronetClient


def test_requested_action_is_extracted_from_forum_root() -> None:
    root = """1. Claim: One claim.
2. Evidence: A source.
3. Commentary: A comment.
4. Requested action: Publish the minutes.
"""
    assert CoronetClient._requested_action(root) == "Publish the minutes."


def test_missing_requested_action_is_none() -> None:
    assert CoronetClient._requested_action("A general chat message") is None
