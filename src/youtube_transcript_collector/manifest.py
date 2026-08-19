from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .state import StateStore


def export_manifest(store: StateStore, output: Path | None = None) -> Path:
    relative = output or Path("subtitles-index.jsonl")
    lines = []
    for row in store.videos():
        row = {"schema_version": 1, **row}
        lines.append(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return store.output.atomic_write_text(relative, "\n".join(lines) + ("\n" if lines else ""))


def manifest_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
