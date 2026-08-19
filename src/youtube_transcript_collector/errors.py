from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class CollectorError(Exception):
    code: str
    message: str
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message
