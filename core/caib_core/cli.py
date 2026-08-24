import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="caib-core")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply db/migrations in order, then sync roles")
    loader_p = sub.add_parser("loader", help="move ingest.landing -> caib.events")
    loader_p.add_argument("--once", action="store_true")
    sub.add_parser("detect", help="run detection rules (Phase 4)")
    args = parser.parse_args()

    if args.cmd == "migrate":
        from caib_core import migrate

        migrate.main()
    elif args.cmd == "loader":
        from caib_core import loader

        loader.main(["--once"] if args.once else [])
    else:
        print(f"caib-core {args.cmd}: not implemented yet", file=sys.stderr)
        raise SystemExit(2)
