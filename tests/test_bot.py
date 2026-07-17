from types import SimpleNamespace

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


def test_committee_internal_channel_is_ignored() -> None:
    channel = SimpleNamespace(category_id=1491596963647324180)
    assert CoronetClient._channel_is_ignored(channel)


def test_thread_in_committee_internal_category_is_ignored() -> None:
    parent = SimpleNamespace(category_id=1491596963647324180)
    thread = SimpleNamespace(category_id=None, parent=parent)
    assert CoronetClient._channel_is_ignored(thread)


def test_other_categories_are_not_ignored() -> None:
    channel = SimpleNamespace(category_id=123)
    assert not CoronetClient._channel_is_ignored(channel)
