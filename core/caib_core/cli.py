import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="caib-core")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply db/migrations in order, then sync roles")
    loader_p = sub.add_parser("loader", help="move ingest.landing -> caib.events")
    loader_p.add_argument("--once", action="store_true")
    detect_p = sub.add_parser("detect", help="run detection rules")
    detect_p.add_argument("--once", action="store_true")
    detect_p.add_argument("--advance-watermarks", action="store_true")
    retention_p = sub.add_parser(
        "retention", help="delete aged-out outbox / closed-alert / dead-letter rows"
    )
    retention_p.add_argument("--once", action="store_true")
    health_p = sub.add_parser("healthcheck", help="assert a loop's heartbeat is recent")
    health_p.add_argument("--service", required=True)
    health_p.add_argument("--max-age", type=float, default=120.0)
    args = parser.parse_args()

    if args.cmd == "migrate":
        from caib_core import migrate

        migrate.main()
    elif args.cmd == "loader":
        from caib_core import loader

        loader.main(["--once"] if args.once else [])
    elif args.cmd == "detect":
        from caib_core import detect

        argv = []
        if args.once:
            argv.append("--once")
        if args.advance_watermarks:
            argv.append("--advance-watermarks")
        detect.main(argv)
    elif args.cmd == "retention":
        from caib_core import retention

        retention.main(["--once"] if args.once else [])
    elif args.cmd == "healthcheck":
        from caib_core import heartbeat

        heartbeat.main(["--service", args.service, "--max-age", str(args.max_age)])
    else:
        print(f"caib-core {args.cmd}: not implemented yet", file=sys.stderr)
        raise SystemExit(2)
