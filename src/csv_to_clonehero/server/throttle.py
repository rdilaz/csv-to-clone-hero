"""Global egress throttle shared by every user and background job.

Enchor rate-limits hard (HTTP 429) above ~1-2 req/s per IP. A public app
shares ONE server IP, so concurrent users must share a single global request
budget rather than each opening their own hot adapter (which is what the
single-user app did). Being slow is fine; correctness and completion messaging
matter more than speed.

Design:
  * ``GlobalGate`` serializes Enchor-bound background jobs: at most one
    discovery/export job advances network queries at a time. Each job still
    uses its own adapter instance (whose internal AIMD throttle and
    ``network.request_delay_seconds`` stay in charge of pacing).
  * A process-global instance is exposed via :func:`global_gate` so every
    worker thread shares the same budget.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import threading
import time


class GlobalGate:
    """A lock shared by all background job workers.

    ``acquire()`` blocks until no other Enchor-bound job is advancing network
    work. A ``fair`` flag keeps users roughly in insertion order so one slow
    job cannot starve everyone: we re-check a FIFO condition variable.
    """

    def __init__(self, max_concurrent: int = 1, *, fair: bool = True) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self.max_concurrent = max_concurrent
        self.fair = bool(fair)
        self._lock = threading.Lock()
        self._available = max_concurrent
        self._waiters: deque[int] = deque()
        self._next_ticket = 0
        self._cv = threading.Condition(self._lock)

    @contextmanager
    def acquire(self, *, timeout: float | None = None, tag: str = ""):
        entered = self._acquire(timeout=timeout, tag=tag)
        try:
            yield entered
        finally:
            if entered:
                self._release(tag=tag)

    def _acquire(self, timeout: float | None, tag: str) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            ticket = self._next_ticket
            self._next_ticket += 1
            self._waiters.append(ticket)
            try:
                while self._available <= 0 or (
                    self.fair and self._waiters[0] != ticket
                ):
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._waiters.remove(ticket)
                            self._cv.notify_all()
                            return False
                        self._cv.wait(remaining)
                    else:
                        self._cv.wait()
                self._waiters.remove(ticket)
                self._available -= 1
                return True
            except BaseException:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
                self._cv.notify_all()
                raise

    def _release(self, tag: str) -> None:
        with self._cv:
            self._available += 1
            self._cv.notify_all()

    @property
    def in_use(self) -> int:
        with self._lock:
            return self.max_concurrent - self._available


_gate: GlobalGate | None = None
_gate_lock = threading.Lock()


def configure_global_gate(max_concurrent: int = 1) -> GlobalGate:
    """Create/replace the process-global gate (idempotent on first call)."""
    global _gate
    with _gate_lock:
        if _gate is None:
            _gate = GlobalGate(max_concurrent=max_concurrent)
    return _gate


def global_gate() -> GlobalGate:
    if _gate is None:
        configure_global_gate(1)
    assert _gate is not None
    return _gate


def reset_global_gate_for_tests() -> None:
    global _gate
    with _gate_lock:
        _gate = None
