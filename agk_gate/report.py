"""AssetGatekeeper, part 6 of 7: the Reporter, telemetry, notifications and the skipped-files summary.

Split out of asset_gatekeeper.py on 11 Nov 2026 with no change in behaviour;
asset_gatekeeper.py still exposes every name. See agk_gate/__init__.py.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import queue
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Optional, Sequence

from agk_gate.common import (
    DEFAULT_IDENTITY, HTTP_TIMEOUT, IDENTITY_MODES, IGNORED_DIR_PREFIXES, IGNORED_PREFIXES,
    IGNORED_SUFFIXES, is_candidate, LOG, PAYLOAD_SCHEMA, TELEMETRY_QUEUE_MAX, TRACKED_SUFFIXES,
    USER_AGENT,
)
from agk_gate.config import (
    Verdict,
)
from agk_gate.audit import (
    _utc_now,
)


# --- #
# Reporting
# --- #

def _safe_username() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no passwd entry / locked-down container
        return ""


def build_telemetry_payload(verdict: Verdict, root: Optional[Path] = None,
                            identity: str = DEFAULT_IDENTITY) -> dict:
    """One flat JSON object per asset: the studio's record of what was gated.

    Deliberately flat - a data warehouse can ingest it without a nested schema,
    and the plugin contract (``--json``) stays a superset of these keys.

    ``identity`` decides how much of the artist's machine travels with it; see
    ``ServerSettings.identity``. The keys are always present, because the ingest
    contract is a fixed schema - what changes is whether they carry a value.
    """
    detail = identity if identity in IDENTITY_MODES else DEFAULT_IDENTITY
    path = verdict.path
    relative = path.name
    if root is not None:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:  # single-file mode: outside the watch folder
            pass
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = -1
    absolute = (str((root / path).resolve())
                if root is not None and not Path(path).is_absolute() else str(path))
    full = detail == "full"
    # "host" keeps the machine name: a dashboard groups by workstation, and a
    # fleet of "unknown" buckets is worse than useless. What it drops is the OS
    # account, which is the identifying half.
    include_host = detail in ("full", "host")
    return {
        "schema": PAYLOAD_SCHEMA,
        "timestamp": _utc_now(),
        "hostname": platform.node() if include_host else "",
        "username": _safe_username() if full else "",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "name": path.name,
        # Absolute by default: the dashboard hands this straight to the
        # workstation when a reviewer clicks "Send to Workstation", so a bare
        # relative name is not enough. The relative form is kept for display and
        # grouping, and identity="none" sends only that.
        "path": absolute if detail in ("full", "host") else relative,
        "relative_path": relative,
        "size_bytes": size_bytes,
        "passed": verdict.passed,
        # PASSED / HEALED / FAILED / ERROR: a healed asset passed, but the studio
        # still wants to know it was rewritten, and what was done to it.
        "status": verdict.status,
        "metric": verdict.metric,
        "stats": dict(verdict.stats),
        "problems": list(verdict.problems),
        "modifications": list(verdict.heal_notes),
        "warnings": list(verdict.warnings),
    }


class Telemetry:
    """Fire-and-forget POST of every verdict to the studio endpoint.

    Three rules, in priority order:

    1. **Never raise.** A dead endpoint is a debug line, not a crash - the artist
       still gets their PASSED/FAILED verdict either way.
    2. **Never block the gate.** ``submit()`` only enqueues; one daemon thread
       owns the sockets. A 2 s timeout can therefore delay the *network*, never
       the inspection.
    3. **Never grow without bound.** The queue is capped; past that, payloads are
       dropped and counted rather than slowing the local run.
    """

    def __init__(self, url: str, root: Optional[Path] = None, token: str = "",
                 timeout: float = HTTP_TIMEOUT, queue_max: int = TELEMETRY_QUEUE_MAX,
                 identity: str = DEFAULT_IDENTITY) -> None:
        self.url = url
        self.root = root
        self.token = token
        self.identity = identity if identity in IDENTITY_MODES else DEFAULT_IDENTITY
        self.timeout = float(timeout)
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self._warned = False
        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=queue_max)
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        if not url.lower().startswith("https://"):
            # Asset names and paths are shipping in clear text; say it once.
            LOG.warning("telemetry endpoint is not https - payloads are sent in clear text")

    # - producer side - #

    def submit(self, verdict: Verdict, root: Optional[Path] = None) -> None:
        """Enqueue one verdict. Never blocks, never raises."""
        if self._closed:
            return
        try:
            payload = build_telemetry_payload(verdict, self.root if root is None else root,
                                              identity=self.identity)
        except Exception as exc:  # a malformed Verdict must not stop the line
            self.dropped += 1
            LOG.debug("telemetry: could not build payload: %s", exc)
            return
        self._ensure_worker()
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self.dropped += 1
            LOG.debug("telemetry: queue full, dropped %s", verdict.path.name)

    def _ensure_worker(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="agk-telemetry", daemon=True)
            self._thread.start()

    # - worker side - #

    def _run(self) -> None:
        while True:
            payload = self._queue.get()
            if payload is None:  # shutdown sentinel
                return
            self._post(payload)

    def _post(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                response.read()  # drain, so the socket is released promptly
            self.sent += 1
        except urllib.error.HTTPError as exc:
            self.failed += 1
            LOG.debug("telemetry: POST rejected (%s %s)", exc.code, exc.reason)
            hint = {401: "check the token (ASSET_GATEKEEPER_TOKEN)",
                    403: "if the dashboard is reached by name, start it with "
                         "--allow-host <that name>; otherwise check the token"}.get(
                exc.code, "the dashboard refused the payload")
            self._warn_once(f"refused by {self.url} ({exc.code} {exc.reason}) - {hint}")
        except Exception as exc:
            # Quiet by contract for repeats: the artist's terminal is not a network
            # monitor. But one line per run is owed - a dashboard that silently
            # stops filling is how a studio loses a month of verdicts.
            self.failed += 1
            LOG.debug("telemetry: POST failed (%s): %s", type(exc).__name__, exc)
            self._warn_once(f"cannot reach {self.url} ({type(exc).__name__})")

    def _warn_once(self, problem: str) -> None:
        if self._warned:
            return
        self._warned = True
        LOG.warning("telemetry %s; verdicts are not being recorded on the dashboard "
                    "(repeats are only shown with -v)", problem)

    # - lifecycle - #

    def close(self, timeout: Optional[float] = None) -> None:
        """Flush what is already queued, bounded by one request timeout."""
        self._closed = True
        if self._thread is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(self.timeout if timeout is None else timeout)
        LOG.debug("telemetry: %d sent, %d failed, %d dropped", self.sent, self.failed, self.dropped)
        if self.failed or self.dropped:
            LOG.warning("telemetry: %d verdict(s) not delivered to %s (%d sent)",
                        self.failed + self.dropped, self.url, self.sent)


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def printable(text: str) -> str:
    """Text safe for a terminal: control characters shown as \\xNN. A node or
    file name in an asset is the author's to choose, and an ESC in one must not
    recolour, move or clear the reader's terminal."""
    return _CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", str(text))


class Reporter:
    """Formats verdicts as one terminal line each, with optional ANSI colour."""

    GREEN = "\033[32m"
    RED = "\033[31m"
    DIM = "\033[2m"
    RESET = "\033[0m"

    def __init__(self, color: bool = True, root: Optional[Path] = None,
                 json_mode: bool = False, telemetry: Optional[Telemetry] = None,
                 notifier: Optional[object] = None) -> None:
        self.color = color and self._ansi_supported()
        self.root = root
        self.json_mode = json_mode
        self.telemetry = telemetry
        self.notifier = notifier
        self.passed = 0
        self.failed = 0
        # Kept only for --report (JUnit / SARIF): a watch that runs for weeks
        # must not hold every verdict it ever printed.
        self.keep_verdicts = False
        self.verdicts: list[Verdict] = []
        self._lock = threading.Lock()

    @staticmethod
    def _ansi_supported() -> bool:
        if not sys.stdout.isatty():
            return False
        if os.name == "nt":  # enable VT processing on modern Windows terminals
            try:
                import ctypes

                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            except Exception:
                return False
        return True

    def _paint(self, text: str, code: str) -> str:
        return f"{code}{text}{self.RESET}" if self.color else text

    def display_path(self, path: Path) -> str:
        if self.root is not None:
            try:
                return str(path.relative_to(self.root))
            except ValueError:
                pass
        return str(path)

    def report(self, verdict: Verdict) -> None:
        if self.keep_verdicts:
            with self._lock:
                self.verdicts.append(verdict)
        # Telemetry mirrors the local verdict; it never influences it.
        if self.telemetry is not None:
            self.telemetry.submit(verdict)
        # Same contract for desktop toasts: queued, rate-limited, non-blocking.
        if self.notifier is not None:
            self.notifier.notify_verdict(verdict)
        if self.json_mode:
            # Machine-readable contract for the DCC plugins: one object per file.
            with self._lock:
                self.passed += int(verdict.passed)
                self.failed += int(not verdict.passed)
                print(json.dumps({
                    "file": str(verdict.path),
                    "name": verdict.path.name,
                    "passed": verdict.passed,
                    "status": verdict.status,
                    "metric": verdict.metric,
                    "problems": list(verdict.problems),
                    "modifications": list(verdict.heal_notes),
                    "warnings": list(verdict.warnings),
                    # Numbers behind the metric: triangles, memory_kb, texel density...
                    "stats": {name: value for name, value in verdict.stats},
                }), flush=True)
            return

        with self._lock:
            if verdict.passed:
                self.passed += 1
                # A healed asset passed, but the artist must be told it was rewritten.
                tag = self._paint(printable(verdict.tag) if verdict.healed else "PASSED", self.GREEN)
            else:
                self.failed += 1
                tag = self._paint(printable(verdict.tag), self.RED)

            detail = f" {self._paint(printable(verdict.metric), self.DIM)}" if verdict.metric else ""
            LOG.info("%s  %s%s", tag, printable(self.display_path(verdict.path)), detail)

            # Advisories sit under the verdict, dimmed: they inform, they never
            # change the exit code, and they must not look like failures.
            for warning in verdict.warnings:
                LOG.info("       %s", self._paint(f"note: {printable(warning)}", self.DIM))

    def summary(self, skipped: Optional[dict] = None,
                totals: Sequence["Verdict"] = ()) -> None:
        total = self.passed + self.failed
        if self.json_mode:
            document = {"total": total, "passed": self.passed, "failed": self.failed}
            if skipped is not None:
                document["skipped"] = skipped
            # [[totals]] over budget fail the run (exit 1) without being an asset:
            # they ride on the summary, so --json readers see why it failed
            # (11 Nov 2026: "failed": 0 with exit 1 and nothing else said).
            document["totals_failed"] = [
                {"total": str(verdict.path).replace("\\", "/").removeprefix("[total] "),
                 "problems": list(verdict.problems), "metric": verdict.metric,
                 "stats": {name: value for name, value in verdict.stats}}
                for verdict in totals]
            print(json.dumps({"summary": document}), flush=True)
            return
        LOG.info("%d asset(s): %d passed, %d failed", total, self.passed, self.failed)
        if skipped and skipped.get("total"):
            LOG.info("%s", describe_skipped(skipped))

    def close(self, timeout: Optional[float] = None) -> None:
        """Flush pending telemetry and toasts. Bounded, and a no-op when off."""
        if self.telemetry is not None:
            self.telemetry.close(timeout)
        if self.notifier is not None:
            self.notifier.close(timeout)


def skipped_files(files: Iterable[Path], exclude: Sequence[str] = ()) -> dict:
    """What a run walked past and why: {"total", "unsupported": {".jfif": 6},
    "excluded": n}. Scratch files, hidden files and the tool's own (rules.toml,
    .agk folders) are not counted - only what a person put there and might
    expect to be checked. Reference images saved as .jfif used to be ignored
    without a word, so a folder looked checked when half of it was not."""
    unsupported: dict[str, int] = {}
    excluded = 0
    for path in files:
        name = path.name
        if (name.startswith(IGNORED_PREFIXES) or name.startswith(".")
                or name.lower() in QUIET_NAMES or name.lower().endswith(IGNORED_SUFFIXES)
                or any(part.startswith(IGNORED_DIR_PREFIXES) or part == ".agk"
                       for part in path.parts[:-1])):
            continue
        suffix = path.suffix.lower()
        if suffix not in TRACKED_SUFFIXES:
            key = suffix or "(no extension)"
            unsupported[key] = unsupported.get(key, 0) + 1
        elif not is_candidate(path, exclude):
            excluded += 1
    ordered = dict(sorted(unsupported.items(), key=lambda item: (-item[1], item[0])))
    return {"total": sum(ordered.values()) + excluded, "unsupported": ordered,
            "excluded": excluded}


def describe_skipped(skipped: dict, limit: int = 5) -> str:
    """6 file(s) skipped: unsupported type .jfif x6 (a JPEG: rename it to .jpg to check it)"""
    parts = []
    kinds = list(skipped.get("unsupported", {}).items())
    for suffix, count in kinds[:limit]:
        hint = SKIPPED_HINTS.get(suffix)
        parts.append(f"{suffix} x{count}" + (f" ({hint})" if hint else ""))
    if len(kinds) > limit:
        parts.append(f"{len(kinds) - limit} more type(s)")
    pieces = []
    if parts:
        pieces.append("unsupported type " + ", ".join(parts))
    if skipped.get("excluded"):
        pieces.append(f"{skipped['excluded']} by [filters] exclude / --exclude")
    return f"{skipped.get('total', 0)} file(s) skipped: " + "; ".join(pieces)


#: A word of advice for the unsupported types people most often drop in.
SKIPPED_HINTS = {
    ".jfif": "a JPEG: rename it to .jpg to check it",
    ".jpe": "a JPEG: rename it to .jpg to check it",
    ".bmp": "engines want PNG or TGA",
    ".gif": "engines want PNG or TGA",
    ".psd": "a Photoshop file: export a PNG or TGA",
    ".kra": "a Krita file: export a PNG",
    ".blend": "a Blender scene: export FBX, glTF or OBJ",
    ".ztl": "a ZBrush tool: export an OBJ",
    ".zpr": "a ZBrush project: export an OBJ",
    ".ma": "a Maya scene: export FBX", ".mb": "a Maya scene: export FBX",
    ".max": "a 3ds Max scene: export FBX",
    ".spp": "a Substance Painter project: export the textures",
    ".stl": "a print mesh: export OBJ or glTF for a game",
    ".dae": "Collada: export FBX or glTF",
    ".usd": "USD is not read yet", ".usda": "USD is not read yet", ".usdz": "USD is not read yet",
}


#: Files that belong to the tool or the system, never mentioned as "skipped".
QUIET_NAMES = frozenset({"rules.toml", "rules.example.toml", "asset_gatekeeper.toml",
                         "thumbs.db", "desktop.ini", ".ds_store"})
