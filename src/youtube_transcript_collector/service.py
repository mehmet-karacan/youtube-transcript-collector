from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .backend import Backend, YtDlpBackend, _windows_directory
from .contracts import CollectionRequest, RunStatus, Scope
from .errors import CollectorError
from .manifest import export_manifest
from .state import StateStore, utc_now
from .targets import validate_video_id
from .text import safe_title, transcript_filename, vtt_to_text

DEFERRED_LIVE = {"is_live", "is_upcoming", "post_live"}


def _video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _normalize_date(raw: str | None) -> str | None:
    if not raw:
        return None
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    try:
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError:
        return None


def _worker_environment() -> dict[str, str]:
    allowed = {
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "YTC_COOKIE_FILE",
    }
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


class CollectorService:
    def __init__(
        self,
        state_dir: Path,
        backend: Backend | None = None,
        *,
        background_backend: str | None = None,
    ):
        self.store = StateStore(state_dir)
        self.backend = backend or YtDlpBackend()
        self.background_backend = background_backend

    def start(
        self,
        request: CollectionRequest,
        *,
        idempotency_key: str | None = None,
        background: bool = True,
    ) -> dict[str, Any]:
        request.validate()
        run, created = self.store.create_run(request, idempotency_key)
        if not created:
            return {"run": run, "created": False}
        self._log(run["run_id"], "run queued", {"request_digest": request.digest})
        if not background:
            self.execute(run["run_id"])
            return {"run": self.store.get_run(run["run_id"]), "created": True}
        command = [
            sys.executable,
            "-I",
            "-m",
            "youtube_transcript_collector.worker",
            "--state-dir",
            str(self.store.root),
            "--run-id",
            run["run_id"],
        ]
        if self.background_backend == "controlled-sleep":
            command.append("--test-controlled-backend")
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            "cwd": Path(sys.executable).resolve().parent,
            "env": _worker_environment(),
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        process = subprocess.Popen(command, **kwargs)
        self.store.update_run(run["run_id"], RunStatus.QUEUED, pid=process.pid)
        return {"run": self.store.get_run(run["run_id"]), "created": True}

    def execute(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        request = CollectionRequest.from_dict(run["request"])
        if RunStatus(run["status"]) == RunStatus.CANCEL_REQUESTED:
            self.store.update_run(run_id, RunStatus.CANCELLED, pid=None, active_child_pid=None)
            self._log(run_id, "run cancelled before worker start")
            return
        self.store.update_run(run_id, RunStatus.RUNNING, pid=os.getpid(), heartbeat_at=utc_now())
        self._log(run_id, "run started")
        try:
            candidates = self._candidates(request)
            for candidate in candidates:
                self.store.heartbeat(run_id)
                if self.store.cancel_requested(run_id):
                    self.store.update_run(
                        run_id, RunStatus.CANCELLED, pid=None, active_child_pid=None
                    )
                    self._log(run_id, "run cancelled")
                    return
                self._collect_candidate(run_id, candidate, request)
            manifest = export_manifest(self.store)
            self.store.update_run(run_id, RunStatus.SUCCEEDED, pid=None, active_child_pid=None)
            self._log(
                run_id,
                "run completed",
                {"manifest": str(manifest), "candidate_count": len(candidates)},
            )
        except CollectorError as exc:
            if exc.code == "run_cancelled":
                self.store.update_run(run_id, RunStatus.CANCELLED, pid=None, active_child_pid=None)
                self._log(run_id, "run cancellation confirmed")
                return
            self.store.update_run(
                run_id,
                RunStatus.FAILED,
                pid=None,
                active_child_pid=None,
                error_code=exc.code,
                error_message=exc.message,
            )
            self._log(
                run_id, "run failed", {"code": exc.code, "message": exc.message}, level="error"
            )
        except Exception as exc:  # defensive worker boundary
            self.store.update_run(
                run_id,
                RunStatus.FAILED,
                pid=None,
                active_child_pid=None,
                error_code="internal_error",
                error_message=str(exc),
            )
            self._log(
                run_id, "run failed", {"code": "internal_error", "message": str(exc)}, level="error"
            )

    def _candidates(self, request: CollectionRequest) -> list[dict[str, Any]]:
        if request.scope in {Scope.SINGLE_VIDEO, Scope.VIDEO_LIST}:
            return [{"url": target, "id": target.rsplit("=", 1)[-1]} for target in request.targets]
        discovered = self.backend.discover(request.targets[0])
        candidates = []
        query = request.selector.title_query.casefold() if request.selector.title_query else None
        for item in discovered:
            video_id = validate_video_id(item.get("id"), source="discovery")
            title = str(item.get("title") or "")
            if query and query not in title.casefold():
                continue
            candidates.append(
                {
                    "id": video_id,
                    "url": _video_url(video_id),
                }
            )
        if request.selector.latest:
            candidates = candidates[: request.selector.latest]
        return candidates

    def _collect_candidate(
        self, run_id: str, candidate: dict[str, Any], request: CollectionRequest
    ) -> None:
        video_id = validate_video_id(candidate.get("id"), source="candidate")
        existing = self.store.video(video_id)
        if existing and existing["state"] == "text-complete":
            integrity_error = self._completed_integrity_error(existing)
            if integrity_error is None:
                self._log(run_id, "video already complete", {"video_id": video_id})
                return
            existing.update(
                state="quarantined",
                state_reason=integrity_error,
                last_checked_at=utc_now(),
                next_retry_at=None,
            )
            self.store.upsert_video(existing)
            self._log(
                run_id,
                "completed transcript failed integrity verification; refetching",
                {"video_id": video_id, "reason": integrity_error},
                level="warning",
            )
        if existing and existing["state"] in {"deferred-live", "caption-unavailable"}:
            retry_at = existing.get("next_retry_at")
            if retry_at:
                due = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
                if due > datetime.now(UTC):
                    self._log(
                        run_id,
                        "video retry not due",
                        {"video_id": video_id, "next_retry_at": retry_at},
                    )
                    return
        metadata = self.backend.inspect(candidate["url"])
        metadata_id = validate_video_id(metadata.get("id"), source="metadata")
        if metadata_id != video_id:
            raise CollectorError(
                "backend_video_id_mismatch",
                "backend metadata ID does not match the requested video",
                {"requested_video_id": video_id, "metadata_video_id": metadata_id},
            )
        title = str(metadata.get("title") or video_id)
        live_status = metadata.get("live_status")
        date_source = (
            "release_date"
            if live_status == "was_live" and metadata.get("release_date")
            else "upload_date"
        )
        date = _normalize_date(metadata.get(date_source))
        if request.selector.date_from and (not date or date < request.selector.date_from):
            return
        if request.selector.date_to and (not date or date > request.selector.date_to):
            return
        common = {
            "video_id": video_id,
            "channel_id": metadata.get("channel_id"),
            "original_title": title,
            "safe_title": safe_title(title),
            "date": date,
            "date_source": date_source if date else "unknown",
            "webpage_url": metadata.get("webpage_url") or candidate["url"],
            "live_status": live_status,
            "language": None,
            "transcript_relpath": None,
            "transcript_sha256": None,
            "size_bytes": None,
            "last_checked_at": utc_now(),
            "attempt_count": (existing or {}).get("attempt_count", 0) + 1,
            "retry_policy_version": 1,
        }
        if live_status in DEFERRED_LIVE:
            common.update(
                state="deferred-live",
                state_reason=f"live-status-{live_status}",
                next_retry_at=(datetime.now(UTC) + timedelta(hours=6))
                .isoformat()
                .replace("+00:00", "Z"),
            )
            self.store.upsert_video(common)
            self._log(
                run_id, "live video deferred", {"video_id": video_id, "live_status": live_status}
            )
            return
        if live_status == "was_live" and not request.include_completed_live:
            return
        raw_vtt, language = self.backend.fetch_vtt(candidate["url"], request.languages)
        if raw_vtt is None:
            common.update(
                state="caption-unavailable",
                state_reason="requested-language-unavailable",
                next_retry_at=(datetime.now(UTC) + timedelta(days=1))
                .isoformat()
                .replace("+00:00", "Z"),
            )
            self.store.upsert_video(common)
            self._log(run_id, "caption unavailable", {"video_id": video_id})
            return
        text = vtt_to_text(raw_vtt)
        if not text.strip():
            common.update(
                state="quarantined", state_reason="empty-or-invalid-vtt", next_retry_at=None
            )
            self.store.upsert_video(common)
            self._log(run_id, "subtitle quarantined", {"video_id": video_id}, level="warning")
            return
        filename = transcript_filename(date, video_id, title)
        output = self.store.output.atomic_write_text(Path("subtitles") / filename, text)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        common.update(
            language=language,
            transcript_relpath=str(output.relative_to(self.store.root)).replace("\\", "/"),
            transcript_sha256=digest,
            size_bytes=output.stat().st_size,
            state="text-complete",
            state_reason="transcript-written",
            next_retry_at=None,
        )
        self.store.upsert_video(common)
        self._log(
            run_id,
            "transcript written",
            {"video_id": video_id, "path": common["transcript_relpath"]},
        )

    def status(self, run_id: str) -> dict[str, Any]:
        return self.store.reconcile_stale_run(run_id)

    def logs(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.events(run_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        terminal = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
        if RunStatus(run["status"]) in terminal:
            return run
        self.store.update_run(run_id, RunStatus.CANCEL_REQUESTED)
        self._log(run_id, "cancellation requested")
        return self.store.get_run(run_id)

    def _completed_integrity_error(self, record: dict[str, Any]) -> str | None:
        raw_path = record.get("transcript_relpath")
        if not isinstance(raw_path, str) or not raw_path:
            return "integrity-path-missing"
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            return "integrity-path-escape"
        root = self.store.root.resolve()
        unresolved = root / relative
        cursor = root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                return "integrity-symlink-rejected"
        path = unresolved.resolve()
        if not path.is_relative_to(root):
            return "integrity-path-escape"
        if not path.is_file():
            return "integrity-file-missing"
        if path.stat().st_size != record.get("size_bytes"):
            return "integrity-size-mismatch"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != record.get("transcript_sha256"):
            return "integrity-hash-mismatch"
        return None

    def _log(
        self, run_id: str, message: str, data: dict[str, Any] | None = None, *, level: str = "info"
    ) -> None:
        self.store.add_event(run_id, message, level=level, data=data)
        log_path = Path("runs") / f"{run_id}.log"
        payload = json.dumps(
            {"created_at": utc_now(), "level": level, "message": message, "data": data or {}},
            ensure_ascii=False,
            sort_keys=True,
        )
        self.store.output.append_text(log_path, payload + "\n")
