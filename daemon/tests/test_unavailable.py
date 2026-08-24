"""Unavailable playlist entries.

A YouTube playlist accumulates deleted, private and region-blocked videos. mpv cannot resolve a
title for those, so the list shows a bare URL. The daemon flags them and the app hides them.
"""

from mpvbridge.state import StateTracker, build_playlist


def entry_for(item):
    return build_playlist([item]).entries[0]


def test_a_resolved_remote_entry_is_available():
    entry = entry_for({"filename": "https://youtu.be/aaaaaaaaaaa", "title": "Real Song"})

    assert entry.unavailable is False
    assert entry.title == "Real Song"
    assert entry.url == "https://youtu.be/aaaaaaaaaaa"


def test_a_remote_entry_with_no_title_is_unavailable():
    entry = entry_for({"filename": "https://youtu.be/bbbbbbbbbbb"})

    assert entry.unavailable is True
    assert entry.title == "https://youtu.be/bbbbbbbbbbb"


def test_a_remote_entry_titled_with_its_own_url_is_unavailable():
    url = "https://youtu.be/ccccccccccc"
    entry = entry_for({"filename": url, "title": url})

    assert entry.unavailable is True


def test_yt_dlp_placeholder_titles_are_unavailable():
    for placeholder in ("[Deleted video]", "[Private video]", "[Video unavailable]"):
        entry = entry_for({"filename": "https://youtu.be/ddddddddddd", "title": placeholder})
        assert entry.unavailable is True, placeholder


def test_placeholder_matching_ignores_case():
    entry = entry_for({"filename": "https://youtu.be/eeeeeeeeeee", "title": "[DELETED VIDEO]"})

    assert entry.unavailable is True


def test_a_local_file_without_a_title_is_still_available():
    """The regression that matters: local files legitimately carry no title tag, and marking
    them unavailable would hide an entire local library."""
    entry = entry_for({"filename": "/storage/emulated/0/Music/track.opus"})

    assert entry.unavailable is False
    assert entry.title == "/storage/emulated/0/Music/track.opus"


def test_a_local_file_with_a_title_is_available():
    entry = entry_for({"filename": "/storage/music/track.opus", "title": "Local Song"})

    assert entry.unavailable is False


def test_mixed_playlist_flags_only_the_broken_entries():
    raw = [
        {"filename": "https://youtu.be/aaaaaaaaaaa", "title": "Good"},
        {"filename": "https://youtu.be/bbbbbbbbbbb"},
        {"filename": "https://youtu.be/ccccccccccc", "title": "[Deleted video]"},
        {"filename": "/storage/music/local.opus"},
    ]

    assert [e.unavailable for e in build_playlist(raw).entries] == [False, True, True, False]


def test_state_message_carries_the_current_url():
    tracker = StateTracker()
    tracker.update("path", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    tracker.update("media-title", "Song")

    message = tracker.snapshot().to_message()

    assert message["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_playlist_entries_carry_urls():
    raw = [{"filename": "https://youtu.be/aaaaaaaaaaa", "title": "Good"}]

    assert build_playlist(raw).entries[0].to_message()["url"] == "https://youtu.be/aaaaaaaaaaa"
