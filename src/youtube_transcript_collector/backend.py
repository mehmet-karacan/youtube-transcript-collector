from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from .errors import CollectorError

MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_DISCOVERY_BYTES = 16 * 1024 * 1024
MAX_VTT_BYTES = 32 * 1024 * 1024
MAX_DISCOVERY_ITEMS = 10_000
ALLOWED_LIVE_STATUS = {None, "not_live", "is_live", "is_upcoming", "post_live", "was_live"}


def _windows_directory() -> Path:
    if os.name != "nt":
        raise RuntimeError("Windows directory requested on a non-Windows platform")
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise CollectorError("trusted_runtime_unavailable", "Windows system directory is unavailable")
    return Path(buffer.value).resolve(strict=True)


def normalize_metadata(value: object) -> dict[str, Any]:
    """Return a bounded allowlist; all string values remain untrusted data."""
    if not isinstance(value, dict):
        raise CollectorError("invalid_backend_metadata", "yt-dlp metadata must be an object")
    result: dict[str, Any] = {}
    limits = {
        "id": 11,
        "title": 500,
        "channel_id": 128,
        "upload_date": 8,
        "release_date": 8,
        "webpage_url": 2048,
    }
    for key, limit in limits.items():
        item = value.get(key)
        if item is None:
            result[key] = None
        elif isinstance(item, str) and len(item) <= limit:
            result[key] = item
        else:
            raise CollectorError("invalid_backend_metadata", f"invalid or oversized {key}")
    live_status = value.get("live_status")
    if live_status not in ALLOWED_LIVE_STATUS:
        raise CollectorError("invalid_backend_metadata", "invalid live_status")
    result["live_status"] = live_status
    duration = value.get("duration")
    if duration is not None and (not isinstance(duration, (int, float)) or duration < 0):
        raise CollectorError("invalid_backend_metadata", "invalid duration")
    result["duration"] = duration
    return result


class Backend(Protocol):
    def discover(self, target: str) -> list[dict[str, Any]]: ...

    def inspect(self, target: str) -> dict[str, Any]: ...

    def fetch_vtt(
        self, target: str, languages: tuple[str, ...]
    ) -> tuple[str | None, str | None]: ...


class YtDlpBackend:
    _process_pacing_lock = threading.Lock()
    _process_next_request_at = 0.0

    def __init__(
        self,
        *,
        executable: Path | None = None,
        min_delay: float = 1.0,
        retries: int = 3,
        cooldown_seconds: int = 900,
        cookie_file: Path | None = None,
        cancel_check: Callable[[], bool] | None = None,
        child_pid_changed: Callable[[int | None], None] | None = None,
        cooldown_get: Callable[[], str | None] | None = None,
        cooldown_set: Callable[[str], None] | None = None,
        pace_reserve: Callable[[float], float] | None = None,
        launch_guard: Callable[[float], AbstractContextManager[None]] | None = None,
        timeout_seconds: float = 300,
    ):
        if executable is None:
            self.command_prefix = [str(Path(sys.executable).resolve(strict=True)), "-I", "-m", "yt_dlp"]
        else:
            candidate = Path(executable)
            if not candidate.is_absolute():
                raise CollectorError(
                    "untrusted_yt_dlp_executable", "custom yt-dlp executable must be absolute"
                )
            resolved = candidate.resolve(strict=True)
            metadata = candidate.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            if candidate.is_symlink() or attributes & 0x400 or not resolved.is_file():
                raise CollectorError(
                    "untrusted_yt_dlp_executable", "custom yt-dlp executable is not trusted"
                )
            self.command_prefix = [str(resolved)]
        self.min_delay = max(0.0, min_delay)
        self.retries = max(0, retries)
        self.cooldown_seconds = cooldown_seconds
        self.cookie_file = cookie_file
        self.cancel_check = cancel_check or (lambda: False)
        self.child_pid_changed = child_pid_changed or (lambda _pid: None)
        self.cooldown_get = cooldown_get or (lambda: None)
        self.cooldown_set = cooldown_set or (lambda _value: None)
        self.pace_reserve = pace_reserve
        self.launch_guard = launch_guard or (lambda _delay: nullcontext())
        self.timeout_seconds = max(0.1, timeout_seconds)
        self._rate_limit_circuit_open = False

    def _base(self) -> list[str]:
        args = [
            *self.command_prefix,
            "--ignore-config",
            "--no-plugin-dirs",
            "--no-cache-dir",
            "--no-warnings",
            "--no-progress",
            "--retries",
            str(self.retries),
            "--extractor-retries",
            str(self.retries),
        ]
        return args

    @staticmethod
    def _environment() -> dict[str, str]:
        allowed = {"LANG", "LC_ALL", "NO_COLOR", "SSL_CERT_DIR", "SSL_CERT_FILE", "TEMP", "TMP", "TMPDIR"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        python_dir = str(Path(sys.executable).resolve(strict=True).parent)
        if os.name == "nt":
            windows = _windows_directory()
            system32 = windows / "System32"
            environment.update(
                {
                    "COMSPEC": str(system32 / "cmd.exe"),
                    "PATH": os.pathsep.join((python_dir, str(system32))),
                    "PATHEXT": ".COM;.EXE;.BAT;.CMD",
                    "SYSTEMROOT": str(windows),
                    "WINDIR": str(windows),
                }
            )
        else:
            environment["PATH"] = os.pathsep.join((python_dir, "/usr/bin", "/bin"))
        return environment

    def _check_cooldown(self) -> None:
        cooldown_until = self.cooldown_get()
        if cooldown_until:
            due = datetime.fromisoformat(cooldown_until.replace("Z", "+00:00"))
            if due > datetime.now(UTC):
                raise CollectorError(
                    "rate_limit_cooldown_active",
                    "persisted rate-limit cooldown is still active",
                    {"cooldown_until": cooldown_until},
                )

    def _reserve_process_slot(self) -> float:
        if self.pace_reserve is not None:
            return max(0.0, self.pace_reserve(self.min_delay))
        with self._process_pacing_lock:
            now = time.monotonic()
            slot = max(now, self._process_next_request_at)
            type(self)._process_next_request_at = slot + self.min_delay
            return slot - now

    def _wait_for_slot(self) -> None:
        deadline = time.monotonic() + self._reserve_process_slot()
        while time.monotonic() < deadline:
            if self.cancel_check():
                raise CollectorError("run_cancelled", "run cancellation was confirmed")
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def _terminate_tree(self, process: subprocess.Popen[Any], temporary: Path) -> None:
        if os.name == "nt" and type(process).__module__ == "subprocess":
            taskkill = _windows_directory() / "System32/taskkill.exe"
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                cwd=temporary,
                env=self._environment(),
            )
        elif os.name == "nt":
            process.terminate()
        elif type(process).__module__ == "subprocess":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name != "nt" and type(process).__module__ == "subprocess":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.kill()
            process.wait(timeout=5)

    def _run(
        self,
        args: list[str],
        *,
        max_output_bytes: int = MAX_METADATA_BYTES,
        watched_directory: Path | None = None,
        max_generated_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self._check_cooldown()
        if self._rate_limit_circuit_open:
            raise CollectorError(
                "rate_limit_circuit_open",
                "rate-limit circuit is open; wait before starting a new run",
            )
        self._wait_for_slot()
        if self.cancel_check():
            raise CollectorError("run_cancelled", "run cancellation was confirmed")
        with tempfile.TemporaryDirectory(prefix="yt-transcripts-process-") as temporary_raw:
            temporary = Path(temporary_raw)
            command = list(args)
            if self.cookie_file:
                try:
                    cookie_bytes = self.cookie_file.resolve(strict=True).read_bytes()
                except OSError as exc:
                    raise CollectorError("cookie_file_unreadable", "cookie file could not be read") from exc
                cookie_snapshot = temporary / "cookies.snapshot.txt"
                cookie_snapshot.write_bytes(cookie_bytes)
                cookie_snapshot.chmod(stat.S_IRUSR | stat.S_IWUSR)
                command.extend(["--cookies", str(cookie_snapshot)])
            stdout_path = temporary / "stdout.capture"
            stderr_path = temporary / "stderr.capture"
            with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
                kwargs: dict[str, Any] = {
                    "stdin": subprocess.DEVNULL,
                    "stdout": stdout_stream,
                    "stderr": stderr_stream,
                    "close_fds": True,
                    "cwd": temporary,
                    "env": self._environment(),
                }
                if os.name == "nt":
                    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    kwargs["start_new_session"] = True
                try:
                    with self.launch_guard(self.min_delay):
                        self._check_cooldown()
                        process = subprocess.Popen(command, **kwargs)
                        self.child_pid_changed(process.pid)
                except FileNotFoundError as exc:
                    raise CollectorError("yt_dlp_missing", "trusted Python runtime was not found") from exc
                deadline = time.monotonic() + self.timeout_seconds
                try:
                    while process.poll() is None:
                        if self.cancel_check():
                            self._terminate_tree(process, temporary)
                            raise CollectorError("run_cancelled", "run cancellation was confirmed")
                        captured = stdout_path.stat().st_size + stderr_path.stat().st_size
                        if captured > max_output_bytes:
                            self._terminate_tree(process, temporary)
                            raise CollectorError(
                                "yt_dlp_output_limit", "yt-dlp exceeded the output byte limit"
                            )
                        if watched_directory is not None and max_generated_bytes is not None:
                            generated = sum(
                                item.stat().st_size
                                for item in watched_directory.rglob("*")
                                if item.is_file()
                            )
                            if generated > max_generated_bytes:
                                self._terminate_tree(process, temporary)
                                raise CollectorError(
                                    "subtitle_size_limit", "subtitle exceeded the byte limit"
                                )
                        if time.monotonic() >= deadline:
                            self._terminate_tree(process, temporary)
                            raise CollectorError("yt_dlp_timeout", "yt-dlp exceeded its wall-time limit")
                        time.sleep(0.05)
                    process.wait(timeout=1)
                finally:
                    self.child_pid_changed(None)
            stdout_bytes = stdout_path.read_bytes()
            stderr_bytes = stderr_path.read_bytes()
            if len(stdout_bytes) + len(stderr_bytes) > max_output_bytes:
                raise CollectorError("yt_dlp_output_limit", "yt-dlp exceeded the output byte limit")
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode:
            message = (stderr or stdout or "yt-dlp failed").strip()[-2000:]
            code = "rate_limited" if "429" in message else "yt_dlp_failed"
            if code == "rate_limited":
                self._rate_limit_circuit_open = True
                cooldown = (
                    datetime.now(UTC) + timedelta(seconds=self.cooldown_seconds)
                ).isoformat()
                self.cooldown_set(cooldown.replace("+00:00", "Z"))
            raise CollectorError(code, message)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def _discover_page(self, target: str) -> list[dict[str, Any]]:
        result = self._run(
            [*self._base(), "--flat-playlist", "--dump-json", target],
            max_output_bytes=MAX_DISCOVERY_BYTES,
        )
        items: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            if len(items) >= MAX_DISCOVERY_ITEMS:
                raise CollectorError("discovery_limit", "channel discovery exceeded 10000 items")
            item = json.loads(line)
            if not isinstance(item, dict):
                raise CollectorError("invalid_backend_metadata", "discovery item must be an object")
            items.append(item)
        return items

    @staticmethod
    def _channel_tab(root: str, candidate: object) -> str | None:
        if not isinstance(candidate, str) or len(candidate) > 2048:
            return None
        root_parsed = urlparse(root)
        parsed = urlparse(candidate)
        if parsed.scheme != "https" or parsed.netloc != root_parsed.netloc:
            return None
        root_path = root_parsed.path.rstrip("/")
        for tab in ("videos", "shorts", "streams"):
            if parsed.path.rstrip("/") == f"{root_path}/{tab}" and not parsed.query:
                return candidate.rstrip("/")
        return None

    def discover(self, target: str) -> list[dict[str, Any]]:
        root_items = self._discover_page(target)
        tabs: list[str] = []
        videos: list[dict[str, Any]] = []
        for item in root_items:
            if item.get("_type") == "playlist":
                tab = self._channel_tab(target, item.get("webpage_url"))
                if tab and tab not in tabs:
                    tabs.append(tab)
                continue
            videos.append(item)

        # Recent yt-dlp versions may expose a channel root as nested Videos,
        # Shorts, and Live playlists. Expand only same-channel well-known tabs;
        # never follow an arbitrary backend-provided playlist URL.
        for tab in tabs:
            for item in self._discover_page(tab):
                if item.get("_type") != "playlist":
                    videos.append(item)
                if len(videos) > MAX_DISCOVERY_ITEMS:
                    raise CollectorError("discovery_limit", "channel discovery exceeded 10000 items")

        deduplicated: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in videos:
            video_id = item.get("id")
            if isinstance(video_id, str) and video_id not in seen:
                seen.add(video_id)
                deduplicated.append(item)
        return deduplicated

    def probe_version(self) -> str | None:
        result = self._run(
            [*self.command_prefix, "--ignore-config", "--no-plugin-dirs", "--no-cache-dir", "--version"],
            max_output_bytes=64 * 1024,
        )
        return result.stdout.strip() or None

    def inspect(self, target: str) -> dict[str, Any]:
        result = self._run([*self._base(), "--skip-download", "--dump-single-json", target])
        return normalize_metadata(json.loads(result.stdout))

    def fetch_vtt(self, target: str, languages: tuple[str, ...]) -> tuple[str | None, str | None]:
        # Languages are an ordered fallback contract. Requesting them together
        # lets a failure for a lower-priority translation discard an already
        # available preferred subtitle, so probe exactly one language at a time.
        for language in languages:
            with tempfile.TemporaryDirectory(prefix="yt-transcripts-") as temporary:
                # Never interpolate backend-provided IDs into a filesystem path.
                template = str(Path(temporary) / "subtitle.%(ext)s")
                args = [
                    *self._base(),
                    "--skip-download",
                    "--write-subs",
                    "--write-auto-subs",
                    "--sub-format",
                    "vtt",
                    "--sub-langs",
                    language,
                    "--match-filters",
                    "live_status != is_live & live_status != is_upcoming & live_status != post_live",
                    "--output",
                    template,
                    target,
                ]
                self._run(
                    args,
                    watched_directory=Path(temporary),
                    max_generated_bytes=MAX_VTT_BYTES,
                )
                files = sorted(Path(temporary).glob("*.vtt"))
                if not files:
                    continue
                chosen = files[0]
                if chosen.stat().st_size > MAX_VTT_BYTES:
                    raise CollectorError("subtitle_size_limit", "subtitle exceeded the byte limit")
                return chosen.read_text(encoding="utf-8", errors="replace"), language
        return None, None
