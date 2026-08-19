"""Compatibility entry point for validating the bundled Agent Skill."""

from __future__ import annotations

import sys
from pathlib import Path

from skill_portability import PortabilityError, validate_skill


def main() -> int:
    try:
        info = validate_skill(Path(sys.argv[1]))
    except (OSError, PortabilityError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Skill is valid: {info.name} ({info.digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
