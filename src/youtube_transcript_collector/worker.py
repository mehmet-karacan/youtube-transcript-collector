from __future__ import annotations

import argparse
import os
import threading
import time
from pathlib import Path
from typing import Any

from .backend import YtDlpBackend
from .contracts import CollectionRequest
from .errors import CollectorError
from .service import CollectorService
from .state import StateStore


class _ControlledSleepBackend:
    """Private deterministic backend used only by background lifecycle tests."""

    def __init__(self, store: StateStore, run_id: str):
        self.store = store
        self.run_id = run_id

    def discover(self, target: str) -> list[dict[str, Any]]:
        raise AssertionError("controlled backend expects a single video")

    def inspect(self, target: str) -> dict[str, Any]:
        while not self.store.cancel_requested(self.run_id):
            time.sleep(0.05)
        raise CollectorError("run_cancelled", "controlled worker observed cancellation")

    def fetch_vtt(self, target: str, languages: tuple[str, ...]):
        raise AssertionError("controlled backend never reaches subtitle fetch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--test-controlled-backend", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    store = StateStore(args.state_dir)
    run = store.get_run(args.run_id)
    request = CollectionRequest.from_dict(run["request"])
    cookie = os.environ.get("YTC_COOKIE_FILE")
    if args.test_controlled_backend:
        backend = _ControlledSleepBackend(store, args.run_id)
    else:
        policy = request.rate_policy
        backend = YtDlpBackend(
            cookie_file=Path(cookie) if cookie else None,
            min_delay=policy.min_delay_seconds,
            retries=policy.max_retries,
            cooldown_seconds=policy.rate_limit_cooldown_seconds,
            cancel_check=lambda: store.cancel_requested(args.run_id),
            child_pid_changed=lambda pid: store.set_active_child(args.run_id, pid),
            cooldown_get=lambda: store.get_runtime_value("youtube_rate_limit_cooldown_until"),
            cooldown_set=lambda value: store.set_runtime_value(
                "youtube_rate_limit_cooldown_until", value
            ),
            pace_reserve=store.reserve_request_slot,
            launch_guard=store.request_launch_guard,
        )
    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(2):
            store.heartbeat(args.run_id)

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    try:
        CollectorService(args.state_dir, backend).execute(args.run_id)
    finally:
        stop.set()
        heartbeat_thread.join(timeout=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
