"""What the exporter publishes, and — more of the file than usual — what it does not.

The omissions are the reviewable part of this package: an exporter that ships a misleading
metric is worse than one that ships nothing, because an alert gets written against it.
"""

from __future__ import annotations

import threading
import time

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families
from technocore_exporter.collector import TechnocoreCollector
from technocore_exporter.fetch import StatsUnavailableError


class _Collector(TechnocoreCollector):
    """The collector with the network replaced by a value, or an exception."""

    def __init__(self, payload=None, error=None):
        super().__init__("http://origin.invalid/stats", "token")
        self._payload = payload
        self._error = error

    def _read(self):
        if self._error is not None:
            raise self._error
        return self._payload


@pytest.fixture
def render(stats, monkeypatch):
    """Render the exposition page for a given digest, or for a failed read.

    The module-level `fetch_stats` is what gets replaced, not a method on the collector, so
    every test drives `collect()` by the same path production does.
    """

    def _render(payload=None, error=None):
        collector = _Collector(payload if error is None else None, error)
        monkeypatch.setattr(
            "technocore_exporter.collector.fetch_stats",
            lambda *a, **k: collector._read(),
        )
        registry = CollectorRegistry()
        registry.register(collector)
        return generate_latest(registry).decode()

    return lambda payload=stats, error=None: _render(payload, error)


def _families(text: str) -> dict:
    return {f.name: f for f in text_string_to_metric_families(text)}


def _sample(text: str, name: str, **labels) -> float:
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
                return s.value
    raise AssertionError(f"no sample {name}{labels or ''} in output")


def test_the_output_is_valid_prometheus_exposition(render):
    """A parser round-trip, not a string compare: this is the check promtool also makes."""
    text = render()
    families = _families(text)
    assert families, "nothing parsed"
    for family in families.values():
        assert family.documentation, f"{family.name} has no HELP"
        assert family.type in {"gauge", "counter", "unknown"}


def test_the_listing_labels_partition_the_room_total(render, stats):
    """listed + unlisted == total, on real numbers. Summing this label is correct."""
    text = render()
    listed = _sample(text, "technocore_rooms_listing", state="listed")
    unlisted = _sample(text, "technocore_rooms_listing", state="unlisted")
    total = _sample(text, "technocore_rooms")
    assert listed == stats["rooms"]["listed"] == 4
    assert unlisted == stats["rooms"]["unlisted"] == 2
    assert listed + unlisted == total == 6


def test_the_class_counts_do_not_partition_and_say_so(render, stats):
    """The trap this exporter exists to not fall into.

    The fixture holds a room that is both a mailbox and unlisted, and one that is unlisted
    with no other marker — so the class counts fall short of the total, and a dashboard
    that summed them would under-report occupancy while looking arithmetically tidy.
    """
    text = render()
    classes = sum(
        _sample(text, "technocore_rooms_class", **{"class": name})
        for name in ("mailbox", "ownable", "ephemeral")
    )
    unclassified = _sample(text, "technocore_rooms_unclassified")
    total = _sample(text, "technocore_rooms")
    assert classes + unclassified == 5
    assert total == 6
    assert classes + unclassified < total, "fixture no longer exercises the overlap"
    help_text = _families(text)["technocore_rooms_class"].documentation
    assert "OVERLAP" in help_text and "never be summed" in help_text


def test_every_label_value_is_from_a_fixed_set(render):
    """No room name, namespace, DID, IP or URL may ever become a label value.

    Asserted as an allowlist rather than a denylist: a denylist passes for whatever nobody
    thought of, and the whole point is that this label set is closed.
    """
    allowed = {
        "state": {"listed", "unlisted"},
        "class": {"mailbox", "ownable", "ephemeral"},
        "reason": {"idle", "stillborn"},
        "outcome": {"success", "error"},
    }
    for family in text_string_to_metric_families(render()):
        for s in family.samples:
            for key, value in s.labels.items():
                assert key in allowed, f"unexpected label {key!r} on {s.name}"
                assert value in allowed[key], f"unexpected value {value!r} for {key}"


def test_per_worker_request_counters_are_not_exported(render, stats):
    """`requests` is per worker; consecutive scrapes of a load-balanced URL can land on
    different processes, so no arrangement of those samples is a monotonic counter."""
    assert "requests" in stats, "fixture should still carry the field being refused"
    text = render()
    for forbidden in ("technocore_requests", "technocore_read", "technocore_rate_limited"):
        assert forbidden not in text
    assert "uptime_seconds" not in text
    assert "per_worker" not in text


def test_history_is_not_republished_as_series(render, stats):
    """Only the newest sample's timestamp is used, and only as freshness."""
    assert stats["history"], "fixture should carry at least one stored sample"
    text = render()
    assert "technocore_history" not in text
    assert _sample(text, "technocore_stats_sample_age_seconds") >= 0


def test_engagement_is_not_exported(render, stats):
    """A bounded-window sample, not a service total — it must not sit beside real ones."""
    assert "engagement" in stats
    text = render()
    for forbidden in ("engagement", "zero_response", "nick_diversity", "windowed_messages"):
        assert forbidden not in text


def test_counters_are_counters_and_gauges_are_gauges(render):
    """`_total` on counters and not on gauges is the convention promtool enforces."""
    families = _families(render())
    assert families["technocore_messages"].type == "counter"
    assert families["technocore_rooms"].type == "gauge"
    text = render()
    assert "technocore_messages_total" in text
    assert "technocore_rooms_total" not in text


def test_the_reap_reason_label_carries_both_values(render, stats):
    text = render()
    assert _sample(text, "technocore_rooms_reaped_total", reason="idle") == 0
    assert _sample(text, "technocore_rooms_reaped_total", reason="stillborn") == 0
    assert stats["counters"]["reaped_idle"] == 0


def test_the_reap_reasons_are_not_transposed(render, stats):
    """The fixture has both reap counters at 0, so no other test can tell them apart.

    With `reaped_idle == reaped_stillborn == 0`, swapping the two values in `_REAP_REASONS`
    passes every other assertion in this file — including the one above, which checks both
    samples are 0. The mapping direction is pinned here against distinct numbers instead.
    """
    counters = {**stats["counters"], "reaped_idle": 7, "reaped_stillborn": 3}
    text = render(payload={**stats, "counters": counters})
    assert _sample(text, "technocore_rooms_reaped_total", reason="idle") == 7
    assert _sample(text, "technocore_rooms_reaped_total", reason="stillborn") == 3


def test_the_real_values_are_carried_through(render, stats):
    """The mapping itself, against the captured digest."""
    text = render()
    assert _sample(text, "technocore_messages_total") == stats["counters"]["messages"] == 9
    assert _sample(text, "technocore_rooms_created_total") == 5
    assert _sample(text, "technocore_notes_written_total") == 5
    assert _sample(text, "technocore_topics_written_total") == 3
    assert _sample(text, "technocore_room_bytes") == stats["bytes"]["rooms"] == 1200
    assert _sample(text, "technocore_note_bytes") == 63
    assert _sample(text, "technocore_notes") == 4
    assert _sample(text, "technocore_notes_capacity") == 163840
    assert _sample(text, "technocore_notes_capacity_per_namespace") == 5120
    assert _sample(text, "technocore_rooms_capacity") == 5120
    assert _sample(text, "technocore_room_bytes_capacity") == 5368709120


def test_counter_help_states_the_lag_and_reset_behaviour(render):
    """An operator who does not know these are batched reads a flat line as an outage."""
    doc = _families(render())["technocore_messages"].documentation
    assert "Best effort" in doc and "lags" in doc and "resets to zero" in doc


def test_a_failed_scrape_reports_zero_and_still_serves_self_metrics(render):
    """The failure mode that must not look like an empty service.

    Without these, a wrong token and a service with no rooms are the same absence of
    samples — and `absent()` alerts are the ones people forget to write.
    """
    text = render(error=StatsUnavailableError("status 404 (wrong or unset CHAT_STATS_TOKEN?)"))
    assert _sample(text, "technocore_exporter_scrape_success") == 0
    assert _sample(text, "technocore_exporter_scrapes_total", outcome="error") == 1
    assert "technocore_rooms " not in text, "no stale storage gauges on a failed scrape"


def test_the_failure_reason_never_reaches_a_metric(render):
    """The origin's failure text must not drive the label set."""
    text = render(error=StatsUnavailableError("unreachable: [Errno -2] Name or service not known"))
    assert "Errno" not in text and "not known" not in text


def test_an_unexpected_error_still_publishes_a_failed_scrape(render):
    """`collect()` must never propagate, whatever the cause.

    An exception escaping here makes the client library answer /metrics with a 500, so the
    scrape learns nothing — not even that the exporter is alive. Two transport families
    have already escaped a narrow handler in fetch.py (a bare socket timeout, and
    http.client.HTTPException), so the broad catch is the structural answer rather than a
    third guess at a complete list.
    """
    text = render(error=RuntimeError("something nobody predicted"))
    assert _sample(text, "technocore_exporter_scrape_success") == 0
    assert _sample(text, "technocore_exporter_scrapes_total", outcome="error") == 1
    assert "technocore_rooms " not in text
    assert "nobody predicted" not in text, "the cause goes to the log, never to a metric"


def test_a_bool_timestamp_is_not_read_as_a_number(render, stats):
    """`True` is an int in Python; a sample age of 'now minus True' would be nonsense."""
    payload = {**stats, "history": [{"t": True}]}
    assert "technocore_stats_sample_age_seconds" not in render(payload=payload)


# ------------------------------------------------------- found in self-review, not by CI


def test_a_mapping_error_cannot_escape_collect(render, stats, monkeypatch):
    """The guard has to cover the mapping, not only the fetch.

    `collect()` was a generator whose try/except wrapped `fetch_stats` alone, so the
    mapping ran later, on the consumer's iteration, and any error in it escaped — leaving
    /metrics answering 500, which is exactly what the broad catch exists to prevent. The
    families are built inside the guard now. Reproduced before the fix by making one
    mapping function raise.
    """
    import technocore_exporter.collector as module

    monkeypatch.setattr(module, "_capacity", lambda p: (_ for _ in ()).throw(KeyError("bug")))
    text = render()
    assert _sample(text, "technocore_exporter_scrape_success") == 0
    assert "technocore_rooms " not in text, "a partial page is worse than a failed one"
    assert "bug" not in text, "the cause goes to the log, never to a metric"


def test_overlapping_scrapes_are_serialised(stats, monkeypatch):
    """`start_http_server` is threaded, so collect() runs concurrently on one instance.

    Asserted as non-overlap rather than as a final count: `self._scrapes[...] += 1` is an
    unguarded read-modify-write without the lock — the same shape as the `_buckets` race in
    core's limiter — but a count assertion can pass by luck, since losing a bump needs the
    threads to interleave on exactly that bytecode. Depth is deterministic: if any two
    bodies ever overlap, the maximum observed depth is 2 and the test fails every time.

    Serialising is not deduplicating, and the last assertion below is what says so: eight
    threads produce eight successful reads, one after another. The lock bounds how many
    origin requests are in flight at once, not how many are made.
    """
    import technocore_exporter.collector as module

    depth = 0
    peak = 0
    seen = threading.Lock()

    def tracked_fetch(*a, **k):
        nonlocal depth, peak
        with seen:
            depth += 1
            peak = max(peak, depth)
        time.sleep(0.01)
        with seen:
            depth -= 1
        return stats

    monkeypatch.setattr(module, "fetch_stats", tracked_fetch)
    collector = TechnocoreCollector("http://origin.invalid/stats", "token")
    threads = [threading.Thread(target=lambda: list(collector.collect())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a scrape deadlocked"
    assert peak == 1, f"{peak} scrapes overlapped; the collector lock is not holding"
    assert collector._scrapes["success"] == 8
