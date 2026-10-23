"""`capcom limits`: the usage windows in the terminal and the seed from the index's
rate-limit errors (the status line change that starts recording is in `install`)."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from typing import TextIO

from capcom.config import Config
from capcom.cost.prices import load_prices
from capcom.limits.accounts import analyze_accounts, find_account, load_known
from capcom.limits.model import (
    append_samples,
    load_samples,
    samples_path,
    seed_samples,
)
from capcom.limits.record import adopt
from capcom.limits.view import NOT_RECORDING, format_accounts, format_views
from capcom.types import Index


def adopt_samples(config: Config) -> None:
    """Move the samples a version before the state folder kept in the cache; a failure
    is said on stderr and leaves them where they are."""
    try:
        adopt(str(config.cache_dir), str(config.state_dir))
    except OSError as err:
        print(
            f"capcom: cannot move the usage samples to {config.state_dir} ({err})", file=sys.stderr
        )


def _open(config: Config) -> Index | None:
    from capcom.store.index import open_index

    try:
        return open_index(config.index_path, readonly=True)
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as err:
        print(f"capcom: cannot read the index at {config.index_path} ({err})", file=sys.stderr)
        print("run `capcom scan` first", file=sys.stderr)
        return None


def run_limits(
    config: Config,
    seed: bool = False,
    dry_run: bool = False,
    out: TextIO | None = None,
    now: datetime | None = None,
    index: Index | None = None,
    account: str | None = None,
) -> int:
    out = out or sys.stdout
    now = now or datetime.now(UTC)
    index = index or _open(config)
    if index is None:
        return 1
    adopt_samples(config)
    if seed:
        found = seed_samples(index)
        print(
            f"{found.errors} rate-limit errors in the index; {len(found.samples)} name the "
            f"window they hit, {found.unknown_window} do not (skipped).",
            file=out,
        )
        if dry_run:
            for s in found.samples:
                when = s.time.astimezone().strftime("%Y-%m-%d %H:%M")
                reset = s.resets_at.astimezone().strftime("%Y-%m-%d %H:%M") if s.resets_at else "?"
                print(f"  {when}  {s.window.value:<9}  100 %  resets {reset}", file=out)
            print("dry run: nothing written", file=out)
            return 0
        added = append_samples(config.state_dir, found.samples)
        print(f"{len(added)} new samples in {samples_path(config.state_dir)}", file=out)
        return 0
    samples = load_samples(samples_path(config.state_dir))
    if not samples:
        print(f"No samples in {samples_path(config.state_dir)}.", file=out)
        print(NOT_RECORDING, file=out)
        return 0
    from capcom.timeline.groups import ProjectNames

    names = ProjectNames.build(index.sessions(), config.openclaw_root)
    accounts, comb = analyze_accounts(
        index,
        load_prices(),
        samples,
        load_known(config.state_dir),
        now,
        lambda d: names.label(names.key(d)),
    )
    print(f"capcom limits ({len(samples)} samples, {samples_path(config.state_dir)})\n", file=out)
    if account is not None:
        one = find_account(accounts, account)
        if one is None:
            known = ", ".join(a.account.label for a in accounts)
            print(f"capcom: no account {account!r} in the samples (there are: {known})", file=out)
            return 1
        print(f"== {one.account.label}", file=out)
        print(format_views(one.views, one.samples, now), file=out)
        return 0
    print(format_accounts(accounts, comb, samples, now), file=out)
    return 0
