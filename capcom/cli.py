"""capcom's command line. Each command calls one module's entry point; the modules
own the work."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from capcom import __version__
from capcom.config import load_config


def _scan(args: argparse.Namespace) -> int:
    from capcom.store.scan import run_scan

    config = load_config()
    stats = run_scan(config, quiet=args.quiet)
    if not args.quiet:
        print(
            f"{stats.files_read} of {stats.files_seen} files read, "
            f"{stats.bytes_read / 1e6:.1f} MB, {stats.records} records, "
            f"{stats.skipped} lines skipped, {stats.files_failed} files failed, "
            f"{stats.seconds:.1f} s"
        )
    return 0


def _top(args: argparse.Namespace) -> int:
    from capcom.cost.top import run_top

    return run_top(load_config(), by=args.by, days=args.days, limit=args.limit)


def _calibrate(args: argparse.Namespace) -> int:
    from capcom.cost.top import run_calibrate

    return run_calibrate(load_config(), limit=args.limit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capcom", description=__doc__)
    parser.add_argument("--version", action="version", version=f"capcom {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="read new log lines into the index")
    scan.add_argument("-q", "--quiet", action="store_true")
    scan.set_defaults(func=_scan)

    top = sub.add_parser("top", help="totals by project, session, agent type or model")
    top.add_argument(
        "--by", choices=["project", "session", "agent", "model", "day"], default="project"
    )
    top.add_argument("--days", type=int, default=14, help="window, in days (0: all)")
    top.add_argument("--limit", type=int, default=20)
    top.set_defaults(func=_top)

    cal = sub.add_parser("calibrate", help="capcom's totals against Claude Code's cost-state")
    cal.add_argument("--limit", type=int, default=20)
    cal.set_defaults(func=_calibrate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
