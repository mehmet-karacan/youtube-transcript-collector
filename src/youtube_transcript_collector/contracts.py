from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from .errors import CollectorError

VIDEO_URL = re.compile(r"^https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}$")
CHANNEL_URL = re.compile(
    r"^https://www\.youtube\.com/(?:@[A-Za-z0-9._-]{3,30}|(?:channel|c|user)/[A-Za-z0-9._-]+)$"
)
LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")


class Scope(StrEnum):
    SINGLE_VIDEO = "single_video"
    VIDEO_LIST = "video_list"
    CHANNEL_ALL = "channel_all"
    CHANNEL_SELECTION = "channel_selection"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class VideoState(StrEnum):
    DISCOVERED = "discovered"
    DEFERRED_LIVE = "deferred-live"
    CAPTION_UNAVAILABLE = "caption-unavailable"
    TEXT_COMPLETE = "text-complete"
    QUARANTINED = "quarantined"
    LEGACY_ARCHIVE_ONLY = "legacy-archive-only"


@dataclass(frozen=True, slots=True)
class RatePolicy:
    min_delay_seconds: float = 1.0
    max_retries: int = 3
    rate_limit_cooldown_seconds: int = 900

    def validate(self) -> None:
        if not 0.5 <= self.min_delay_seconds <= 60:
            raise CollectorError(
                "invalid_rate_policy", "min_delay_seconds must be between 0.5 and 60"
            )
        if not 0 <= self.max_retries <= 20:
            raise CollectorError("invalid_rate_policy", "max_retries must be between 0 and 20")
        if not 60 <= self.rate_limit_cooldown_seconds <= 86_400:
            raise CollectorError(
                "invalid_rate_policy",
                "rate_limit_cooldown_seconds must be between 60 and 86400",
            )


@dataclass(frozen=True, slots=True)
class Selector:
    latest: int | None = None
    title_query: str | None = None
    date_from: str | None = None
    date_to: str | None = None

    def validate(self) -> None:
        if self.latest is not None and not 1 <= self.latest <= 10_000:
            raise CollectorError("invalid_selector", "latest must be between 1 and 10000")
        for name, value in (("date_from", self.date_from), ("date_to", self.date_to)):
            if value is not None:
                import datetime as dt

                try:
                    dt.date.fromisoformat(value)
                except ValueError as exc:
                    raise CollectorError("invalid_selector", f"{name} must be YYYY-MM-DD") from exc
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise CollectorError("invalid_selector", "date_from must not be after date_to")
        if self.title_query is not None and not self.title_query.strip():
            raise CollectorError("invalid_selector", "title_query must not be blank")
        if self.title_query is not None and len(self.title_query) > 200:
            raise CollectorError("invalid_selector", "title_query must be at most 200 characters")

    @property
    def is_empty(self) -> bool:
        return not any((self.latest, self.title_query, self.date_from, self.date_to))


@dataclass(frozen=True, slots=True)
class CollectionRequest:
    scope: Scope
    targets: tuple[str, ...]
    languages: tuple[str, ...] = ("en",)
    selector: Selector = field(default_factory=Selector)
    include_completed_live: bool = True
    rate_policy: RatePolicy = field(default_factory=RatePolicy)

    def validate(self) -> None:
        if not self.targets:
            raise CollectorError("invalid_request", "at least one target is required")
        if len(self.targets) > 500:
            raise CollectorError("invalid_request", "at most 500 video targets are allowed")
        if any(len(target) > 2048 for target in self.targets):
            raise CollectorError("invalid_request", "targets must be at most 2048 characters")
        if len(set(self.targets)) != len(self.targets):
            raise CollectorError("invalid_request", "targets must be unique")
        if (
            self.scope in {Scope.SINGLE_VIDEO, Scope.CHANNEL_ALL, Scope.CHANNEL_SELECTION}
            and len(self.targets) != 1
        ):
            raise CollectorError("invalid_request", f"{self.scope} requires exactly one target")
        if self.scope == Scope.VIDEO_LIST and len(self.targets) < 2:
            raise CollectorError("invalid_request", "video_list requires at least two videos")
        expected = (
            CHANNEL_URL if self.scope in {Scope.CHANNEL_ALL, Scope.CHANNEL_SELECTION} else VIDEO_URL
        )
        if any(not expected.fullmatch(target) for target in self.targets):
            raise CollectorError(
                "invalid_request", "targets are not canonical for the selected scope"
            )
        if self.scope == Scope.CHANNEL_SELECTION and self.selector.is_empty:
            raise CollectorError("invalid_request", "channel_selection requires a selector")
        if self.scope != Scope.CHANNEL_SELECTION and not self.selector.is_empty:
            raise CollectorError(
                "invalid_request", "selectors are only valid for channel_selection"
            )
        if not self.languages or len(self.languages) > 10:
            raise CollectorError(
                "invalid_request", "languages must contain between 1 and 10 language tags"
            )
        if any(item.casefold() == "all" or not LANGUAGE_TAG.fullmatch(item) for item in self.languages):
            raise CollectorError(
                "invalid_request",
                "languages must be literal BCP-47-like tags; wildcards and expressions are forbidden",
            )
        if len(set(self.languages)) != len(self.languages):
            raise CollectorError("invalid_request", "languages must be unique")
        self.selector.validate()
        self.rate_policy.validate()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["scope"] = self.scope.value
        data["targets"] = list(self.targets)
        data["languages"] = list(self.languages)
        return data

    @property
    def digest(self) -> str:
        raw = json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollectionRequest:
        selector = Selector(**data.get("selector", {}))
        rate_policy = RatePolicy(**data.get("rate_policy", {}))
        request = cls(
            scope=Scope(data["scope"]),
            targets=tuple(data["targets"]),
            languages=tuple(data.get("languages", ["en"])),
            selector=selector,
            include_completed_live=bool(data.get("include_completed_live", True)),
            rate_policy=rate_policy,
        )
        request.validate()
        return request
