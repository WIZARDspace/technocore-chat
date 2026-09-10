"""The one call, and the three ways it goes wrong."""

from __future__ import annotations

import http.client
import http.server
import io
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest
from technocore_exporter.fetch import REQUIRED, StatsUnavailableError, fetch_stats

URL = "http://origin.invalid/stats"


def _valid() -> dict:
    """The smallest digest validate() accepts: every required field, all zero."""
    return {section: dict.fromkeys(fields, 0) for section, fields in REQUIRED.items()}


class _Response(io.BytesIO):
    """Enough of an HTTPResponse for the one `with urlopen(...)` in fetch_stats.

    io.BytesIO is already a context manager that closes itself, so this only adds the
    `.status` the code reads — overriding __enter__/__exit__ would narrow the inherited
    signature for nothing.
    """

    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status


class _Opener:
    """Stands in for urlopen, recording the request so the header assertions can read it."""

    def __init__(self, result):
        self.result = result
        self.seen: urllib.request.Request | None = None

    def __call__(self, request, timeout=None):
        if isinstance(self.result, Exception):
            raise self.result
        self.seen = request
        return self.result


class _StubOpener:
    """Stands in for the module's opener — the transport boundary, and the only thing a
    unit test should replace. Patching `urllib.request.urlopen` would no longer intercept:
    fetch_stats goes through a build_opener() instance so redirects can be refused."""

    def __init__(self, result):
        self.inner = _Opener(result)

    def open(self, request, timeout=None):
        return self.inner(request, timeout)

    @property
    def seen(self):
        return self.inner.seen


def _patch(monkeypatch, result) -> _StubOpener:
    opener = _StubOpener(result)
    monkeypatch.setattr("technocore_exporter.fetch._OPENER", opener)
    return opener


def test_the_token_rides_in_a_header_never_the_url(monkeypatch):
    """A query parameter would land in the proxy logs, and the service ignores it there."""
    opener = _patch(monkeypatch, _Response(json.dumps(_valid()).encode()))
    fetch_stats(URL, "s3cret")
    assert opener.seen is not None
    assert opener.seen.get_full_url() == URL
    assert "s3cret" not in opener.seen.get_full_url()
    assert opener.seen.get_header("X-stats-token") == "s3cret"


def test_a_404_hints_at_the_token(monkeypatch):
    """The endpoint reports itself missing rather than forbidden, so the hint is the fix."""
    _patch(monkeypatch, urllib.error.HTTPError(URL, 404, "Not Found", Message(), None))
    with pytest.raises(StatsUnavailableError, match="wrong or unset CHAT_STATS_TOKEN"):
        fetch_stats(URL, "wrong")


def test_no_response_body_reaches_the_error(monkeypatch):
    """/stats answers 404 with the same bytes an unrouted path gets; a proxy answers with
    its own page. Neither belongs in a log line an operator will paste somewhere."""
    body = io.BytesIO(b"<html>upstream said something quotable</html>")
    _patch(monkeypatch, urllib.error.HTTPError(URL, 502, "Bad Gateway", Message(), body))
    with pytest.raises(StatsUnavailableError) as caught:
        fetch_stats(URL, "token")
    assert "quotable" not in str(caught.value)
    assert "502" in str(caught.value)


def test_a_non_json_body_is_refused(monkeypatch):
    _patch(monkeypatch, _Response(b"not json at all"))
    with pytest.raises(StatsUnavailableError, match="not JSON"):
        fetch_stats(URL, "token")


def test_a_json_array_is_refused(monkeypatch):
    """Valid JSON, wrong shape — `.get` on a list is an AttributeError at scrape time."""
    _patch(monkeypatch, _Response(b"[1, 2, 3]"))
    with pytest.raises(StatsUnavailableError, match="not a JSON object"):
        fetch_stats(URL, "token")


def test_an_unreachable_origin_is_reported_not_raised_raw(monkeypatch):
    _patch(monkeypatch, urllib.error.URLError("connection refused"))
    with pytest.raises(StatsUnavailableError, match="unreachable"):
        fetch_stats(URL, "token")


def test_a_timeout_is_reported_rather_than_escaping(monkeypatch):
    """The regression behind splitting TimeoutError out of the URLError handler.

    `urlopen` raises a bare socket timeout, which is a TimeoutError and not a URLError, so
    it has no `.reason`. Handled together, the f-string raised AttributeError from inside
    the handler — past StatsUnavailableError, out of `collect()`, and into a 500 on
    /metrics. A slow origin has to read as a failed scrape, not as a broken exporter.
    """
    _patch(monkeypatch, TimeoutError())
    with pytest.raises(StatsUnavailableError, match="timed out after"):
        fetch_stats(URL, "token", timeout=2.5)


def test_a_2xx_that_is_not_200_is_refused(monkeypatch):
    """Defensive, and cheap to hold: urlopen raises HTTPError for 4xx/5xx, so this branch
    is for the odd success — a 204, or a redirect the opener followed to an empty body.
    Falling through would hand `json.loads` an empty string and report it as 'not JSON',
    which sends an operator looking at the wrong thing."""
    _patch(monkeypatch, _Response(b"", status=204))
    with pytest.raises(StatsUnavailableError, match="status 204"):
        fetch_stats(URL, "token")


# --------------------------------------------------------------- reported by @osr21


def test_the_token_never_follows_a_redirect(monkeypatch):
    """A real credential leak, and not a theoretical one.

    `urllib.request.HTTPRedirectHandler.redirect_request` copies every header except
    `content-length` and `content-type` onto the redirected request, so a 302 from the
    configured URL to any host hands `X-Stats-Token` to that host and `urlopen` follows it
    silently. Reproduced end to end against two loopback servers before the fix: the sink
    received the sentinel token and `fetch_stats` accepted the sink's reply.
    """
    for code in (301, 302, 303, 307, 308):
        _patch(monkeypatch, urllib.error.HTTPError(URL, code, "Found", Message(), None))
        with pytest.raises(StatsUnavailableError, match="refusing to send the token"):
            fetch_stats(URL, "sentinel-token")


def test_the_redirect_handler_refuses_rather_than_rewrites():
    """Pinned on the handler itself, because the behaviour above is one `return None`."""
    from technocore_exporter.fetch import _NoRedirect

    assert _NoRedirect().redirect_request(None, None, 302, "Found", Message(), URL) is None


@pytest.mark.parametrize(
    "exc",
    [
        http.client.BadStatusLine("broken"),
        http.client.IncompleteRead(b"half"),
        http.client.LineTooLong("header line"),
    ],
)
def test_a_protocol_error_is_reported_not_raised_raw(monkeypatch, exc):
    """The sibling of the TimeoutError case, and the same lesson twice.

    `http.client.HTTPException` subclasses are raised while parsing a response and are
    neither `URLError` nor `TimeoutError`, so a narrow handler lets them escape as
    themselves — out of `collect()` and into a 500 on /metrics, which reports nothing at
    all rather than reporting a failed scrape.
    """
    _patch(monkeypatch, exc)
    with pytest.raises(StatsUnavailableError, match="protocol error"):
        fetch_stats(URL, "token")


def test_a_digest_missing_a_required_field_is_refused(monkeypatch):
    """Absent is not zero.

    Mapping a partial digest and defaulting the rest publishes `technocore_notes 0` beside
    `scrape_success 1` — a healthy-looking empty service, indistinguishable from a real
    one, against which a note-capacity alert can never fire.
    """
    _patch(monkeypatch, _Response(json.dumps({"rooms": {"total": 7}}).encode()))
    with pytest.raises(StatsUnavailableError, match="missing rooms."):
        fetch_stats(URL, "token")


def test_a_missing_section_is_refused(monkeypatch):
    payload = _valid()
    del payload["counters"]
    _patch(monkeypatch, _Response(json.dumps(payload).encode()))
    with pytest.raises(StatsUnavailableError, match="no counters object"):
        fetch_stats(URL, "token")


@pytest.mark.parametrize(
    ("value", "match"),
    [
        (True, "not a number"),
        (False, "not a number"),
        ("12", "not a number"),
        (None, "not a number"),
        (-1, "negative"),
        (float("nan"), "not finite"),
        (float("inf"), "not finite"),
    ],
)
def test_an_untrustworthy_field_value_is_refused(monkeypatch, value, match):
    """`True` is an `int` in Python, so a bool would publish as 1.0 — a wrong number rather
    than a missing one. NaN and the infinities parse and publish, then poison every rate
    and ratio computed from them downstream."""
    payload = _valid()
    payload["rooms"]["total"] = value
    _patch(monkeypatch, _Response(json.dumps(payload).encode()))
    with pytest.raises(StatsUnavailableError, match=match):
        fetch_stats(URL, "token")


def test_a_complete_digest_is_accepted(monkeypatch):
    """The other direction, so the validator cannot pass by refusing everything."""
    _patch(monkeypatch, _Response(json.dumps(_valid()).encode()))
    assert fetch_stats(URL, "token")["rooms"]["total"] == 0


def test_the_opener_ignores_ambient_proxy_configuration():
    """The direct half: no proxy handler in the module's opener carries any mapping.

    `build_opener()` installs a default `ProxyHandler` that reads HTTP_PROXY/HTTPS_PROXY
    from the environment, so without `ProxyHandler({})` the token goes to whatever those
    name. Asserted on the opener itself rather than on behaviour, because the handler is
    constructed once at import and a behavioural test in this process would pass for the
    wrong reason — the environment having been read before the test set it.
    """
    from technocore_exporter.fetch import _OPENER

    # `ProxyHandler({})` registers no per-scheme `<type>_open` method, so OpenerDirector
    # keeps none of it — the absence *is* the property, and it is stronger than an empty
    # mapping being present. Verified against the contrast: with HTTP_PROXY set, a plain
    # `build_opener()` carries a ProxyHandler holding {'http': 'http://...'}, while
    # `build_opener(ProxyHandler({}))` carries none.
    # getattr because `OpenerDirector.handlers` is real at runtime but absent from the
    # typeshed stubs, and a suppression comment would hide a genuine future error here.
    handlers = getattr(_OPENER, "handlers", None)
    assert handlers, "opener exposes no handlers to inspect"
    assert not [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]


def test_a_configured_proxy_never_receives_the_token(tmp_path):
    """The end-to-end half, in a subprocess because the opener is built at import.

    Reported by @yukkie3276: with HTTP_PROXY set and no NO_PROXY, urllib routed the whole
    request — `X-Stats-Token` included — through the proxy. Reproduced before the fix; the
    sink received the sentinel token and the absolute URL as its request path.
    """
    script = tmp_path / "probe.py"
    script.write_text(
        "import http.server, os, sys, threading, json\n"
        "seen = {}\n"
        "class P(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        seen['t'] = self.headers.get('X-Stats-Token')\n"
        "        b = b'{}'\n"
        "        self.send_response(200); self.send_header('Content-Length', str(len(b)))\n"
        "        self.end_headers(); self.wfile.write(b)\n"
        "    def log_message(self, *a): pass\n"
        "srv = http.server.HTTPServer(('127.0.0.1', 0), P)\n"
        "threading.Thread(target=srv.serve_forever, daemon=True).start()\n"
        "os.environ['HTTP_PROXY'] = 'http://127.0.0.1:%d' % srv.server_port\n"
        "os.environ.pop('NO_PROXY', None); os.environ.pop('no_proxy', None)\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from technocore_exporter.fetch import fetch_stats, StatsUnavailableError\n"
        "try:\n"
        "    fetch_stats('http://stats.example.invalid/stats', 'sentinel-token', timeout=3)\n"
        "except StatsUnavailableError:\n"
        "    pass\n"
        "print(json.dumps(seen.get('t')))\n"
    )
    src = Path(__file__).resolve().parents[2] / "exporter" / "src"
    result = subprocess.run(
        [sys.executable, str(script), str(src)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) is None, "the proxy received the stats token"


def test_an_oversized_body_is_refused_without_reading_it_all(monkeypatch):
    """`response.read()` with no argument reads whatever the far side sends.

    Monitoring that can be made to exhaust memory takes the observability down with the
    thing it was watching, so the read is capped and one byte over the cap is enough to
    refuse.
    """
    from technocore_exporter.fetch import MAX_BODY_BYTES

    _patch(monkeypatch, _Response(b"x" * (MAX_BODY_BYTES + 10)))
    with pytest.raises(StatsUnavailableError, match="exceeded"):
        fetch_stats(URL, "token")


def test_an_ordinary_digest_is_still_read(monkeypatch):
    """The cap must not pass by refusing everything. A real digest is nowhere near it."""
    payload = json.dumps(_valid()).encode()
    assert len(payload) < 4096
    _patch(monkeypatch, _Response(payload))
    assert fetch_stats(URL, "token")["rooms"]["total"] == 0


@pytest.mark.parametrize(("cap_delta", "refused"), [(0, False), (-1, True)])
def test_the_body_cap_is_exact_at_its_boundary(monkeypatch, cap_delta, refused):
    """Exactly at the cap is read; one byte over is refused.

    Against a cap moved to the fixture's own length rather than against an 8 MiB body: the
    boundary is what `read(MAX_BODY_BYTES + 1)` plus `len(raw) > MAX_BODY_BYTES` gets wrong
    by one in either direction, and the previous pair of tests only covered "far under" and
    "far over", where an off-by-one is invisible.
    """
    payload = json.dumps(_valid()).encode()
    monkeypatch.setattr("technocore_exporter.fetch.MAX_BODY_BYTES", len(payload) + cap_delta)
    _patch(monkeypatch, _Response(payload))
    if refused:
        with pytest.raises(StatsUnavailableError, match="exceeded"):
            fetch_stats(URL, "token")
    else:
        assert fetch_stats(URL, "token")["rooms"]["total"] == 0


def test_the_token_never_reaches_a_redirect_target_end_to_end():
    """The same property as the parametrised test above, proved against real sockets.

    The unit test asserts the message for each 3xx; this asserts the thing that actually
    matters — that the sink never sees the credential. Kept alongside rather than instead:
    the unit test would still pass if `_NoRedirect` were removed and something else began
    raising, and this one would not.
    """
    received = {}

    class Sink(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            received["token"] = self.headers.get("X-Stats-Token")
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # Name matches BaseHTTPRequestHandler.log_message exactly; renaming the first
        # parameter would break a keyword call through the base signature.
        def log_message(self, format, *args):  # noqa: A002 - see above
            pass

    sink = http.server.HTTPServer(("127.0.0.1", 0), Sink)

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/stats")
            self.send_header("Content-Length", "0")
            self.end_headers()

        # Name matches BaseHTTPRequestHandler.log_message exactly; renaming the first
        # parameter would break a keyword call through the base signature.
        def log_message(self, format, *args):  # noqa: A002 - see above
            pass

    source = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    for server in (sink, source):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(StatsUnavailableError, match="refusing to send the token"):
            fetch_stats(f"http://127.0.0.1:{source.server_port}/stats", "sentinel-token", 5)
        assert received.get("token") is None, "the redirect target received the stats token"
    finally:
        for server in (sink, source):
            server.shutdown()
            server.server_close()
