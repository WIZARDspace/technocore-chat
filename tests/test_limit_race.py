import sys
import threading
from collections import OrderedDict

import limit
from limit import take, _buckets


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, ip):
        self.headers = {}
        self.client = _FakeClient(ip)


def test_take_survives_concurrent_eviction_of_own_key(monkeypatch):
    """Deterministic reproduction of #378: simulates another thread's
    popitem(last=False) evicting this thread's own bucket key in the exact
    window between take() writing the entry and calling move_to_end on it."""
    key = ("10.0.0.1", "msg")

    class _EvictingDict(OrderedDict):
        def move_to_end(self, k, *args, **kwargs):
            if k == key and k in self:
                del self[k]  # the concurrent popitem(last=False) this test simulates
            return super().move_to_end(k, *args, **kwargs)

    fake_buckets = _EvictingDict()
    fake_buckets[key] = (5.0, 0.0)  # pre-seed so take() takes the "existing key" branch
    monkeypatch.setattr(limit, "_buckets", fake_buckets)

    req = _FakeRequest("10.0.0.1")
    take(req, "msg", per_min=1000, burst=1000, max_buckets=50)


def test_take_concurrent_no_keyerror():
    """Statistical sanity check: hammer take() from many threads on overlapping
    keys under heavy eviction pressure. Not guaranteed to catch the race on its
    own -- see test_take_survives_concurrent_eviction_of_own_key for the
    deterministic reproduction -- but should stay green on the fixed code."""
    _buckets.clear()
    errors = []

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        def worker(i):
            req = _FakeRequest(f"10.0.0.{i % 4}")
            try:
                for _ in range(2000):
                    take(req, "msg", per_min=1000, burst=1000, max_buckets=2)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old_interval)

    assert not errors, f"take() raised under concurrency: {errors}"
