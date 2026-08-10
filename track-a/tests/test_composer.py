"""A5 composer: exact outbound message content for every message type.

The spec's example strings are asserted verbatim so copy changes are
deliberate and reviewed.
"""

from shared_contract import CONTRACT_VERSION

from track_a.composer import (
    CANCEL_REPLY_TEXT,
    GENERIC_ERROR_REPLY_TEXT,
    compose_cancelled,
    compose_completion,
    compose_confirmation,
    compose_error,
)

OWNER = "15551234567"


def make_intent(action, content_type, fields):
    return {
        "contract_version": CONTRACT_VERSION,
        "owner_id": OWNER,
        "action": action,
        "content_type": content_type,
        "fields": fields,
        "confidence": 0.95,
    }


# ------------------------------------------------------------ confirmations


def test_job_create_confirmation():
    intent = make_intent(
        "create", "job",
        {
            "title": "Part-time Barista",
            "location": "Downtown",
            "remote": False,
            "description": "$18/hr, cash handling",
        },
    )
    text = compose_confirmation(intent)
    assert text == (
        "Post job: 'Part-time Barista' — Downtown, on-site. "
        "$18/hr, cash handling. Reply YES to publish, NO to cancel."
    )


def test_job_create_remote_omits_location():
    intent = make_intent(
        "create", "job",
        {"title": "Remote Writer", "remote": True, "description": "Weekly blog posts"},
    )
    text = compose_confirmation(intent)
    assert text == (
        "Post job: 'Remote Writer' — remote. "
        "Weekly blog posts. Reply YES to publish, NO to cancel."
    )


def test_job_create_minimal():
    intent = make_intent("create", "job", {"title": "Cashier"})
    assert compose_confirmation(intent) == (
        "Post job: 'Cashier'. Reply YES to publish, NO to cancel."
    )


def test_job_update_confirmation():
    intent = make_intent(
        "update", "job", {"title": "Barista", "description": "Now $20/hr"}
    )
    assert compose_confirmation(intent) == (
        "Update job 'Barista': description: Now $20/hr. Reply YES to publish, NO to cancel."
    )


def test_job_delete_confirmation():
    intent = make_intent("delete", "job", {"title": "Barista"})
    assert compose_confirmation(intent) == (
        "Remove job 'Barista'. Reply YES to remove, NO to cancel."
    )


def test_announcement_create_confirmation():
    intent = make_intent(
        "create", "announcement", {"title": "Summer Hours", "body": "Open until 9pm"}
    )
    assert compose_confirmation(intent) == (
        "Post announcement: 'Summer Hours'. Open until 9pm. "
        "Reply YES to publish, NO to cancel."
    )


def test_announcement_update_confirmation_matches_spec_example():
    intent = make_intent(
        "update", "announcement", {"title": "Holiday Closure", "body": "Closed Dec 25"}
    )
    assert compose_confirmation(intent) == (
        "Update announcement 'Holiday Closure' to: 'Closed Dec 25'. "
        "Reply YES to publish, NO to cancel."
    )


def test_announcement_delete_confirmation():
    intent = make_intent("delete", "announcement", {"title": "Holiday Closure"})
    assert compose_confirmation(intent) == (
        "Remove announcement 'Holiday Closure'. Reply YES to remove, NO to cancel."
    )


def test_business_info_single_field_matches_spec_example():
    intent = make_intent("update", "business_info", {"hours": "Mon-Fri 9-6"})
    assert compose_confirmation(intent) == (
        "Update your hours to: hours: Mon-Fri 9-6. Reply YES to confirm, NO to cancel."
    )


def test_business_info_multiple_fields():
    intent = make_intent(
        "update", "business_info",
        {"hours": "Mon-Fri 9-6", "phone": "(555) 123-4567"},
    )
    assert compose_confirmation(intent) == (
        "Update your hours and phone to: hours: Mon-Fri 9-6; phone: (555) 123-4567. "
        "Reply YES to confirm, NO to cancel."
    )


def test_image_update_matches_spec_example():
    intent = make_intent(
        "update", "image", {"slot": "homepage_banner", "media_base64": "..."}
    )
    assert compose_confirmation(intent) == (
        "Replace your homepage banner with the photo you sent. "
        "Reply YES to confirm, NO to cancel."
    )


def test_image_create_and_delete():
    create = make_intent("create", "image", {"slot": "logo", "media_base64": "..."})
    assert compose_confirmation(create) == (
        "Set your logo to the photo you sent. Reply YES to confirm, NO to cancel."
    )
    delete = make_intent("delete", "image", {"slot": "gallery"})
    assert compose_confirmation(delete) == (
        "Remove your gallery image. Reply YES to confirm, NO to cancel."
    )


def test_every_confirmation_ends_with_yes_no_instruction():
    cases = [
        make_intent("create", "job", {"title": "X", "description": "y"}),
        make_intent("update", "job", {"title": "X", "location": "Z"}),
        make_intent("delete", "job", {"title": "X"}),
        make_intent("create", "announcement", {"title": "X", "body": "y"}),
        make_intent("update", "announcement", {"title": "X", "body": "y"}),
        make_intent("delete", "announcement", {"title": "X"}),
        make_intent("update", "business_info", {"hours": "9-5"}),
        make_intent("update", "image", {"slot": "logo"}),
        make_intent("delete", "image", {"slot": "logo"}),
    ]
    for intent in cases:
        text = compose_confirmation(intent)
        assert text.rstrip().endswith(
            ("YES to publish, NO to cancel.", "YES to confirm, NO to cancel.",
             "YES to remove, NO to cancel.")
        ), text


# ------------------------------------------------- completions / errors / no


def test_completion_with_live_url():
    assert compose_completion("https://example.com/jobs/barista") == (
        "Done! Here's the live change: https://example.com/jobs/barista. "
        "You can undo this within 24h by replying UNDO."
    )


def test_completion_without_live_url():
    assert compose_completion(None) == (
        "Done! The change is live. You can undo this within 24h by replying UNDO."
    )


def test_error_with_message_uses_it():
    text = compose_error("the page 'jobs' was not found")
    assert text == (
        "Something went wrong: the page 'jobs' was not found. "
        "Nothing was published. Want to try again?"
    )


def test_error_without_message_uses_generic():
    assert compose_error(None) == GENERIC_ERROR_REPLY_TEXT
    assert compose_error("") == GENERIC_ERROR_REPLY_TEXT


def test_cancel_text():
    assert compose_cancelled() == CANCEL_REPLY_TEXT
    assert CANCEL_REPLY_TEXT == "Okay, cancelled — nothing was changed."
