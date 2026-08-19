import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from youtube_transcript_collector.contracts import RunStatus
from youtube_transcript_collector.errors import CollectorError
from youtube_transcript_collector.managed_output import ROOT_MARKER
from youtube_transcript_collector.state import StateStore
from youtube_transcript_collector.targets import build_request


def test_state_marker_schema_version_and_active_run_cap(tmp_path):
    store = StateStore(tmp_path)
    assert (tmp_path / ROOT_MARKER).read_text(encoding="ascii") == (
        "youtube-transcript-collector-v1\n"
    )
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    request = build_request(videos=["abcdefghijk"])
    for _ in range(4):
        store.create_run(request, None)
    with pytest.raises(CollectorError) as raised:
        store.create_run(request, None)
    assert raised.value.code == "active_run_limit"


def test_event_retention_is_bounded(tmp_path):
    store = StateStore(tmp_path)
    run, _ = store.create_run(build_request(videos=["abcdefghijk"]), None)
    for index in range(1005):
        store.add_event(run["run_id"], f"event-{index}")
    events = store.events(run["run_id"])
    assert len(events) == 1000
    assert events[0]["message"] == "event-5"


def test_stale_worker_lease_is_reconciled_when_process_is_absent(tmp_path):
    store = StateStore(tmp_path)
    run, _ = store.create_run(build_request(videos=["abcdefghijk"]), None)
    stale = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    with store.connect() as connection:
        connection.execute(
            "UPDATE runs SET status=?,pid=?,heartbeat_at=?,updated_at=? WHERE run_id=?",
            ("running", 2_000_000_000, stale, stale, run["run_id"]),
        )
    reconciled = store.reconcile_stale_run(run["run_id"])
    assert reconciled["status"] == "failed"
    assert reconciled["error_code"] == "worker_lease_expired"


def test_pidless_queued_run_expires_and_default_retry_creates_new_generation(tmp_path):
    store = StateStore(tmp_path)
    request = build_request(videos=["abcdefghijk"])
    first, _ = store.create_run(request, None)
    stale = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    with store.connect() as connection:
        connection.execute(
            "UPDATE runs SET updated_at=? WHERE run_id=?",
            (stale, first["run_id"]),
        )
    failed = store.reconcile_stale_run(first["run_id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "worker_lease_expired"
    retry, created = store.create_run(request, None)
    assert created is True
    assert retry["run_id"] != first["run_id"]
    assert retry["generation"] == first["generation"] + 1


def test_explicit_idempotency_key_replays_failed_outcome_until_caller_uses_new_key(tmp_path):
    store = StateStore(tmp_path)
    request = build_request(videos=["abcdefghijk"])
    first, _ = store.create_run(request, "stable-key")
    store.update_run(
        first["run_id"],
        RunStatus.FAILED,
        error_code="worker_lease_expired",
        error_message="failed",
    )
    replay, created = store.create_run(request, "stable-key")
    assert created is False
    assert replay["run_id"] == first["run_id"]
    assert replay["status"] == "failed"
    retry, retry_created = store.create_run(request, "caller-approved-retry-key")
    assert retry_created is True
    assert retry["run_id"] != first["run_id"]
    assert retry["generation"] == first["generation"] + 1
