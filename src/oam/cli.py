from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import httpx
from dotenv import load_dotenv

from oam import __version__
from oam.config import load_config
from oam.models.client import OpenAICompatibleProbeClient
from oam.models.preflight import CapabilityProbeClient, run_model_preflight
from oam.paths import default_config_path


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
    api.add_argument("--port", type=int, default=8080)

    worker = commands.add_parser("worker", help="run a durable worker")
    worker.add_argument("--config", type=Path, default=selected_config_path)
    worker.add_argument(
        "--once", action="store_true", help="process at most one queued source and exit"
    )

    recovery = commands.add_parser(
        "recover-bootstrap", help="issue a replacement bootstrap administrator credential"
    )
    recovery.add_argument("--config", type=Path, default=selected_config_path)
    return parser


def _preflight(
    config_path: Path,
    *,
    as_json: bool,
    environment: Mapping[str, str] | None = None,
    probe_client: CapabilityProbeClient | None = None,
) -> int:
    resolved = load_config(config_path)
    missing = sorted(resolved.config.models.required_unbound_roles())
    runtime_environment = os.environ if environment is None else environment
    if probe_client is None:
        with httpx.Client(
            timeout=resolved.config.model.attempt_timeout_seconds,
            trust_env=False,
        ) as http:
            report = run_model_preflight(
                resolved.config.models,
                runtime_environment,
                OpenAICompatibleProbeClient(http=http),
            )
    else:
        report = run_model_preflight(
            resolved.config.models,
            runtime_environment,
            probe_client,
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

        uvicorn.run(create_app(args.config, bind_host=args.host), host=args.host, port=args.port)
        return 0
    if args.command == "migrate":
        from oam.db.migrations import migrate_from_environment

        return migrate_from_environment(args.config)
    if args.command == "worker":
        from oam.workers.runner import run_worker_from_environment

        return run_worker_from_environment(args.config, once=args.once)
    if args.command == "recover-bootstrap":
        from oam.auth.bootstrap import recover_bootstrap_from_environment

        return recover_bootstrap_from_environment(args.config)
    parser.print_help()
    return 0


def entrypoint(argv: list[str] | None = None, *, dotenv_path: str | Path | None = None) -> int:
    selected_dotenv_path = Path.cwd() / ".env" if dotenv_path is None else Path(dotenv_path)
    load_dotenv(dotenv_path=selected_dotenv_path, override=False)
    return main(argv)


if __name__ == "__main__":
    sys.exit(entrypoint())
