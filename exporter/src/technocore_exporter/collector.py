"""Map the `/stats` digest onto Prometheus metric families.

Scope is deliberately narrow: only what `store.service_stats` returns. `/stats` carries
five things this exporter does not publish — three from `service_stats` itself and two that
`app.py` adds to the view — and each omission is a decision rather than an oversight; see
OMITTED below.

Naming follows the Prometheus conventions: gauges carry no `_total`, counters do (the
client library appends it), and every byte figure is named `_bytes` because base units
are the convention rather than a preference.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

from .fetch import DEFAULT_TIMEOUT, StatsUnavailableError, fetch_stats

log = logging.getLogger("technocore_exporter")

# The service's own counter names, mapped to (metric, help). `reaped_idle` and
# `reaped_stillborn` are folded into one family with a `reason` label, which is the only
# label in the whole exporter whose values come from the service rather than a constant —
# and it is bounded to exactly these two, because store.COUNTER_KEYS is a fixed tuple.
_REAP_REASONS = {"reaped_idle": "idle", "reaped_stillborn": "stillborn"}

_COUNTERS = {
    "messages": ("technocore_messages", "Messages appended to any room, lifetime."),
    "rooms_created": ("technocore_rooms_created", "Rooms created, lifetime."),
    "notes_written": ("technocore_notes_written", "Note writes, lifetime."),
    "topics_written": ("technocore_topics_written", "Room topic writes, lifetime."),
}

# What this exporter will not publish, and why. Kept as code-adjacent prose because the
# absences are the part a reviewer has to check.
#
# OMITTED: `requests`   — per *worker*, not per service. `_requests` is a plain module
#          dict, so under `--workers N` the digest reports roughly one worker's share
#          (src/app.py:1914 records it under-reporting by 3x once production moved to
#          `--workers 3`). Consecutive scrapes of a load-balanced URL can land on
#          different processes, so no arrangement of these samples is a monotonic
#          counter. Multiplying by `workers` is an estimate, not counter aggregation.
# OMITTED: `history`    — the stored samples. Prometheus stores the samples it scrapes;
#          re-exporting a ring as timestamp-labelled series would double-store it and
#          produce a second, disagreeing history. Only the newest sample's timestamp is
#          used, and only as a freshness gauge.
# OMITTED: `engagement` — pooled over the 50 most recently active rooms, so it is a
#          bounded-window sample rather than a service total. Publishing it beside real
#          totals invites an alert on a number that does not mean what its neighbours
#          mean.
#
# The last two are added to the view by `app.py`, not by `service_stats`, so they are
# outside this package's stated scope by construction — named anyway, because "three
# omissions" invites the reader to check and find five things in the digest:
#
# OMITTED: `capacity_limits` — request-shaping constants (message_chars, read_per_min and
#          friends), not occupancy. The two that actually bound the aggregates here are
#          already published from `service_stats`: `room_bytes_total` is the same constant
#          as `bytes.rooms_capacity` (technocore_room_bytes_capacity) and MAX_ROOMS
#          arrives as `rooms.capacity` (technocore_rooms_capacity).
# OMITTED: `client_identity` — `distinct_identities` counts a module-level dict, so it is
#          per *worker* for exactly the reason `requests` is, and `client_ip_header` is a
#          configuration string rather than a measurement.


def _rooms(rooms: dict) -> Iterable:
    """Room occupancy.

    The split between a label and separate metric names is the whole point of this
    function. `listed`/`unlisted` genuinely partition the room population, so they are
    label values on one metric and `sum by () (technocore_rooms_listing)` is correct.
    The class markers do not partition anything: `room_classes` composes by prefix, so
    `mb-p-x` is both a mailbox and unlisted (its docstring: `mb-p-x -> {mb, p}`), and a
    room can carry several markers at once. Summing those would double-count, so they
    are separate metric names — a shape in which nobody is tempted to add them up.
    """
    total = rooms.get("total", 0)
    yield GaugeMetricFamily(
        "technocore_rooms",
        "Rooms that exist, including unlisted ones. This is the figure the room cap bounds.",
        value=total,
    )
    yield GaugeMetricFamily(
        "technocore_rooms_capacity",
        "Maximum rooms this deployment will hold (store.MAX_ROOMS).",
        value=rooms.get("capacity", 0),
    )
    listing = GaugeMetricFamily(
        "technocore_rooms_listing",
        "Rooms by whether GET /rooms enumerates them. A true partition: these sum to technocore_rooms.",
        labels=["state"],
    )
    for state in ("listed", "unlisted"):
        listing.add_metric([state], rooms.get(state, 0))
    yield listing
    classes = GaugeMetricFamily(
        "technocore_rooms_class",
        "Rooms carrying each class marker. These OVERLAP and must never be summed: a name "
        "composes markers by prefix, so mb-p-x counts under mailbox and is also unlisted.",
        labels=["class"],
    )
    for name in ("mailbox", "ownable", "ephemeral"):
        classes.add_metric([name], rooms.get(name, 0))
    yield classes
    yield GaugeMetricFamily(
        "technocore_rooms_unclassified",
        "Rooms carrying no class marker at all. Not the complement of technocore_rooms_class, "
        "because those overlap.",
        value=rooms.get("open", 0),
    )


def _capacity(payload: dict) -> Iterable:
    """Byte and note gauges — the pressure an operator actually alerts on."""
    size = payload.get("bytes", {})
    notes = payload.get("notes", {})
    yield GaugeMetricFamily(
        "technocore_room_bytes",
        "Bytes held by room files.",
        value=size.get("rooms", 0),
    )
    yield GaugeMetricFamily(
        "technocore_room_bytes_capacity",
        "Byte budget rooms are held to (store.MAX_TOTAL_ROOM_BYTES). The enforced bound, "
        "not MAX_ROOMS * MAX_ROOM_BYTES.",
        value=size.get("rooms_capacity", 0),
    )
    yield GaugeMetricFamily(
        "technocore_note_bytes",
        "Bytes held by note files. Refreshed on create and otherwise at most one reap "
        "interval stale, so it is a gauge to watch rather than a bound a write is refused against.",
        value=size.get("notes", 0),
    )
    yield GaugeMetricFamily(
        "technocore_notes", "Notes stored across every namespace.", value=notes.get("total", 0)
    )
    yield GaugeMetricFamily(
        "technocore_notes_capacity",
        "Maximum notes across all namespaces (store.MAX_NOTES_TOTAL).",
        value=notes.get("capacity", 0),
    )
    yield GaugeMetricFamily(
        "technocore_notes_capacity_per_namespace",
        "Maximum notes in any one namespace (store.MAX_NOTES_PER_NS). A namespace can hit "
        "this while technocore_notes is far from its own cap.",
        value=notes.get("capacity_per_namespace", 0),
    )


def _counters(counters: dict) -> Iterable:
    """The six lifetime counters (store.COUNTER_KEYS).

    Best effort on both axes, and the HELP text says so because an operator who does not
    know it will read a flat line as an outage. They lag: message bumps are batched per
    worker and flushed on an interval or at shutdown, so a hard kill loses the tail. They
    can also reset: the file is rebuilt as zeros if it is lost, which Prometheus reads as
    a counter reset and handles, but which makes `increase()` over that window an
    undercount rather than a gap.
    """
    lag = " Best effort: batched per worker, so it lags, and resets to zero if the counter file is lost."
    for key, (name, help_text) in _COUNTERS.items():
        yield CounterMetricFamily(name, help_text + lag, value=counters.get(key, 0))
    reaped = CounterMetricFamily(
        "technocore_rooms_reaped",
        "Rooms removed by the reaper, by reason." + lag,
        labels=["reason"],
    )
    for key, reason in _REAP_REASONS.items():
        reaped.add_metric([reason], counters.get(key, 0))
    yield reaped


class TechnocoreCollector(Collector):
    """Scrapes /stats once per Prometheus scrape and maps it.

    The service caches that digest for CHAT_STATS_CACHE_SECONDS (60 by default) because
    building it is an O(cap) walk. Scraping faster than the cache therefore buys no
    freshness at all — it only spends requests — which is why the shipped scrape config
    sets 60s and why `technocore_stats_sample_age_seconds` is exported: it lets an
    operator see the staleness rather than assume it away.
    """

    def __init__(self, url: str, token: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout
        self._scrapes = {"success": 0, "error": 0}
        self._last_success = 0.0
        # `start_http_server` runs a ThreadingWSGIServer, so two overlapping scrapes call
        # collect() on this one instance concurrently. Without the lock the counter bumps
        # below are an unguarded read-modify-write and one is silently lost — the same
        # shape as the `_buckets` race in core's limiter.
        #
        # It serialises; it does not coalesce. The second scrape still makes its own origin
        # request once it holds the lock, so the guarantee is at most one request in flight
        # per process, not one request per pair of overlapping scrapes — and a scrape that
        # arrives while a slow read is running waits for it, so /metrics can take up to two
        # timeouts to answer. Deduplicating instead would mean serving one scrape a sample
        # fetched for another, which is a worse trade for a 60s scrape interval.
        self._lock = threading.Lock()

    def collect(self) -> Iterable:
        """Yield the page. Never raises — see _gather."""
        yield from self._gather()

    def _gather(self) -> list:
        """Build every family for one scrape, or the failure telemetry alone.

        A list rather than a generator, and this is the point rather than a style choice.
        `collect()` being a generator meant the try/except below only ever guarded the
        fetch: the mapping ran after the block, on the consumer's iteration, so any error
        in it escaped and the client library answered /metrics with a 500. Building the
        families inside the guard is what makes "no failure escapes" true of the mapping
        as well as the transport.
        """
        with self._lock:
            started = time.monotonic()
            try:
                payload = fetch_stats(self._url, self._token, self._timeout)
                families = [
                    *_rooms(payload.get("rooms", {})),
                    *_capacity(payload),
                    *_counters(payload.get("counters", {})),
                    *self._sample_age(payload),
                ]
            except StatsUnavailableError:
                # No re-raise and no detail in a metric: a failed scrape is reported as
                # success=0 and an error count, and the reason goes to the log. Encoding
                # it as a label value would let the origin's failure mode drive the labels.
                self._scrapes["error"] += 1
                return list(self._self_metrics(time.monotonic() - started, ok=False))
            except Exception:
                # Deliberately broad, and the narrow handlers in fetch.py are still the
                # real answer. This is the structural one: a scrape that receives a 500
                # learns nothing, not even that the exporter is alive. Two transport
                # families have already escaped a narrow handler here (a bare socket
                # timeout, and http.client.HTTPException), so the assumption that any such
                # list is complete has been wrong twice.
                log.exception("unexpected error reading %s", self._url)
                self._scrapes["error"] += 1
                return list(self._self_metrics(time.monotonic() - started, ok=False))
            self._scrapes["success"] += 1
            self._last_success = time.time()
            return families + list(self._self_metrics(time.monotonic() - started, ok=True))

    def _sample_age(self, payload: dict) -> Iterable:
        """Age of the newest stored sample.

        This is the only thing taken from `history`, and it is not the age of the figures
        above: those come from a cache at most CHAT_STATS_CACHE_SECONDS old, while the
        stored samples are written at most every SNAPSHOT_EVERY (300s) and kept for
        SNAPSHOT_KEEP_SECONDS (30h). It is named for the sample rather than for the scrape
        because of that gap.

        Do not alert on it. Sampling is driven by writes, not by a timer, so a service
        nobody is writing to has an unboundedly old newest sample while being perfectly
        healthy — measured at 300.6s on an idle probe instance moments after the last
        write. `technocore_exporter_scrape_success` is the availability signal; this is
        context for reading the history, and a floor under how stale a `history`-derived
        number can be.
        """
        history = payload.get("history")
        if not isinstance(history, list) or not history:
            return
        newest = history[-1]
        stamp = newest.get("t") if isinstance(newest, dict) else None
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            return
        yield GaugeMetricFamily(
            "technocore_stats_sample_age_seconds",
            "Age of the newest stored aggregate sample. Samples are written by writes, at "
            "most one per 300s — so this grows without bound on an idle service and is NOT "
            "an availability signal. Not the age of the gauges above, which come from a "
            "separate 60s cache.",
            value=max(0.0, time.time() - float(stamp)),
        )

    def _self_metrics(self, duration: float, ok: bool) -> Iterable:
        """Whether the exporter itself is working — the first thing to alert on.

        Without these, a broken token is indistinguishable from a service with no rooms:
        both render as an absence of samples, and `absent()` alerts are the ones people
        forget to write.
        """
        yield GaugeMetricFamily(
            "technocore_exporter_scrape_success",
            "1 if the most recent /stats read succeeded, 0 otherwise.",
            value=1 if ok else 0,
        )
        yield GaugeMetricFamily(
            "technocore_exporter_scrape_duration_seconds",
            "Wall time of the most recent /stats read.",
            value=duration,
        )
        yield GaugeMetricFamily(
            "technocore_exporter_last_success_timestamp_seconds",
            "Unix time of the last successful /stats read; 0 if there has not been one.",
            value=self._last_success,
        )
        scrapes = CounterMetricFamily(
            "technocore_exporter_scrapes",
            "/stats reads attempted by this exporter process, by outcome.",
            labels=["outcome"],
        )
        for outcome in ("success", "error"):
            scrapes.add_metric([outcome], self._scrapes[outcome])
        yield scrapes
