from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Any

from .errors import CollectorError
from .manifest import export_manifest
from .state import StateStore, utc_now
from .text import safe_title

LEGACY_NAME = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}|unknown-date)_(?P<id>[A-Za-z0-9_-]{11})_(?P<title>.+)\.txt$"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_source(entry: dict[str, Any]) -> Path:
    source = Path(entry["path"])
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise CollectorError("migration_source_changed", "migration source is unavailable") from exc
    attributes = getattr(metadata, "st_file_attributes", 0)
    if stat.S_ISLNK(metadata.st_mode) or attributes & 0x400 or not stat.S_ISREG(metadata.st_mode):
        raise CollectorError("migration_source_changed", "migration source is not a regular file")
    if metadata.st_size != entry["size_bytes"] or _sha256(source) != entry["sha256"]:
        raise CollectorError("migration_source_changed", "migration source changed after preview")
    after = source.lstat()
    if (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise CollectorError("migration_source_changed", "migration source changed during validation")
    return source


def preview_migration(legacy_dir: Path) -> dict[str, Any]:
    root = legacy_dir.resolve()
    subtitle_dir = root / "subtitles" if (root / "subtitles").is_dir() else root
    entries: list[dict[str, Any]] = []
    invalid: list[str] = []
    for path in sorted(subtitle_dir.glob("*.txt"), key=lambda item: item.name):
        match = LEGACY_NAME.fullmatch(path.name)
        if not match:
            invalid.append(path.name)
            continue
        entries.append(
            {
                "video_id": match["id"],
                "date": match["date"],
                "title": match["title"],
                "path": str(path),
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )
    archive_candidates = [root / ".download-archive.txt", root / "download-archive.txt"]
    archive = next((path for path in archive_candidates if path.exists()), None)
    archive_ids: set[str] = set()
    archive_hash = None
    if archive:
        archive_hash = _sha256(archive)
        for line in archive.read_text(encoding="utf-8", errors="replace").splitlines():
            token = line.strip().split()[-1] if line.strip() else ""
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", token):
                archive_ids.add(token)
    text_ids = {item["video_id"] for item in entries}
    body = {
        "schema_version": 1,
        "legacy_root": str(root),
        "subtitle_count": len(entries),
        "archive_count": len(archive_ids),
        "archive_only_ids": sorted(archive_ids - text_ids),
        "invalid_files": invalid,
        "archive_sha256": archive_hash,
        "entries": entries,
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**body, "plan_digest": hashlib.sha256(encoded.encode()).hexdigest(), "read_only": True}


def apply_migration(legacy_dir: Path, state_dir: Path, plan_digest: str) -> dict[str, Any]:
    preview = preview_migration(legacy_dir)
    if preview["plan_digest"] != plan_digest:
        raise CollectorError(
            "migration_plan_mismatch", "migration preview changed; create a new preview"
        )
    store = StateStore(state_dir)
    now = utc_now()
    prepared: list[tuple[dict[str, Any], Path, Path, bool]] = []
    for entry in preview["entries"]:
        source = _validate_source(entry)
        relative = Path("subtitles") / source.name
        destination = store.output.path(relative, create_parent=True)
        if destination.exists() and _sha256(destination) != entry["sha256"]:
            raise CollectorError(
                "migration_destination_collision",
                "collector destination already contains different transcript content",
                {"filename": destination.name},
            )
        prepared.append((entry, source, relative, destination.exists()))

    created: list[Path] = []
    records: list[dict[str, Any]] = []
    try:
        for entry, source, relative_destination, existed in prepared:
            destination = store.output.path(relative_destination, create_parent=True)
            if not existed:
                destination = store.output.atomic_copy(
                    source,
                    relative_destination,
                    expected_sha256=entry["sha256"],
                    expected_size=entry["size_bytes"],
                )
                created.append(relative_destination)
                _validate_source(entry)
            records.append(
                {
                    "video_id": entry["video_id"],
                    "channel_id": None,
                    "original_title": entry["title"],
                    "safe_title": safe_title(entry["title"]),
                    "date": entry["date"],
                    "date_source": "legacy-filename",
                    "webpage_url": f"https://www.youtube.com/watch?v={entry['video_id']}",
                    "live_status": None,
                    "language": None,
                    "transcript_relpath": str(destination.relative_to(store.root)).replace(
                        "\\", "/"
                    ),
                    "transcript_sha256": entry["sha256"],
                    "size_bytes": entry["size_bytes"],
                    "state": "text-complete",
                    "state_reason": "legacy-import",
                    "last_checked_at": now,
                    "next_retry_at": None,
                    "attempt_count": 1,
                    "retry_policy_version": 1,
                }
            )
        for video_id in preview["archive_only_ids"]:
            records.append(
                {
                    "video_id": video_id,
                    "channel_id": None,
                    "original_title": video_id,
                    "safe_title": video_id,
                    "date": None,
                    "date_source": "unknown",
                    "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
                    "live_status": None,
                    "language": None,
                    "transcript_relpath": None,
                    "transcript_sha256": None,
                    "size_bytes": None,
                    "state": "legacy-archive-only",
                    "state_reason": "legacy-archive-only-needs-probe",
                    "last_checked_at": now,
                    "next_retry_at": now,
                    "attempt_count": 1,
                    "retry_policy_version": 1,
                }
            )
        store.upsert_videos(records)
    except Exception:
        for relative in reversed(created):
            store.output.remove_file(relative)
        raise
    manifest = export_manifest(store)
    return {
        "applied": len(preview["entries"]),
        "archive_only_registered": len(preview["archive_only_ids"]),
        "plan_digest": plan_digest,
        "manifest": str(manifest),
        "legacy_mutated": False,
    }
