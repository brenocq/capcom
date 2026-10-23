"""static/marks.js, run by node: the one write (postChange) when the server was
restarted under an open page, so the page's write token is the old launch's."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

# A stand-in page and server: `document` holds the token of the launch the page was
# loaded from, and `fetch` answers from `replies` in order, recording each request.
PRELUDE = """
globalThis.document = { querySelector: (sel) => sel.includes('capcom-token')
  ? { getAttribute: () => 'old-token' } : null };
globalThis.window = { dispatchEvent: () => {} };
globalThis.Event = class { constructor(type) { this.type = type; } };
const calls = [];
let replies = [];
const page = (tok) => ({ status: 200,
  html: `<head><meta name="capcom-token" content="${tok}">\\n</head>` });
const refused = { status: 403, body: { error: 'bad_token', message: 'from the server' } };
globalThis.fetch = async (url, opts = {}) => {
  const token = opts.headers && opts.headers['X-Capcom-Token'];
  calls.push({ url, method: opts.method || 'GET', token });
  const r = replies.shift();
  if (r === 'down') throw new TypeError('Failed to fetch');
  return { ok: r.status >= 200 && r.status < 300, status: r.status,
    json: async () => { if (!r.body) throw new SyntaxError('no json'); return r.body; },
    text: async () => r.html || '' };
};
const m = await import('./marks.js');
const run = async (rs) => {
  replies = rs; calls.length = 0;
  try { return { ok: await m.postChange({ action: 'unmark', id: 1 }), calls: [...calls] }; }
  catch (e) { return { status: e.status, message: e.message, calls: [...calls] }; }
};
"""


def _run(body: str) -> object:
    script = f"{PRELUDE}\n{body}\n"
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(out.stdout)


def test_a_refused_token_is_fetched_again_and_the_write_retried_once() -> None:
    got = _run(
        "const a = await run([refused, page('new-token'), { status: 200, body: { unmarked: 1 } }]);"
        "const b = await run([{ status: 200, body: { unmarked: 1 } }]);"  # keeps the new one
        "console.log(JSON.stringify([a, b]))"
    )
    assert isinstance(got, list)
    a, b = got
    assert a["ok"] == {"unmarked": 1}
    assert [(c["method"], c["url"], c.get("token")) for c in a["calls"]] == [
        ("POST", "/api/changes", "old-token"),
        ("GET", "/", None),
        ("POST", "/api/changes", "new-token"),
    ]
    assert [c["token"] for c in b["calls"]] == ["new-token"]


def test_a_retry_that_still_fails_says_to_reload() -> None:
    got = _run(
        "const a = await run([refused, page('new-token'), refused]);"
        "const b = await run([refused, page('new-token')]);"  # the same token: no retry
        "const c = await run([refused, { status: 503, html: 'no index' }]);"
        "console.log(JSON.stringify([a, b, c]))"
    )
    assert isinstance(got, list)
    for r, posts in zip(got, (2, 1, 1), strict=True):
        assert r["status"] == 403
        assert "restarted" in r["message"] and "Reload the page" in r["message"]
        assert sum(c["method"] == "POST" for c in r["calls"]) == posts


def test_other_failures_keep_their_own_message() -> None:
    got = _run(
        "const a = await run(['down']);"
        "const b = await run([{ status: 405 }]);"
        "const c = await run([{ status: 409,"
        "  body: { error: 'already_marked', message: 'Marked already.' } }]);"
        "const d = await run([{ status: 403,"
        "  body: { error: 'cross_origin', message: 'Only capcom.' } }]);"
        "console.log(JSON.stringify([a, b, c, d]))"
    )
    assert isinstance(got, list)
    a, b, c, d = got
    assert "not answering" in a["message"]
    assert b["status"] == 405 and "read-only" in b["message"]
    assert c["message"] == "Marked already."
    assert d["message"] == "Only capcom." and len(d["calls"]) == 1  # no token fetch
