#!/usr/bin/env python3
r"""Native desktop toasts, standard library only.

Fire-and-forget by contract: ``Notifier.notify()`` enqueues and returns. A single
daemon thread shells out to the platform's own notification mechanism, so a
missing DBus daemon, a locked-down Windows session or a headless box costs a log
line — never a slow gate.

Backends, tried in order:
    Windows  WinRT toast via PowerShell, falling back to a tray balloon
    Linux    notify-send, falling back to zenity
    macOS    osascript

Arguments are passed through environment variables rather than interpolated into
the script. Asset names are artist-controlled text and this module must never be
a command-injection vector.
"""

from __future__ import annotations

import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time

# One process spawn per toast is fine at human rates; the queue is what stops a
# 300-asset sweep from opening 300 subprocesses.
DEFAULT_MAX_PER_MINUTE = 6
SUBPROCESS_TIMEOUT = 6.0


def _windows_script() -> str:
    """A toast attempt plus a tray-balloon fallback, in one PowerShell process."""
    return r"""
$ErrorActionPreference = 'Stop'
$title = $env:AGK_TOAST_TITLE
$body  = $env:AGK_TOAST_BODY
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null
    $template = @"
<toast><visual><binding template="ToastGeneric">
<text>$title</text><text>$body</text>
</binding></visual></toast>
"@
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($template)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('AssetGatekeeper').Show($toast)
    exit 0
} catch {
    # Old Windows / no AppUserModelID: a tray balloon still gets the message out.
    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $icon = New-Object System.Windows.Forms.NotifyIcon
        $icon.Icon = [System.Drawing.SystemIcons]::Information
        $icon.Visible = $true
        $icon.ShowBalloonTip(5000, $title, $body, 'Info')
        Start-Sleep -Seconds 5
        $icon.Dispose()
        exit 0
    } catch { exit 1 }
}
"""


def detect_backend() -> str:
    """Which mechanism this machine has, or "none". Cheap: no process is spawned."""
    system = platform.system()
    if system == "Windows":
        return "windows-toast" if shutil.which("powershell") else "none"
    if system == "Darwin":
        return "osascript" if shutil.which("osascript") else "none"
    if shutil.which("notify-send"):
        return "notify-send"
    if shutil.which("zenity"):
        return "zenity"
    return "none"


def _command(backend: str, title: str, body: str) -> tuple[list[str], dict]:
    """Return (argv, env) for one toast. Text goes through env, never the argv."""
    env = dict(os.environ)
    env["AGK_TOAST_TITLE"] = title
    env["AGK_TOAST_BODY"] = body
    if backend == "windows-toast":
        return (["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                 "-Command", _windows_script()], env)
    if backend == "notify-send":
        return (["notify-send", "--app-name=AssetGatekeeper", "--urgency=normal",
                 title, body], env)
    if backend == "zenity":
        return (["zenity", "--notification", "--text=%s\n%s" % (title, body)], env)
    if backend == "osascript":
        script = 'display notification (system attribute "AGK_TOAST_BODY") ' \
                 'with title (system attribute "AGK_TOAST_TITLE")'
        return (["osascript", "-e", script], env)
    return ([], env)


class Notifier:
    """Queued desktop notifications. Never blocks, never raises."""

    def __init__(self, enabled: bool = True, max_per_minute: int = DEFAULT_MAX_PER_MINUTE,
                 backend: str | None = None) -> None:
        self.enabled = bool(enabled)
        self.backend = backend or (detect_backend() if self.enabled else "none")
        self.max_per_minute = max(1, int(max_per_minute))
        self.sent = 0
        self.suppressed = 0
        self.failed = 0
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=32)
        self._thread: threading.Thread | None = None
        self._closed = False
        self._window_start = time.monotonic()
        self._window_count = 0
        self._lock = threading.Lock()

    # -- public -----------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.enabled and self.backend != "none"

    def notify(self, title: str, body: str) -> bool:
        """Queue a toast. Returns False if it was dropped (never raises)."""
        if not self.available or self._closed:
            return False
        if not self._take_rate_slot():
            self.suppressed += 1
            return False
        try:
            self._queue.put_nowait((title[:120], body[:400]))
        except queue.Full:
            self.suppressed += 1
            return False
        self._ensure_worker()
        return True

    def notify_verdict(self, verdict) -> bool:
        """Map a Verdict to the two cases worth interrupting a human for."""
        name = getattr(getattr(verdict, "path", None), "name", "asset")
        problems = getattr(verdict, "problems", ())
        if getattr(verdict, "passed", False):
            notes = getattr(verdict, "heal_notes", ())
            if not notes:
                return False
            return self.notify(f"Asset healed · {name}", "; ".join(notes)[:300])
        if not problems:
            return False
        first = problems[0]
        extra = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
        return self.notify(f"Validation failed · {name}", f"{first}{extra}"[:300])

    def close(self, timeout: float | None = None) -> None:
        """Let queued toasts finish, then stop. Bounded, because it is shutdown."""
        budget = SUBPROCESS_TIMEOUT if timeout is None else timeout
        if self._thread is not None:
            deadline = time.monotonic() + budget
            while time.monotonic() < deadline and not self._queue.empty():
                time.sleep(0.02)
        self._closed = True
        if self._thread is None:
            return
        try:
            self._queue.put_nowait(None)  # type: ignore[arg-type]
        except queue.Full:
            pass
        self._thread.join(budget)

    # -- internals --------------------------------------------------------------

    def _take_rate_slot(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if now - self._window_start >= 60.0:
                self._window_start, self._window_count = now, 0
            if self._window_count >= self.max_per_minute:
                return False
            self._window_count += 1
            return True

    def _ensure_worker(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="agk-notify", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._send(*item)

    def _send(self, title: str, body: str) -> None:
        argv, env = _command(self.backend, title, body)
        if not argv:
            return
        try:
            completed = subprocess.run(argv, env=env, capture_output=True,
                                       timeout=SUBPROCESS_TIMEOUT)
            if completed.returncode == 0:
                self.sent += 1
            else:
                self.failed += 1
                print(f"notify: backend exited {completed.returncode}", file=sys.stderr)
        except Exception as exc:      # missing binary, no DBus, timeout, ...
            self.failed += 1
            print(f"notify: {type(exc).__name__}: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """`python notifier.py "title" "body"` — handy for testing a machine's setup."""
    args = list(sys.argv[1:] if argv is None else argv)
    backend = detect_backend()
    print(f"backend: {backend}")
    if backend == "none":
        print("no notification backend available on this machine")
        return 2
    if not args:
        args = ["AssetGatekeeper", "Notifications are working."]
    notifier = Notifier(backend=backend)
    first, rest = args[0], " ".join(args[1:]) or "Notification test"
    notifier.notify(first, rest)
    notifier.close(timeout=8.0)
    print(f"sent={notifier.sent} failed={notifier.failed}")
    return 0 if notifier.sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
