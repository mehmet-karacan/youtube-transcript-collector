import pytest

from youtube_transcript_collector.contracts import Scope
from youtube_transcript_collector.errors import CollectorError
from youtube_transcript_collector.targets import build_request, resolve_channel, resolve_video


def test_channel_tabs_normalize_to_root():
    root = "https://www.youtube.com/@FixtureChannel123"
    for tab in ("videos", "shorts", "streams", "featured"):
        assert resolve_channel(f"{root}/{tab}") == root
    assert resolve_channel("@FixtureChannel123") == root
    channel_id = "UCabcdefghijklmnopqrstuv"
    assert resolve_channel(f"https://youtube.com/channel/{channel_id}/streams") == (
        f"https://www.youtube.com/channel/{channel_id}"
    )
    assert resolve_channel("https://youtube.com/c/LegacyName/videos") == (
        "https://www.youtube.com/c/LegacyName"
    )


def test_video_variants_normalize():
    expected = "https://www.youtube.com/watch?v=abcdefghijk"
    assert resolve_video("abcdefghijk") == expected
    assert resolve_video("https://youtu.be/abcdefghijk?t=3") == expected
    assert resolve_video("https://youtube.com/shorts/abcdefghijk") == expected


def test_request_scopes_and_validation():
    assert build_request(videos=["abcdefghijk"]).scope == Scope.SINGLE_VIDEO
    assert build_request(videos=["abcdefghijk", "lmnopqrstuv"]).scope == Scope.VIDEO_LIST
    assert build_request(channel="@FixtureChannel123").scope == Scope.CHANNEL_ALL
    assert build_request(channel="@FixtureChannel123", latest=10).scope == Scope.CHANNEL_SELECTION
    with pytest.raises(CollectorError):
        build_request(channel="@FixtureChannel123", videos=["abcdefghijk"])


def test_reject_non_youtube_or_playlist_targets():
    with pytest.raises(CollectorError):
        resolve_video("https://example.com/abcdefghijk")
    with pytest.raises(CollectorError):
        resolve_channel("https://youtube.com/playlist?list=x")
