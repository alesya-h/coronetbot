from types import SimpleNamespace

from coronetbot.bot import CoronetClient, detected_image_media_type


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


def test_forum_prefix_reminder_is_soft_and_handles_mismatches() -> None:
    assert (
        CoronetClient._title_prefix_reminder(
            "C: A valid claim", is_forum=True, recommended_prefix=None
        )
        is None
    )
    missing = CoronetClient._title_prefix_reminder(
        "A question", is_forum=True, recommended_prefix="Q: "
    )
    assert missing is not None and "left in place" in missing[0] and "`Q: `" in missing[0]
    mismatched = CoronetClient._title_prefix_reminder(
        "C: What happened?", is_forum=True, recommended_prefix="Q: "
    )
    assert mismatched is not None and "`Q: `" in mismatched[0]
    assert (
        CoronetClient._title_prefix_reminder(
            "A chat thread", is_forum=False, recommended_prefix="Q: "
        )
        is None
    )


def test_image_media_type_is_detected_from_content() -> None:
    assert detected_image_media_type(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert detected_image_media_type(b"\xff\xd8\xffrest") == "image/jpeg"
    assert detected_image_media_type(b"GIF89arest") == "image/gif"
    assert detected_image_media_type(b"RIFF1234WEBPrest") == "image/webp"
    assert detected_image_media_type(b"not an image") is None


def test_attachment_audit_listing_includes_metadata_and_raw_url() -> None:
    attachment = SimpleNamespace(
        filename="evidence.png",
        size=1234,
        content_type="image/png",
        url="https://cdn.discord.test/evidence.png?signature=abc",
    )
    listing = CoronetClient._attachments_audit_listing([attachment])
    assert "`evidence.png` (1234 bytes, image/png)" in listing
    assert "https://cdn.discord.test/evidence.png?signature=abc" in listing


async def test_image_attachment_is_downloaded_and_prepared() -> None:
    class Attachment:
        filename = "evidence.png"
        content_type = "image/png"
        size = 12
        url = "https://cdn.discord.test/evidence.png"

        async def read(self, *, use_cached: bool) -> bytes:
            assert use_cached
            return b"\x89PNG\r\n\x1a\nrest"

    client = SimpleNamespace(config=SimpleNamespace(max_images_per_message=4, max_image_bytes=100))
    prepared = await CoronetClient._prepare_attachments(client, [Attachment()])

    assert len(prepared.images) == 1
    assert prepared.images[0].filename == "evidence.png"
    assert prepared.images[0].media_type == "image/png"
    assert not prepared.unavailable_images
    assert "included" in prepared.metadata[0]["image_analysis"]


async def test_oversize_image_fails_preparation_without_download() -> None:
    class Attachment:
        filename = "huge.png"
        content_type = "image/png"
        size = 101
        url = "https://cdn.discord.test/huge.png"

        async def read(self, *, use_cached: bool) -> bytes:
            raise AssertionError("oversize image must not be downloaded")

    client = SimpleNamespace(config=SimpleNamespace(max_images_per_message=4, max_image_bytes=100))
    prepared = await CoronetClient._prepare_attachments(client, [Attachment()])

    assert not prepared.images
    assert prepared.unavailable_images == ("huge.png",)
    assert "size limit" in prepared.metadata[0]["image_analysis"]
