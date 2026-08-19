import pytest

from youtube_transcript_collector.contracts import CollectionRequest, Scope
from youtube_transcript_collector.errors import CollectorError
from youtube_transcript_collector.targets import build_request, resolve_video


@pytest.mark.parametrize("language", ["*", "all", "en.*", "../../secret", "en,fr"])
def test_language_wildcards_and_expressions_are_rejected(language):
    with pytest.raises(CollectorError):
        build_request(videos=["abcdefghijk"], languages=[language])


def test_target_count_and_length_are_bounded():
    targets = tuple(f"https://www.youtube.com/watch?v={index:011d}" for index in range(501))
    with pytest.raises(CollectorError, match="500"):
        CollectionRequest(Scope.VIDEO_LIST, targets).validate()
    with pytest.raises(CollectorError, match="too long"):
        resolve_video("x" * 2049)


def test_title_query_length_is_bounded():
    with pytest.raises(CollectorError, match="200"):
        build_request(channel="@example", title_query="x" * 201)
