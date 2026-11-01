"""Validate assets that are not in a working copy.

Two problems, one module:

1. **Remote sources.** A CI runner (or a submit trigger) is given a depot path,
   a changelist or a URL, not a folder. :func:`materialise` turns any of those
   into local files in a temp tree, and :func:`validate_sources` gates them. No
   clone, no sync, no workspace - which is what makes "validate before submit"
   possible on a farm of disposable machines.

2. **The farm itself.** :class:`RunnerHub` is a deliberately small job queue
   (claim / result / status) and :class:`RunnerClient` is the worker side. Both
   are stdlib HTTP so a studio can run them behind its existing proxy, and the
   protocol is three endpoints instead of a framework.

Everything here is read-only with respect to version control, and every fetch is
size-capped so one pathological asset cannot exhaust a runner.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .config import VCSConfig
from .vcs import PerforceClient, VCSError

DEPOT_PREFIXES = ("//",)


class RemoteError(Exception):
    """A remote validation problem, phrased for the person who can fix it."""


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Source:
    """One thing to validate: a URL, a depot path, or a changelist."""

    kind: str                      # url | depot | changelist
    location: str
    revision: str = ""

    @property
    def label(self) -> str:
        return f"{self.location}@{self.revision}" if self.revision else self.location


def parse_source(text: str) -> Source:
    """Classify a source string. Ambiguity is refused rather than guessed."""
    raw = str(text or "").strip()
    if not raw:
        raise RemoteError("empty source")
    lowered = raw.lower()
    if lowered.startswith(("http://", "https://")):
        return Source("url", raw)
    if raw.startswith(DEPOT_PREFIXES):
        location, _, revision = raw.partition("@")
        return Source("depot", location, revision)
    if raw.isdigit() or (raw.startswith("cl:") and raw[3:].isdigit()):
        return Source("changelist", raw.split(":", 1)[-1])
    raise RemoteError(
        f"cannot tell what '{raw}' is: use https://..., //depot/path@rev, or a "
        "changelist number")


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

class Fetcher:
    """HTTP(S) fetcher with a hard size cap and no ambient credentials."""

    def __init__(self, timeout: float = 60.0, max_bytes: int = 512 * 1024 * 1024,
                 token: str = "") -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.token = token

    def fetch(self, url: str, destination: Path,
              opener: Optional[Callable[[urllib.request.Request, float], Any]] = None
              ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": "AssetGatekeeper/1.0 (remote validation)"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        total = 0
        try:
            with destination.open("wb") as handle:
                if opener is not None:
                    response = opener(request, self.timeout)
                else:
                    response = urllib.request.urlopen(request, timeout=self.timeout)  # noqa: S310
                with response:
                    while True:
                        chunk = response.read(1 << 20)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise RemoteError(
                                f"{url} is larger than the {self.max_bytes:,} byte cap")
                        handle.write(chunk)
        except urllib.error.HTTPError as exc:
            destination.unlink(missing_ok=True)
            raise RemoteError(f"HTTP {exc.code} fetching {url}") from exc
        except urllib.error.URLError as exc:
            destination.unlink(missing_ok=True)
            raise RemoteError(f"could not fetch {url}: {exc.reason}") from exc
        except RemoteError:
            destination.unlink(missing_ok=True)
            raise
        if total == 0:
            destination.unlink(missing_ok=True)
            raise RemoteError(f"{url} returned an empty body")
        return destination


def _safe_name(label: str) -> str:
    cleaned = label.replace("://", "_").replace("//", "_").replace(":", "_")
    cleaned = cleaned.replace("\\", "_").replace("/", "_").replace("@", "_v")
    return cleaned[:120] or "asset"


def materialise(sources: Iterable[Source], workdir: Path,
                vcs_config: Optional[VCSConfig] = None,
                perforce: Optional[PerforceClient] = None,
                fetcher: Optional[Fetcher] = None,
                suffixes: Optional[Iterable[str]] = None) -> list[tuple[Source, Path]]:
    """Turn sources into files inside ``workdir``. Nothing else is touched."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    vcs_config = vcs_config or VCSConfig()
    fetcher = fetcher or Fetcher(timeout=vcs_config.fetch_timeout,
                                 max_bytes=vcs_config.max_asset_bytes)
    def client() -> PerforceClient:
        nonlocal perforce
        if perforce is None:
            perforce = PerforceClient(vcs_config)
        return perforce

    materialised: list[tuple[Source, Path]] = []
    for source in sources:
        if source.kind == "url":
            destination = workdir / _safe_name(source.location)
            materialised.append((source, fetcher.fetch(source.location, destination)))
        elif source.kind == "depot":
            destination = workdir / _safe_name(source.location)
            materialised.append((source, client().print_to(source.location, destination,
                                                           source.revision)))
        else:
            written = client().materialise(source.location, workdir / f"cl_{source.location}",
                                           suffixes)
            for path in written:
                materialised.append((Source("depot", str(path.name)), path))
            if not written:
                raise RemoteError(f"changelist {source.location} has no matching asset files")
    if not materialised:
        raise RemoteError("no sources to validate")
    return materialised


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

@dataclass
class RemoteResult:
    source: str
    path: str
    status: str
    problems: list[str] = field(default_factory=list)
    metric: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "path": self.path, "status": self.status,
                "problems": list(self.problems), "metric": self.metric,
                "error": self.error}


@dataclass
class RemoteReport:
    results: list[RemoteResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(result.status in ("PASSED", "HEALED") for result in self.results)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status not in ("PASSED", "HEALED"))

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "failed": self.failed,
                "results": [r.as_dict() for r in self.results]}


def _gatekeeper(budget: Any = None) -> Any:
    try:
        import asset_gatekeeper as agk            # noqa: PLC0415 - top-level module
    except ModuleNotFoundError as exc:            # pragma: no cover
        raise RemoteError("asset_gatekeeper is not importable: install the package "
                          "or run from the repository root") from exc
    return agk.Gatekeeper(budget if budget is not None else agk.Budget())


def validate_sources(sources: Iterable[Source], workdir: Path,
                     vcs_config: Optional[VCSConfig] = None,
                     perforce: Optional[PerforceClient] = None,
                     fetcher: Optional[Fetcher] = None,
                     budget: Any = None,
                     suffixes: Optional[Iterable[str]] = None) -> RemoteReport:
    """Materialise then gate. Healing is never attempted on remote content."""
    gate = _gatekeeper(budget)
    report = RemoteReport()
    try:
        pairs = materialise(sources, workdir, vcs_config, perforce, fetcher, suffixes)
    except (RemoteError, VCSError) as exc:
        report.results.append(RemoteResult(source="<none>", path="", status="ERROR",
                                           error=str(exc)))
        return report
    for source, path in pairs:
        try:
            verdict = gate.check(path)
        except Exception as exc:                  # a runner must report, not crash
            report.results.append(RemoteResult(source=source.label, path=str(path),
                                               status="ERROR", error=str(exc)))
            continue
        report.results.append(RemoteResult(
            source=source.label, path=str(path), status=verdict.status,
            problems=list(verdict.problems), metric=getattr(verdict, "metric", "") or ""))
    return report


# --------------------------------------------------------------------------- #
# Runner farm: hub and client
# --------------------------------------------------------------------------- #

@dataclass
class Job:
    id: str
    sources: list[str]
    project: str = ""
    budget: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "sources": list(self.sources),
                "project": self.project, "budget": dict(self.budget)}


class RunnerHub:
    """A job queue with three endpoints. Small on purpose.

    POST /claim   {"runner": "name"}      -> 200 job JSON, or 204 when idle
    POST /result  {"id": "...", "report": {...}} -> 202
    GET  /status                          -> queue depth and recent jobs

    Auth is a single bearer token: runners are machines, and a machine that needs
    a session cookie is a machine that will store one in a CI variable.
    """

    def __init__(self, token: str = "", host: str = "127.0.0.1", port: int = 0) -> None:
        self.token = token
        self.jobs: queue.Queue = queue.Queue()
        self.pending: dict[str, Job] = {}
        self.results: dict[str, dict] = {}
        self.lock = threading.Lock()
        hub = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AssetGatekeeperRunnerHub/1.0"

            def log_message(self, *args: Any) -> None:      # quiet by default
                return

            def _json(self, status: int, payload: dict | None = None) -> None:
                body = b"" if payload is None else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                if body:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _authorised(self) -> bool:
                if not hub.token:
                    return True
                header = self.headers.get("Authorization", "")
                return header == f"Bearer {hub.token}"

            def do_GET(self) -> None:                      # noqa: N802
                if not self._authorised():
                    self._json(401, {"error": "unauthorised"})
                    return
                if self.path.rstrip("/") == "/status":
                    self._json(200, {"queued": hub.jobs.qsize(),
                                     "claimed": len(hub.pending),
                                     "completed": len(hub.results)})
                    return
                self._json(404, {"error": "not found"})

            def do_POST(self) -> None:                     # noqa: N802
                if not self._authorised():
                    self._json(401, {"error": "unauthorised"})
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(min(length, 8 * 1024 * 1024)) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    self._json(400, {"error": "body is not JSON"})
                    return
                route = self.path.rstrip("/")
                if route == "/claim":
                    job = hub.claim()
                    self._json(200, job.as_dict()) if job else self._json(204)
                    return
                if route == "/result":
                    job_id = str(payload.get("id", ""))
                    if not job_id:
                        self._json(400, {"error": "missing id"})
                        return
                    hub.complete(job_id, payload.get("report", {}))
                    self._json(202, {"stored": job_id})
                    return
                self._json(404, {"error": "not found"})

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://{host}:{self.port}"

    # -- queue ------------------------------------------------------------- #

    def submit(self, sources: Iterable[str], project: str = "",
               budget: Optional[dict] = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], sources=list(sources), project=project,
                  budget=budget or {})
        self.jobs.put(job)
        return job

    def claim(self) -> Optional[Job]:
        try:
            job = self.jobs.get_nowait()
        except queue.Empty:
            return None
        with self.lock:
            self.pending[job.id] = job
        return job

    def complete(self, job_id: str, report: dict) -> None:
        with self.lock:
            self.pending.pop(job_id, None)
            self.results[job_id] = {"report": report, "at": time.time()}

    def start(self) -> "RunnerHub":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class RunnerClient:
    """Worker side: claim a job, validate it, post the report."""

    def __init__(self, hub_url: str, token: str = "", name: str = "runner",
                 timeout: float = 15.0) -> None:
        self.hub_url = hub_url.rstrip("/")
        self.token = token
        self.name = name
        self.timeout = timeout

    def _call(self, path: str, payload: Optional[dict] = None) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.hub_url + path, data=data,
                                         headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                body = response.read(4 * 1024 * 1024)
                return response.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as exc:
            if exc.code == 204:
                return 204, {}
            raise RemoteError(f"hub returned HTTP {exc.code} for {path}") from exc
        except urllib.error.URLError as exc:
            raise RemoteError(f"cannot reach the hub at {self.hub_url}: {exc.reason}") from exc

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def claim(self) -> Optional[Job]:
        status, payload = self._call("/claim", {"runner": self.name})
        if status == 204 or not payload:
            return None
        return Job(id=str(payload["id"]), sources=list(payload.get("sources", [])),
                   project=str(payload.get("project", "")),
                   budget=dict(payload.get("budget", {}) or {}))

    def report(self, job_id: str, report: dict) -> None:
        self._call("/result", {"id": job_id, "report": report})

    def work_once(self, workdir: Path, vcs_config: Optional[VCSConfig] = None) -> Optional[dict]:
        job = self.claim()
        if job is None:
            return None
        report = validate_sources([parse_source(s) for s in job.sources],
                                  workdir / job.id, vcs_config=vcs_config)
        payload = report.as_dict()
        payload["job"] = job.id
        self.report(job.id, payload)
        return payload

    def work(self, workdir: Path, max_jobs: int = 1, idle_seconds: float = 0.5,
             vcs_config: Optional[VCSConfig] = None) -> list[dict]:
        """Bounded loop: a runner never spins forever by accident."""
        done: list[dict] = []
        for _ in range(max(1, max_jobs)):
            result = self.work_once(workdir, vcs_config)
            if result is None:
                if idle_seconds:
                    time.sleep(idle_seconds)
                break
            done.append(result)
        return done
