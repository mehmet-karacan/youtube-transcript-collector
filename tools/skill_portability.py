"""Validate and install the bundled Agent Skill into explicit workspace roots.

This utility intentionally never discovers or writes a user-global directory and
does not invoke an agent executable.  It makes portability checks deterministic
in development and CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import NamedTuple

TARGETS = {
    "universal": Path(".agents/skills"),
    "codex": Path(".agents/skills"),
    "claude-code": Path(".claude/skills"),
    "opencode": Path(".opencode/skills"),
    "gemini-cli": Path(".gemini/skills"),
}
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LINK_PATTERN = re.compile(r"\[[^]]+\]\(([^)]+)\)")
WINDOWS_REPLACE_RETRY_DELAYS = (0.005, 0.01, 0.02, 0.04, 0.08, 0.16)
WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset({5, 32, 33})
IS_WINDOWS = os.name == "nt"


class PortabilityError(ValueError):
    """A stable, user-actionable validation or installation failure."""


class SkillInfo(NamedTuple):
    name: str
    description: str
    digest: str


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(path.stat(follow_symlinks=False).st_file_attributes & 0x400)
    except AttributeError:
        return False


def _reject_links(root: Path) -> None:
    for item in (root, *root.rglob("*")):
        if _is_reparse(item):
            raise PortabilityError(f"skill package contains a link/reparse point: {item}")


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        contents = path.read_bytes()
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def validate_skill(skill: Path) -> SkillInfo:
    """Validate the portable Agent Skills contract, excluding optional UI metadata."""
    skill = skill.resolve(strict=True)
    if not skill.is_dir():
        raise PortabilityError("skill path must be a directory")
    _reject_links(skill)
    files = [item for item in skill.rglob("*") if item.is_file()]
    if len(files) > 64 or sum(item.stat().st_size for item in files) > 1024 * 1024:
        raise PortabilityError("skill package exceeds the 64-file/1-MiB safety cap")
    markdown = skill / "SKILL.md"
    if not markdown.is_file():
        raise PortabilityError("SKILL.md is required")
    source = markdown.read_text(encoding="utf-8")
    if not source.startswith("---\n") or "\n---\n" not in source[4:]:
        raise PortabilityError("SKILL.md must contain YAML frontmatter")
    frontmatter, body = source[4:].split("\n---\n", 1)
    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if ":" not in line:
            raise PortabilityError("frontmatter fields must be single-line key/value pairs")
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    if set(fields) != {"name", "description"}:
        raise PortabilityError("SKILL.md frontmatter must contain only name and description")
    name = fields["name"]
    description = fields["description"]
    if len(name) > 64 or not NAME_PATTERN.fullmatch(name) or name != skill.name:
        raise PortabilityError("skill name must be <=64 characters and match its kebab-case directory")
    if not (1 <= len(description) <= 1024) or "<" in description or ">" in description:
        raise PortabilityError("skill description must be 1-1024 characters without angle brackets")
    if not body.strip():
        raise PortabilityError("SKILL.md body is required")
    for target in LINK_PATTERN.findall(body):
        if "://" in target or target.startswith("#"):
            continue
        referenced = (skill / target.split("#", 1)[0]).resolve()
        if not referenced.is_relative_to(skill) or not referenced.exists():
            raise PortabilityError(f"missing or escaping local reference: {target}")
    return SkillInfo(name=name, description=description, digest=_tree_digest(skill))


def _verify_workspace(workspace: Path) -> Path:
    workspace = Path(os.path.abspath(workspace))
    missing: list[Path] = []
    cursor = workspace
    while not os.path.lexists(cursor):
        missing.append(cursor)
        if cursor.parent == cursor:
            raise PortabilityError("workspace has no existing ancestor")
        cursor = cursor.parent
    for current in (cursor, *cursor.parents):
        if _is_reparse(current):
            raise PortabilityError(f"workspace contains a link/reparse point: {current}")
    if not cursor.is_dir():
        raise PortabilityError("workspace ancestor is not a directory")
    for directory in reversed(missing):
        if _is_reparse(directory.parent):
            raise PortabilityError("workspace parent changed during creation")
        directory.mkdir()
        if _is_reparse(directory):
            raise PortabilityError("workspace became a link/reparse point")
    return workspace.resolve(strict=True)


def _target_parent(root: Path, relative: Path, *, create: bool) -> Path:
    cursor = root
    for part in relative.parts:
        candidate = cursor / part
        if os.path.lexists(candidate):
            if _is_reparse(candidate) or not candidate.is_dir():
                raise PortabilityError(f"target path is not a safe directory: {candidate}")
            if not candidate.resolve(strict=True).is_relative_to(root):
                raise PortabilityError("target path escapes workspace")
        elif create:
            if _is_reparse(cursor):
                raise PortabilityError(f"target parent is a link/reparse point: {cursor}")
            candidate.mkdir()
        else:
            break
        cursor = candidate
    return root / relative


def _verify_staged_replace(root: Path, temporary: Path, destination: Path) -> None:
    if not destination.is_relative_to(root) or not temporary.is_relative_to(root):
        raise PortabilityError("staged installation path escapes workspace")
    relative_parent = destination.parent.relative_to(root)
    verified_parent = _target_parent(root, relative_parent, create=False)
    if verified_parent.resolve(strict=True) != destination.parent.resolve(strict=True):
        raise PortabilityError("target parent changed during installation")
    if temporary.parent != destination.parent:
        raise PortabilityError("staging directory is not beside destination")
    if not temporary.exists() or not temporary.is_dir() or _is_reparse(temporary):
        raise PortabilityError("staging directory changed during installation")
    if not temporary.resolve(strict=True).is_relative_to(root):
        raise PortabilityError("staging directory escapes workspace")
    if os.path.lexists(destination):
        if _is_reparse(destination):
            raise PortabilityError("installation destination became a link/reparse point")
        raise PortabilityError("installation destination appeared during installation")


def _replace_staged(root: Path, temporary: Path, destination: Path) -> None:
    retry_index = 0
    while True:
        _verify_staged_replace(root, temporary, destination)
        try:
            os.replace(temporary, destination)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if not IS_WINDOWS or winerror not in WINDOWS_TRANSIENT_REPLACE_ERRORS:
                raise
            if retry_index >= len(WINDOWS_REPLACE_RETRY_DELAYS):
                raise
            delay = WINDOWS_REPLACE_RETRY_DELAYS[retry_index]
            retry_index += 1
            time.sleep(delay)


def _remove_installed_tree(root: Path, destination: Path) -> None:
    if not os.path.lexists(destination):
        return
    if not destination.is_relative_to(root):
        raise PortabilityError("rollback destination escapes workspace")
    _target_parent(root, destination.parent.relative_to(root), create=False)
    if _is_reparse(destination) or not destination.is_dir():
        raise PortabilityError("rollback destination is not a safe directory")
    if not destination.resolve(strict=True).is_relative_to(root):
        raise PortabilityError("rollback destination escapes workspace")
    shutil.rmtree(destination)


def install_skill(
    skill: Path,
    workspace: Path,
    targets: list[str],
    *,
    expected_digest: str | None = None,
) -> dict[str, object]:
    """Copy a validated skill to selected project-local discovery directories."""
    info = validate_skill(skill)
    if expected_digest is not None and expected_digest != info.digest:
        raise PortabilityError("skill digest does not match the reviewed expected digest")
    source = skill.resolve(strict=True)
    root = _verify_workspace(workspace)
    if not targets:
        raise PortabilityError("at least one --target is required")
    unknown = sorted(set(targets) - TARGETS.keys())
    if unknown:
        raise PortabilityError(f"unknown target(s): {', '.join(unknown)}")

    plans: list[tuple[str, Path, bool]] = []
    for target in dict.fromkeys(targets):
        parent = _target_parent(root, TARGETS[target], create=False)
        destination = parent / info.name
        if not destination.resolve(strict=False).is_relative_to(root):
            raise PortabilityError("installation destination escapes workspace")
        if os.path.lexists(destination):
            _reject_links(destination)
            if _tree_digest(destination) != info.digest:
                raise PortabilityError(f"existing skill differs for target {target}")
            plans.append((target, destination, False))
        else:
            plans.append((target, destination, True))

    staged: list[tuple[Path, Path]] = []
    staged_destinations: set[Path] = set()
    created_destinations: list[Path] = []
    try:
        for _target, destination, create in plans:
            if not create or destination in staged_destinations:
                continue
            _target_parent(root, TARGETS[_target], create=True)
            if _is_reparse(destination.parent):
                raise PortabilityError(
                    f"target directory is a link/reparse point: {destination.parent}"
                )
            temporary = destination.parent / f".{info.name}.tmp-{uuid.uuid4().hex}"
            shutil.copytree(source, temporary)
            staged.append((temporary, destination))
            staged_destinations.add(destination)
            _reject_links(temporary)
            if _tree_digest(temporary) != info.digest:
                raise PortabilityError("copied skill digest mismatch")
        for temporary, destination in staged:
            _replace_staged(root, temporary, destination)
            created_destinations.append(destination)
    except Exception:
        for destination in reversed(created_destinations):
            try:
                _remove_installed_tree(root, destination)
            except (OSError, PortabilityError):
                pass
        raise
    finally:
        for temporary, _destination in staged:
            try:
                _remove_installed_tree(root, temporary)
            except (OSError, PortabilityError):
                pass

    installed = []
    for target, destination, create in plans:
        installed.append(
            {
                "target": target,
                "path": destination.relative_to(root).as_posix(),
                "created": destination in created_destinations if create else False,
            }
        )
    return {"ok": True, "skill": info.name, "digest": info.digest, "installs": installed}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate an Agent Skill package")
    validate.add_argument("--skill", type=Path, required=True)
    install = commands.add_parser("install", help="install into an explicit project workspace")
    install.add_argument("--skill", type=Path, required=True)
    install.add_argument("--workspace", type=Path, required=True)
    install.add_argument("--target", choices=sorted(TARGETS), action="append", required=True)
    install.add_argument(
        "--expected-digest",
        required=True,
        help="exact SHA-256 returned by a separately reviewed validate command",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            info = validate_skill(args.skill)
            result = {"ok": True, "skill": info.name, "digest": info.digest}
        else:
            result = install_skill(
                args.skill,
                args.workspace,
                args.target,
                expected_digest=args.expected_digest,
            )
    except (OSError, PortabilityError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "skill_portability_error", "message": str(exc)}}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
