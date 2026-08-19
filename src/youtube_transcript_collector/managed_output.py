from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import CollectorError

REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
MAX_STATE_BYTES = 2 * 1024 * 1024 * 1024
ROOT_MARKER = ".youtube-transcript-collector-root"
WINDOWS_REPLACE_RETRY_DELAYS = (0.005, 0.01, 0.02, 0.04, 0.08, 0.16)
WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset({5, 32, 33})
IS_WINDOWS = os.name == "nt"


class ManagedOutput:
    """Fail-closed writes confined to one non-reparse managed root."""

    def __init__(self, configured_root: Path):
        configured = configured_root.absolute()
        self._create_root_safely(configured)
        self._reject_reparse(configured)
        self.root = configured.resolve(strict=True)
        if not self.root.is_dir():
            self._unsafe("configured state root is not a directory")
        try:
            self.root.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass
        marker = self.root / ROOT_MARKER
        if self._lexists(marker):
            self._reject_reparse(marker)
            if not marker.is_file() or marker.read_text(encoding="ascii") != "youtube-transcript-collector-v1\n":
                self._unsafe("configured state root marker is invalid")
        else:
            marker.write_text("youtube-transcript-collector-v1\n", encoding="ascii")
            try:
                marker.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

    def _unsafe(self, message: str, relative: Path | None = None) -> None:
        details = {"relative_path": str(relative)} if relative is not None else None
        raise CollectorError("unsafe_managed_output_path", message, details)

    @staticmethod
    def _lexists(path: Path) -> bool:
        return os.path.lexists(path)

    def _reject_reparse(self, path: Path, relative: Path | None = None) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            self._unsafe(f"managed path metadata could not be read: {exc}", relative)
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & REPARSE_POINT:
            self._unsafe("symlink, junction, or reparse point is not allowed", relative)

    def _create_root_safely(self, configured: Path) -> None:
        missing: list[Path] = []
        cursor = configured
        while not self._lexists(cursor):
            missing.append(cursor)
            if cursor.parent == cursor:
                self._unsafe("configured state root has no existing ancestor")
            cursor = cursor.parent
        for existing in (cursor, *cursor.parents):
            if self._lexists(existing):
                self._reject_reparse(existing)
        if not cursor.is_dir():
            self._unsafe("configured state root parent is not a directory")
        for directory in reversed(missing):
            self._reject_reparse(directory.parent)
            try:
                directory.mkdir()
            except OSError as exc:
                self._unsafe(f"managed directory could not be created: {exc}")
            self._reject_reparse(directory)

    def _relative(self, value: str | Path) -> Path:
        relative = Path(value)
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            self._unsafe("managed output path must be relative and confined", relative)
        return relative

    def _inside(self, path: Path, relative: Path) -> None:
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            self._unsafe(f"managed path could not be resolved: {exc}", relative)
        if not resolved.is_relative_to(self.root):
            self._unsafe("managed output path escapes configured root", relative)

    def _replace_temporary(self, temporary: Path, relative: Path) -> Path:
        """Replace a managed file, tolerating bounded Windows sharing races.

        Antivirus scanners and concurrent readers can briefly hold a destination
        handle without delete sharing on Windows.  Every retry revalidates both
        paths so the delay cannot turn a transient sharing race into a path escape.
        Persistent access denial is returned unchanged after the bounded window.
        """
        retry_index = 0
        while True:
            destination = self.path(relative, create_parent=False)
            self._reject_reparse(temporary, relative)
            self._inside(temporary, relative)
            try:
                os.replace(temporary, destination)
                return destination
            except OSError as exc:
                winerror = getattr(exc, "winerror", None)
                if not IS_WINDOWS or winerror not in WINDOWS_TRANSIENT_REPLACE_ERRORS:
                    raise
                if retry_index >= len(WINDOWS_REPLACE_RETRY_DELAYS):
                    raise
                delay = WINDOWS_REPLACE_RETRY_DELAYS[retry_index]
                retry_index += 1
                time.sleep(delay)

    def path(self, value: str | Path, *, create_parent: bool = False) -> Path:
        relative = self._relative(value)
        parts = relative.parts
        parent_parts = parts[:-1] if parts else ()
        cursor = self.root
        for part in parent_parts:
            cursor /= part
            if self._lexists(cursor):
                self._reject_reparse(cursor, relative)
                if not cursor.is_dir():
                    self._unsafe("managed output parent is not a directory", relative)
                self._inside(cursor, relative)
            elif create_parent:
                self._reject_reparse(cursor.parent, relative)
                self._inside(cursor.parent, relative)
                try:
                    cursor.mkdir()
                except OSError as exc:
                    self._unsafe(f"managed directory could not be created: {exc}", relative)
                self._reject_reparse(cursor, relative)
                self._inside(cursor, relative)
            else:
                self._unsafe("managed output parent does not exist", relative)
        destination = self.root / relative
        if self._lexists(destination):
            self._reject_reparse(destination, relative)
            self._inside(destination, relative)
        self._reject_reparse(destination.parent, relative)
        self._inside(destination.parent, relative)
        return destination

    def ensure_dir(self, value: str | Path) -> Path:
        relative = self._relative(value)
        marker = relative / ".managed-directory-marker"
        self.path(marker, create_parent=True)
        directory = self.root / relative
        self._reject_reparse(directory, relative)
        self._inside(directory, relative)
        return directory

    @contextmanager
    def _write_lock(self):
        # The validated marker is created before ManagedOutput is exposed and is
        # always non-empty, avoiding a cross-thread/process lock-file init race.
        lock_path = self.path(ROOT_MARKER, create_parent=False)
        with lock_path.open("r+b") as stream:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def atomic_write_bytes(self, value: str | Path, content: bytes) -> Path:
        with self._write_lock():
            return self._atomic_write_bytes(value, content)

    def _managed_usage(self) -> int:
        """Measure stable files while tolerating SQLite sidecar removal races."""
        usage = 0
        for item in self.root.rglob("*"):
            try:
                metadata = item.stat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                usage += metadata.st_size
        return usage

    def _atomic_write_bytes(self, value: str | Path, content: bytes) -> Path:
        relative = self._relative(value)
        destination = self.path(relative, create_parent=True)
        existing_size = destination.stat().st_size if destination.exists() else 0
        usage = self._managed_usage()
        if usage - existing_size + len(content) > MAX_STATE_BYTES:
            raise CollectorError("state_size_limit", "managed state root exceeds 2 GiB")
        parent = destination.parent
        descriptor, temporary_raw = tempfile.mkstemp(
            dir=parent, prefix=f".{destination.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_raw)
        try:
            self._reject_reparse(temporary, relative)
            self._inside(temporary, relative)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            destination = self._replace_temporary(temporary, relative)
            self._reject_reparse(destination, relative)
            self._inside(destination, relative)
            return destination
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def atomic_write_text(self, value: str | Path, content: str) -> Path:
        return self.atomic_write_bytes(value, content.encode("utf-8"))

    def atomic_copy(
        self,
        source: Path,
        value: str | Path,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> Path:
        with self._write_lock():
            return self._atomic_copy(
                source,
                value,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
            )

    def _atomic_copy(
        self,
        source: Path,
        value: str | Path,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> Path:
        relative = self._relative(value)
        destination = self.path(relative, create_parent=True)
        existing_size = destination.stat().st_size if destination.exists() else 0
        source_size = source.stat().st_size
        usage = self._managed_usage()
        if usage - existing_size + source_size > MAX_STATE_BYTES:
            raise CollectorError("state_size_limit", "managed state root exceeds 2 GiB")
        parent = destination.parent
        descriptor, temporary_raw = tempfile.mkstemp(
            dir=parent, prefix=f".{destination.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_raw)
        try:
            source_before = source.lstat()
            source_attributes = getattr(source_before, "st_file_attributes", 0)
            if stat.S_ISLNK(source_before.st_mode) or source_attributes & REPARSE_POINT:
                raise CollectorError("migration_source_changed", "migration source is a link")
            if not stat.S_ISREG(source_before.st_mode):
                raise CollectorError("migration_source_changed", "migration source is not a file")
            self._reject_reparse(temporary, relative)
            self._inside(temporary, relative)
            with source.open("rb") as source_stream, os.fdopen(descriptor, "wb") as target:
                opened_before = os.fstat(source_stream.fileno())
                if (opened_before.st_dev, opened_before.st_ino) != (
                    source_before.st_dev,
                    source_before.st_ino,
                ):
                    raise CollectorError(
                        "migration_source_changed", "migration source changed before copy"
                    )
                digest = hashlib.sha256()
                copied = 0
                for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    copied += len(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
                opened_after = os.fstat(source_stream.fileno())
            source_after = source.lstat()
            identity_before = (
                source_before.st_dev,
                source_before.st_ino,
                source_before.st_size,
                source_before.st_mtime_ns,
            )
            identity_after = (
                source_after.st_dev,
                source_after.st_ino,
                source_after.st_size,
                source_after.st_mtime_ns,
            )
            opened_identity_after = (
                opened_after.st_dev,
                opened_after.st_ino,
                opened_after.st_size,
                opened_after.st_mtime_ns,
            )
            if identity_before != identity_after or identity_before != opened_identity_after:
                raise CollectorError(
                    "migration_source_changed", "migration source changed during copy"
                )
            if expected_size is not None and copied != expected_size:
                raise CollectorError("migration_source_changed", "migration source size changed")
            if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
                raise CollectorError("migration_source_changed", "migration source hash changed")
            destination = self._replace_temporary(temporary, relative)
            self._reject_reparse(destination, relative)
            self._inside(destination, relative)
            if expected_size is not None and destination.stat().st_size != expected_size:
                raise CollectorError("migration_copy_mismatch", "migration copy size mismatch")
            if expected_sha256 is not None:
                destination_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                if destination_digest != expected_sha256:
                    raise CollectorError("migration_copy_mismatch", "migration copy hash mismatch")
            return destination
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def append_text(self, value: str | Path, content: str) -> Path:
        with self._write_lock():
            relative = self._relative(value)
            destination = self.path(relative, create_parent=True)
            previous = destination.read_bytes() if destination.exists() else b""
            return self._atomic_write_bytes(relative, previous + content.encode("utf-8"))

    def remove_file(self, value: str | Path) -> None:
        """Remove one verified managed regular file for transactional rollback."""
        with self._write_lock():
            relative = self._relative(value)
            destination = self.path(relative, create_parent=False)
            if not destination.exists():
                return
            if not destination.is_file():
                self._unsafe("rollback target is not a regular file", relative)
            destination.unlink()
