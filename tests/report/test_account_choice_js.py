"""The account filter of Overview and Limits, run by node: the choice is kept in the
browser's storage (never in the URL), falls back to "All accounts", and a choice
made in another tab redraws the page."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

LIM = {
    "accounts": [
        {"id": "u1", "email": "a@example.test", "label": "a@example.test", "current": True},
        {"id": "u2", "email": "b@example.test", "label": "b@example.test", "current": False},
    ]
}


def _run(body: str, storage: str = "ok") -> object:
    """Run `body` (which sets `out`) with limits.js imported; `storage` is "ok"
    (an in-memory store), or "throws" (every access fails, as when it is off)."""
    script = (
        "globalThis.document = { createElement: () => ({}), createElementNS: () => ({}) };\n"
        "const handlers = {}; const fired = [];\n"
        "globalThis.window = { addEventListener: (t, f) => { (handlers[t] ||= []).push(f); },"
        " dispatchEvent: (e) => { fired.push(e.type); } };\n"
        "const mem = new Map();\n"
        + (
            "Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {"
            " getItem: (k) => mem.has(k) ? mem.get(k) : null,"
            " setItem: (k, v) => mem.set(k, String(v)) } });\n"
            if storage == "ok"
            else "Object.defineProperty(globalThis, 'localStorage', { configurable: true,"
            " get() { throw new Error('denied'); } });\n"
        )
        + "const m = await import('./limits.js');\n"
        + f"const lim = {json.dumps(LIM)};\n"
        + "const key = (c) => c === 'all' ? 'all' : c && c.id;\n"
        + "let out;\n"
        + body
        + "\nconsole.log(JSON.stringify(out));\n"
    )
    res = subprocess.run(
        ["node", "--no-experimental-webstorage", "--input-type=module", "-e", script],
        cwd=STATIC,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "TZ": "UTC"},
    )
    if res.returncode != 0 and "--no-experimental-webstorage" in res.stderr:
        # an older node without the flag (and without its own localStorage)
        res = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=STATIC,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "TZ": "UTC"},
        )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_nothing_remembered_shows_all_accounts() -> None:
    assert _run("out = key(m.chosenAccount(lim));") == "all"


def test_the_remembered_account_is_shown_by_email_or_id() -> None:
    out = _run(
        "mem.set('capcom.limits.account', 'b@example.test'); const a = key(m.chosenAccount(lim));"
        "mem.set('capcom.limits.account', 'u1'); const b = key(m.chosenAccount(lim));"
        "out = [a, b];"
    )
    assert out == ["u2", "u1"]


def test_an_account_no_longer_listed_falls_back_to_all() -> None:
    out = _run(
        "mem.set('capcom.limits.account', 'gone@example.test'); out = key(m.chosenAccount(lim));"
    )
    assert out == "all"


def test_storage_off_falls_back_to_all() -> None:
    assert _run("out = key(m.chosenAccount(lim));", storage="throws") == "all"


def test_a_single_account_is_always_itself() -> None:
    out = _run(
        "mem.set('capcom.limits.account', 'all');"
        "out = key(m.chosenAccount({ accounts: [lim.accounts[1]] }));"
    )
    assert out == "u2"


def test_a_choice_in_another_tab_redraws_and_others_do_not() -> None:
    out = _run(
        "for (const f of handlers.storage || []) f({ key: 'capcom-theme' });"
        "const before = fired.length;"
        "for (const f of handlers.storage || []) f({ key: 'capcom.limits.account' });"
        "out = [before, fired];"
    )
    assert out == [0, ["capcom-route"]]


def test_the_page_never_puts_the_account_in_a_link() -> None:
    src = (STATIC / "limits.js").read_text() + (STATIC / "overview.js").read_text()
    assert "account=" not in src
    assert "setQuery({ account" not in src
