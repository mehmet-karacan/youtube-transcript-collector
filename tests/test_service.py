import hashlib
import json
import time
from pathlib import Path

import pytest

from youtube_transcript_collector.contracts import RunStatus, VideoState
from youtube_transcript_collector.errors import CollectorError
from youtube_transcript_collector.manifest import manifest_records
from youtube_transcript_collector.service import CollectorService
from youtube_transcript_collector.targets import build_request

FIXTURES = Path(__file__).parent / "fixtures"


class FakeBackend:
    def __init__(self, metadata=None, vtt=True):
        self.metadata = metadata or json.loads((FIXTURES / "metadata.json").read_text())
        self.vtt = vtt
        self.inspect_calls = 0

    def discover(self, target):
        return [
            {
                "id": "abcdefghijk",
                "title": "MCP: A Practical Guide",
                "url": "https://youtube.com/watch?v=abcdefghijk",
            }
        ]

    def inspect(self, target):
        self.inspect_calls += 1
        return self.metadata

    def fetch_vtt(self, target, languages):
        if not self.vtt:
            return None, None
        return (FIXTURES / "sample.vtt").read_text(), languages[0]


def test_collection_is_durable_and_idempotent(tmp_path):
    backend = FakeBackend()
    service = CollectorService(tmp_path, backend)
    request = build_request(videos=["abcdefghijk"])
    first = service.start(request, idempotency_key="stable-key", background=False)
    second = service.start(request, idempotency_key="stable-key", background=False)
    assert first["created"] is True
    assert second["created"] is False
    assert second["run"]["run_id"] == first["run"]["run_id"]
    assert service.status(first["run"]["run_id"])["status"] == RunStatus.SUCCEEDED
    assert backend.inspect_calls == 1
    record = manifest_records(tmp_path / "subtitles-index.jsonl")[0]
    assert record["state"] == VideoState.TEXT_COMPLETE
    assert (
        tmp_path / record["transcript_relpath"]
    ).read_text() == "Hello world\nAgent-ready transcript.\n"


def test_live_and_caption_states_do_not_complete(tmp_path):
    metadata = json.loads((FIXTURES / "metadata.json").read_text())
    metadata["live_status"] = "is_live"
    service = CollectorService(tmp_path / "live", FakeBackend(metadata))
    service.start(build_request(videos=["abcdefghijk"]), background=False)
    assert service.store.video("abcdefghijk")["state"] == VideoState.DEFERRED_LIVE
    assert not list((tmp_path / "live").glob("subtitles/*.txt"))

    service = CollectorService(tmp_path / "missing", FakeBackend(vtt=False))
    service.start(build_request(videos=["abcdefghijk"]), background=False)
    assert service.store.video("abcdefghijk")["state"] == VideoState.CAPTION_UNAVAILABLE


def test_was_live_prefers_release_date(tmp_path):
    metadata = json.loads((FIXTURES / "metadata.json").read_text())
    metadata.update(live_status="was_live", release_date="20260818")
    service = CollectorService(tmp_path, FakeBackend(metadata))
    service.start(build_request(videos=["abcdefghijk"]), background=False)
    record = service.store.video("abcdefghijk")
    assert record["date"] == "2026-08-18"
    assert record["date_source"] == "release_date"


def test_idempotency_conflict_and_cancel_queued_run(tmp_path):
    service = CollectorService(tmp_path, FakeBackend())
    first = build_request(videos=["abcdefghijk"])
    second = build_request(videos=["lmnopqrstuv"])
    run, _ = service.store.create_run(first, "one-effect")
    with pytest.raises(CollectorError) as raised:
        service.store.create_run(second, "one-effect")
    assert raised.value.code == "idempotency_conflict"
    cancelled = service.cancel(run["run_id"])
    assert cancelled["status"] == RunStatus.CANCEL_REQUESTED
    service.store.set_active_child(run["run_id"], None)
    assert service.status(run["run_id"])["status"] == RunStatus.CANCEL_REQUESTED


def test_negative_cache_skips_network_until_retry_is_due(tmp_path):
    backend = FakeBackend(vtt=False)
    service = CollectorService(tmp_path, backend)
    request = build_request(videos=["abcdefghijk"])
    service.start(request, background=False)
    service.start(request, background=False)
    assert backend.inspect_calls == 1


@pytest.mark.parametrize("damage", ["missing", "hash-mismatch"])
def test_completed_transcript_is_verified_and_refetched(tmp_path, damage):
    backend = FakeBackend()
    service = CollectorService(tmp_path, backend)
    request = build_request(videos=["abcdefghijk"])
    service.start(request, background=False)
    record = service.store.video("abcdefghijk")
    path = tmp_path / record["transcript_relpath"]
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"X" * record["size_bytes"])
    service.start(request, background=False)
    repaired = service.store.video("abcdefghijk")
    assert backend.inspect_calls == 2
    assert repaired["state"] == VideoState.TEXT_COMPLETE
    assert hashlib.sha256(path.read_bytes()).hexdigest() == repaired["transcript_sha256"]


def test_complete_path_escape_never_writes_outside_state_root(tmp_path):
    state_dir = tmp_path / "state"
    backend = FakeBackend()
    service = CollectorService(state_dir, backend)
    request = build_request(videos=["abcdefghijk"])
    service.start(request, background=False)
    record = service.store.video("abcdefghijk")
    outside = tmp_path / "outside.txt"
    outside.write_text("do-not-touch", encoding="utf-8")
    record.update(
        transcript_relpath="../outside.txt",
        size_bytes=outside.stat().st_size,
        transcript_sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
    )
    service.store.upsert_video(record)
    service.start(request, background=False)
    repaired = service.store.video("abcdefghijk")
    assert outside.read_text(encoding="utf-8") == "do-not-touch"
    assert not Path(repaired["transcript_relpath"]).is_absolute()
    assert (state_dir / repaired["transcript_relpath"]).is_file()


def test_invalid_discovery_id_fails_before_state_or_path_use(tmp_path):
    class MaliciousDiscovery(FakeBackend):
        def discover(self, target):
            return [{"id": "../outside", "title": "bad"}]

    service = CollectorService(tmp_path / "state", MaliciousDiscovery())
    result = service.start(build_request(channel="@FixtureChannel123"), background=False)
    assert result["run"]["status"] == RunStatus.FAILED
    assert result["run"]["error_code"] == "invalid_backend_video_id"
    assert service.store.videos() == []
    assert not (tmp_path / "outside.txt").exists()


def test_background_cancel_is_cooperative_and_confirmed(tmp_path):
    service = CollectorService(tmp_path, background_backend="controlled-sleep")
    result = service.start(build_request(videos=["abcdefghijk"]), background=True)
    run_id = result["run"]["run_id"]
    deadline = time.monotonic() + 10
    while service.status(run_id)["status"] != RunStatus.RUNNING:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    requested = service.cancel(run_id)
    assert requested["status"] in {RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED}
    if requested["status"] == RunStatus.CANCELLED:
        assert requested["pid"] is None
    while service.status(run_id)["status"] != RunStatus.CANCELLED:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    final = service.status(run_id)
    assert final["pid"] is None
    assert final["active_child_pid"] is None


def test_background_worker_ignores_hostile_cwd_and_pythonpath(monkeypatch, tmp_path):
    hostile = tmp_path / "hostile"
    package = hostile / "youtube_transcript_collector"
    package.mkdir(parents=True)
    marker = tmp_path / "hijacked.txt"
    payload = f"from pathlib import Path\nPath({str(marker)!r}).write_text('hijacked')\n"
    (package / "__init__.py").write_text(payload, encoding="utf-8")
    (package / "worker.py").write_text(payload, encoding="utf-8")
    monkeypatch.chdir(hostile)
    monkeypatch.setenv("PYTHONPATH", str(hostile))
    monkeypatch.setenv("PATH", str(hostile))
    monkeypatch.setenv("SYSTEMROOT", str(hostile))

    service = CollectorService(tmp_path / "state", background_backend="controlled-sleep")
    result = service.start(build_request(videos=["abcdefghijk"]), background=True)
    run_id = result["run"]["run_id"]
    deadline = time.monotonic() + 10
    while service.status(run_id)["status"] != RunStatus.RUNNING:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    service.cancel(run_id)
    while service.status(run_id)["status"] != RunStatus.CANCELLED:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert not marker.exists()
