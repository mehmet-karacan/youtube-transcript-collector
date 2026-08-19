#!/usr/bin/env python3
"""Verify the release lock and emit/check a deterministic CycloneDX SBOM."""

from __future__ import annotations

import argparse
import email
import hashlib
import json
import re
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from packaging.markers import default_environment
from packaging.requirements import Requirement

LOCK_TARGET = "# Target: CPython 3.14, Windows x86-64."
LOCK_INDEX = "--index-url https://pypi.org/simple/"
LOCK_BINARY_POLICY = "--only-binary :all:"
LOCK_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[A-Za-z0-9_.+-]+) "
    r"--hash=sha256:(?P<sha256>[0-9a-f]{64})$"
)
CONSTRAINT_LINE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>\S+)$")
REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)(?P<spec>.*)$")
SOURCE_DATE_EPOCH = "1750000000"
TOOL_VERSION = "1"
MANIFEST_SCHEMA_VERSION = 1
SOURCE_INDEX = "https://pypi.org/simple/"
ACQUISITION_BASIS = (
    "pre-existing local pip cache response body copied into a complete offline "
    "wheelhouse; SHA-256 and standard wheel metadata recomputed locally"
)
CI_ROOT_REF = "urn:youtube-transcript-collector:ci-build-environment"
ARTIFACT_ROOT_REF = "urn:youtube-transcript-collector:release-artifacts"
FORBIDDEN_PARTS = {".git", ".local-plans", "dist", "sbom"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key"}


class ProvenanceError(ValueError):
    pass


@dataclass(frozen=True)
class LockedDistribution:
    name: str
    version: str
    sha256: str

    @property
    def normalized_name(self) -> str:
        return normalize_name(self.name)


def _wheel_metadata(path: Path) -> dict[str, object]:
    if path.suffix.lower() != ".whl":
        raise ProvenanceError(f"provenance input is not a wheel: {path.name}")
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            wheel_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")
            ]
            if len(metadata_names) != 1 or len(wheel_names) != 1:
                raise ProvenanceError(f"wheel metadata layout is invalid: {path.name}")
            metadata = email.message_from_bytes(archive.read(metadata_names[0]))
            wheel = email.message_from_bytes(archive.read(wheel_names[0]))
    except zipfile.BadZipFile as exc:
        raise ProvenanceError(f"invalid wheel archive: {path.name}") from exc
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise ProvenanceError(f"wheel name/version metadata is missing: {path.name}")
    return {
        "name": normalize_name(name),
        "version": version,
        "wheel_filename": path.name,
        "sha256": sha256(path),
        "source_index_url": f"{SOURCE_INDEX}{normalize_name(name)}/",
        "acquisition_basis": ACQUISITION_BASIS,
        "wheel_tags": sorted(wheel.get_all("Tag", [])),
        "requires_dist": metadata.get_all("Requires-Dist", []),
    }


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_lock(path: Path) -> dict[str, LockedDistribution]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if LOCK_TARGET not in lines:
        raise ProvenanceError("lock target declaration is missing or changed")
    if lines.count(LOCK_INDEX) != 1 or lines.count(LOCK_BINARY_POLICY) != 1:
        raise ProvenanceError("lock must declare the reviewed index and wheel-only policy")
    records: dict[str, LockedDistribution] = {}
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line in {LOCK_INDEX, LOCK_BINARY_POLICY}:
            continue
        match = LOCK_LINE.fullmatch(line)
        if match is None:
            raise ProvenanceError(f"invalid hash-locked requirement at line {line_number}")
        record = LockedDistribution(**match.groupdict())
        key = record.normalized_name
        if key in records:
            raise ProvenanceError(f"duplicate locked distribution: {key}")
        records[key] = record
    if not records:
        raise ProvenanceError("release lock is empty")
    return records


def parse_constraints(path: Path) -> dict[str, str]:
    records: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = CONSTRAINT_LINE.fullmatch(line)
        if match is None:
            raise ProvenanceError(f"constraint is not exact at line {line_number}")
        key = normalize_name(match.group("name"))
        if key in records:
            raise ProvenanceError(f"duplicate constraint: {key}")
        records[key] = match.group("version")
    return records


def _version_key(version: str) -> tuple[int, ...]:
    if re.fullmatch(r"\d+(?:\.\d+)*", version) is None:
        raise ProvenanceError(f"unsupported version grammar in release input: {version}")
    return tuple(int(part) for part in version.split("."))


def _requirement_allows(requirement: str, locked: LockedDistribution) -> bool:
    match = REQUIREMENT.fullmatch(requirement)
    if match is None or normalize_name(match.group("name")) != locked.normalized_name:
        return False
    locked_key = _version_key(locked.version)
    spec = match.group("spec")
    for clause in filter(None, (part.strip() for part in spec.split(","))):
        operator = next((item for item in (">=", "<=", "==", ">", "<") if clause.startswith(item)), None)
        if operator is None:
            raise ProvenanceError(f"unsupported requirement clause: {clause}")
        expected = _version_key(clause[len(operator) :])
        checks = {
            ">=": locked_key >= expected,
            "<=": locked_key <= expected,
            "==": locked_key == expected,
            ">": locked_key > expected,
            "<": locked_key < expected,
        }
        if not checks[operator]:
            return False
    return True


def verify_lock(lock_path: Path, constraints_path: Path, pyproject_path: Path) -> dict[str, LockedDistribution]:
    locked = parse_lock(lock_path)
    constrained = parse_constraints(constraints_path)
    locked_versions = {name: record.version for name, record in locked.items()}
    if locked_versions != constrained:
        missing = sorted(set(constrained) - set(locked_versions))
        extra = sorted(set(locked_versions) - set(constrained))
        changed = sorted(
            name
            for name in set(locked_versions) & set(constrained)
            if locked_versions[name] != constrained[name]
        )
        raise ProvenanceError(
            f"lock/constraint drift: missing={missing}, extra={extra}, changed={changed}"
        )
    project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    direct_requirements = [
        *project["build-system"]["requires"],
        *project["project"]["dependencies"],
        *project["project"]["optional-dependencies"]["dev"],
    ]
    for requirement in direct_requirements:
        name_match = REQUIREMENT.fullmatch(requirement)
        if name_match is None:
            raise ProvenanceError(f"unsupported direct requirement: {requirement}")
        name = normalize_name(name_match.group("name"))
        record = locked.get(name)
        if record is None or not _requirement_allows(requirement, record):
            raise ProvenanceError(f"direct requirement is not satisfied by lock: {requirement}")
    return locked


def verify_wheelhouse(locked: dict[str, LockedDistribution], wheelhouse: Path) -> None:
    available: dict[str, list[Path]] = {}
    for item in wheelhouse.iterdir():
        if item.is_file():
            available.setdefault(sha256(item), []).append(item)
    missing = [record.name for record in locked.values() if record.sha256 not in available]
    if missing:
        raise ProvenanceError(f"wheelhouse lacks locked bytes: {sorted(missing)}")


def build_manifest(
    locked: dict[str, LockedDistribution], wheelhouse: Path
) -> dict[str, object]:
    wheels = sorted(item for item in wheelhouse.iterdir() if item.is_file())
    artifacts = [_wheel_metadata(item) for item in wheels]
    by_name: dict[str, dict[str, object]] = {}
    for artifact in artifacts:
        name = str(artifact["name"])
        if name in by_name:
            raise ProvenanceError(f"duplicate wheel project in wheelhouse: {name}")
        by_name[name] = artifact
    if set(by_name) != set(locked):
        raise ProvenanceError(
            "wheelhouse project set differs from lock: "
            f"missing={sorted(set(locked) - set(by_name))}, "
            f"extra={sorted(set(by_name) - set(locked))}"
        )
    for name, record in locked.items():
        artifact = by_name[name]
        if artifact["version"] != record.version or artifact["sha256"] != record.sha256:
            raise ProvenanceError(f"wheel identity/hash differs from lock: {name}")
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "target": "CPython 3.14 / Windows x86-64",
        "source_index_url": SOURCE_INDEX,
        "artifacts": [by_name[name] for name in sorted(by_name)],
    }


def encoded_manifest(document: dict[str, object]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def verify_manifest(
    manifest_path: Path,
    locked: dict[str, LockedDistribution],
    wheelhouse: Path | None = None,
) -> dict[str, dict[str, object]]:
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"provenance manifest is unreadable: {exc}") from exc
    if (
        document.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or document.get("target") != "CPython 3.14 / Windows x86-64"
        or document.get("source_index_url") != SOURCE_INDEX
        or not isinstance(document.get("artifacts"), list)
    ):
        raise ProvenanceError("provenance manifest header is invalid")
    artifacts: dict[str, dict[str, object]] = {}
    required_fields = {
        "name",
        "version",
        "wheel_filename",
        "sha256",
        "source_index_url",
        "acquisition_basis",
        "wheel_tags",
        "requires_dist",
    }
    for raw in document["artifacts"]:
        if not isinstance(raw, dict) or set(raw) != required_fields:
            raise ProvenanceError("provenance artifact fields are invalid")
        name = raw["name"]
        if not isinstance(name, str) or name != normalize_name(name) or name in artifacts:
            raise ProvenanceError("provenance artifact name is invalid or duplicated")
        record = locked.get(name)
        if record is None:
            raise ProvenanceError(f"provenance artifact is not locked: {name}")
        if raw["version"] != record.version or raw["sha256"] != record.sha256:
            raise ProvenanceError(f"provenance identity/hash drift: {name}")
        if raw["source_index_url"] != f"{SOURCE_INDEX}{name}/":
            raise ProvenanceError(f"provenance source index drift: {name}")
        if raw["acquisition_basis"] != ACQUISITION_BASIS:
            raise ProvenanceError(f"provenance acquisition basis drift: {name}")
        if (
            not isinstance(raw["wheel_filename"], str)
            or Path(raw["wheel_filename"]).name != raw["wheel_filename"]
            or not raw["wheel_filename"].endswith(".whl")
            or not isinstance(raw["wheel_tags"], list)
            or not raw["wheel_tags"]
            or not all(isinstance(value, str) and value for value in raw["wheel_tags"])
            or not isinstance(raw["requires_dist"], list)
            or not all(isinstance(value, str) and value for value in raw["requires_dist"])
        ):
            raise ProvenanceError(f"provenance wheel metadata is invalid: {name}")
        artifacts[name] = raw
    if set(artifacts) != set(locked):
        raise ProvenanceError(
            f"provenance/lock project drift: missing={sorted(set(locked) - set(artifacts))}"
        )
    if wheelhouse is not None:
        expected = build_manifest(locked, wheelhouse)
        if encoded_manifest(document) != encoded_manifest(expected):
            raise ProvenanceError("provenance manifest differs from complete wheelhouse bytes")
    return artifacts


def _target_dependency_names(artifact: dict[str, object]) -> list[str]:
    environment = default_environment()
    environment.update(
        {
            "implementation_name": "cpython",
            "os_name": "nt",
            "platform_python_implementation": "CPython",
            "python_full_version": "3.14.0",
            "python_version": "3.14",
            "sys_platform": "win32",
            "extra": "",
        }
    )
    names: set[str] = set()
    for raw in artifact["requires_dist"]:
        requirement = Requirement(str(raw))
        if requirement.marker is None or requirement.marker.evaluate(environment):
            names.add(normalize_name(requirement.name))
    return sorted(names)


def _safe_archive_names(names: list[str], *, sdist: bool) -> None:
    for raw in names:
        path = PurePosixPath(raw)
        lower_parts = {part.lower() for part in path.parts}
        if path.is_absolute() or ".." in path.parts:
            raise ProvenanceError(f"unsafe archive member: {raw}")
        if lower_parts & FORBIDDEN_PARTS:
            raise ProvenanceError(f"forbidden release output in archive: {raw}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise ProvenanceError(f"state or secret-like file in archive: {raw}")
        if any(part.lower().startswith(("cookie", "license")) for part in path.parts):
            raise ProvenanceError(f"unapproved release file in archive: {raw}")
        if not sdist and "skills" in lower_parts:
            raise ProvenanceError(f"repository-only skill leaked into wheel: {raw}")


def verify_artifacts(wheel: Path, sdist: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
        _safe_archive_names(wheel_names, sdist=False)
        if any(
            PurePosixPath(name).parts[0]
            in {"tests", "tools", "schemas", "skills", "requirements", "sbom"}
            for name in wheel_names
        ):
            raise ProvenanceError("repository-only content leaked into wheel")
        metadata_names = [name for name in wheel_names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ProvenanceError("wheel must contain exactly one METADATA file")
        metadata = archive.read(metadata_names[0]).decode("utf-8")
        if "Requires-Dist: yt-dlp<2027,>=2025.1.1" not in metadata:
            raise ProvenanceError("wheel runtime dependency metadata changed")
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        member_names = [member.name for member in members]
        _safe_archive_names(member_names, sdist=True)
        if any(member.issym() or member.islnk() for member in members):
            raise ProvenanceError("sdist contains a link")
        required_suffixes = {
            "/README.md",
            "/constraints-ci.txt",
            "/pyproject.toml",
            "/provenance/locked-artifacts.json",
            "/requirements/ci-windows-py314.lock",
            "/tools/release_provenance.py",
            "/skills/youtube-transcripts/SKILL.md",
            "/schemas/collection-request.schema.json",
        }
        missing = sorted(
            suffix
            for suffix in required_suffixes
            if not any(f"/{name}".endswith(suffix) for name in member_names)
        )
        if missing:
            raise ProvenanceError(f"sdist lacks reviewed release inputs: {missing}")


def _component(record: LockedDistribution, role: str) -> dict[str, object]:
    normalized = record.normalized_name
    return {
        "bom-ref": f"pkg:pypi/{normalized}@{record.version}",
        "type": "library",
        "name": normalized,
        "version": record.version,
        "scope": "required" if role == "runtime" else "excluded",
        "purl": f"pkg:pypi/{normalized}@{record.version}",
        "hashes": [{"alg": "SHA-256", "content": record.sha256}],
        "properties": [{"name": "youtube-transcript-collector:role", "value": role}],
    }


def build_sbom(
    lock_path: Path,
    constraints_path: Path,
    pyproject_path: Path,
    manifest_path: Path,
    wheel: Path,
    sdist: Path,
) -> dict[str, object]:
    locked = verify_lock(lock_path, constraints_path, pyproject_path)
    manifest = verify_manifest(manifest_path, locked)
    verify_artifacts(wheel, sdist)
    project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]
    root_ref = f"pkg:pypi/{project['name']}@{project['version']}"
    runtime_ref = f"pkg:pypi/yt-dlp@{locked['yt-dlp'].version}"
    components = [
        _component(record, "runtime" if name == "yt-dlp" else "ci-build")
        for name, record in sorted(locked.items())
    ]
    components.extend(
        [
            {
                "bom-ref": CI_ROOT_REF,
                "type": "application",
                "name": "youtube-transcript-collector-ci-build-environment",
                "version": "1",
                "scope": "excluded",
                "properties": [
                    {"name": "youtube-transcript-collector:role", "value": "ci-build-root"}
                ],
            },
            {
                "bom-ref": ARTIFACT_ROOT_REF,
                "type": "data",
                "name": "youtube-transcript-collector-release-artifacts",
                "version": project["version"],
                "properties": [
                    {"name": "youtube-transcript-collector:role", "value": "artifact-root"}
                ],
            },
        ]
    )
    artifact_refs: list[str] = []
    for kind, path in (("wheel", wheel), ("sdist", sdist)):
        artifact_ref = f"artifact:{kind}:{path.name}"
        artifact_refs.append(artifact_ref)
        components.append(
            {
                "bom-ref": artifact_ref,
                "type": "file",
                "name": path.name,
                "version": project["version"],
                "hashes": [{"alg": "SHA-256", "content": sha256(path)}],
                "properties": [
                    {"name": "youtube-transcript-collector:role", "value": "distribution"},
                    {"name": "youtube-transcript-collector:format", "value": kind},
                ],
            }
        )
    components.sort(key=lambda item: str(item["bom-ref"]))
    dependency_rows = []
    for name, artifact in sorted(manifest.items()):
        children = _target_dependency_names(artifact)
        unknown = sorted(set(children) - set(locked))
        if unknown:
            raise ProvenanceError(f"active wheel dependencies are not locked for {name}: {unknown}")
        dependency_rows.append(
            {
                "ref": f"pkg:pypi/{name}@{locked[name].version}",
                "dependsOn": [f"pkg:pypi/{child}@{locked[child].version}" for child in children],
            }
        )
    ci_direct_names = {"build", "editables", "hatchling", "jsonschema", "pytest", "ruff"}
    if not ci_direct_names.issubset(locked):
        raise ProvenanceError("CI/build direct dependency roots are not fully locked")
    dependency_rows.extend(
        [
            {"ref": root_ref, "dependsOn": [runtime_ref]},
            {
                "ref": CI_ROOT_REF,
                "dependsOn": [
                    f"pkg:pypi/{name}@{locked[name].version}" for name in sorted(ci_direct_names)
                ],
            },
            {"ref": ARTIFACT_ROOT_REF, "dependsOn": sorted(artifact_refs)},
            *({"ref": artifact_ref, "dependsOn": []} for artifact_ref in artifact_refs),
        ]
    )
    dependency_rows.sort(key=lambda item: str(item["ref"]))
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "youtube-transcript-collector-release-provenance",
                        "version": TOOL_VERSION,
                    }
                ]
            },
            "component": {
                "bom-ref": root_ref,
                "type": "application",
                "name": project["name"],
                "version": project["version"],
                "purl": root_ref,
            },
            "properties": [
                {"name": "youtube-transcript-collector:source-date-epoch", "value": SOURCE_DATE_EPOCH},
                {"name": "youtube-transcript-collector:lock-sha256", "value": sha256(lock_path)},
                {
                    "name": "youtube-transcript-collector:provenance-manifest-sha256",
                    "value": sha256(manifest_path),
                },
            ],
        },
        "components": components,
        "dependencies": dependency_rows,
    }


def encoded_sbom(document: dict[str, object]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=Path("requirements/ci-windows-py314.lock"))
    parser.add_argument("--constraints", type=Path, default=Path("constraints-ci.txt"))
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument(
        "--manifest", type=Path, default=Path("provenance/locked-artifacts.json")
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    lock_parser = subparsers.add_parser("lock-check")
    lock_parser.add_argument(
        "--wheelhouse",
        type=Path,
        help="optional complete directory containing bytes for every locked distribution; caches are never searched",
    )
    for command in ("manifest-generate", "manifest-check"):
        manifest_parser = subparsers.add_parser(command)
        manifest_parser.add_argument("--wheelhouse", type=Path, required=True)
    for command in ("generate", "check"):
        sbom_parser = subparsers.add_parser(command)
        sbom_parser.add_argument("--wheel", type=Path, required=True)
        sbom_parser.add_argument("--sdist", type=Path, required=True)
        sbom_parser.add_argument("--sbom", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "lock-check":
            locked = verify_lock(args.lock, args.constraints, args.pyproject)
            verify_manifest(args.manifest, locked, args.wheelhouse)
            if args.wheelhouse is not None:
                verify_wheelhouse(locked, args.wheelhouse)
            print(json.dumps({"locked_distributions": len(locked), "ok": True}, sort_keys=True))
            return 0
        if args.command in {"manifest-generate", "manifest-check"}:
            locked = verify_lock(args.lock, args.constraints, args.pyproject)
            expected = encoded_manifest(build_manifest(locked, args.wheelhouse))
            if args.command == "manifest-generate":
                args.manifest.parent.mkdir(parents=True, exist_ok=True)
                args.manifest.write_bytes(expected)
            elif not args.manifest.is_file() or args.manifest.read_bytes() != expected:
                raise ProvenanceError("provenance manifest differs from complete wheelhouse bytes")
            print(
                json.dumps(
                    {"artifacts": len(locked), "manifest_sha256": sha256(args.manifest), "ok": True},
                    sort_keys=True,
                )
            )
            return 0
        document = build_sbom(
            args.lock,
            args.constraints,
            args.pyproject,
            args.manifest,
            args.wheel,
            args.sdist,
        )
        expected = encoded_sbom(document)
        if args.command == "generate":
            args.sbom.parent.mkdir(parents=True, exist_ok=True)
            args.sbom.write_bytes(expected)
            print(json.dumps({"ok": True, "sbom_sha256": sha256(args.sbom)}, sort_keys=True))
            return 0
        if not args.sbom.is_file() or args.sbom.read_bytes() != expected:
            raise ProvenanceError("SBOM is stale or artifact bytes do not match")
        print(json.dumps({"ok": True, "sbom_sha256": sha256(args.sbom)}, sort_keys=True))
        return 0
    except (OSError, ProvenanceError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(json.dumps({"error": str(exc), "ok": False}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
