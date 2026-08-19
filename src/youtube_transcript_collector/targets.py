from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from .contracts import CollectionRequest, RatePolicy, Scope, Selector
from .errors import CollectorError

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
HANDLE_RE = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


def validate_video_id(value: object, *, source: str = "target") -> str:
    if not isinstance(value, str) or not VIDEO_ID_RE.fullmatch(value):
        raise CollectorError(
            "invalid_backend_video_id",
            "backend returned an invalid YouTube video ID",
            {"source": source},
        )
    return value


def resolve_video(value: str) -> str:
    if len(value) > 2048:
        raise CollectorError("invalid_video_target", "video target is too long")
    value = value.strip()
    if VIDEO_ID_RE.fullmatch(value):
        return f"https://www.youtube.com/watch?v={value}"
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = parsed.netloc.lower().removeprefix("www.")
    video_id: str | None = None
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif parsed.path.startswith(("/shorts/", "/live/", "/embed/")):
            video_id = parsed.path.strip("/").split("/")[1]
    if not video_id or not VIDEO_ID_RE.fullmatch(video_id):
        raise CollectorError(
            "invalid_video_target", "expected a YouTube video ID or video URL", {"target": value}
        )
    return f"https://www.youtube.com/watch?v={video_id}"


def resolve_channel(value: str) -> str:
    if len(value) > 2048:
        raise CollectorError("invalid_channel_target", "channel target is too long")
    value = value.strip().rstrip("/")
    if HANDLE_RE.fullmatch(value):
        return f"https://www.youtube.com/{value}"
    if CHANNEL_ID_RE.fullmatch(value):
        return f"https://www.youtube.com/channel/{value}"
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = parsed.netloc.lower().removeprefix("www.")
    if host not in {"youtube.com", "m.youtube.com"}:
        raise CollectorError("invalid_channel_target", "expected a YouTube channel URL or handle")
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise CollectorError("invalid_channel_target", "channel URL is missing an identifier")
    if parts[0].startswith("@") and HANDLE_RE.fullmatch(parts[0]):
        root = f"https://www.youtube.com/{parts[0]}"
        tail = parts[1:]
    elif parts[0] in {"channel", "c", "user"} and len(parts) >= 2:
        root = f"https://www.youtube.com/{parts[0]}/{parts[1]}"
        tail = parts[2:]
    else:
        raise CollectorError(
            "invalid_channel_target", "watch and playlist URLs are not channel targets"
        )
    allowed_tail = {"videos", "shorts", "streams", "featured"}
    if len(tail) > 1 or (tail and tail[0] not in allowed_tail):
        raise CollectorError("invalid_channel_target", "unsupported channel tab")
    return root


def build_request(
    *,
    videos: list[str] | None = None,
    channel: str | None = None,
    languages: list[str] | None = None,
    latest: int | None = None,
    title_query: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_delay_seconds: float = 1.0,
    max_retries: int = 3,
    rate_limit_cooldown_seconds: int = 900,
) -> CollectionRequest:
    selector = Selector(
        latest=latest, title_query=title_query, date_from=date_from, date_to=date_to
    )
    if channel and videos:
        raise CollectorError("invalid_request", "choose channel or video targets, not both")
    if channel:
        scope = Scope.CHANNEL_ALL if selector.is_empty else Scope.CHANNEL_SELECTION
        targets = (resolve_channel(channel),)
    elif videos:
        targets = tuple(resolve_video(item) for item in videos)
        scope = Scope.SINGLE_VIDEO if len(targets) == 1 else Scope.VIDEO_LIST
    else:
        raise CollectorError("invalid_request", "provide --channel or at least one --video")
    request = CollectionRequest(
        scope,
        targets,
        tuple(languages or ["en"]),
        selector,
        rate_policy=RatePolicy(
            min_delay_seconds=min_delay_seconds,
            max_retries=max_retries,
            rate_limit_cooldown_seconds=rate_limit_cooldown_seconds,
        ),
    )
    request.validate()
    return request
