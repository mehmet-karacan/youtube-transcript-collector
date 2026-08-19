import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from tools.release_provenance import (
    ProvenanceError,
    build_sbom,
    encoded_sbom,
    main,
    verify_lock,
    verify_manifest,
    verify_wheelhouse,
)

ROOT = Path(__file__).parents[1]
LOCK = ROOT / "requirements/ci-windows-py314.lock"
CONSTRAINTS = ROOT / "constraints-ci.txt"
PYPROJECT = ROOT / "pyproject.toml"
MANIFEST = ROOT / "provenance/locked-artifacts.json"


def _artifacts(tmp_path):
    wheel = tmp_path / "youtube_transcript_collector-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("youtube_transcript_collector/__init__.py", "")
        archive.writestr(
            "youtube_transcript_collector-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: youtube-transcript-collector\n"
            "Version: 0.1.0\nRequires-Dist: yt-dlp<2027,>=2025.1.1\n",
        )
    sdist = tmp_path / "youtube_transcript_collector-0.1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for relative in (
            "README.md",
            "constraints-ci.txt",
            "pyproject.toml",
            "provenance/locked-artifacts.json",
            "requirements/ci-windows-py314.lock",
            "tools/release_provenance.py",
            "skills/youtube-transcripts/SKILL.md",
            "schemas/collection-request.schema.json",
        ):
            data = b"source\n"
            info = tarfile.TarInfo(f"youtube_transcript_collector-0.1.0/{relative}")
            info.size = len(data)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return wheel, sdist


def test_release_lock_matches_constraints_and_direct_requirements():
    locked = verify_lock(LOCK, CONSTRAINTS, PYPROJECT)
    artifacts = verify_manifest(MANIFEST, locked)
    assert locked["yt-dlp"].version == "2026.7.4"
    assert len(locked) == 20
    assert len(artifacts) == 20


def test_release_lock_rejects_constraint_drift(tmp_path):
    changed = tmp_path / "constraints.txt"
    changed.write_text(
        CONSTRAINTS.read_text(encoding="utf-8").replace("pytest==9.1.1", "pytest==9.1.0"),
        encoding="utf-8",
    )
    with pytest.raises(ProvenanceError, match="lock/constraint drift"):
        verify_lock(LOCK, changed, PYPROJECT)


def test_wheelhouse_validation_requires_every_locked_distribution(tmp_path):
    locked = verify_lock(LOCK, CONSTRAINTS, PYPROJECT)
    with pytest.raises(ProvenanceError, match="wheelhouse lacks locked bytes"):
        verify_wheelhouse(locked, tmp_path)


@pytest.mark.parametrize("drift", ["missing", "source", "hash"])
def test_manifest_rejects_missing_source_and_hash_drift(tmp_path, drift):
    locked = verify_lock(LOCK, CONSTRAINTS, PYPROJECT)
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if drift == "missing":
        document["artifacts"].pop()
    elif drift == "source":
        document["artifacts"][0]["source_index_url"] = "https://example.invalid/simple/"
    else:
        document["artifacts"][0]["sha256"] = "0" * 64
    changed = tmp_path / "manifest.json"
    changed.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ProvenanceError, match="provenance"):
        verify_manifest(changed, locked)


def test_sbom_is_deterministic_and_detects_artifact_mismatch(tmp_path):
    wheel, sdist = _artifacts(tmp_path)
    first = build_sbom(LOCK, CONSTRAINTS, PYPROJECT, MANIFEST, wheel, sdist)
    second = build_sbom(LOCK, CONSTRAINTS, PYPROJECT, MANIFEST, wheel, sdist)
    assert encoded_sbom(first) == encoded_sbom(second)
    assert "timestamp" not in first["metadata"]
    dependency_rows = {row["ref"]: row["dependsOn"] for row in first["dependencies"]}
    assert dependency_rows["pkg:pypi/youtube-transcript-collector@0.1.0"] == [
        "pkg:pypi/yt-dlp@2026.7.4"
    ]

    sbom = tmp_path / "project.cdx.json"
    sbom.write_bytes(encoded_sbom(first))
    assert main(
        [
            "--lock",
            str(LOCK),
            "--constraints",
            str(CONSTRAINTS),
            "--pyproject",
            str(PYPROJECT),
            "--manifest",
            str(MANIFEST),
            "check",
            "--wheel",
            str(wheel),
            "--sdist",
            str(sdist),
            "--sbom",
            str(sbom),
        ]
    ) == 0

    with wheel.open("ab") as stream:
        stream.write(b"tampered")
    assert main(
        [
            "--lock",
            str(LOCK),
            "--constraints",
            str(CONSTRAINTS),
            "--pyproject",
            str(PYPROJECT),
            "--manifest",
            str(MANIFEST),
            "check",
            "--wheel",
            str(wheel),
            "--sdist",
            str(sdist),
            "--sbom",
            str(sbom),
        ]
    ) == 1


def test_generated_sbom_is_cyclonedx_json(tmp_path):
    wheel, sdist = _artifacts(tmp_path)
    payload = json.loads(
        encoded_sbom(build_sbom(LOCK, CONSTRAINTS, PYPROJECT, MANIFEST, wheel, sdist))
    )
    assert payload["bomFormat"] == "CycloneDX"
    assert payload["specVersion"] == "1.6"
    assert any(component["name"] == "yt-dlp" for component in payload["components"])


def test_sbom_ci_build_graph_has_no_orphaned_locked_component(tmp_path):
    wheel, sdist = _artifacts(tmp_path)
    payload = build_sbom(LOCK, CONSTRAINTS, PYPROJECT, MANIFEST, wheel, sdist)
    dependencies = {row["ref"]: set(row["dependsOn"]) for row in payload["dependencies"]}
    pending = ["urn:youtube-transcript-collector:ci-build-environment"]
    reachable = set()
    while pending:
        reference = pending.pop()
        if reference in reachable:
            continue
        reachable.add(reference)
        pending.extend(dependencies.get(reference, ()))
    ci_components = {
        component["bom-ref"]
        for component in payload["components"]
        if {item["value"] for item in component.get("properties", [])} == {"ci-build"}
    }
    assert ci_components
    assert ci_components <= reachable
    assert dependencies["pkg:pypi/build@1.5.0"] == {
        "pkg:pypi/colorama@0.4.6",
        "pkg:pypi/packaging@26.3",
        "pkg:pypi/pyproject-hooks@1.2.0",
    }
