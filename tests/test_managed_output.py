import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from youtube_transcript_collector.errors import CollectorError
from youtube_transcript_collector.managed_output import (
    WINDOWS_REPLACE_RETRY_DELAYS,
    ManagedOutput,
)
from youtube_transcript_collector.migration import apply_migration, preview_migration
from youtube_transcript_collector.state import StateStore


def _directory_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")


@pytest.mark.parametrize("relative", ["subtitles/video.txt", "runs/run.log"])
def test_intermediate_symlink_rejects_transcript_and_log_writes(tmp_path, relative):
    state = tmp_path / "state"
    outside = tmp_path / "outside"
    outside.mkdir()
    output = ManagedOutput(state)
    _directory_symlink(state / Path(relative).parts[0], outside)
    with pytest.raises(CollectorError) as raised:
        output.atomic_write_text(relative, "must stay inside")
    assert raised.value.code == "unsafe_managed_output_path"
    assert list(outside.iterdir()) == []


def test_destination_symlink_rejects_manifest_replace(tmp_path):
    state = tmp_path / "state"
    outside = tmp_path / "outside.jsonl"
    outside.write_text("sentinel", encoding="utf-8")
    output = ManagedOutput(state)
    try:
        (state / "subtitles-index.jsonl").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")
    with pytest.raises(CollectorError) as raised:
        output.atomic_write_text("subtitles-index.jsonl", "replacement")
    assert raised.value.code == "unsafe_managed_output_path"
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_migration_copy_rejects_symlinked_managed_subtitles(tmp_path):
    legacy = tmp_path / "legacy"
    legacy_subtitles = legacy / "subtitles"
    legacy_subtitles.mkdir(parents=True)
    (legacy_subtitles / "2026-08-18_abcdefghijk_Title.txt").write_text(
        "legacy transcript\n", encoding="utf-8"
    )
    preview = preview_migration(legacy)
    state = tmp_path / "state"
    StateStore(state)
    outside = tmp_path / "outside"
    outside.mkdir()
    _directory_symlink(state / "subtitles", outside)
    with pytest.raises(CollectorError) as raised:
        apply_migration(legacy, state, preview["plan_digest"])
    assert raised.value.code == "unsafe_managed_output_path"
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_windows_junction_is_rejected_for_root_and_nested_output(tmp_path):
    def junction(link, target):
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode:
            pytest.skip(f"junction creation is unavailable: {created.stderr or created.stdout}")

    outside = tmp_path / "outside"
    outside.mkdir()
    junction_root = tmp_path / "junction-root"
    junction(junction_root, outside)
    with pytest.raises(CollectorError) as root_error:
        ManagedOutput(junction_root)
    assert root_error.value.code == "unsafe_managed_output_path"

    state = tmp_path / "state"
    output = ManagedOutput(state)
    nested = state / "subtitles"
    junction(nested, outside)
    with pytest.raises(CollectorError) as nested_error:
        output.atomic_write_text("subtitles/video.txt", "blocked")
    assert nested_error.value.code == "unsafe_managed_output_path"

    log_target = tmp_path / "outside-logs"
    log_target.mkdir()
    junction(state / "runs", log_target)
    with pytest.raises(CollectorError) as log_error:
        output.append_text("runs/run.log", "blocked")
    assert log_error.value.code == "unsafe_managed_output_path"

    legacy = tmp_path / "legacy-junction"
    legacy_subtitles = legacy / "subtitles"
    legacy_subtitles.mkdir(parents=True)
    (legacy_subtitles / "2026-08-18_abcdefghijk_Title.txt").write_text(
        "legacy transcript\n", encoding="utf-8"
    )
    preview = preview_migration(legacy)
    migration_state = tmp_path / "migration-state"
    StateStore(migration_state)
    migration_target = tmp_path / "outside-migration"
    migration_target.mkdir()
    junction(migration_state / "subtitles", migration_target)
    with pytest.raises(CollectorError) as migration_error:
        apply_migration(legacy, migration_state, preview["plan_digest"])
    assert migration_error.value.code == "unsafe_managed_output_path"
    assert list(outside.iterdir()) == []
    assert list(log_target.iterdir()) == []
    assert list(migration_target.iterdir()) == []


def test_concurrent_log_appends_are_serialized(tmp_path):
    output = ManagedOutput(tmp_path / "state")

    def append(index):
        output.append_text("runs/shared.log", f"line-{index}\n")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(100)))
    lines = (tmp_path / "state/runs/shared.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    assert set(lines) == {f"line-{index}" for index in range(100)}


def test_usage_scan_tolerates_disappearing_sqlite_sidecar(tmp_path, monkeypatch):
    output = ManagedOutput(tmp_path / "state")
    sidecar = output.root / "state.sqlite3-wal"
    sidecar.write_bytes(b"transient")
    real_stat = Path.stat
    disappeared = False

    def racing_stat(path, *args, **kwargs):
        nonlocal disappeared
        if path == sidecar and not disappeared:
            disappeared = True
            sidecar.unlink()
            raise FileNotFoundError(sidecar)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", racing_stat)
    output.atomic_write_text("runs/race.log", "safe\n")

    assert disappeared is True
    assert (output.root / "runs/race.log").read_text(encoding="utf-8") == "safe\n"


def test_windows_replace_retries_transient_contention_and_rechecks_paths(tmp_path):
    output = ManagedOutput(tmp_path / "state")
    real_replace = os.replace
    replace_calls = 0
    path_checks = 0
    real_path = output.path

    def checked_path(*args, **kwargs):
        nonlocal path_checks
        path_checks += 1
        return real_path(*args, **kwargs)

    def contended_replace(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls <= 3:
            error = PermissionError("simulated Windows sharing contention")
            error.winerror = 5
            raise error
        return real_replace(source, destination)

    with (
        patch("youtube_transcript_collector.managed_output.IS_WINDOWS", True),
        patch("youtube_transcript_collector.managed_output.time.sleep"),
        patch.object(output, "path", side_effect=checked_path),
        patch(
            "youtube_transcript_collector.managed_output.os.replace",
            side_effect=contended_replace,
        ),
    ):
        output.atomic_write_text("runs/contended.log", "safe\n")

    assert replace_calls == 4
    assert path_checks >= replace_calls
    assert (tmp_path / "state/runs/contended.log").read_text(encoding="utf-8") == "safe\n"


def test_windows_replace_persistent_denial_is_not_masked_and_temp_is_cleaned(tmp_path):
    output = ManagedOutput(tmp_path / "state")

    def denied_replace(source, destination):
        error = PermissionError("persistent Windows access denial")
        error.winerror = 5
        raise error

    with (
        patch("youtube_transcript_collector.managed_output.IS_WINDOWS", True),
        patch("youtube_transcript_collector.managed_output.time.sleep"),
        patch(
            "youtube_transcript_collector.managed_output.os.replace",
            side_effect=denied_replace,
        ) as replace,
        pytest.raises(PermissionError, match="persistent Windows access denial"),
    ):
        output.atomic_write_text("runs/denied.log", "blocked\n")

    assert replace.call_count == 1 + len(WINDOWS_REPLACE_RETRY_DELAYS)
    assert not (tmp_path / "state/runs/denied.log").exists()
    assert list((tmp_path / "state/runs").glob("*.tmp")) == []
