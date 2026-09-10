"""The one call this exporter makes: GET /stats with the operator's token.

Kept separate from the collector so the mapping can be tested against a fixture with no
socket, which is what makes the golden-output test in tests/exporter/test_collector.py
exact.

Two rules hold this file together, and both exist because the digest is behind a bearer
token that is the whole gate on it:

  * the token goes to the configured origin and nowhere else — see _NoRedirect for
    redirects and _OPENER for ambient proxy configuration; and
  * nothing raised by the transport escapes as itself. A scrape that fails must reach the
    collector as StatsUnavailableError so it can be published as success 0. Anything that
    escapes turns /metrics into a 500, which is the one answer a scrape must never get.
"""

from __future__ import annotations

import http.client
import json
import math
import urllib.error
import urllib.request

# Below Prometheus's common 10s scrape_timeout, so a slow origin surfaces as a failed
# scrape with telemetry rather than as a scrape timeout with no samples at all — the
# second says nothing about which side is unwell.
DEFAULT_TIMEOUT = 5.0

# A ceiling on what one scrape will read into memory. The digest is aggregates plus at most
# 30h of five-minute samples — a few hundred KB at the very top end — so this is orders of
# magnitude of headroom rather than a tight bound. It exists because `response.read()` with
# no argument will read whatever the far side sends, and "the far side" is reachable by
# anything that can occupy the configured address: a wedged origin, a captive portal, or a
# proxy the operator did not intend. An exporter is monitoring, and monitoring that can be
# made to exhaust memory takes the observability down with the thing it was watching.
MAX_BODY_BYTES = 8 * 1024 * 1024

# Every field the mapping reads, plus `notes.bytes`, which it does not: the digest carries
# the same number twice (`bytes.notes` is `note_stats()["bytes"]`) and a digest that has
# only one of them is not the shape this was written against. A digest missing any of these
# is refused rather than mapped: absent is not zero, and a note-capacity alert that reads 0
# because the field was missing is worse than no sample, because it will never fire.
REQUIRED = {
    "rooms": ("total", "listed", "unlisted", "open", "mailbox", "ownable", "ephemeral", "capacity"),
    "bytes": ("rooms", "notes", "rooms_capacity"),
    "notes": ("total", "bytes", "capacity", "capacity_per_namespace"),
    "counters": (
        "messages",
        "rooms_created",
        "reaped_idle",
        "reaped_stillborn",
        "notes_written",
        "topics_written",
    ),
}


class StatsUnavailableError(Exception):
    """The digest could not be read, or could not be trusted. Carries no response body.

    The body is deliberately dropped rather than attached: /stats answers 404 with the
    same bytes an unrouted path gets, so a body here would only ever be that constant or
    a proxy's error page, and attaching it invites an operator to paste a page that came
    back from somewhere other than the origin into a log or a ticket.
    """


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so the token cannot follow one off the configured origin.

    This is not belt-and-braces. `urllib.request.HTTPRedirectHandler.redirect_request`
    copies every header except `content-length` and `content-type` onto the new request,
    so a 302 from the configured URL to any host — a misconfiguration, a proxy, a
    compromised hop — hands `X-Stats-Token` to that host, and `urlopen` follows it
    silently. Returning None makes urlopen raise the 3xx as an HTTPError instead, which
    the caller below reports as an unavailable source.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# ProxyHandler({}) first, and it is not decoration: build_opener() installs a default
# ProxyHandler that reads HTTP_PROXY/HTTPS_PROXY from the environment, so a process
# started with either set would send this request — token included — to the proxy rather
# than to the configured origin. An empty mapping disables that discovery entirely.
#
# Disabling rather than honouring it is the right default for a credential-bearing client
# whose origin defaults to 127.0.0.1: proxying localhost is almost never intended, the
# NO_PROXY exemption for it is a well-known footgun to get wrong, and the invariant this
# module states is that the token reaches the configured origin and nowhere else. An
# operator who genuinely needs an egress proxy needs an explicit, trusted setting for it,
# not an ambient environment variable.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _number(value, where: str) -> float:
    """A digest field as a metric value, or refuse the sample.

    `bool` is checked before `int` because in Python `True` is an `int`, and a gauge that
    silently reads 1.0 from a `true` is a wrong number rather than a missing one. NaN and
    the infinities are refused for the same reason: they parse, they publish, and they
    poison every rate and ratio computed downstream.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StatsUnavailableError(f"{where} is not a number")
    if not math.isfinite(value):
        raise StatsUnavailableError(f"{where} is not finite")
    if value < 0:
        raise StatsUnavailableError(f"{where} is negative")
    return float(value)


def validate(payload: dict) -> dict:
    """Refuse a structurally incomplete or untrustworthy digest.

    Every field the mapping reads must be present and a finite non-negative number. The
    alternative — mapping what is there and defaulting the rest to zero — publishes
    `technocore_notes 0` beside `scrape_success 1`, which reads as a healthy empty service
    and is indistinguishable from one.
    """
    for section, fields in REQUIRED.items():
        block = payload.get(section)
        if not isinstance(block, dict):
            raise StatsUnavailableError(f"digest has no {section} object")
        for field in fields:
            if field not in block:
                raise StatsUnavailableError(f"digest is missing {section}.{field}")
            _number(block[field], f"{section}.{field}")
    return payload


def fetch_stats(url: str, token: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Read and validate the JSON digest. Raises StatsUnavailableError for anything else.

    The token rides in `X-Stats-Token`, never in the URL: a query parameter would land in
    the proxy logs and in the exporter's own error text, and the service does not accept
    it there anyway.
    """
    request = urllib.request.Request(url, headers={"X-Stats-Token": token})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise StatsUnavailableError(f"status {response.status}")
            # One byte over the cap is enough to know it was exceeded, without reading
            # the rest of whatever is being sent.
            raw = response.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                raise StatsUnavailableError(f"response exceeded {MAX_BODY_BYTES} bytes")
            payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        # 404 is what a wrong or unset token looks like — the endpoint reports itself
        # missing rather than forbidden, so an operator debugging this needs the hint.
        # A 3xx arrives here too, because _NoRedirect refuses to follow it.
        if exc.code in (301, 302, 303, 307, 308):
            raise StatsUnavailableError(
                f"status {exc.code}: refusing to send the token to a redirect target"
            ) from None
        hint = " (wrong or unset CHAT_STATS_TOKEN?)" if exc.code == 404 else ""
        raise StatsUnavailableError(f"status {exc.code}{hint}") from None
    except urllib.error.URLError as exc:
        raise StatsUnavailableError(f"unreachable: {exc.reason}") from None
    except TimeoutError:
        # Split from URLError deliberately: a bare socket timeout is *not* a URLError and
        # has no `.reason`, so folding the two together raised AttributeError from inside
        # this handler — escaping StatsUnavailableError entirely.
        raise StatsUnavailableError(f"timed out after {timeout}s") from None
    except http.client.HTTPException as exc:
        # The sibling of the TimeoutError case above, and the same lesson: BadStatusLine,
        # IncompleteRead and RemoteDisconnected are raised while parsing a response and
        # are neither URLError nor TimeoutError, so without this they escape as themselves.
        raise StatsUnavailableError(f"protocol error: {type(exc).__name__}") from None
    except ValueError:
        raise StatsUnavailableError("response was not JSON") from None
    if not isinstance(payload, dict):
        raise StatsUnavailableError("response was not a JSON object")
    return validate(payload)
