"""Structured security and compliance events, forwarded to a SIEM.

The events a studio's security team actually asks about, in one place:

    auth.token.rejected      a bearer token was refused
    auth.login.failed        an SSO exchange failed (state, nonce, signature)
    rbac.denied              an action was refused for a path
    rbac.override            a lead loosened a budget for a department path
    budget.violation         an asset blew a budget badly enough to matter
    admin.action             an account, rule or project changed
    migration.applied        a schema migration ran

Format
    One JSON object per line, stable key order, `schema` versioned so a Splunk
    team can write a parser once. Emitters: a local JSONL file (always safe), UDP
    or TCP syslog (RFC 5424), and an HTTP webhook delivered in batches.

Three rules this module enforces
    * **Secrets never reach the log.** A recursive redactor drops anything whose
      key looks like a credential, and truncates long values. An audit pipeline
      that copies a bearer token into Splunk is an exfiltration tool.
    * **The request path never blocks.** Events go onto a bounded queue; a full
      queue drops and counts, because a slow SIEM must not slow the gate.
    * **One emitter failing does not silence the others.** A webhook outage still
      leaves the local file, which is the copy that matters during an incident.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

SCHEMA = "agk.audit/1.0"

#: Severity names map to syslog severities so the two exports agree.
SEVERITY_NUMBER = {"debug": 7, "info": 6, "notice": 5, "warning": 4,
                   "error": 3, "critical": 2}

EVENT_TYPES = {
    "auth.token.rejected": "warning",
    "auth.login.failed": "warning",
    "auth.session.invalid": "warning",
    "rbac.denied": "notice",
    "rbac.override": "notice",
    "budget.violation": "warning",
    "admin.action": "notice",
    "migration.applied": "notice",
    "store.circuit_open": "error",
}

REDACTED = "[redacted]"
SECRET_HINTS = ("token", "password", "secret", "authorization", "cookie",
                "api_key", "apikey", "client_secret", "signature", "private_key")


class AuditError(Exception):
    """A forwarding problem that the operator has to fix."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def redact(value: Any, depth: int = 0) -> Any:
    """Drop credentials, truncate noise. Safe on any JSON value."""
    if depth > 6:
        return "[too deep]"
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in SECRET_HINTS):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = redact(item, depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact(item, depth + 1) for item in value[:50]]
    if isinstance(value, str):
        text = value.replace("\r", " ").replace("\n", " ")
        return text if len(text) <= 400 else text[:397] + "..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:200]


@dataclass
class AuditEvent:
    event: str
    severity: str = "notice"
    actor: str = ""
    action: str = ""
    path: str = ""
    outcome: str = ""
    project_id: Optional[int] = None
    request_id: str = ""
    remote: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = _utc_now()
        if self.severity not in SEVERITY_NUMBER:
            raise AuditError(f"unknown severity {self.severity!r}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "ts": self.timestamp,
            "event": self.event,
            "severity": self.severity,
            "actor": self.actor,
            "action": self.action,
            "path": self.path,
            "outcome": self.outcome,
            "project_id": self.project_id,
            "request_id": self.request_id,
            "remote": self.remote,
            "detail": redact(self.detail),
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), separators=(",", ":"), sort_keys=True)


# --------------------------------------------------------------------------- #
# Emitters
# --------------------------------------------------------------------------- #

class JsonLineEmitter:
    """Append JSONL to a file. The copy that still exists when the SIEM is down."""

    def __init__(self, path: str | Path, fsync: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync
        self._lock = threading.Lock()
        self.written = 0

    def emit(self, event: AuditEvent) -> None:
        line = event.as_json() + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                if self._fsync:
                    handle.flush()
                    os.fsync(handle.fileno())
            self.written += 1

    def close(self) -> None:
        return


_HOSTNAME = socket.gethostname() or "-"


def _syslog_token(value: str, limit: int) -> str:
    """An RFC 5424 header field: printable US-ASCII, no spaces, bounded, or "-"."""
    cleaned = "".join(ch for ch in str(value) if 33 <= ord(ch) <= 126)[:limit]
    return cleaned or "-"


def _epoch(timestamp: str) -> float:
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


class SyslogEmitter:
    """RFC 5424 over UDP (fire and forget) or TCP (framed with a newline)."""

    def __init__(self, host: str, port: int = 514, facility: int = 13,
                 transport: str = "udp", app_name: str = "asset-gatekeeper",
                 timeout: float = 2.0,
                 socket_factory: Optional[Callable[..., socket.socket]] = None) -> None:
        if transport not in ("udp", "tcp"):
            raise AuditError("syslog transport must be 'udp' or 'tcp'")
        self.host = host
        self.port = int(port)
        self.facility = int(facility)
        self.transport = transport
        self.app_name = app_name
        self.timeout = timeout
        self._factory = socket_factory or socket.socket
        self.sent = 0
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        kind = socket.SOCK_DGRAM if self.transport == "udp" else socket.SOCK_STREAM
        sock = self._factory(socket.AF_INET, kind)
        if self.transport == "tcp":
            sock.settimeout(self.timeout)
            sock.connect((self.host, self.port))
        self._sock = sock
        return sock

    def format(self, event: AuditEvent) -> bytes:
        # RFC 5424: <PRI>1 TIMESTAMP HOSTNAME APP-NAME PROCID MSGID SD MSG.
        # HOSTNAME used to be "-" (nil), so every event in the SIEM looked as if
        # it came from the same nowhere; the machine name is what an incident
        # responder filters on first.
        priority = self.facility * 8 + SEVERITY_NUMBER[event.severity]
        header = (f"<{priority}>1 {event.timestamp} {_syslog_token(_HOSTNAME, 255)} "
                  f"{_syslog_token(self.app_name, 48)} {os.getpid()} "
                  f"{_syslog_token(event.event, 32)} - ")
        return (header + event.as_json()).encode("utf-8", "replace")

    def emit(self, event: AuditEvent) -> None:
        payload = self.format(event)
        with self._lock:
            try:
                sock = self._connect()
                if self.transport == "udp":
                    sock.sendto(payload, (self.host, self.port))
                else:
                    sock.sendall(payload + b"\n")
                self.sent += 1
            except OSError:
                # Reconnect once on the next event; a broken socket must not
                # wedge the emitter forever.
                with _suppress():
                    if self._sock is not None:
                        self._sock.close()
                self._sock = None
                raise

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                with _suppress():
                    self._sock.close()
                self._sock = None


class WebhookEmitter:
    """Batch POST to Splunk HEC or any collector that takes JSON.

    A URL containing ``/services/collector`` is Splunk's HTTP Event Collector,
    which accepts only its own envelope - one ``{"time", "host", "sourcetype",
    "event"}`` object per event, concatenated - and answers anything else with
    400 "Event field is required". That envelope is what goes there; every
    other URL gets ``{"schema": ..., "events": [...]}``. (The 1.0 emitter sent
    the generic body to HEC too, including the HEC URL its own docs used.)

    Batched because one HTTP request per event turns a telemetry burst into a
    connection storm. Bounded queue: when the collector is down, events are
    dropped and counted rather than growing a list until the process dies.
    """

    def __init__(self, url: str, token: str = "", batch_size: int = 25,
                 flush_interval: float = 2.0, timeout: float = 5.0,
                 queue_size: int = 5000, max_batches: Optional[int] = None,
                 poster: Optional[Callable[[str, bytes, dict, float], int]] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not url.lower().startswith(("http://", "https://")):
            raise AuditError("webhook url must be http(s)")
        self.url = url
        self.token = token
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = max(0.05, float(flush_interval))
        self.timeout = timeout
        self._poster = poster or _default_poster
        self._sleep = sleep
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, queue_size))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.sent = 0
        self.dropped = 0
        self.failed_batches = 0
        self._max_batches = max_batches

    # -- producer side (never blocks) -------------------------------------- #

    def emit(self, event: AuditEvent) -> None:
        try:
            self._queue.put_nowait(event.as_dict())
        except queue.Full:
            with self._lock:
                self.dropped += 1

    # -- consumer side ----------------------------------------------------- #

    def start(self) -> "WebhookEmitter":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()

        def loop() -> None:
            batch: list[dict] = []
            idle = 0.0
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    item = self._queue.get(timeout=self.flush_interval)
                    batch.append(item)
                    idle = 0.0
                except queue.Empty:
                    idle += self.flush_interval
                if batch and (len(batch) >= self.batch_size or idle >= self.flush_interval):
                    self._send(batch)
                    batch = []
                if self._max_batches is not None and self.sent >= self._max_batches:
                    break
            if batch:
                self._send(batch)

        self._thread = threading.Thread(target=loop, name="agk-audit-webhook",
                                        daemon=True)
        self._thread.start()
        return self

    @property
    def splunk_hec(self) -> bool:
        path = urllib.parse.urlsplit(self.url).path.lower()
        return "/services/collector" in path and not path.rstrip("/").endswith("/raw")

    def body(self, batch: list[dict]) -> bytes:
        if self.splunk_hec:
            return "\n".join(
                json.dumps({"time": round(_epoch(str(item.get("ts", ""))), 3),
                            "host": _HOSTNAME, "source": "asset-gatekeeper",
                            "sourcetype": "_json", "event": item},
                           separators=(",", ":"), sort_keys=True)
                for item in batch).encode("utf-8")
        return json.dumps({"schema": SCHEMA, "events": batch}).encode("utf-8")

    def _send(self, batch: list[dict]) -> None:
        body = self.body(batch)
        headers = {"Content-Type": "application/json",
                   "User-Agent": "AssetGatekeeper-audit/1.0"}
        if self.token:
            headers["Authorization"] = f"Splunk {self.token}"
        for attempt in range(1, 4):
            try:
                status = self._poster(self.url, body, headers, self.timeout)
                if 200 <= status < 300:
                    with self._lock:
                        self.sent += len(batch)
                    return
                if status in (401, 403):
                    break                      # a bad token will not fix itself
            except Exception:
                pass
            self._sleep(min(2.0, 0.2 * attempt))
        with self._lock:
            self.dropped += len(batch)
            self.failed_batches += 1

    def flush(self, timeout: float = 3.0) -> None:
        deadline = time.time() + timeout
        while not self._queue.empty() and time.time() < deadline:
            self._sleep(0.01)
        self._sleep(min(self.flush_interval, 0.05))

    def close(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def as_dict(self) -> dict[str, Any]:
        return {"url": self.url, "sent": self.sent, "dropped": self.dropped,
                "failed_batches": self.failed_batches, "queued": self._queue.qsize()}


def _default_poster(url: str, body: bytes, headers: dict[str, str],
                    timeout: float) -> int:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except urllib.error.URLError as exc:
        raise AuditError(f"webhook unreachable: {exc.reason}") from exc


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info: Any) -> bool:
        return True


# --------------------------------------------------------------------------- #
# The log
# --------------------------------------------------------------------------- #

class AuditLog:
    """Fan-out to every emitter, from a worker thread.

    ``emit`` is safe to call on the request path: it never performs I/O, and a
    full queue increments a counter instead of blocking or growing memory.
    """

    def __init__(self, emitters: Iterable[Any] = (), queue_size: int = 10_000,
                 min_severity: str = "info",
                 on_error: Optional[Callable[[BaseException], None]] = None) -> None:
        self.emitters = list(emitters)
        self.min_severity = min_severity
        self._floor = SEVERITY_NUMBER.get(min_severity, 6)
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, queue_size))
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.emitted = 0
        self.dropped = 0
        self.write_failures = 0
        self._on_error = on_error
        for emitter in self.emitters:
            start = getattr(emitter, "start", None)
            if callable(start):
                start()

    # -- api --------------------------------------------------------------- #

    def emit(self, event: AuditEvent | str, **fields: Any) -> None:
        if isinstance(event, str):
            if event not in EVENT_TYPES and "event" not in fields:
                raise AuditError(f"unknown audit event {event!r}")
            severity = fields.pop("severity", None) or EVENT_TYPES.get(event, "notice")
            event = AuditEvent(event=event, severity=severity, **fields)
        if SEVERITY_NUMBER[event.severity] > self._floor:
            return                                  # quieter than the floor
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._lock:
                self.dropped += 1

    def start(self) -> "AuditLog":
        if self._worker is not None and self._worker.is_alive():
            return self
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    event = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                self._deliver(event)

        self._worker = threading.Thread(target=loop, name="agk-audit", daemon=True)
        self._worker.start()
        return self

    def _deliver(self, event: AuditEvent) -> None:
        delivered = 0
        for emitter in self.emitters:
            try:
                emitter.emit(event)
                delivered += 1
            except Exception as exc:            # one bad sink must not silence others
                with self._lock:
                    self.write_failures += 1
                if self._on_error is not None:
                    self._on_error(exc)
        with self._lock:
            self.emitted += 1 if delivered else 0

    def flush(self, timeout: float = 3.0) -> None:
        deadline = time.time() + timeout
        while not self._queue.empty() and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.02)

    def close(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            self._worker = None
        for emitter in self.emitters:
            closer = getattr(emitter, "close", None)
            if callable(closer):
                with _suppress():
                    closer()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"emitted": self.emitted, "dropped": self.dropped,
                    "write_failures": self.write_failures,
                    "queued": self._queue.qsize(),
                    "emitters": [type(e).__name__ for e in self.emitters]}


class NullLog:
    """A do-nothing log, so callers never need a None check."""

    def emit(self, *args: Any, **kwargs: Any) -> None:
        return

    def flush(self, timeout: float = 0.0) -> None:
        return

    def close(self, timeout: float = 0.0) -> None:
        return

    def stats(self) -> dict[str, Any]:
        return {"emitted": 0, "dropped": 0, "emitters": []}


def build_audit_log(config: Any) -> AuditLog | NullLog:
    """Create the log described by ``[enterprise.audit]``.

    Disabled by default. When enabled without any destination, a local JSONL file
    is used: an audit trail nobody can read is not an audit trail.
    """
    if config is None or not getattr(config, "enabled", False):
        return NullLog()
    emitters: list[Any] = []
    if config.file:
        emitters.append(JsonLineEmitter(config.file))
    if config.syslog_host:
        emitters.append(SyslogEmitter(config.syslog_host, config.syslog_port,
                                      config.syslog_facility, config.syslog_transport))
    if config.webhook_url:
        emitters.append(WebhookEmitter(config.webhook_url, config.webhook_token,
                                       batch_size=config.webhook_batch,
                                       flush_interval=config.webhook_interval))
    if not emitters:
        emitters.append(JsonLineEmitter("agk-audit.jsonl"))
    return AuditLog(emitters, min_severity=config.min_severity).start()
