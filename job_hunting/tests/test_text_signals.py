"""Closed-posting phrase detection. Conservative on purpose: only
phrases on the curated list flip the status; everything else returns
None so the JobPost row stays NULL and the list-view default surfaces
the post.
"""
from job_hunting.lib.text_signals import detect_application_status


def test_returns_none_on_normal_description():
    text = "Senior Engineer. Build great products. Strong Python required."
    assert detect_application_status(text) is None


def test_detects_no_longer_accepting():
    assert (
        detect_application_status("This role is no longer accepting applications.")
        == "closed"
    )


def test_detects_position_closed():
    assert (
        detect_application_status("Notice: this position is closed.") == "closed"
    )


def test_detects_position_filled():
    assert (
        detect_application_status("This position has been filled. Thanks!")
        == "closed"
    )


def test_detects_explicit_closed_marker():
    text = "[CLOSED — applications no longer accepted]\n\nOriginal description follows."
    assert detect_application_status(text) == "closed"


def test_detects_short_closed_bracket():
    assert detect_application_status("[ Closed ]\nrest of post") == "closed"


def test_case_insensitive():
    assert (
        detect_application_status("WE ARE NO LONGER ACCEPTING applications")
        == "closed"
    )


def test_returns_none_on_empty():
    assert detect_application_status("") is None
    assert detect_application_status(None) is None


def test_does_not_false_positive_on_close_word():
    """The word 'close' (proximity) should not fire the detector — only
    the curated phrases do."""
    assert (
        detect_application_status(
            "We work close to the customer and ship continuously."
        )
        is None
    )


def test_does_not_false_positive_on_open_role():
    """A normal "open role" description must not return 'closed'."""
    assert (
        detect_application_status(
            "Open position on the platform team. Apply via the link below."
        )
        is None
    )
