from mpvbridge.state import StateTracker, build_playlist


def test_update_reports_broadcast_worthy_changes():
    tracker = StateTracker()
    assert tracker.update("pause", False) is True
    # Same value again is not worth a broadcast.
    assert tracker.update("pause", False) is False
    assert tracker.update("pause", True) is True


def test_time_pos_is_stored_but_never_broadcast():
    tracker = StateTracker()
    assert tracker.update("time-pos", 12.5) is False
    assert tracker.snapshot().position == 12.5


def test_title_comes_from_media_title():
    tracker = StateTracker()
    tracker.update("media-title", "Some Song (Official Video)")
    tracker.update("path", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert tracker.snapshot().title == "Some Song (Official Video)"


def test_title_is_not_clobbered_by_a_missing_icy_title():
    """The bug in Neo-Oli/Termux-Mpv: a YouTube entry has no icy-title, and the original
    wrapper's else-branch then replaced a perfectly good title with the filename."""
    tracker = StateTracker()
    tracker.update("media-title", "Real Song Title")
    tracker.update("metadata", {"artist": "Someone"})
    tracker.update("path", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    snapshot = tracker.snapshot()

    assert snapshot.title == "Real Song Title"
    assert snapshot.artist == "Someone"


def test_metadata_lookup_is_case_insensitive():
    tracker = StateTracker()
    tracker.update("metadata", {"ARTIST": "Band", "Album": "Record"})

    snapshot = tracker.snapshot()

    assert snapshot.artist == "Band"
    assert snapshot.album == "Record"


def test_icy_title_used_when_media_title_absent():
    tracker = StateTracker()
    tracker.update("metadata", {"icy-title": "Live Stream Now Playing"})

    assert tracker.snapshot().title == "Live Stream Now Playing"


def test_playing_is_false_while_idle():
    tracker = StateTracker()
    tracker.update("pause", False)
    tracker.update("idle-active", True)

    assert tracker.snapshot().playing is False


def test_snapshot_message_shape():
    tracker = StateTracker()
    tracker.update("media-title", "Song")
    tracker.update("pause", False)
    tracker.update("playlist-pos", 3)
    tracker.update("playlist-count", 40)
    tracker.update("duration", 210.4567)
    tracker.update("time-pos", 10.1111)

    message = tracker.snapshot().to_message()

    assert message["type"] == "state"
    assert message["playing"] is True
    assert message["index"] == 3
    assert message["count"] == 40
    assert message["duration"] == 210.457
    assert message["position"] == 10.111


def test_art_is_recomputed_only_when_path_changes():
    tracker = StateTracker()
    tracker.update("path", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    first = tracker.snapshot().art
    second = tracker.snapshot().art

    assert first is second
    assert first is not None and "maxresdefault" in first["url"]

    tracker.update("path", "https://www.youtube.com/watch?v=oHg5SJYRHA0")
    assert tracker.snapshot().art != first


def test_build_playlist_uses_titles_and_marks_current():
    raw = [
        {"filename": "https://youtu.be/aaaaaaaaaaa", "title": "First"},
        {"filename": "https://youtu.be/bbbbbbbbbbb", "title": "Second", "current": True},
        {"filename": "https://youtu.be/ccccccccccc"},
    ]

    playlist = build_playlist(raw)
    entries = playlist.entries

    assert [entry.title for entry in entries] == [
        "First",
        "Second",
        "https://youtu.be/ccccccccccc",
    ]
    assert [entry.current for entry in entries] == [False, True, False]
    assert [entry.index for entry in entries] == [0, 1, 2]


def test_build_playlist_falls_back_to_index_for_current():
    raw = [{"filename": "a"}, {"filename": "b"}]

    playlist = build_playlist(raw, current_index=1)

    assert playlist.entries[1].current is True
    assert playlist.entries[0].current is False


def test_build_playlist_tolerates_garbage():
    assert build_playlist(None).entries == []
    assert build_playlist("nope").entries == []
