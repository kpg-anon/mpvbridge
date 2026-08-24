from mpvbridge.cache import PlaylistCache

URL = "https://www.youtube.com/playlist?list=PLFx"


def test_titles_round_trip(tmp_path):
    cache = PlaylistCache(tmp_path)
    added = cache.remember_titles({"aaaaaaaaaaa": "First", "bbbbbbbbbbb": "Second"})

    assert added == 2
    assert PlaylistCache(tmp_path).title_for("aaaaaaaaaaa") == "First"


def test_remembering_the_same_title_twice_is_not_a_change(tmp_path):
    cache = PlaylistCache(tmp_path)
    cache.remember_titles({"aaaaaaaaaaa": "First"})

    assert cache.remember_titles({"aaaaaaaaaaa": "First"}) == 0
    assert cache.remember_titles({"aaaaaaaaaaa": "Renamed"}) == 1


def test_blank_ids_and_titles_are_ignored(tmp_path):
    cache = PlaylistCache(tmp_path)

    assert cache.remember_titles({"": "no id", "ccccccccccc": ""}) == 0


def test_playlist_round_trip(tmp_path):
    cache = PlaylistCache(tmp_path)
    cache.save_playlist(URL, ["aaaaaaaaaaa", "bbbbbbbbbbb"])

    assert PlaylistCache(tmp_path).load_playlist(URL) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def test_unknown_playlist_is_empty(tmp_path):
    assert PlaylistCache(tmp_path).load_playlist("https://example.com/other") == []


def test_empty_playlist_is_not_written(tmp_path):
    cache = PlaylistCache(tmp_path)
    cache.save_playlist(URL, [])

    assert not cache.playlist_path(URL).exists()


def test_a_corrupt_cache_is_discarded_not_fatal(tmp_path):
    cache = PlaylistCache(tmp_path)
    cache.save_playlist(URL, ["aaaaaaaaaaa"])
    cache.playlist_path(URL).write_text("{ this is not json", encoding="utf-8")
    cache.titles_path.write_text("also not json", encoding="utf-8")

    fresh = PlaylistCache(tmp_path)

    assert fresh.load_playlist(URL) == []
    assert fresh.titles == {}


def test_different_urls_do_not_collide(tmp_path):
    cache = PlaylistCache(tmp_path)
    cache.save_playlist(URL, ["aaaaaaaaaaa"])
    cache.save_playlist("https://www.youtube.com/playlist?list=OTHER", ["bbbbbbbbbbb"])

    assert cache.load_playlist(URL) == ["aaaaaaaaaaa"]
    assert cache.load_playlist("https://www.youtube.com/playlist?list=OTHER") == ["bbbbbbbbbbb"]


def test_clear_removes_everything(tmp_path):
    cache = PlaylistCache(tmp_path)
    cache.remember_titles({"aaaaaaaaaaa": "First"})
    cache.save_playlist(URL, ["aaaaaaaaaaa"])
    assert cache.size_bytes() > 0

    cache.clear()

    assert cache.size_bytes() == 0
    assert PlaylistCache(tmp_path).load_playlist(URL) == []
