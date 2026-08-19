from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .contracts import CollectionRequest, RunStatus
from .errors import CollectorError
from .managed_output import ManagedOutput


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA user_version=1;
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  request_digest TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 1,
  idempotency_key TEXT,
  request_json TEXT NOT NULL,
  status TEXT NOT NULL,
  pid INTEGER,
  active_child_pid INTEGER,
  heartbeat_at TEXT,
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(idempotency_key)
);
CREATE TABLE IF NOT EXISTS videos (
  video_id TEXT PRIMARY KEY,
  channel_id TEXT,
  original_title TEXT NOT NULL,
  safe_title TEXT NOT NULL,
  date TEXT,
  date_source TEXT,
  webpage_url TEXT NOT NULL,
  live_status TEXT,
  language TEXT,
  transcript_relpath TEXT,
  transcript_sha256 TEXT,
  size_bytes INTEGER,
  state TEXT NOT NULL,
  state_reason TEXT NOT NULL,
  last_checked_at TEXT NOT NULL,
  next_retry_at TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 1,
  retry_policy_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  level TEXT NOT NULL,
  message TEXT NOT NULL,
  data_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS runtime_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


class StateStore:
    def __init__(self, root: Path):
        self.output = ManagedOutput(root)
        self.root = self.output.root
        self.database = self.output.path("state.sqlite3", create_parent=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "active_child_pid" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN active_child_pid INTEGER")
            if "heartbeat_at" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN heartbeat_at TEXT")
            if "generation" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
            connection.execute("PRAGMA user_version=1")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.database = self.output.path("state.sqlite3", create_parent=False)
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def create_run(
        self, request: CollectionRequest, idempotency_key: str | None
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        with self.connect() as connection:
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM runs WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if existing:
                    if existing["request_digest"] != request.digest:
                        raise CollectorError(
                            "idempotency_conflict",
                            "idempotency key was already used for a different request",
                            {"run_id": existing["run_id"]},
                        )
                    return self.get_run(existing["run_id"], connection=connection), False
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM runs WHERE status IN (?,?,?)",
                (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED),
            ).fetchone()["count"]
            if active >= 4:
                raise CollectorError("active_run_limit", "state root already has 4 active runs")
            run_id = f"run_{uuid.uuid4().hex}"
            generation = connection.execute(
                "SELECT COALESCE(MAX(generation),0)+1 AS generation FROM runs WHERE request_digest=?",
                (request.digest,),
            ).fetchone()["generation"]
            connection.execute(
                """INSERT INTO runs(
                  run_id,request_digest,generation,idempotency_key,request_json,status,pid,
                  active_child_pid,heartbeat_at,error_code,error_message,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    request.digest,
                    generation,
                    idempotency_key,
                    json.dumps(request.as_dict(), sort_keys=True),
                    RunStatus.QUEUED,
                    None,
                    None,
                    None,
                    None,
                    None,
                    now,
                    now,
                ),
            )
            return self.get_run(run_id, connection=connection), True

    def get_run(
        self, run_id: str, *, connection: sqlite3.Connection | None = None
    ) -> dict[str, Any]:
        if connection is not None:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        else:
            with self.connect() as current:
                row = current.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise CollectorError("run_not_found", "run does not exist", {"run_id": run_id})
        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        return result

    def update_run(self, run_id: str, status: RunStatus, **fields: Any) -> None:
        allowed = {"pid", "active_child_pid", "heartbeat_at", "error_code", "error_message"}
        if unknown := set(fields) - allowed:
            raise ValueError(f"unsupported run fields: {unknown}")
        values = {"status": status.value, "updated_at": utc_now(), **fields}
        assignments = ", ".join(f"{key}=?" for key in values)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE runs SET {assignments} WHERE run_id=?", (*values.values(), run_id)
            )

    def add_event(
        self, run_id: str, message: str, *, level: str = "info", data: dict[str, Any] | None = None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO events(run_id,created_at,level,message,data_json) VALUES (?,?,?,?,?)",
                (
                    run_id,
                    utc_now(),
                    level,
                    message,
                    json.dumps(data or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            connection.execute(
                """DELETE FROM events WHERE event_id IN (
                  SELECT event_id FROM events WHERE run_id=? ORDER BY event_id DESC LIMIT -1 OFFSET 1000
                )""",
                (run_id,),
            )

    def events(self, run_id: str) -> list[dict[str, Any]]:
        self.get_run(run_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT created_at,level,message,data_json FROM events WHERE run_id=? ORDER BY event_id",
                (run_id,),
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["data"] = json.loads(event.pop("data_json"))
            events.append(event)
        return events

    def cancel_requested(self, run_id: str) -> bool:
        return self.get_run(run_id)["status"] == RunStatus.CANCEL_REQUESTED

    def set_active_child(self, run_id: str, pid: int | None) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET active_child_pid=?,updated_at=? WHERE run_id=?",
                (pid, utc_now(), run_id),
            )

    def heartbeat(self, run_id: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET heartbeat_at=?,updated_at=? WHERE run_id=?",
                (now, now, run_id),
            )

    def reconcile_stale_run(self, run_id: str, *, stale_seconds: int = 30) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] not in {
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.CANCEL_REQUESTED,
        }:
            return run
        observed = run.get("heartbeat_at") or run["updated_at"]
        age = datetime.now(UTC) - datetime.fromisoformat(observed.replace("Z", "+00:00"))
        if age.total_seconds() <= stale_seconds:
            return run
        if not run.get("pid"):
            self.update_run(
                run_id,
                RunStatus.FAILED,
                pid=None,
                active_child_pid=None,
                error_code="worker_lease_expired",
                error_message="queued worker never established a heartbeat lease",
            )
            return self.get_run(run_id)
        try:
            os.kill(int(run["pid"]), 0)
            return run
        except (OSError, ValueError):
            self.update_run(
                run_id,
                RunStatus.FAILED,
                pid=None,
                active_child_pid=None,
                error_code="worker_lease_expired",
                error_message="worker heartbeat expired and its process no longer exists",
            )
            return self.get_run(run_id)

    def get_runtime_value(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM runtime_state WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def set_runtime_value(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO runtime_state(key,value,updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (key, value, utc_now()),
            )

    def reserve_request_slot(self, min_delay_seconds: float) -> float:
        """Atomically reserve a launch time shared by all workers for this state root."""
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC)
            cooldown_row = connection.execute(
                "SELECT value FROM runtime_state WHERE key=?",
                ("youtube_rate_limit_cooldown_until",),
            ).fetchone()
            if cooldown_row:
                cooldown = datetime.fromisoformat(str(cooldown_row["value"]).replace("Z", "+00:00"))
                if cooldown > now:
                    raise CollectorError(
                        "rate_limit_cooldown_active",
                        "persisted rate-limit cooldown is still active",
                        {"cooldown_until": cooldown_row["value"]},
                    )
            next_row = connection.execute(
                "SELECT value FROM runtime_state WHERE key=?",
                ("youtube_next_request_at",),
            ).fetchone()
            next_at = now
            if next_row:
                next_at = max(
                    now,
                    datetime.fromisoformat(str(next_row["value"]).replace("Z", "+00:00")),
                )
            following = next_at + timedelta(seconds=max(0.0, min_delay_seconds))
            connection.execute(
                """INSERT INTO runtime_state(key,value,updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (
                    "youtube_next_request_at",
                    following.isoformat().replace("+00:00", "Z"),
                    utc_now(),
                ),
            )
            connection.execute("COMMIT")
            return max(0.0, (next_at - now).total_seconds())
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @contextmanager
    def request_launch_guard(self, min_delay_seconds: float) -> Iterator[None]:
        """Serialize cooldown validation and actual child-launch spacing per state root."""
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM runtime_state WHERE key=?",
                ("youtube_rate_limit_cooldown_until",),
            ).fetchone()
            if row:
                cooldown = datetime.fromisoformat(str(row["value"]).replace("Z", "+00:00"))
                if cooldown > datetime.now(UTC):
                    raise CollectorError(
                        "rate_limit_cooldown_active",
                        "persisted rate-limit cooldown is still active",
                        {"cooldown_until": row["value"]},
                    )
            launch_row = connection.execute(
                "SELECT value FROM runtime_state WHERE key=?",
                ("youtube_last_request_launch_at",),
            ).fetchone()
            if launch_row:
                previous = datetime.fromisoformat(
                    str(launch_row["value"]).replace("Z", "+00:00")
                )
                due = previous + timedelta(seconds=max(0.0, min_delay_seconds))
                remaining = (due - datetime.now(UTC)).total_seconds()
                if remaining > 0:
                    time.sleep(remaining)
            launched_at = utc_now()
            connection.execute(
                """INSERT INTO runtime_state(key,value,updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                ("youtube_last_request_launch_at", launched_at, launched_at),
            )
            yield
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def upsert_video(self, record: dict[str, Any]) -> None:
        self.upsert_videos([record])

    def upsert_videos(self, records: list[dict[str, Any]]) -> None:
        columns = (
            "video_id",
            "channel_id",
            "original_title",
            "safe_title",
            "date",
            "date_source",
            "webpage_url",
            "live_status",
            "language",
            "transcript_relpath",
            "transcript_sha256",
            "size_bytes",
            "state",
            "state_reason",
            "last_checked_at",
            "next_retry_at",
            "attempt_count",
            "retry_policy_version",
        )
        updates = ",".join(f"{column}=excluded.{column}" for column in columns[1:])
        with self.connect() as connection:
            for record in records:
                values = [record.get(column) for column in columns]
                connection.execute(
                    f"INSERT INTO videos ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                    f"ON CONFLICT(video_id) DO UPDATE SET {updates}",
                    values,
                )

    def video(self, video_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM videos WHERE video_id=?", (video_id,)
            ).fetchone()
        return dict(row) if row else None

    def videos(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM videos ORDER BY video_id").fetchall()
        return [dict(row) for row in rows]
