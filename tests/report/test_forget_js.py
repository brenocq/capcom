"""static/lib.js, run by node: forget() while a page still awaits a load."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

# A slow server: each GET answers after 30 ms with its request number, and a
# request whose signal aborts rejects with the abort reason, as fetch does.
PRELUDE = """
globalThis.document = { querySelector: () => null };
globalThis.window = { dispatchEvent: () => {} };
let n = 0;
globalThis.fetch = (url, opts = {}) => new Promise((resolve, reject) => {
  const id = ++n;
  const sig = opts.signal;
  const t = setTimeout(() => resolve({ ok: true, status: 200,
    json: async () => ({ n: id }), text: async () => '' }), 30);
  if (sig) sig.addEventListener('abort', () => { clearTimeout(t); reject(sig.reason); });
});
const lib = await import('./lib.js');
"""


def _run(body: str) -> object:
    out = subprocess.run(
        ["node", "--input-type=module", "-e", f"{PRELUDE}\n{body}\n"],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(out.stdout)


def test_forget_does_not_reject_a_load_a_page_awaits() -> None:
    got = _run(
        """
const waiting = lib.api('checks');          // a page awaits the slow reply
await new Promise((r) => setTimeout(r, 5));
lib.forget();                               // a mark was saved
lib.forget();                               // the 10 s watcher sees it too
let out;
try { out = { data: await waiting }; } catch (e) { out = { error: String(e.message || e) }; }
const again = await lib.api('checks');      // kept: no further fetch
console.log(JSON.stringify({ out, same: again.n === out.data?.n }));
"""
    )
    assert isinstance(got, dict)
    assert "error" not in got["out"], got
    assert got["out"]["data"]["n"] >= 2  # fetched again after the forget, not the old reply
    assert got["same"] is True
