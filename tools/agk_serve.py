#!/usr/bin/env python3
"""One command to run AssetHealth and the Art Workstation, and one to stop them.

    python tools/agk_serve.py up                 start both, print the URLs
    python tools/agk_serve.py up --open          ...and open the dashboard
    python tools/agk_serve.py status             are they running, and where?
    python tools/agk_serve.py urls               the URLs + the gate command
    python tools/agk_serve.py logs --tail 40     recent server log lines
    python tools/agk_serve.py down               stop both
    python tools/agk_serve.py restart            stop, then start as last time
    python tools/agk_serve.py allow [FOLDER]     let the project picker open FOLDER
                                                 (no FOLDER: a folder dialog)

Why a file of record instead of just starting processes: the whole point of a
dashboard is that verdicts land in it. If the dashboard moved from 8080 to 8081
because something else owned 8080, every artist's --telemetry-url still says
8080, and the metrics become fiction. So `up` resolves the ports *first*, tells
you exactly what it chose, writes them to ``.agk/ports.json``, and prints the
gate command that matches - copy it, or point ASSET_GATEKEEPER_SERVER_URL at it.

Deliberate choices:

* The servers run **detached** with their output in ``.agk/logs/``. No console
  windows to keep alive, nothing that dies with the terminal you launched from.
* ``up`` is idempotent: if both are already healthy it says so and exits 0.
* ``down`` refuses to kill a pid that is not one of our servers. A stale
  ports.json from a previous boot must never take out an unrelated process that
  has since reused the pid.
* Ports are resolved here and passed explicitly, then verified by probing
  ``/api/health``. If the port is stolen between the check and the bind, the
  start is retried on the next candidate rather than recorded wrongly.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent


def is_checkout(root: Path = REPO_ROOT) -> bool:
    """A downloaded or cloned copy (Start AssetGatekeeper.bat or Rules.bat beside it), not a pip install."""
    return ((root / "pyproject.toml").is_file() or (root / "Start AssetGatekeeper.bat").is_file()
            or (root / "Rules.bat").is_file())


def data_root() -> Path:
    """Where the servers keep their databases, logs and settings.

    A downloaded copy keeps them beside itself, so deleting the folder removes
    everything. A pip install must not: its home is Python's site-packages,
    which an upgrade replaces and a system Python may not let us write to
    (9 Nov 2026 audit, B2). $AGK_HOME overrides both.
    """
    explicit = os.environ.get("AGK_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if is_checkout():
        return REPO_ROOT
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "AssetGatekeeper"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AssetGatekeeper"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "asset-gatekeeper"


DATA_ROOT = data_root()
STATE_DIR = DATA_ROOT / ".agk"
PORTS_FILE = STATE_DIR / "ports.json"
LOG_DIR = STATE_DIR / "logs"
#: Folders the project picker may open, one per line, written by `allow`
#: (Allow_Folder.bat). It lives in this copy's .agk folder, so deleting the copy
#: removes it too - nothing is left in the Windows environment.
ALLOWED_FILE = STATE_DIR / "allowed_folders.txt"

HEALTH_SCRIPT = REPO_ROOT / "server" / "asset_health_server.py"
WORKSTATION_SCRIPT = REPO_ROOT / "server" / "art_workstation_server.py"

HEALTH_PORT = 8080
WORKSTATION_PORT = 8100
START_ATTEMPTS = 3          # how many ports to try if one is lost to a race
READY_TIMEOUT = 20.0        # seconds to wait for /api/health to answer


# --------------------------------------------------------------------- utils

def say(message: str = "") -> None:
    print(message, flush=True)


def python_exe() -> str:
    """The interpreter to run the servers with.

    Prefers the repo venv (that is where trimesh and Pillow live), then this
    interpreter, which is what a `python tools/agk_serve.py` invocation already
    implies.
    """
    candidates = [REPO_ROOT / ".venv" / "Scripts" / "python.exe",
                  REPO_ROOT / ".venv" / "bin" / "python"]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _detached_flags() -> int:
    if os.name == "nt":
        return 0x00000008 | 0x08000000      # DETACHED_PROCESS | CREATE_NO_WINDOW
    return 0


def port_is_free(host: str, port: int) -> bool:
    """Can we bind it, and is nobody listening on it?

    Two checks because one is not enough: a bind test alone can lie on Windows,
    where SO_REUSEADDR lets a socket bind a port another process is already
    listening on. A connect() to a listening port succeeds, so that catches the
    case the bind test misses.
    """
    address = ("127.0.0.1" if host in ("", "0.0.0.0", "::") else host, port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        if probe.connect_ex(address) == 0:
            return False                    # something is answering there
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(address)
        except OSError:
            return False
    return True


def next_free(host: str, start: int, reserved: tuple[int, ...]) -> int | None:
    for candidate in range(start, start + 25):
        if candidate in reserved:
            continue
        if port_is_free(host, candidate):
            return candidate
    return None


def health_ok(url: str, path: str = "/api/health", timeout: float = 1.5) -> bool:
    """Is a server answering? The two services expose different liveness routes:
    the workstation has /api/health, the dashboard has /api/summary."""
    try:
        with urllib.request.urlopen(f"{url}{path}", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def read_ports() -> dict:
    try:
        return json.loads(PORTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_ports(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PORTS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def process_command(pid: int) -> str:
    """The command line of a pid, or '' when it cannot be read.

    Used for one safety check only: never kill a pid unless it is still one of
    our servers. Pids are recycled; a launcher that trusts them blindly will
    eventually kill somebody's editor.
    """
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"],
                capture_output=True, text=True, timeout=10,
                creationflags=0x08000000)
            return (result.stdout or "").strip()
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def stop_pid(pid: int) -> bool:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                           timeout=15, creationflags=0x08000000)
        else:
            os.kill(pid, 15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ----------------------------------------------------------------------- start

def start_one(label: str, script: Path, args: list[str], host: str,
              preferred: int, reserved: tuple[int, ...], log_name: str,
              probe: str) -> dict | None:
    """Start a server on the first port that actually answers. Returns its record."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{log_name}.log"
    scheme_port = preferred
    for attempt in range(START_ATTEMPTS):
        port = next_free(host, scheme_port, reserved)
        if port is None:
            say(f"  {label}: no free port in {preferred}..{preferred + 24}")
            return None
        log = open(log_path, "ab")
        log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {label} on port {port} ---\n"
                  .encode("utf-8"))
        log.flush()
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment["AGK_DATA_DIR"] = str(DATA_ROOT)       # never scanned as a project
        environment["AGK_PORTS_FILE"] = str(PORTS_FILE)    # how the dashboard finds the workstation
        process = subprocess.Popen(
            [python_exe(), str(script), "--host", host, "--port", str(port), *args,
             "--quiet"],
            cwd=str(DATA_ROOT), env=environment, stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=_detached_flags(),
            start_new_session=(os.name != "nt"))
        url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '', '::') else host}:{port}"
        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline:
            if process.poll() is not None:
                break                       # died: port taken in the race, or a real error
            if health_ok(url, probe):
                return {"url": url, "port": port, "pid": process.pid,
                        "log": str(log_path), "label": label, "probe": probe}
            time.sleep(0.25)
        if process.poll() is None:
            stop_pid(process.pid)           # hung startup: do not leave it behind
        say(f"  {label}: port {port} did not answer, see {log_path}")
        scheme_port = port + 1
    return None


def command_up(args: argparse.Namespace) -> int:
    current = read_ports()
    default_probe = {"dashboard": "/api/summary", "workstation": "/api/health"}
    healthy = {key: bool(current.get(key))
               and health_ok(current[key]["url"], current[key].get("probe", default_probe[key]))
               for key in ("dashboard", "workstation")}
    if healthy["dashboard"] and healthy["workstation"]:
        say("Already running:")
        for key in ("dashboard", "workstation"):
            say(f"  {key:<12} {current[key]['url']}")
        say_portline(current)
        open_ui(args, current)          # a second double-click still opens the page
        return 0

    say(f"Starting servers as {python_exe()}")
    if args.rules:
        rules_args = ["--rules", str(Path(args.rules).expanduser().resolve())]
    else:
        default_rules = DATA_ROOT / "rules.toml"
        rules_args = ["--rules", str(default_rules)] if default_rules.is_file() else []

    records: dict[str, dict] = {}
    dashboard_args = ["--db", str(Path(args.health_db).expanduser())]
    if args.token:
        dashboard_args += ["--token", args.token]
    if args.webhook_url:
        dashboard_args += ["--webhook-url", args.webhook_url]
    if args.webhook_provider:
        dashboard_args += ["--webhook-provider", args.webhook_provider]
    # The project picker can only open folders on this list. The launcher seeds it
    # with the repo and $AGK_PROJECT_ROOTS, so a studio widens it once in the .bat
    # or the environment - never from the browser.
    for root in project_roots(args):
        dashboard_args += ["--allow-root", str(root)]
    if args.demo:
        dashboard_args.append("--demo")
    if args.host not in ("127.0.0.1", "localhost"):
        # A non-loopback bind without a token is a review screen on the LAN.
        if not args.token:
            say("  note: binding to a non-loopback host with no --token; anyone who can "
                "reach the port can write notes and upload")

    if healthy["dashboard"] and current.get("dashboard"):
        records["dashboard"] = current["dashboard"]      # keep what is already there
        say(f"  dashboard: already up on {current['dashboard']['url']}")
    else:
        record = start_one("dashboard", HEALTH_SCRIPT, dashboard_args + rules_args,
                           args.host, args.health_port, (WORKSTATION_PORT,), "dashboard",
                           "/api/summary")
        if record is None:
            return 2
        records["dashboard"] = record
        say(f"  dashboard: {record['url']}")

    if healthy["workstation"] and current.get("workstation"):
        records["workstation"] = current["workstation"]
        say(f"  workstation: already up on {current['workstation']['url']}")
    else:
        workstation_args = ["--db", str(Path(args.workstation_db).expanduser()),
                            "--data", str(Path(args.workstation_data).expanduser())]
        if args.token:
            workstation_args += ["--token", args.token]
        if args.max_upload_mb:
            workstation_args += ["--max-upload-mb", str(args.max_upload_mb)]
        record = start_one("workstation", WORKSTATION_SCRIPT,
                           workstation_args + rules_args, args.host, args.workstation_port,
                           (HEALTH_PORT,), "workstation", "/api/health")
        if record is None:
            return 2
        records["workstation"] = record
        say(f"  workstation: {record['url']}")

    started = {**records, "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "python": python_exe(), "argv": list(getattr(args, "argv", []) or [])}
    write_ports(started)
    say()
    say_portline(started)
    open_ui(args, records)
    return 0


def project_roots(args: argparse.Namespace) -> list[Path]:
    """Folders the picker may open: --allow-root, $AGK_PROJECT_ROOTS, then the repo.

    Trailing separators are stripped because a batch file that passes
    ``"C:\\path\\"`` has already let the backslash escape its own closing quote - a
    caller can hand us a mangled string, so normalise what arrives.
    """
    raw = [str(item) for item in (args.allow_root or [])]
    raw += os.environ.get("AGK_PROJECT_ROOTS", "").split(os.pathsep)
    explicit = any(item.strip().strip('"') for item in raw)
    raw += read_allowed()
    if not explicit:
        raw.append(str(default_root()))  # added folders widen the default, never replace it
    roots: list[Path] = []
    seen: set[str] = set()
    for item in raw:
        # `setx` can leave the quotes on, and the same folder usually arrives from
        # both the environment and --allow-root, so normalise and de-duplicate.
        text = item.strip().strip('"')
        if not text:
            continue
        path = Path(os.path.normpath(text)).expanduser()
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if os.name == "nt":
            key = key.lower()
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)
    if not roots:
        roots = [default_root()]
    return roots


def default_root() -> Path:
    """The folder the picker may always open: this copy, or for a pip install the
    folder `agk-serve up` was run from (normally the project)."""
    return REPO_ROOT if is_checkout() else Path.cwd()


def read_allowed() -> list[str]:
    """Folders added with `allow`, one per line ('#' starts a comment)."""
    try:
        lines = ALLOWED_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def write_allowed(folders: list[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ALLOWED_FILE.write_text(
        "# Folders the AssetGatekeeper project picker may open, one per line.\n"
        "# Added by Allow_Folder.bat; delete a line to take a folder off the list.\n"
        + "".join(f"{folder}\n" for folder in folders), encoding="utf-8")


def _same_folder(a: str, b: str) -> bool:
    def key(text: str) -> str:
        try:
            value = str(Path(os.path.normpath(text)).expanduser().resolve())
        except OSError:
            value = os.path.normpath(text)
        return value.lower() if os.name == "nt" else value
    return key(a) == key(b)


def choose_folder() -> str:
    """A folder picked in the Windows folder dialog, or typed when there is none."""
    try:
        import tkinter
        from tkinter import filedialog
        window = tkinter.Tk()
        window.withdraw()
        window.attributes("-topmost", True)
        chosen = filedialog.askdirectory(
            title="Choose a folder AssetGatekeeper may open", mustexist=True,
            initialdir=str(Path.home()))
        window.destroy()
        return str(chosen or "")
    except Exception:                       # no tkinter, or no display
        try:
            return input("Folder to allow (paste its path, then Enter): ").strip().strip('"')
        except EOFError:
            return ""


def command_allow(args: argparse.Namespace) -> int:
    """Add a folder to the picker's allow-list, then restart if running."""
    folders = read_allowed()
    if args.list:
        say("Folders the project picker may open (besides this one):")
        for folder in folders or ["(none added yet)"]:
            say(f"  {folder}")
        say(f"List kept in {ALLOWED_FILE}")
        return 0
    if args.remove:
        kept = [folder for folder in folders if not _same_folder(folder, args.remove)]
        if len(kept) == len(folders):
            say(f"{args.remove} was not on the list.")
            return 1
        write_allowed(kept)
        say(f"Removed {args.remove}.")
        return restart_if_running()
    text = (args.folder or "").strip().strip('"') or choose_folder()
    if not text:
        say("No folder chosen; nothing changed.")
        return 1
    folder = Path(os.path.normpath(text)).expanduser()
    if not folder.is_dir():
        say(f"{folder} is not a folder on this computer; nothing changed.")
        return 1
    folder = folder.resolve()
    if any(_same_folder(existing, str(folder)) for existing in folders):
        say(f"{folder} is already allowed.")
    else:
        write_allowed(folders + [str(folder)])
        say(f"Allowed: {folder}")
    say("Pick it under 'Allowed folder' on the Rules & Presets page, then 'Use this folder'.")
    return restart_if_running()


def restart_if_running() -> int:
    """The allow-list is read at start-up, so a running dashboard needs a restart."""
    records = read_ports()
    dashboard = records.get("dashboard") or {}
    if not (dashboard.get("url") and health_ok(dashboard["url"], "/api/summary")):
        say("The dashboard is not running; it will use the new list next time it starts.")
        return 0
    say("Restarting the dashboard so it sees the new list...")
    return command_restart(argparse.Namespace())


def command_restart(args: argparse.Namespace) -> int:
    """Stop both, then start them again with the options of the last start."""
    argv = list(read_ports().get("argv") or ["up", "--open", "admin",
                                             "--allow-root", str(default_root())])
    command_down(argparse.Namespace())
    parsed = build_parser().parse_args(argv)
    parsed.argv = argv
    return parsed.func(parsed)


def open_ui(args: argparse.Namespace, records: dict) -> None:
    """Open what was asked for. /admin is the front door: it is where the work is."""
    dashboard = (records.get("dashboard") or {}).get("url")
    if not dashboard or not args.open:
        return
    if args.open in ("admin", "both"):
        webbrowser.open(f"{dashboard}/admin")
    if args.open == "dashboard":
        webbrowser.open(f"{dashboard}/dashboard")
    if args.open == "both":
        workstation = (records.get("workstation") or {}).get("url")
        if workstation:
            webbrowser.open(f"{workstation}/viewer")


def gate_command(records: dict) -> str:
    """The gate command for *these* ports.

    This is the whole reason the ports file exists: if the dashboard moved to
    8081, a command still pointing at 8080 would post verdicts somewhere else.
    """
    url = (records.get("dashboard") or {}).get("url")
    if not url:
        return "asset-gatekeeper <folder> --once --telemetry-url <dashboard not started>"
    return f"asset-gatekeeper <folder> --once --telemetry-url {url}/api/telemetry"


def command_hint(action: str) -> str:
    """How *this* user runs an action: the .bat, the script, or the console command.

    The launcher printed "python tools/agk_serve.py down" to pip users, who have
    no tools/ folder (9 Nov 2026 audit, B6).
    """
    if not is_checkout():
        return f"agk-serve {action}"
    if os.name == "nt":
        return {"down": "double-click Stop.bat", "up": "double-click Start AssetGatekeeper.bat"}.get(
            action, f"tools\\agk-serve.bat {action}")
    return f"python tools/agk_serve.py {action}"


def say_portline(records: dict) -> None:
    dash = records.get("dashboard", {})
    work = records.get("workstation", {})
    say(f"  dashboard    {dash.get('url', '?')}/dashboard")
    say(f"  workstation  {work.get('url', '?')}/viewer")
    if dash.get("url"):
        say(f"  gate         {gate_command(records)}")
    say(f"  stop         {command_hint('down')}")
    say(f"  record       {PORTS_FILE}")


# --------------------------------------------------------------------- stop etc

def command_down(args: argparse.Namespace) -> int:
    records = read_ports()
    if not records:
        say("Nothing recorded in .agk/ports.json.")
    problems = 0
    for key in ("dashboard", "workstation"):
        record = records.get(key) or {}
        pid = record.get("pid")
        if not pid:
            continue
        command = process_command(pid)
        script = "asset_health_server" if key == "dashboard" else "art_workstation_server"
        if command and script not in command:
            say(f"  {key}: pid {pid} is not {script} (now: {command[:60]}...); not killing it")
            problems += 1
            continue
        if not command:
            say(f"  {key}: cannot read pid {pid} (already gone?)")
        elif health_ok(record.get("url", ""), record.get("probe", "/api/health"),
                       timeout=1.0):
            stop_pid(pid)
            say(f"  {key}: stopped (pid {pid})")
        else:
            stop_pid(pid)
            say(f"  {key}: was not answering; pid {pid} stopped anyway")
    PORTS_FILE.unlink(missing_ok=True)
    stopped = {int(pid) for pid in ((records.get(k) or {}).get("pid") for k in
                                    ("dashboard", "workstation")) if pid}
    strays = 0
    for pid, command in stray_servers():
        if pid in stopped:
            continue
        stop_pid(pid)
        strays += 1
        say(f"  also stopped a server started from this folder (pid {pid})")
    if not records and not strays:
        say("Nothing from this folder is running - nothing to stop.")
    return 0 if problems == 0 else 1


def stray_servers() -> list[tuple[int, str]]:
    """Servers running from *this* copy that ports.json does not record.

    A second start, a tools/start_workstation.bat window, or a ports.json removed
    by hand all leave servers nobody can stop by name - and a running server
    keeps its folder locked, so the copy cannot be deleted (9 Nov 2026). Only
    processes whose command line names this copy's own server scripts match.
    """
    wanted = [str(HEALTH_SCRIPT), str(WORKSTATION_SCRIPT)]
    if os.name == "nt":
        wanted = [item.lower() for item in wanted]
    found: list[tuple[int, str]] = []
    for pid, command in _python_processes():
        if pid == os.getpid():
            continue
        if os.name == "nt":
            # Only python*.exe processes are listed; match the script path in them.
            if any(item in command.lower() for item in wanted):
                found.append((pid, command))
        elif command in wanted:
            found.append((pid, command))
    return found


def _python_processes() -> list[tuple[int, str]]:
    """(pid, command line) of every running Python process."""
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                 "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=20, creationflags=0x08000000)
            data = json.loads(result.stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            return []
        if isinstance(data, dict):
            data = [data]
        return [(int(item.get("ProcessId") or 0), str(item.get("CommandLine") or ""))
                for item in data if isinstance(item, dict)]
    found = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            parts = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        # The interpreter itself, not a shell whose command line merely mentions
        # python and the script (that shell may be the one running this).
        if not parts or b"python" not in Path(parts[0].decode("utf-8", "replace")).name.encode():
            continue
        args = [part.decode("utf-8", "replace") for part in parts[1:] if part]
        script = next((arg for arg in args if not arg.startswith("-")), "")
        found.append((int(entry.name), script))
    return found


def command_status(args: argparse.Namespace) -> int:
    records = read_ports()
    if not records:
        say(f"Not running. Start with: {command_hint('up')}")
        return 1
    unhealthy = 0
    for key in ("dashboard", "workstation"):
        record = records.get(key) or {}
        url = record.get("url", "")
        alive = bool(url) and health_ok(url, record.get("probe", "/api/health"))
        if not alive:
            unhealthy += 1
        say(f"  {key:<12} {'running' if alive else 'DOWN   '}  pid {record.get('pid', '?'):<7} "
            f"{url or '-'}")
    say(f"  started      {records.get('started', '?')}")
    return 0 if unhealthy == 0 else 1


def command_urls(args: argparse.Namespace) -> int:
    say_portline(read_ports())
    return 0


def command_logs(args: argparse.Namespace) -> int:
    wanted = ("dashboard", "workstation") if args.service == "both" else (args.service,)
    for name in wanted:
        path = LOG_DIR / f"{name}.log"
        say(f"=== {path} ===")
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            say("  (no log yet)")
            continue
        for line in lines[-args.tail:]:
            say(f"  {line}")
    return 0


# ------------------------------------------------------------------------ CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agk_serve",
        description="Start, stop and inspect the two AssetGatekeeper web services.")
    sub = parser.add_subparsers(dest="command")

    up = sub.add_parser("up", help="start both servers detached")
    up.add_argument("--host", default="127.0.0.1",
                    help="bind address (default 127.0.0.1; anything else needs --token)")
    up.add_argument("--health-port", type=int, default=HEALTH_PORT, help="preferred port")
    up.add_argument("--workstation-port", type=int, default=WORKSTATION_PORT,
                    help="preferred port")
    up.add_argument("--rules", default="", help="rules.toml for both (default ./rules.toml)")
    up.add_argument("--health-db", default="asset_health.db", help="dashboard database")
    up.add_argument("--workstation-db", default="art_workstation.db")
    up.add_argument("--workstation-data", default="workstation_data")
    up.add_argument("--token", default=os.environ.get("AUTH_TOKEN", ""),
                    help="bearer token for both (default $AUTH_TOKEN)")
    up.add_argument("--demo", action="store_true", help="seed the dashboard sample set")
    up.add_argument("--webhook-url", default=os.environ.get("AGK_WEBHOOK_URL", ""),
                    metavar="URL",
                    help="Slack/Discord/Teams webhook for failure alerts (default "
                         "$AGK_WEBHOOK_URL); the dashboard sends one per project per 5 min")
    up.add_argument("--webhook-provider", default="",
                    choices=("", "slack", "discord", "teams"),
                    help="override the envelope guessed from the URL")
    up.add_argument("--open", nargs="?", const="admin",
                    choices=("admin", "dashboard", "both"),
                    help="open the admin page (default), the metrics dashboard, or "
                         "'both' = admin + review screen")
    up.add_argument("--allow-root", action="append", default=[], metavar="PATH",
                    help="folder the project picker may open (repeatable; also "
                         "$AGK_PROJECT_ROOTS and .agk/allowed_folders.txt; default: this repo)")
    up.add_argument("--max-upload-mb", type=int, default=0, metavar="MB",
                    help="largest file the review screen accepts (default 512)")

    restart = sub.add_parser("restart", help="stop, then start again as last time")
    restart.set_defaults(func=command_restart)

    allow = sub.add_parser("allow", help="let the project picker open another folder")
    allow.add_argument("folder", nargs="?", default="",
                       help="the folder (default: choose it in a folder dialog)")
    allow.add_argument("--remove", metavar="FOLDER", default="",
                       help="take a folder off the list instead")
    allow.add_argument("--list", action="store_true", help="show the list and stop")
    allow.set_defaults(func=command_allow)

    down = sub.add_parser("down", help="stop what this tool started")
    down.set_defaults(func=command_down)

    status = sub.add_parser("status", help="are they running, and where?")
    status.set_defaults(func=command_status)

    urls = sub.add_parser("urls", help="print URLs and the gate command")
    urls.set_defaults(func=command_urls)

    logs = sub.add_parser("logs", help="recent server log lines")
    logs.add_argument("service", nargs="?", default="both",
                      choices=("dashboard", "workstation", "both"))
    logs.add_argument("--tail", type=int, default=25)
    logs.set_defaults(func=command_logs)

    up.set_defaults(func=command_up)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw)
    args.argv = raw
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
