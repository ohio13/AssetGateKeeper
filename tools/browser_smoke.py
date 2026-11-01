"""Browser smoke test: drive the real UIs in headless Chromium and fail loudly.

Why this exists: round 2 checked headers and grepped HTML, so two dead pages
shipped. The viewer died on a null element (six ids had been mangled in JS but
not in the HTML), and admin sign-in was refused because ``Referrer-Policy:
no-referrer`` makes Chromium post ``Origin: null``. Neither is visible without a
browser, and neither is caught by a test that only reads source files.

    python tools/browser_smoke.py            # boots its own servers and browser
    python tools/browser_smoke.py --json     # machine-readable
    python tools/browser_smoke.py --keep     # leave the temp data for inspection

Exit codes: 0 clean, 1 a page failed, 2 the harness could not run (no browser).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.cdp import CDP, CDPError  # noqa: E402

BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

OBJECT = b"""v 0 0 0
v 1 0 0
v 0 1 0
f 1 2 3
"""

LAUNCH_FLAGS = (
    "--headless=new",
    "--disable-gpu",
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-sync",
    "--disable-background-networking",
    "--disable-component-update",
    "--remote-debugging-port=0",
)


def find_browser() -> str:
    override = os.environ.get("AGK_BROWSER")
    if override and Path(override).is_file():
        return override
    for candidate in BROWSERS:
        if Path(candidate).is_file():
            return candidate
    return ""


def wait_for_port(port: int, timeout: float = 15.0) -> bool:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.4)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.15)
    return False


class Browser:
    """A headless Chromium plus one attached CDP connection."""

    def __init__(self, executable: str, profile: Path) -> None:
        import subprocess

        self.process = subprocess.Popen(
            [executable, *LAUNCH_FLAGS, f"--user-data-dir={profile}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = profile / "DevToolsActivePort"
        port = 0
        for _ in range(150):
            try:
                port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
                break
            except (OSError, IndexError, ValueError):
                time.sleep(0.1)
        if not port:
            raise CDPError("the browser never opened a DevTools port")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version",
                                    timeout=15) as response:
            version = json.loads(response.read())
        self.cdp = CDP(version["webSocketDebuggerUrl"])

    def close(self) -> None:
        self.cdp.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except Exception:                    # noqa: BLE001 - shutdown is best effort
            self.process.kill()


class Page:
    """One tab, with console errors collected from the moment it opens."""

    def __init__(self, browser: Browser, url: str) -> None:
        self.cdp = browser.cdp
        self.errors: list[str] = []
        target = self.cdp.send("Target.createTarget", {"url": "about:blank"})
        self.target_id = target["targetId"]
        self.session = self.cdp.send(
            "Target.attachToTarget",
            {"targetId": self.target_id, "flatten": True})["sessionId"]
        self.cdp.on(self._listen)
        self.cdp.send("Runtime.enable", {}, self.session)
        self.cdp.send("Log.enable", {}, self.session)
        self.cdp.send("Page.enable", {}, self.session)
        self.cdp.send("Page.navigate", {"url": url}, self.session)
        try:
            self.cdp.wait_for("Page.loadEventFired", self.session, timeout=25)
        except (CDPError, OSError):
            self.errors.append("the page never fired a load event")
        time.sleep(3.0)                      # modules, fetches, first paint

    def _listen(self, message: dict) -> None:
        if message.get("sessionId") != self.session:
            return
        method = message.get("method")
        params = message.get("params", {})
        if method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails", {})
            text = detail.get("exception", {}).get("description") or detail.get("text")
            self.errors.append(f"exception: {text}")
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            joined = " ".join(str(arg.get("value", arg.get("description", "")))
                              for arg in params.get("args", []))
            self.errors.append(f"console: {joined}")
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            if entry.get("level") == "error":
                where = entry.get("url") or ""
                self.errors.append(f"log({entry.get('source')}): {entry.get('text')} {where}".strip())

    def evaluate(self, expression: str):
        result = self.cdp.send("Runtime.evaluate",
                               {"expression": expression, "returnByValue": True,
                                "awaitPromise": True}, self.session)
        if result.get("exceptionDetails"):
            raise CDPError(result["exceptionDetails"].get("text") or "evaluate failed")
        return result.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.cdp.send("Target.closeTarget", {"targetId": self.target_id})
        except (CDPError, OSError):
            pass


def check_page(browser: Browser, name: str, url: str, assertions) -> dict:
    page = Page(browser, url)
    problems: list[str] = []
    try:
        for label, expression, predicate in assertions:
            try:
                value = page.evaluate(expression)
            except CDPError as exc:
                problems.append(f"{label}: {exc}")
                continue
            if not predicate(value):
                problems.append(f"{label}: got {value!r}")
    finally:
        page.close()
    unique = sorted(set(page.errors))
    return {
        "name": name,
        "ok": not unique and not problems,
        "summary": ("rendered, no console errors" if not unique and not problems
                    else f"{len(unique)} console error(s), {len(problems)} failed check(s)"),
        "problems": (problems + unique)[:6],
    }


def check_admin_login(browser: Browser, url: str, username: str, password: str) -> dict:
    """Sign in the way a browser does: fill the form and let it navigate.

    The submit cannot be asserted inside the same evaluate - the navigation
    destroys the execution context ("Inspected target navigated or closed"), which
    is exactly what a naive test does.
    """
    page = Page(browser, url)
    problems: list[str] = []
    try:
        if not page.evaluate("!!document.querySelector('form')"):
            problems.append("the sign-in form is missing")
        else:
            page.evaluate(
                f"document.querySelector('input[name=username]').value = {json.dumps(username)};"
                f"for (const field of document.querySelectorAll('input[name=password]'))"
                f" field.value = {json.dumps(password)};"
                "true")
            page.evaluate("document.querySelector('form').requestSubmit(); true")
            try:
                page.cdp.wait_for("Page.frameNavigated", page.session, timeout=20)
            except (CDPError, OSError):
                problems.append("the form did not navigate anywhere")
            time.sleep(1.5)
            try:
                path = page.evaluate("document.location.pathname")
            except CDPError as exc:
                path = None
                problems.append(f"could not read the landing page: {exc}")
            if path is not None and not (str(path).startswith("/admin")
                                         and path != "/admin/login"):
                problems.append(f"sign-in landed on {path!r}, not /admin")
    finally:
        page.close()
    unique = sorted(set(page.errors))
    return {
        "name": "admin-login",
        "ok": not unique and not problems,
        "summary": ("signed in from a real browser, no console errors"
                    if not unique and not problems
                    else f"{len(unique)} console error(s), {len(problems)} failed check(s)"),
        "problems": (problems + unique)[:6],
    }


def build_assertions():
    return {
        "viewer": [
            ("the 3D canvas exists (three.js initialised)",
             "!!document.querySelector('#canvas-host canvas')", lambda value: value is True),
            ("the asset list is populated",
             "document.querySelectorAll('#asset-list > *').length",
             lambda value: isinstance(value, (int, float)) and value > 0),
            ("the rules pane and its table exist",
             "!!document.querySelector('#pane-rules') && !!document.querySelector('#rules-table')",
             lambda value: value is True),
            ("the note form is usable",
             "(() => { const box = document.querySelector('#note-text');"
             " return !!box && box.disabled === false; })()",
             lambda value: value is True),
        ],
        "dashboard": [
            ("the dashboard rendered its cards",
             "document.querySelectorAll('.card').length",
             lambda value: isinstance(value, (int, float)) and value > 0),
            ("the telemetry table has a row",
             "document.querySelectorAll('tbody tr').length",
             lambda value: isinstance(value, (int, float)) and value > 0),
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Headless browser smoke test")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep the temp data")
    args = parser.parse_args(argv)

    browser_path = find_browser()
    if not browser_path:
        print("error: no Chromium-based browser found (set AGK_BROWSER)", file=sys.stderr)
        return 2

    import server.art_workstation_server as aws
    import server.asset_health_server as ahs

    work = Path(tempfile.mkdtemp(prefix="agk_browser_"))
    data_dir = work / "data"
    data_dir.mkdir()
    ws_db = work / "workstation.db"
    health_db = work / "health.db"

    aws.init_db(ws_db)
    aws.create_asset(ws_db, data_dir, "smoke.obj", OBJECT, uploader="smoke")
    ahs.init_db(health_db)
    ahs.store_verdict(health_db, {
        "timestamp": "2026-11-01T00:00:00Z", "name": "smoke.obj", "path": "smoke.obj",
        "passed": False, "problems": ["12 triangles exceeds the 10 budget"],
        "metric": "12 tris", "size_bytes": len(OBJECT), "hostname": "smoke",
        "username": "smoke", "status": "FAILED",
    }) if hasattr(ahs, "store_verdict") else None
    username, password = "smoke", "smoke-password-123"
    ahs.create_admin_user(health_db, username, "director", password,
                          created_by="browser smoke")

    workstation = aws.build_server("127.0.0.1", 0, ws_db, data_dir, quiet=True)
    health = ahs.build_server("127.0.0.1", 0, health_db, quiet=True)
    ws_port = workstation.server_address[1]
    health_port = health.server_address[1]
    for server in (workstation, health):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    for port in (ws_port, health_port):
        if not wait_for_port(port):
            print(f"error: the server on {port} never came up", file=sys.stderr)
            return 2

    report: dict = {"pages": []}
    browser = None
    try:
        browser = Browser(browser_path, work / "profile")
        cases = build_assertions()
        report["pages"].append(check_page(
            browser, "viewer", f"http://127.0.0.1:{ws_port}/viewer", cases["viewer"]))
        report["pages"].append(check_page(
            browser, "dashboard", f"http://127.0.0.1:{health_port}/dashboard",
            cases["dashboard"]))
        report["pages"].append(check_admin_login(
            browser, f"http://127.0.0.1:{health_port}/admin/login", username, password))
        report["ok"] = all(page["ok"] for page in report["pages"])
    except Exception as exc:                 # noqa: BLE001 - report, never traceback
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if browser is not None:
            browser.close()
        for server in (workstation, health):
            try:
                server.shutdown()
            except Exception:                # noqa: BLE001
                pass
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)

    if args.json:
        print(json.dumps(report))
    else:
        for page in report["pages"]:
            print(f"  {'ok  ' if page['ok'] else 'FAIL'} {page['name']}: {page['summary']}")
            for problem in page["problems"]:
                print(f"       {problem}")
        if report.get("error"):
            print(f"  harness: {report['error']}")
        print()
        print("browser smoke:", "clean" if report["ok"] else "FAILED")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
