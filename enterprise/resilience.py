"""Resilience: circuit breakers, bounded retries, and a health ping loop.

The situation this exists for
    A dashboard that is up while the database is briefly unreachable should not
    turn every request into a 30-second timeout. It should fail fast, keep
    serving what it can, and recover on its own when the cluster comes back.

What is deliberately simple
    * One thread-safe breaker per endpoint, three states, no magic numbers the
      operator cannot see: threshold, reset timeout, and whether the last probe
      succeeded are all exposed through ``stats()``.
    * Retries are bounded and jittered. A retry storm from two hundred
      workstations is how an outage becomes an incident.
    * The health loop pings with a short statement timeout and records the result
      in the same breaker the query path consults, so there is one truth.

What is deliberately absent
    * No background thread that retries writes. A lost write is a decision, not
      something to guess at - the callers of this module are read paths and
      health checks.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Sequence

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half-open"


class CircuitOpenError(Exception):
    """The breaker refused the call: the endpoint is known to be failing."""


class State(str, Enum):
    CLOSED = CLOSED
    OPEN = OPEN
    HALF_OPEN = HALF_OPEN


@dataclass
class BreakerStats:
    name: str
    state: str
    failures: int
    successes: int
    opened_at: Optional[float]
    last_error: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "failures": self.failures,
                "successes": self.successes, "opened_at": self.opened_at,
                "last_error": self.last_error}


class CircuitBreaker:
    """Trip after N consecutive failures; probe again after a cooldown.

    Half-open admits exactly one probe at a time: letting a burst through the
    moment a database comes back is the classic thundering herd.
    """

    def __init__(self, name: str, failure_threshold: int = 5,
                 reset_timeout: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        self.name = name
        self.failure_threshold = int(failure_threshold)
        self.reset_timeout = float(reset_timeout)
        self._clock = clock
        self._state = State.CLOSED
        self._failures = 0
        self._successes = 0
        self._opened_at: Optional[float] = None
        self._probe_in_flight = False
        self._last_error = ""
        self._lock = threading.Lock()

    # -- state -------------------------------------------------------------- #

    @property
    def state(self) -> State:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> State:
        if self._state is State.OPEN and self._opened_at is not None:
            if self._clock() - self._opened_at >= self.reset_timeout:
                # The cooldown expired: allow one probe. Do not close yet - only
                # a successful call proves the endpoint is back.
                self._state = State.HALF_OPEN
                self._probe_in_flight = False
        return self._state

    # -- gating ------------------------------------------------------------- #

    def allow(self) -> bool:
        """May a call go through right now?"""
        with self._lock:
            state = self._state_locked()
            if state is State.CLOSED:
                return True
            if state is State.OPEN:
                return False
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._successes += 1
            self._failures = 0
            self._probe_in_flight = False
            self._state = State.CLOSED
            self._opened_at = None
            self._last_error = ""

    def record_failure(self, error: object = "") -> None:
        with self._lock:
            self._failures += 1
            self._probe_in_flight = False
            self._last_error = str(error)[:200]
            if self._state is State.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = State.OPEN
                self._opened_at = self._clock()

    def reset(self) -> None:
        """Force closed. Only tests and an operator's 'try it now' should use this."""
        with self._lock:
            self._state = State.CLOSED
            self._failures = 0
            self._probe_in_flight = False
            self._opened_at = None
            self._last_error = ""

    # -- introspection ------------------------------------------------------ #

    def stats(self) -> BreakerStats:
        with self._lock:
            return BreakerStats(name=self.name, state=self._state_locked().value,
                                failures=self._failures, successes=self._successes,
                                opened_at=self._opened_at, last_error=self._last_error)

    def as_dict(self) -> dict[str, Any]:
        return self.stats().as_dict()


def call_with_breaker(breaker: CircuitBreaker,
                      call: Callable[[], Any]) -> Any:
    """Run ``call`` if the breaker allows it, recording the outcome.

    The exception type is preserved: a caller that catches ``OperationalError``
    still gets one. Only the refusal is a new type, because "we did not even try"
    is a different fact from "the database said no".
    """
    if not breaker.allow():
        raise CircuitOpenError(f"{breaker.name}: circuit open")
    try:
        result = call()
    except Exception as exc:
        breaker.record_failure(exc)
        raise
    breaker.record_success()
    return result


def retry(call: Callable[[], Any], attempts: int = 3, base_delay: float = 0.05,
          max_delay: float = 2.0, jitter: float = 0.25,
          sleep: Callable[[float], None] = time.sleep,
          rng: Optional[random.Random] = None,
          retry_on: Sequence[type] = (Exception,),
          on_retry: Optional[Callable[[int, BaseException, float], None]] = None) -> Any:
    """Bounded exponential backoff with full jitter.

    ``jitter`` is a fraction of the delay, applied downward, so two hundred
    runners recovering from the same blip do not line up again.
    """
    source = rng or random.Random()
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return call()
        except retry_on as exc:                 # type: ignore[misc]
            last_exc = exc
            if attempt >= attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            if jitter:
                delay *= 1.0 - source.random() * jitter
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)
    assert last_exc is not None
    raise last_exc


@dataclass
class HealthSample:
    ok: bool
    at: float
    duration_ms: float
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "at": self.at, "duration_ms": round(self.duration_ms, 3),
                "error": self.error}


class HealthMonitor:
    """A ping loop that feeds one breaker and keeps the last N samples.

    The thread is daemonised and stoppable, because a library that leaves a
    thread behind makes every test suite hang at exit.
    """

    def __init__(self, ping: Callable[[], Any], name: str = "db",
                 interval: float = 30.0, breaker: Optional[CircuitBreaker] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 history: int = 20) -> None:
        self.ping = ping
        self.name = name
        self.interval = float(interval)
        self.breaker = breaker
        self._clock = clock
        self._sleep = sleep
        self._history: list[HealthSample] = []
        self._history_size = max(1, history)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.checks = 0
        self.failures = 0

    def check_now(self) -> HealthSample:
        started = self._clock()
        try:
            self.ping()
            sample = HealthSample(True, self._clock(), (self._clock() - started) * 1000)
            if self.breaker is not None:
                self.breaker.record_success()
        except Exception as exc:
            sample = HealthSample(False, self._clock(), (self._clock() - started) * 1000,
                                  str(exc)[:200])
            if self.breaker is not None:
                self.breaker.record_failure(exc)
        with self._lock:
            self.checks += 1
            if not sample.ok:
                self.failures += 1
            self._history.append(sample)
            del self._history[:-self._history_size]
        return sample

    def start(self) -> "HealthMonitor":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                self.check_now()
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=loop, name=f"agk-health-{self.name}",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def history(self) -> list[HealthSample]:
        with self._lock:
            return list(self._history)

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            last = self._history[-1].as_dict() if self._history else None
            return {"name": self.name, "interval": self.interval, "checks": self.checks,
                    "failures": self.failures, "last": last}


@dataclass
class Endpoint:
    """One database this process can talk to, with its own breaker."""

    name: str
    store: Any
    breaker: CircuitBreaker = field(default_factory=lambda: CircuitBreaker("endpoint"))
    monitor: Optional[HealthMonitor] = None

    @property
    def healthy(self) -> bool:
        return self.breaker.state is not State.OPEN

    def as_dict(self) -> dict[str, Any]:
        return {"endpoint": self.name, "breaker": self.breaker.as_dict(),
                "health": self.monitor.as_dict() if self.monitor else None}
