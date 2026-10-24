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


def _story(days: int, show: bool) -> None:
    """The "what changed" line (capcom.report.story) above a command's output."""
    if not show:
        return
    from capcom.report.story import story_line

    line = story_line(load_config(), days)
    if line:
        print(line + "\n", flush=True)


def _top(args: argparse.Namespace) -> int:
    from capcom.cost.top import run_top

    _story(args.days, not args.no_story)
    return run_top(load_config(), by=args.by, days=args.days, limit=args.limit)


def _calibrate(args: argparse.Namespace) -> int:
    from capcom.report.calibration import run_calibrate

    return run_calibrate(load_config(), limit=args.limit, days=args.days, project=args.project)


def _serve(args: argparse.Namespace) -> int:
    from capcom.report.server import run_serve

    return run_serve(
        load_config(),
        port=args.port,
        open_browser=args.open,
        scan=not args.no_scan,
        read_only=args.read_only,
        ask_claude=not args.no_ask,
    )


def _spikes(args: argparse.Namespace) -> int:
    from capcom.explain.cli import run_spikes

    _story(args.days, not args.no_story)
    return run_spikes(load_config(), days=args.days, limit=args.limit)


def _checks(args: argparse.Namespace) -> int:
    from capcom.checks.cli import run_checks_view

    return run_checks_view(load_config(), days=args.days, limit=args.limit)


def _mark(args: argparse.Namespace) -> int:
    from capcom.changes.cli import run_mark

    return run_mark(
        load_config(),
        args.finding,
        on=args.on,
        other=args.other,
        note=args.note,
        noticed=args.noticed,
    )


def _changes(args: argparse.Namespace) -> int:
    from capcom.changes.cli import run_changes

    return run_changes(load_config(), args.id, days=args.days, noticed=args.noticed)


def _unmark(args: argparse.Namespace) -> int:
    from capcom.changes.cli import run_unmark

    return run_unmark(load_config(), args.id)


def _limits(args: argparse.Namespace) -> int:
    if args.install or args.uninstall:
        from capcom.limits.install import run_install

        return run_install(load_config(), sys.stdout, remove=args.uninstall, yes=args.yes)
    from capcom.limits.cli import run_limits

    return run_limits(load_config(), seed=args.seed, dry_run=args.dry_run, account=args.account)


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
    top.add_argument(
        "--no-story", action="store_true", help="leave out the 'spend rose' line at the top"
    )
    top.set_defaults(func=_top)

    cal = sub.add_parser("calibrate", help="capcom's totals against Claude Code's cost-state")
    cal.add_argument("--limit", type=int, default=20)
    cal.add_argument(
        "--days", type=int, default=0, help="sessions active in the last N days (0: all)"
    )
    cal.add_argument("--project", help="one project: its label, folder or project folder")
    cal.set_defaults(func=_calibrate)

    serve = sub.add_parser("serve", help="the local website, on 127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--open", action="store_true", help="open it in the browser")
    serve.add_argument(
        "--no-scan", action="store_true", help="do not rescan the logs in the background"
    )
    serve.add_argument(
        "--read-only",
        action="store_true",
        help="the website cannot mark changes as applied (no writes at all)",
    )
    serve.add_argument(
        "--no-ask",
        action="store_true",
        help="no Ask Roger chat: the website never starts your claude CLI",
    )
    serve.set_defaults(func=_serve)

    spikes = sub.add_parser("spikes", help="spend spikes and their causes")
    spikes.add_argument("--days", type=int, default=14, help="window, in days (0: all)")
    spikes.add_argument("--limit", type=int, default=20)
    spikes.add_argument(
        "--no-story", action="store_true", help="leave out the 'spend rose' line at the top"
    )
    spikes.set_defaults(func=_spikes)

    checks = sub.add_parser("checks", help="setup warnings with their weekly saving")
    checks.add_argument("--days", type=int, default=14, help="window, in days (0: all)")
    checks.add_argument("--limit", type=int, default=20)
    checks.set_defaults(func=_checks)

    mark = sub.add_parser("mark", help="record that you applied a finding's change on a date")
    mark.add_argument("finding", help="its row in `capcom checks`, or its id kind@scope")
    mark.add_argument("--on", default="today", help="today, yesterday or YYYY-MM-DD")
    mark.add_argument(
        "--other", action="store_true", help="you made another change than the suggested one"
    )
    mark.add_argument("--note", default="", help="your own words, kept on this machine")
    mark.add_argument("--noticed", action="store_true", help=argparse.SUPPRESS)
    mark.set_defaults(func=_mark)

    changes = sub.add_parser("changes", help="did it work? the marked changes and their verdicts")
    changes.add_argument("id", type=int, nargs="?", help="one mark in detail")
    changes.add_argument(
        "--days", type=int, default=0, help="marks applied in the last N days (0: all)"
    )
    changes.add_argument(
        "--noticed", action="store_true", help="changes capcom saw in agent definitions"
    )
    changes.set_defaults(func=_changes)

    unmark = sub.add_parser("unmark", help="remove a mark")
    unmark.add_argument("id", type=int)
    unmark.set_defaults(func=_unmark)

    limits = sub.add_parser(
        "limits", help="the 5-hour and weekly usage windows: now, forecast, what spent them"
    )
    limits.add_argument(
        "--seed", action="store_true", help="add the rate-limit errors in the index as samples"
    )
    limits.add_argument(
        "--dry-run", action="store_true", help="with --seed: show the samples, write nothing"
    )
    setup = limits.add_mutually_exclusive_group()
    setup.add_argument(
        "--install",
        action="store_true",
        help="start recording: show the status line change, and make it if you say yes",
    )
    setup.add_argument(
        "--uninstall", action="store_true", help="stop recording: undo that change, after asking"
    )
    limits.add_argument(
        "-y", "--yes", action="store_true", help="with --install or --uninstall: do not ask"
    )
    limits.add_argument(
        "--account", metavar="EMAIL", help="only this account (its email, or its id)"
    )
    limits.set_defaults(func=_limits)

    rec = sub.add_parser(
        "record-limits",
        help="status line wrapper: pass stdin through and record the usage windows",
    )
    rec.add_argument("--line", action="store_true", help="print a short line instead of stdin")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["record-limits"]:
        # on the status line's path: no argument parsing, no other imports
        from capcom.limits.record import run

        return run(argv[1:])
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
