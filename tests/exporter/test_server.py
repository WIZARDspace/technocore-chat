"""Configuration rules, which is where this package's security properties live."""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry
from technocore_exporter.fetch import DEFAULT_TIMEOUT
from technocore_exporter.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_URL,
    SCRAPE_TIMEOUT_CEILING,
    build,
    describe,
    settings,
)

TOKEN = {"TECHNOCORE_STATS_TOKEN": "s3cret-token"}


def test_a_missing_token_stops_the_process_with_the_reason():
    """Not a warning and not a 404 loop: without the token there is nothing to export, and
    the failure mode at the origin (404, not 401) is the one an operator misreads."""
    with pytest.raises(SystemExit, match="CHAT_STATS_TOKEN"):
        settings(env={})


def test_an_empty_token_counts_as_missing():
    with pytest.raises(SystemExit):
        settings(env={"TECHNOCORE_STATS_TOKEN": ""})


def test_the_defaults_are_loopback_and_the_local_origin():
    """Loopback by default because /metrics is not gated at all."""
    config = settings(env=TOKEN)
    assert config.host == DEFAULT_HOST == "127.0.0.1"
    assert config.port == DEFAULT_PORT == 9464
    assert config.url == DEFAULT_URL
    # Below Prometheus's common 10s scrape_timeout, so a slow origin is reported as a
    # failed scrape rather than swallowed by the scrape timing out with no samples.
    assert config.timeout == DEFAULT_TIMEOUT < 10.0


def test_every_setting_is_overridable():
    config = settings(
        env={
            **TOKEN,
            "TECHNOCORE_STATS_URL": "https://chat.example/stats",
            "TECHNOCORE_EXPORTER_HOST": "0.0.0.0",
            "TECHNOCORE_EXPORTER_PORT": "9999",
            "TECHNOCORE_STATS_TIMEOUT": "2.5",
        }
    )
    assert config.url == "https://chat.example/stats"
    assert (config.host, config.port, config.timeout) == ("0.0.0.0", 9999, 2.5)


def test_the_token_never_reaches_the_startup_log():
    """Asserted rather than left to a reviewer noticing a future format string change."""
    config = settings(env={**TOKEN, "TECHNOCORE_STATS_URL": "https://chat.example/stats"})
    line = describe(config)
    assert "s3cret-token" not in line
    assert "chat.example" in line and "9464" in line


def test_the_collector_is_wired_with_the_configured_values():
    registry = CollectorRegistry()
    config = settings(env={**TOKEN, "TECHNOCORE_STATS_URL": "https://chat.example/stats"})
    collector = build(config, registry=registry)
    assert collector._url == "https://chat.example/stats"
    assert collector._token == "s3cret-token"
    assert collector._timeout == DEFAULT_TIMEOUT


# ------------------------------------------------- boot validation, reported by @Minh3132


@pytest.mark.parametrize(
    "raw", ["-1", "-0.5", "nan", "NaN", "inf", "-inf", "Infinity", "0", "abc", ""]
)
def test_an_unusable_timeout_is_refused_at_boot(raw):
    """The failure mode this closes is worse than a wrong number.

    A negative or NaN timeout raises ValueError from socket.settimeout and inf raises
    OverflowError — on every scrape, not at boot. Since collect() deliberately converts any
    unexpected exception into scrape_success 0, an unvalidated typo here produces a process
    that starts, stays up, answers /metrics forever and can never once succeed. Refusing at
    boot is the difference between a visible misconfiguration and an exporter that looks
    alive and is not.
    """
    with pytest.raises(SystemExit, match="TECHNOCORE_STATS_TIMEOUT"):
        settings(env={**TOKEN, "TECHNOCORE_STATS_TIMEOUT": raw})


def test_a_timeout_at_or_above_the_scrape_ceiling_is_refused():
    """The headroom in the README is part of the contract, so it is enforced rather than
    described: at or above Prometheus's own default scrape_timeout the source can no longer
    fail first, and the failure lands as a scrape timeout with no samples."""
    with pytest.raises(SystemExit, match="must be below"):
        settings(env={**TOKEN, "TECHNOCORE_STATS_TIMEOUT": str(SCRAPE_TIMEOUT_CEILING)})
    assert settings(env={**TOKEN, "TECHNOCORE_STATS_TIMEOUT": "9.9"}).timeout == 9.9


@pytest.mark.parametrize("raw", ["0", "-1", "65536", "99999", "abc", "8.5", ""])
def test_an_unusable_port_is_refused_at_boot(raw):
    with pytest.raises(SystemExit, match="TECHNOCORE_EXPORTER_PORT"):
        settings(env={**TOKEN, "TECHNOCORE_EXPORTER_PORT": raw})


@pytest.mark.parametrize(
    "raw",
    ["", "not a url", "ftp://host/stats", "file:///etc/passwd", "/stats", "http:///stats"],
)
def test_an_unusable_stats_url_is_refused_at_boot(raw):
    """Same class as the timeout: `urllib.request.Request` raises for an unknown scheme at
    *request* time, which the broad catch in collect() would render as a permanently
    failing scrape rather than as the configuration error it is."""
    with pytest.raises(SystemExit, match="TECHNOCORE_STATS_URL"):
        settings(env={**TOKEN, "TECHNOCORE_STATS_URL": raw})


def test_the_usable_configuration_is_still_accepted():
    """The other direction, so the validators cannot pass by refusing everything."""
    config = settings(
        env={
            **TOKEN,
            "TECHNOCORE_STATS_URL": "https://chat.example/stats",
            "TECHNOCORE_EXPORTER_PORT": "9999",
            "TECHNOCORE_STATS_TIMEOUT": "2.5",
        }
    )
    assert (config.url, config.port, config.timeout) == ("https://chat.example/stats", 9999, 2.5)


@pytest.mark.parametrize(
    "raw",
    [
        "http://user:s3cret@host/stats",
        "http://user@host/stats",
        "https://admin:hunter2@chat.example/stats",
    ],
)
def test_a_url_embedding_credentials_is_refused(raw):
    """`describe()` writes this URL to the startup log, so URL credentials would put a
    second secret in the one place the stats token is deliberately kept out of. /stats
    authenticates with X-Stats-Token and nothing else, so these can only be a mistake."""
    with pytest.raises(SystemExit, match="must not embed credentials"):
        settings(env={**TOKEN, "TECHNOCORE_STATS_URL": raw})


def test_the_refusal_does_not_echo_the_credential():
    """A refusal that quotes the value would write the secret to stderr instead."""
    try:
        settings(env={**TOKEN, "TECHNOCORE_STATS_URL": "http://user:s3cret@host/stats"})
    except SystemExit as exc:
        assert "s3cret" not in str(exc)
    else:
        raise AssertionError("expected a refusal")
