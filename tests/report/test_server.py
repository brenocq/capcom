"""The server: binding, Host check, methods, static files, pages, missing index."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC
from pathlib import Path

import pytest

from capcom.report.server import HOST, Site, make_server
from tests.report.conftest import FAKE, NOW, TABLE, Client, serve


def test_binds_to_loopback_only(index_path: Path) -> None:
    server = make_server(Site(index_path, TABLE, FAKE), 0)
    try:
        assert HOST == "127.0.0.1"
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_rejects_foreign_host_header(client: Client) -> None:
    status, _, _ = client.get("/api/status", host="evil.example:8787")
    assert status == 403
    assert client.get("/api/status", host="localhost:8787")[0] == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_get_only(client: Client, method: str) -> None:
    assert client.request(method, "/api/status") == 405


def test_no_cors_and_safe_headers(client: Client) -> None:
    status, _, headers = client.get("/api/status")
    assert status == 200
    assert not any(k.lower().startswith("access-control-") for k in headers)
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_static_files_and_types(client: Client) -> None:
    for name, ctype in [("app.js", "text/javascript"), ("style.css", "text/css")]:
        status, body, headers = client.get(f"/static/{name}")
        assert status == 200, name
        assert headers["Content-Type"].startswith(ctype)
        assert body


@pytest.mark.parametrize(
    "path",
    [
        "/static/../server.py",
        "/static/%2e%2e/server.py",
        "/static/..%2fserver.py",
        "/static/%2e%2e%2f%2e%2e%2fconfig.py",
        "/static//etc/passwd",
        "/static/",
        "/static/missing.js",
    ],
)
def test_static_path_traversal(client: Client, path: str) -> None:
    status, body, _ = client.get(path)
    assert status == 404
    assert b"import" not in body and b"root:" not in body


@pytest.mark.parametrize("path", ["/", "/spend", "/sessions", "/session/s1", "/spikes", "/checks"])
def test_pages_serve_the_app(client: Client, path: str) -> None:
    status, body, headers = client.get(path)
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"/static/app.js" in body


@pytest.mark.parametrize("path", ["/nope", "/session/s1/x", "/api/nope", "/api/session"])
def test_unknown_paths(client: Client, path: str) -> None:
    assert client.get(path)[0] == 404


@pytest.fixture
def missing(tmp_path: Path) -> Iterator[Client]:
    yield from serve(Site(tmp_path / "none" / "index.sqlite", TABLE, FAKE, lambda: NOW, UTC))


def test_missing_index(missing: Client) -> None:
    status, data, _ = missing.get("/api/overview")
    assert status == 503
    assert data["error"] == "no_index"
    assert "capcom scan" in data["message"]
    status, body, _ = missing.get("/")
    assert status == 503
    assert b"capcom scan" in body
    # static files still load, so the page can be styled
    assert missing.get("/static/style.css")[0] == 200


def test_old_index(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    con.execute("INSERT INTO schema_version VALUES (1)")
    con.commit()
    con.close()
    for c in serve(Site(path, TABLE, FAKE, lambda: NOW, UTC)):
        status, data, _ = c.get("/api/spend")
        assert status == 503
        assert "another version" in data["message"]
        status, body, _ = c.get("/spend")
        assert status == 503
        assert b"capcom scan" in body


def test_index_opened_read_only(client: Client, index_path: Path) -> None:
    before = index_path.stat().st_mtime_ns
    for path in ["/api/overview", "/api/spend", "/api/session/s1", "/api/live"]:
        assert client.get(path)[0] == 200
    assert index_path.stat().st_mtime_ns == before


def test_static_modules_resolve() -> None:
    """Every file index.html loads, and every module a script imports, exists."""
    import re

    from capcom.report.server import STATIC

    html = (STATIC / "index.html").read_text()
    names = set(re.findall(r"/static/([\w.]+)", html))
    for js in STATIC.glob("*.js"):
        names |= set(re.findall(r"from '\./([\w.]+)'", js.read_text()))
    assert names
    assert all((STATIC / n).is_file() for n in names), names
    # no network: no absolute URLs in the static files
    for f in STATIC.iterdir():
        assert "http://" not in f.read_text().replace("http://www.w3.org/2000/svg", "")
        assert "https://" not in f.read_text(), f.name
