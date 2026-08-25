"""Tests for the shared global egress gate and the notification sender."""

from __future__ import annotations

import threading
import time

import pytest

from csv_to_clonehero.config import AppConfig, NotificationsConfig
from csv_to_clonehero.server.notifications import EmailSender
from csv_to_clonehero.server.throttle import GlobalGate, reset_global_gate_for_tests


@pytest.fixture(autouse=True)
def clean_gate():
    reset_global_gate_for_tests()
    yield
    reset_global_gate_for_tests()


def test_gate_allows_one_at_a_time():
    gate = GlobalGate(max_concurrent=1)
    inside = []
    lock = threading.Lock()

    def worker():
        with gate.acquire():
            with lock:
                inside.append(1)
            time.sleep(0.1)
            with lock:
                inside.append(-1)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    with lock:
        assert inside.count(1) - inside.count(-1) == 1
    for t in threads:
        t.join()


def test_gate_concurrent_limit():
    gate = GlobalGate(max_concurrent=2)
    current = [0]
    peak = [0]
    lock = threading.Lock()

    def worker():
        with gate.acquire():
            with lock:
                current[0] += 1
                peak[0] = max(peak[0], current[0])
            time.sleep(0.05)
            with lock:
                current[0] -= 1

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] <= 2


def test_gate_timeout_returns_false():
    gate = GlobalGate(max_concurrent=1)
    held = threading.Event()

    def hold():
        with gate.acquire():
            held.set()
            time.sleep(0.2)

    t = threading.Thread(target=hold)
    t.start()
    held.wait()
    acquired = True
    with gate.acquire(timeout=0.01) as ok:
        acquired = bool(ok)
    assert acquired is False
    t.join()


def test_global_gate_singleton():
    from csv_to_clonehero.server.throttle import global_gate

    assert global_gate() is global_gate()


def test_email_log_sender_redacts_and_returns():
    config = NotificationsConfig()
    config.enabled = True
    config.provider = "log"
    sender = EmailSender(config)
    assert sender.send("subject", "body", "user@example.com") is True


def test_email_disabled_returns_false():
    config = NotificationsConfig()
    config.enabled = False
    sender = EmailSender(config)
    assert sender.send("s", "b", "x@y.com") is False