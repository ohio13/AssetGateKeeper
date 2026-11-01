"""``agk-enterprise`` - the operator's command.

    agk-enterprise doctor                       what is configured, what is missing
    agk-enterprise trends --db metrics.db       compliance / growth / errors
    agk-enterprise rbac-check --path /environments/rock --group env-leads
    agk-enterprise validate //depot/proj/main/rock.obj@head
    agk-enterprise migrate --sqlite asset_health.db --dsn postgresql://...
    agk-enterprise hub --token ... --submit //depot/...   (job queue for runners)
    agk-enterprise runner --hub http://hub:9000 --jobs 4

Exit codes are the contract: 0 success, 1 a validation failed, 2 a configuration
or usage problem. A CI job can rely on that without parsing anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import analytics
from .config import ConfigError, describe, load_config
from .rbac import AuthorizationEngine, Principal, RBACError
from .store import StoreError, migrate_sqlite, open_store


def _emit(payload: object, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=False))
    else:
        print(text)


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    lines = describe(config)
    for warning in config.warnings:
        lines.append(f"warning       {warning}")

    # Optional dependencies, reported as facts rather than tracebacks.
    for module, why in (("psycopg", "PostgreSQL backend"),
                        ("cryptography", "RS256/ES256 token signatures"),
                        ("signxml", "SAML assertion signatures")):
        try:
            __import__(module)
            lines.append(f"package       {module}: installed")
        except ModuleNotFoundError:
            lines.append(f"package       {module}: MISSING ({why})")

    if config.vcs.perforce_configured:
        from .vcs import PerforceClient, VCSError
        try:
            ok, reason = PerforceClient(config.vcs).available()
        except VCSError as exc:
            ok, reason = False, str(exc)
        lines.append(f"perforce      {'reachable' if ok else 'unreachable: ' + reason}")

    if config.database.is_postgres:
        try:
            store = open_store(config.database.dsn, config.database)
            store.query("SELECT 1 AS one")
            store.close()
            lines.append("database      connection OK")
        except StoreError as exc:
            lines.append(f"database      FAILED: {exc}")

    _emit({"config": config.source, "lines": lines}, args.json, "\n".join(lines))
    return 0


def cmd_trends(args: argparse.Namespace) -> int:
    """Read-only: no principal is needed, and none is accepted."""
    try:
        config = load_config(args.config)
        target = args.db or (config.database.dsn if config.database.is_postgres
                             else config.database.sqlite_path)
        store = open_store(target, config.database)
        if args.kind == "compliance":
            report = analytics.compliance_trend(store, bucket=args.bucket,
                                                since=args.since,
                                                project_id=args.project)
            text = analytics.render_ascii(report)
            payload = report.as_dict()
        elif args.kind == "growth":
            report = analytics.polygon_growth(store, bucket=args.bucket,
                                              since=args.since,
                                              project_id=args.project)
            text = "\n".join(
                f"{point.bucket:<10} avg {point.average_triangles:>9,}  "
                f"peak {point.peak_triangles:>9,}  n={point.assets}"
                for point in report.points)
            payload = report.as_dict()
        else:
            clusters = analytics.error_frequency(store, since=args.since,
                                                 project_id=args.project,
                                                 limit=args.limit)
            text = "\n".join(f"{c.count:>5}  {c.example[:64]}" for c in clusters) \
                or "(no problems recorded)"
            payload = {"clusters": [c.as_dict() for c in clusters]}
        store.close()
    except (ConfigError, StoreError, analytics.AnalyticsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _emit(payload, args.json, text)
    return 0


def cmd_rbac(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        engine = AuthorizationEngine.from_specs(config.rbac.grants,
                                                config.rbac.default_deny,
                                                config.rbac.director_role)
        principal = Principal(subject=args.user or "cli",
                              username=args.user or "cli",
                              groups=tuple(args.group or ()),
                              roles=tuple(args.role or ()))
        permission = (args.permission or "").strip()
        decision = engine.decide(principal, permission, args.path) if permission else None
        report = {
            "user": principal.username,
            "path": args.path,
            "permissions": sorted(engine.permissions_for(principal, args.path)),
            "rules": engine.explain(args.path),
            "warnings": engine.audit_policy(),
        }
        if decision is not None:
            report["decision"] = decision.as_dict()
        text_lines = [
            f"user    {principal.username} ({', '.join(sorted(principal.subjects))})",
            f"path    {args.path}",
            f"may     {', '.join(report['permissions']) or '(nothing)'}",
        ]
        if decision is not None:
            text_lines.append(
                f"{'ALLOW' if decision.allowed else 'DENY '}   {permission}: {decision.reason}")
        for warning in report["warnings"]:
            text_lines.append(f"warning {warning}")
        _emit(report, args.json, "\n".join(text_lines))
        if decision is not None and not decision.allowed:
            return 1
        return 0
    except (ConfigError, RBACError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def cmd_validate(args: argparse.Namespace) -> int:
    from .remote import RemoteError, parse_source, validate_sources
    from .vcs import temp_directory
    try:
        config = load_config(args.config)
        sources = [parse_source(item) for item in args.sources]
        workdir = Path(args.workdir) if args.workdir else temp_directory()
        report = validate_sources(sources, workdir, vcs_config=config.vcs)
    except (ConfigError, RemoteError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = "\n".join(
        f"{result.status:<8} {result.source}"
        + (f"  [{'; '.join(result.problems)}]" if result.problems else "")
        + (f"  {result.error}" if result.error else "")
        for result in report.results)
    _emit(report.as_dict(), args.json, text)
    return 0 if report.ok else 1


def cmd_migrate(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        dsn = args.dsn or config.database.dsn
        if not dsn.lower().startswith(("postgres://", "postgresql://")):
            print("error: --dsn must be a postgresql:// DSN", file=sys.stderr)
            return 2
        target = open_store(dsn, config.database)
        if args.init_schema:
            target.executescript(_schema_text())
        copied = migrate_sqlite(args.sqlite, target, batch=args.batch)
        target.close()
    except (ConfigError, StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = "\n".join(f"{table:<18}{count:>8,}" for table, count in copied.items())
    _emit({"copied": copied}, args.json, text or "(nothing to copy)")
    return 0


def _schema_text() -> str:
    try:
        import server.asset_health_server as health   # noqa: PLC0415
        return health.SCHEMA
    except Exception as exc:                           # pragma: no cover
        raise StoreError(f"cannot read the AssetHealth schema: {exc}") from exc


def cmd_hub(args: argparse.Namespace) -> int:
    from .remote import RunnerHub
    hub = RunnerHub(token=args.token, host=args.host, port=args.port)
    for item in args.submit or ():
        hub.submit([item], project=args.project)
    hub.start()
    print(f"runner hub listening on {hub.url}")
    print("POST /claim  POST /result  GET /status   (Ctrl+C to stop)")
    try:
        while True:
            import time
            time.sleep(1.0)
    except KeyboardInterrupt:
        hub.stop()
        print("\nstopped")
    return 0


def cmd_runner(args: argparse.Namespace) -> int:
    from .remote import RemoteError, RunnerClient
    from .vcs import temp_directory
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    client = RunnerClient(args.hub, token=args.token, name=args.name)
    workdir = Path(args.workdir) if args.workdir else temp_directory("agk-runner-")
    try:
        done = client.work(workdir, max_jobs=args.jobs, idle_seconds=args.idle,
                           vcs_config=config.vcs)
    except RemoteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = "\n".join(
        f"{item.get('job', '?')}: {'ok' if item.get('ok') else 'failed'}"
        for item in done) or "no jobs claimed"
    _emit(done, args.json, text)
    return 0 if all(item.get("ok") for item in done) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agk-enterprise",
        description="Enterprise operations for AssetGatekeeper: trends, RBAC, "
                    "remote validation, runners and the PostgreSQL migration.")
    parser.add_argument("--config", help="path to enterprise.toml")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="report what is configured")
    doctor.set_defaults(func=cmd_doctor)

    trends = sub.add_parser("trends", help="historical analytics")
    trends.add_argument("kind", choices=("compliance", "growth", "errors"),
                        nargs="?", default="compliance")
    trends.add_argument("--db", help="SQLite path or PostgreSQL DSN")
    trends.add_argument("--since", help="90d, 18m or 2024-01-01")
    trends.add_argument("--bucket", default="month", choices=analytics.BUCKETS)
    trends.add_argument("--project", type=int, help="project id")
    trends.add_argument("--limit", type=int, default=20)
    trends.set_defaults(func=cmd_trends)

    rbac = sub.add_parser("rbac-check", help="evaluate a path-scoped decision")
    rbac.add_argument("--path", default="/")
    rbac.add_argument("--user")
    rbac.add_argument("--group", action="append")
    rbac.add_argument("--role", action="append")
    rbac.add_argument("--permission", default="rules.override")
    rbac.set_defaults(func=cmd_rbac)

    validate = sub.add_parser("validate", help="gate depot paths / URLs / changelists")
    validate.add_argument("sources", nargs="+")
    validate.add_argument("--workdir")
    validate.set_defaults(func=cmd_validate)

    migrate = sub.add_parser("migrate", help="copy SQLite into PostgreSQL")
    migrate.add_argument("--sqlite", required=True)
    migrate.add_argument("--dsn", required=True)
    migrate.add_argument("--batch", type=int, default=500)
    migrate.add_argument("--init-schema", action="store_true",
                         help="create tables before copying")
    migrate.set_defaults(func=cmd_migrate)

    hub = sub.add_parser("hub", help="run the runner job queue (blocks)")
    hub.add_argument("--host", default="127.0.0.1")
    hub.add_argument("--port", type=int, default=9000)
    hub.add_argument("--token", default="")
    hub.add_argument("--project", default="")
    hub.add_argument("--submit", action="append")
    hub.set_defaults(func=cmd_hub)

    runner = sub.add_parser("runner", help="claim and validate jobs from a hub")
    runner.add_argument("--hub", required=True)
    runner.add_argument("--token", default="")
    runner.add_argument("--name", default="runner")
    runner.add_argument("--jobs", type=int, default=1)
    runner.add_argument("--idle", type=float, default=0.5)
    runner.add_argument("--workdir")
    runner.set_defaults(func=cmd_runner)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130



if __name__ == "__main__":
    raise SystemExit(main())
