from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "youtube-transcripts"
SPEC = importlib.util.spec_from_file_location("skill_portability", ROOT / "tools" / "skill_portability.py")
assert SPEC and SPEC.loader
portability = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(portability)


def test_target_contract_fixture_matches_implementation() -> None:
    fixture = json.loads((ROOT / "tests/fixtures/skill-targets.json").read_text(encoding="utf-8"))
    assert fixture == {name: path.as_posix() for name, path in portability.TARGETS.items()}


def test_canonical_skill_is_vendor_neutral_and_ui_metadata_is_optional(tmp_path: Path) -> None:
    copied = tmp_path / "youtube-transcripts"
    shutil.copytree(SKILL, copied)
    shutil.rmtree(copied / "agents")
    info = portability.validate_skill(copied)
    assert info.name == "youtube-transcripts"
    canonical = (copied / "SKILL.md").read_text(encoding="utf-8").casefold()
    assert all(vendor not in canonical for vendor in ("openai", "codex", "claude", "opencode", "gemini"))


@pytest.mark.parametrize("target", sorted(portability.TARGETS))
def test_installs_each_agent_target_without_agent_binaries(tmp_path: Path, target: str) -> None:
    digest = portability.validate_skill(SKILL).digest
    result = portability.install_skill(SKILL, tmp_path, [target], expected_digest=digest)
    destination = tmp_path / portability.TARGETS[target] / "youtube-transcripts"
    assert result["ok"] is True
    assert (destination / "SKILL.md").read_bytes() == (SKILL / "SKILL.md").read_bytes()
    assert (destination / "references/cli-contract.md").is_file()
    assert (destination / "agents/openai.yaml").is_file()
    assert destination.resolve().is_relative_to(tmp_path.resolve())


def test_multi_target_install_is_reproducible_and_idempotent(tmp_path: Path) -> None:
    targets = sorted(portability.TARGETS)
    digest = portability.validate_skill(SKILL).digest
    first = portability.install_skill(SKILL, tmp_path, targets, expected_digest=digest)
    second = portability.install_skill(SKILL, tmp_path, targets, expected_digest=digest)
    assert all(item["created"] for item in first["installs"])
    assert not any(item["created"] for item in second["installs"])
    assert first["digest"] == second["digest"]


def test_existing_modified_skill_fails_before_other_target_is_written(tmp_path: Path) -> None:
    bad = tmp_path / portability.TARGETS["opencode"] / "youtube-transcripts"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("changed", encoding="utf-8")
    with pytest.raises(portability.PortabilityError, match="existing skill differs"):
        portability.install_skill(
            SKILL,
            tmp_path,
            ["universal", "opencode"],
            expected_digest=portability.validate_skill(SKILL).digest,
        )
    assert not (tmp_path / portability.TARGETS["universal"] / "youtube-transcripts").exists()


def test_codex_and_universal_share_the_agents_skills_convention(tmp_path: Path) -> None:
    digest = portability.validate_skill(SKILL).digest
    result = portability.install_skill(
        SKILL, tmp_path, ["codex", "universal"], expected_digest=digest
    )
    assert {item["path"] for item in result["installs"]} == {
        ".agents/skills/youtube-transcripts"
    }
    assert (tmp_path / ".agents/skills/youtube-transcripts/SKILL.md").is_file()
    assert not (tmp_path / ".codex").exists()


def test_install_rejects_unreviewed_digest(tmp_path: Path) -> None:
    with pytest.raises(portability.PortabilityError, match="reviewed expected digest"):
        portability.install_skill(SKILL, tmp_path, ["universal"], expected_digest="0" * 64)


def test_install_rejects_symlinked_target_without_outside_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (workspace / ".agents").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    with pytest.raises(portability.PortabilityError, match="safe directory"):
        portability.install_skill(
            SKILL,
            workspace,
            ["universal"],
            expected_digest=portability.validate_skill(SKILL).digest,
        )
    assert list(outside.iterdir()) == []


def test_windows_replace_retries_transient_denial_and_rechecks_paths(tmp_path: Path) -> None:
    digest = portability.validate_skill(SKILL).digest
    real_replace = portability.os.replace
    real_verify = portability._verify_staged_replace
    replace_calls = 0
    verify_calls = 0

    def verified(*args):
        nonlocal verify_calls
        verify_calls += 1
        return real_verify(*args)

    def contended(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls <= 3:
            error = PermissionError("simulated Windows sharing contention")
            error.winerror = 5
            raise error
        return real_replace(source, destination)

    with (
        patch.object(portability, "IS_WINDOWS", True),
        patch.object(portability.time, "sleep"),
        patch.object(portability, "_verify_staged_replace", side_effect=verified),
        patch.object(portability.os, "replace", side_effect=contended),
    ):
        result = portability.install_skill(
            SKILL, tmp_path, ["opencode"], expected_digest=digest
        )

    assert result["installs"][0]["created"] is True
    assert replace_calls == 4
    assert verify_calls == replace_calls
    assert (tmp_path / ".opencode/skills/youtube-transcripts/SKILL.md").is_file()


def test_windows_replace_persistent_denial_propagates_and_cleans_stage(tmp_path: Path) -> None:
    digest = portability.validate_skill(SKILL).digest

    def denied(source, destination):
        error = PermissionError("persistent Windows access denial")
        error.winerror = 5
        raise error

    with (
        patch.object(portability, "IS_WINDOWS", True),
        patch.object(portability.time, "sleep"),
        patch.object(portability.os, "replace", side_effect=denied) as replace,
        pytest.raises(PermissionError, match="persistent Windows access denial"),
    ):
        portability.install_skill(SKILL, tmp_path, ["opencode"], expected_digest=digest)

    assert replace.call_count == 1 + len(portability.WINDOWS_REPLACE_RETRY_DELAYS)
    assert not (tmp_path / ".opencode/skills/youtube-transcripts").exists()
    assert list((tmp_path / ".opencode/skills").glob("*.tmp-*")) == []


def test_multi_target_persistent_denial_rolls_back_prior_install(tmp_path: Path) -> None:
    digest = portability.validate_skill(SKILL).digest
    real_replace = portability.os.replace

    def fail_opencode(source, destination):
        if ".opencode" in destination.parts:
            error = PermissionError("persistent second-target denial")
            error.winerror = 32
            raise error
        return real_replace(source, destination)

    with (
        patch.object(portability, "IS_WINDOWS", True),
        patch.object(portability.time, "sleep"),
        patch.object(portability.os, "replace", side_effect=fail_opencode),
        pytest.raises(PermissionError, match="persistent second-target denial"),
    ):
        portability.install_skill(
            SKILL,
            tmp_path,
            ["universal", "opencode"],
            expected_digest=digest,
        )

    assert not (tmp_path / ".agents/skills/youtube-transcripts").exists()
    assert not (tmp_path / ".opencode/skills/youtube-transcripts").exists()
    assert list(tmp_path.rglob("*.tmp-*")) == []
