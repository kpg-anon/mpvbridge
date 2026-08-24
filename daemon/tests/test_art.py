from mpvbridge import art


def test_youtube_id_from_watch_url():
    assert art.youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_youtube_id_with_playlist_params():
    url = "https://www.youtube.com/watch?list=PL123&v=dQw4w9WgXcQ&index=4"
    assert art.youtube_id(url) == "dQw4w9WgXcQ"


def test_youtube_id_short_and_music_and_shorts():
    assert art.youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert art.youtube_id("https://music.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert art.youtube_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_youtube_id_rejects_non_youtube():
    assert art.youtube_id("https://example.com/watch?v=dQw4w9WgXcQ") is None
    assert art.youtube_id("/storage/music/track.opus") is None
    assert art.youtube_id(None) is None


def test_resolve_youtube_gives_both_thumbnail_urls():
    resolved = art.resolve("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert resolved == {
        "url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
        "fallbackUrl": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
    }


def test_resolve_local_sidecar_cover(tmp_path):
    track = tmp_path / "track.opus"
    track.write_bytes(b"not really audio")
    (tmp_path / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0jpegbytes")

    resolved = art.resolve(str(track))

    assert resolved is not None
    assert resolved["mime"] == "image/jpeg"
    assert resolved["data"]


def test_resolve_prefers_same_named_cover(tmp_path):
    track = tmp_path / "track.opus"
    track.write_bytes(b"audio")
    (tmp_path / "track.png").write_bytes(b"pngbytes")
    (tmp_path / "cover.jpg").write_bytes(b"jpegbytes")

    resolved = art.resolve(str(track))

    assert resolved is not None
    assert resolved["mime"] == "image/png"


def test_resolve_skips_oversized_cover(tmp_path, monkeypatch):
    monkeypatch.setattr(art, "MAX_INLINE_ART_BYTES", 8)
    track = tmp_path / "track.opus"
    track.write_bytes(b"audio")
    (tmp_path / "cover.jpg").write_bytes(b"x" * 64)

    assert art.resolve(str(track)) is None


def test_resolve_missing_file_is_none():
    assert art.resolve("/nowhere/at/all.mp3") is None
