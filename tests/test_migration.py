import pytest

from youtube_transcript_collector.errors import CollectorError
from youtube_transcript_collector.managed_output import ManagedOutput
from youtube_transcript_collector.migration import apply_migration, preview_migration
from youtube_transcript_collector.service import CollectorService
from youtube_transcript_collector.state import StateStore
from youtube_transcript_collector.targets import build_request


class _NoNetworkBackend:
    def discover(self, target):
        raise AssertionError("migration reuse must not discover")

    def inspect(self, target):
        raise AssertionError("migration reuse must not inspect")

    def fetch_vtt(self, target, languages):
        raise AssertionError("migration reuse must not fetch")


def test_migration_preview_and_exact_apply_do_not_mutate_legacy(tmp_path):
    legacy = tmp_path / "legacy"
    subtitles = legacy / "subtitles"
    subtitles.mkdir(parents=True)
    transcript = subtitles / "2026-08-18_abcdefghijk_Title.txt"
    transcript.write_text("hello\n", encoding="utf-8")
    archive = legacy / ".download-archive.txt"
    archive.write_text("youtube abcdefghijk\nyoutube lmnopqrstuv\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (transcript, archive)}
    preview = preview_migration(legacy)
    assert preview["subtitle_count"] == 1
    assert preview["archive_count"] == 2
    assert preview["archive_only_ids"] == ["lmnopqrstuv"]
    with pytest.raises(CollectorError):
        apply_migration(legacy, tmp_path / "state", "0" * 64)
    result = apply_migration(legacy, tmp_path / "state", preview["plan_digest"])
    assert result["legacy_mutated"] is False
    assert result["archive_only_registered"] == 1
    store = StateStore(tmp_path / "state")
    imported = store.video("abcdefghijk")
    assert imported["state"] == "text-complete"
    copied = (tmp_path / "state" / imported["transcript_relpath"]).read_bytes()
    assert copied == transcript.read_bytes()
    archive_only = store.video("lmnopqrstuv")
    assert archive_only["state"] == "legacy-archive-only"
    assert archive_only["transcript_relpath"] is None
    reuse = CollectorService(tmp_path / "state", _NoNetworkBackend()).start(
        build_request(videos=["abcdefghijk"]), background=False
    )
    assert reuse["run"]["status"] == "succeeded"
    assert all(path.read_bytes() == content for path, content in before.items())


def test_migration_rejects_source_mutation_at_copy_boundary(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy"
    subtitles = legacy / "subtitles"
    subtitles.mkdir(parents=True)
    transcript = subtitles / "2026-08-18_abcdefghijk_Title.txt"
    transcript.write_text("planned\n", encoding="utf-8")
    preview = preview_migration(legacy)
    original = ManagedOutput.atomic_copy

    def mutate_then_copy(self, source, value, **kwargs):
        source.write_text("attacker replacement\n", encoding="utf-8")
        return original(self, source, value, **kwargs)

    monkeypatch.setattr(ManagedOutput, "atomic_copy", mutate_then_copy)
    state = tmp_path / "state"
    with pytest.raises(CollectorError) as raised:
        apply_migration(legacy, state, preview["plan_digest"])
    assert raised.value.code == "migration_source_changed"
    assert not (state / "subtitles" / transcript.name).exists()
    assert StateStore(state).videos() == []


def test_multifile_migration_rolls_back_prior_copies_on_later_failure(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy"
    subtitles = legacy / "subtitles"
    subtitles.mkdir(parents=True)
    first = subtitles / "2026-08-17_abcdefghijk_First.txt"
    second = subtitles / "2026-08-18_lmnopqrstuv_Second.txt"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    preview = preview_migration(legacy)
    original = ManagedOutput.atomic_copy
    calls = 0

    def fail_second(self, source, value, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CollectorError("simulated_copy_failure", "simulated second copy failure")
        return original(self, source, value, **kwargs)

    monkeypatch.setattr(ManagedOutput, "atomic_copy", fail_second)
    state = tmp_path / "state"
    with pytest.raises(CollectorError) as raised:
        apply_migration(legacy, state, preview["plan_digest"])
    assert raised.value.code == "simulated_copy_failure"
    assert not (state / "subtitles" / first.name).exists()
    assert not (state / "subtitles" / second.name).exists()
    assert StateStore(state).videos() == []
