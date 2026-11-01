from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

import httpx
from dotenv import load_dotenv

from oam import __version__
from oam.config import ConfigurationError, load_config
from oam.models.client import OpenAICompatibleProbeClient
from oam.models.preflight import CapabilityProbeClient, run_model_preflight
from oam.paths import default_config_path
from oam.startup import validate_startup_environment

DEFAULT_API_PORT = 17333


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oam", description="Open Agent Memory")
    parser.add_argument("--version", action="version", version=f"open-agent-memory {__version__}")
    commands = parser.add_subparsers(dest="command")
    selected_config_path = Path(os.environ.get("OAM_CONFIG", default_config_path()))

    preflight = commands.add_parser("preflight", help="validate configuration and model roles")
    preflight.add_argument("--config", type=Path, default=selected_config_path)
    preflight.add_argument("--json", action="store_true")

    migrate = commands.add_parser("migrate", help="apply PostgreSQL migrations")
    migrate.add_argument("--config", type=Path, default=selected_config_path)

    api = commands.add_parser("api", help="run the HTTP API")
    api.add_argument("--config", type=Path, default=selected_config_path)
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=DEFAULT_API_PORT)

    worker = commands.add_parser("worker", help="run a durable worker")
    worker.add_argument("--config", type=Path, default=selected_config_path)
    worker.add_argument(
        "--once", action="store_true", help="process at most one queued source and exit"
    )

    l2_hold = commands.add_parser("l2-hold", help="manage a user's L2 consolidation hold")
    l2_hold.add_argument("--config", type=Path, default=selected_config_path)
    l2_hold.add_argument("action", choices=("hold", "release", "status"))
    l2_hold.add_argument("--tenant-id", required=True)
    l2_hold.add_argument("--user-id", required=True)

    recovery = commands.add_parser(
        "recover-bootstrap", help="issue a replacement bootstrap administrator credential"
    )
    recovery.add_argument("--config", type=Path, default=selected_config_path)
    permits = commands.add_parser(
        "model-permits", help="inspect and reconcile model permits with operator database authority"
    )
    permits.add_argument("--config", type=Path, default=selected_config_path)
    permit_actions = permits.add_subparsers(dest="permit_action", required=True)
    permit_actions.add_parser(
        "list", help="list active or forcibly released permit metadata as JSON"
    )
    reconcile = permit_actions.add_parser("reconcile", help="record an operator-confirmed outcome")
    reconcile.add_argument("permit_id", type=UUID)
    reconcile.add_argument("--reason", required=True)
    outcome = reconcile.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--known-completed", action="store_true")
    outcome.add_argument(
        "--force-unknown",
        action="store_true",
        help="release capacity while retaining unknown remote concurrency",
    )
    permit_actions.add_parser(
        "configure",
        help="replace stored limits after stopping every API and worker process",
        description=(
            "Stop every API and worker process before replacing deployment limits from YAML. "
            "Active model permits and HTTP requests prevent this operation."
        ),
    )
    export_validation = commands.add_parser(
        "validate-export", help="validate a downloaded portable export directory"
    )
    export_validation.add_argument("directory", type=Path)
    return parser


def _preflight(
    config_path: Path,
    *,
    as_json: bool,
    environment: Mapping[str, str] | None = None,
    probe_client: CapabilityProbeClient | None = None,
) -> int:
    resolved = load_config(config_path)
    missing = sorted(resolved.config.required_unbound_roles())
    runtime_environment = os.environ if environment is None else environment
    if probe_client is None:
        with httpx.Client(
            timeout=resolved.config.model.attempt_timeout_seconds,
            trust_env=False,
        ) as http:
            report = run_model_preflight(
                resolved.config.models,
                runtime_environment,
                OpenAICompatibleProbeClient(
                    http=http, config=resolved.config, environment=runtime_environment
                ),
                required_roles=resolved.config.required_model_roles(),
            )
    else:
        report = run_model_preflight(
            resolved.config.models,
            runtime_environment,
            probe_client,
            required_roles=resolved.config.required_model_roles(),
        )
    payload = {
        "config_sha256": resolved.sha256,
        "contract_version": resolved.config.contract_version,
        "missing_required_roles": missing,
        "ready": report.runnable,
        "roles": [
            {
                "capability": result.capability.value,
                "detail": result.detail,
                "endpoint": result.endpoint,
                "model": result.model,
                "role": result.role,
                "status": result.status.value,
            }
            for result in report.roles
        ],
    }
    if as_json:
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    else:
        print("ready" if payload["ready"] else f"not ready: missing {', '.join(missing)}")
    return 0 if payload["ready"] else 2


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    if args.command == "preflight":
        return _preflight(args.config, as_json=args.json)
    if args.command == "api":
        import uvicorn

        from oam.api.app import create_app

        try:
            config = load_config(args.config).config
            validate_startup_environment(config, os.environ, command="api")
            app = create_app(args.config, bind_host=args.host)
        except ConfigurationError as error:
            print(str(error), file=sys.stderr)
            return 2
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    if args.command == "migrate":
        from oam.db.migrations import migrate_from_environment

        return migrate_from_environment(args.config)
    if args.command == "worker":
        from oam.workers.runner import run_worker_from_environment

        return run_worker_from_environment(args.config, once=args.once)
    if args.command == "l2-hold":
        from oam.workers.l2_hold import l2_hold_from_environment

        return l2_hold_from_environment(args.config, args.action, args.tenant_id, args.user_id)
    if args.command == "recover-bootstrap":
        from oam.auth.bootstrap import recover_bootstrap_from_environment

        return recover_bootstrap_from_environment(args.config)
    if args.command == "model-permits":
        from oam.models.permits import model_permits_from_environment

        return model_permits_from_environment(
            args.config,
            args.permit_action,
            permit_id=getattr(args, "permit_id", None),
            known_completed=getattr(args, "known_completed", False),
            reason=getattr(args, "reason", ""),
        )
    if args.command == "validate-export":
        from oam.exports.validation import ExportValidationError, validate_directory

        try:
            counts = validate_directory(args.directory)
        except ExportValidationError:
            print("invalid export", file=sys.stderr)
            return 2
        print(f"valid export: {sum(counts.values())} records")
        return 0
    parser.print_help()
    return 0


def entrypoint(argv: list[str] | None = None, *, dotenv_path: str | Path | None = None) -> int:
    selected_dotenv_path = Path.cwd() / ".env" if dotenv_path is None else Path(dotenv_path)
    load_dotenv(dotenv_path=selected_dotenv_path, override=False)
    return main(argv)


if __name__ == "__main__":
    sys.exit(entrypoint())
