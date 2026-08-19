from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .backend import YtDlpBackend
from .errors import CollectorError
from .migration import apply_migration, preview_migration
from .service import CollectorService
from .targets import build_request, resolve_channel, resolve_video


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CollectorError("invalid_arguments", message)


def _emit(payload: dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _success(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "schema_version": 1}


def _failure(error: CollectorError) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": error.code, "message": error.message, "details": error.details or {}},
        "schema_version": 1,
    }


def _add_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--video", action="append", default=[])
    parser.add_argument("--channel")
    parser.add_argument("--language", action="append", dest="languages")
    parser.add_argument("--latest", type=int)
    parser.add_argument("--title-query")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument(
        "--min-delay-seconds",
        type=float,
        default=1.0,
        help="bounded delay between yt-dlp calls (0.5-60; default: 1.0)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="yt-dlp retry bound (0-20; default: 3)",
    )
    parser.add_argument(
        "--rate-limit-cooldown-seconds",
        type=int,
        default=900,
        help="persistent 429 cooldown (60-86400; default: 900)",
    )


def _request(args: argparse.Namespace):
    return build_request(
        videos=args.video,
        channel=args.channel,
        languages=args.languages,
        latest=args.latest,
        title_query=args.title_query,
        date_from=args.date_from,
        date_to=args.date_to,
        min_delay_seconds=args.min_delay_seconds,
        max_retries=args.max_retries,
        rate_limit_cooldown_seconds=args.rate_limit_cooldown_seconds,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(prog="yt-transcripts", description="Durable YouTube transcript collector")
    parser.add_argument(
        "--json", action="store_true", dest="json_mode", help="emit stable JSON on stdout"
    )
    parser.add_argument(
        "--state-dir", type=Path, default=Path(os.environ.get("YTC_STATE_DIR", ".yt-transcripts"))
    )
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    commands.add_parser("doctor")

    targets = commands.add_parser("targets").add_subparsers(
        dest="targets_command", required=True, parser_class=Parser
    )
    resolve = targets.add_parser("resolve")
    resolve.add_argument("--video", action="append", default=[])
    resolve.add_argument("--channel")

    metadata = commands.add_parser("metadata").add_subparsers(
        dest="metadata_command", required=True, parser_class=Parser
    )
    inspect = metadata.add_parser("inspect")
    inspect.add_argument("target")
    inspect.add_argument("--cookies", type=Path)

    collect = commands.add_parser("collect").add_subparsers(
        dest="collect_command", required=True, parser_class=Parser
    )
    preview = collect.add_parser("preview")
    _add_target_args(preview)
    start = collect.add_parser("start")
    _add_target_args(start)
    start.add_argument(
        "--idempotency-key",
        help="exact replay key; omit for a new request generation",
    )
    start.add_argument("--foreground", action="store_true", help="run synchronously")
    start.add_argument("--cookies", type=Path)

    runs = commands.add_parser("runs").add_subparsers(
        dest="runs_command", required=True, parser_class=Parser
    )
    for name in ("status", "cancel", "logs"):
        child = runs.add_parser(name)
        child.add_argument("run_id")

    migrate = commands.add_parser("migrate").add_subparsers(
        dest="migrate_command", required=True, parser_class=Parser
    )
    migration_preview = migrate.add_parser("preview")
    migration_preview.add_argument("--legacy-dir", required=True, type=Path)
    migration_apply = migrate.add_parser("apply")
    migration_apply.add_argument("--legacy-dir", required=True, type=Path)
    migration_apply.add_argument("--plan-digest", required=True)
    return parser


def dispatch(args: argparse.Namespace) -> Any:
    if args.command == "doctor":
        try:
            version = importlib.metadata.version("yt-dlp")
        except importlib.metadata.PackageNotFoundError:
            version = None
        return {
            "collector_version": __version__,
            "python": sys.version.split()[0],
            "yt_dlp": {"available": version is not None, "version": version},
            "state_dir": str(args.state_dir.resolve()),
        }
    if args.command == "targets":
        if bool(args.channel) == bool(args.video):
            raise CollectorError("invalid_request", "provide exactly one of --channel or --video")
        return (
            {"channel": resolve_channel(args.channel)}
            if args.channel
            else {"videos": [resolve_video(item) for item in args.video]}
        )
    if args.command == "metadata":
        return YtDlpBackend(cookie_file=args.cookies).inspect(resolve_video(args.target))
    if args.command == "collect":
        request = _request(args)
        if args.collect_command == "preview":
            return {
                "request": request.as_dict(),
                "request_digest": request.digest,
                "effect_sha256": request.digest,
                "network_used": False,
            }
        if args.cookies:
            os.environ["YTC_COOKIE_FILE"] = str(args.cookies.resolve())
        policy = request.rate_policy
        service = CollectorService(args.state_dir)
        if args.foreground:
            service.backend = YtDlpBackend(
                cookie_file=args.cookies,
                min_delay=policy.min_delay_seconds,
                retries=policy.max_retries,
                cooldown_seconds=policy.rate_limit_cooldown_seconds,
                cooldown_get=lambda: service.store.get_runtime_value(
                    "youtube_rate_limit_cooldown_until"
                ),
                cooldown_set=lambda value: service.store.set_runtime_value(
                    "youtube_rate_limit_cooldown_until", value
                ),
                pace_reserve=service.store.reserve_request_slot,
                launch_guard=service.store.request_launch_guard,
            )
        result = service.start(
            request, idempotency_key=args.idempotency_key, background=not args.foreground
        )
        result["effect_sha256"] = request.digest
        return result
    if args.command == "runs":
        service = CollectorService(args.state_dir)
        if args.runs_command == "status":
            return service.status(args.run_id)
        if args.runs_command == "cancel":
            return service.cancel(args.run_id)
        return {"run_id": args.run_id, "events": service.logs(args.run_id)}
    if args.command == "migrate":
        if args.migrate_command == "preview":
            return preview_migration(args.legacy_dir)
        return apply_migration(args.legacy_dir, args.state_dir, args.plan_digest)
    raise CollectorError("invalid_arguments", "unknown command")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in raw
    if json_mode and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        args = build_parser().parse_args(raw)
        _emit(_success(dispatch(args)), args.json_mode)
        return 0
    except CollectorError as error:
        _emit(_failure(error), json_mode)
        return 2
    except Exception as error:  # stable boundary; never expose a traceback to agents
        wrapped = CollectorError("internal_error", str(error))
        _emit(_failure(wrapped), json_mode)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
