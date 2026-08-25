"""Run: uv run --group dev python -m pytest tests

Sync handlers (room_say, note_write, rooms, ...) run in Starlette's threadpool, so two
requests from one IP can call limit.take() at the same instant. take()'s read/compute/
write on the module-level _buckets dict has no lock: this pins the one-token case as
observable behavior (how many of two concurrent callers are granted), not as a detail of
_buckets' own implementation.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from types import SimpleNamespace

import limit


def _fake_request(ip: str) -> SimpleNamespace:
    """The one thing take() reads off a request when no ip_header is configured."""
    return SimpleNamespace(headers={}, client=SimpleNamespace(host=ip))


class _RendezvousBucket(OrderedDict):
    """get() reads immediately, then holds the value it read until a second caller has
    also read -- so neither call can return (and take() cannot write back) until both
    have observed the same pre-write state. OrderedDict, like the real _buckets, because
    take() also calls move_to_end() and popitem(last=False) on it.

    The hold is a barrier with a timeout, not an unconditional wait: once take()'s
    read-modify-write is under a lock, the two calls serialize and the second cannot even
    call get() until the first has finished and released the lock. The first then times out
    waiting for a second reader that will never arrive at the barrier, returns what it
    already (correctly) read, and finishes; the second, once the lock admits it, reads the
    first's already-written value and its own barrier wait times out immediately (the
    barrier is already broken) instead of pairing it with a stale read. Two callers timing
    out separately and each returning their own honestly-read value is the fixed behavior
    working, not a failure of this helper.
    """

    def __init__(self, barrier: threading.Barrier):
        super().__init__()
        self._barrier = barrier

    def get(self, key, default=None):
        value = super().get(key, default)  # the read happens first, always
        try:
            self._barrier.wait(timeout=1.0)  # then hold it until a second reader catches up
        except threading.BrokenBarrierError:
            pass  # no second reader arrived -- return what was actually read, honestly
        return value


def test_two_concurrent_takes_on_a_one_token_bucket_grant_exactly_one(monkeypatch) -> None:
    """A bucket holding one token, read by two threads that are forced to see the same
    pre-decrement state before either writes back.

    Unlocked, both threads read the untouched (1.0, now) starting tuple, both compute
    tokens >= 1.0, and both are granted: two wait==0 results out of a bucket of one, the
    lost update. Locked, the second thread's get() cannot reach the barrier until the first
    has released the lock and returned, so it observes the first's already-spent bucket
    instead and is refused.
    """
    barrier = threading.Barrier(2)
    monkeypatch.setattr(limit, "_buckets", _RendezvousBucket(barrier))

    ip = "203.0.113.9"
    # Appended, not indexed: list.append is atomic under the GIL, and which caller finishes
    # first does not matter -- only that, across both, exactly one grant and one refusal
    # come back.
    results: list[tuple[int, float]] = []

    def call() -> None:
        results.append(limit.take(_fake_request(ip), "probe", per_min=1, burst=1))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not any(t.is_alive() for t in threads), "a take() call never returned"
    assert len(results) == 2, f"expected 2 results, got {results}"

    granted = [wait for _, wait in results if wait == 0.0]
    refused = [wait for _, wait in results if wait > 0.0]
    assert len(granted) == 1 and len(refused) == 1, (
        f"a one-token bucket did not grant exactly one of two concurrent callers: results={results}"
    )
