#!/usr/bin/env python3
"""Install what AssetGatekeeper needs into this Python, then check it loads.

    python tools/install_extras.py            install anything missing, then check
    python tools/install_extras.py --check    only say what is there and what is not
    python tools/install_extras.py --dry-run  print the pip command, run nothing

Install_Extras.bat runs this with the same Python that Start AssetGatekeeper.bat uses, so a
library lands where the servers will look for it. Before this, the review page
said "pip install fast-simplification xatlas scipy" and a Windows user had to
know which Python that meant, and to type it in cmd rather than inside Python.

Two groups:

* **Needed** - what the gate itself cannot run without (trimesh, Pillow, ...).
  Start AssetGatekeeper.bat refuses to start without them, and points here.
* **Extras** - optional: the Polypaint bake (fast-simplification, xatlas,
  scipy; Python 3.10 or newer) and watchdog (faster folder watching).

Only missing or broken packages are installed; nothing already working is
upgraded, so running this twice changes nothing. The lists match the
dependencies in pyproject.toml (a test keeps them in step).

Exit codes: 0 everything needed and every extra this Python can take works;
1 something could not be installed or does not load; 2 bad arguments.

Standard library only: it has to run before anything else is installed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

#: Python the bake libraries need (fast-simplification fails to import on 3.9).
BAKE_MIN_PYTHON = (3, 10)
MIN_PYTHON = (3, 9)


@dataclass(frozen=True)
class Package:
    pip_name: str          # what pip installs, with its version range
    module: str            # what Python imports to prove it works
    purpose: str           # one line a non-programmer can read
    needed: bool           # True: the gate needs it; False: an optional extra
    min_python: tuple = MIN_PYTHON
    max_python: Optional[tuple] = None   # exclusive: (3, 11) means "below 3.11"

    @property
    def name(self) -> str:
        """The bare package name, without its version range."""
        return re.split(r"[<>=!~;\[ ]", self.pip_name, maxsplit=1)[0]

    def applies(self, version: tuple = sys.version_info[:2]) -> bool:
        if version < self.min_python:
            return False
        return self.max_python is None or version < self.max_python


#: Keep in step with [project] dependencies and the [full] extra in pyproject.toml.
PACKAGES: tuple = (
    Package("trimesh>=4.0,<6", "trimesh", "reads 3D models", True),
    Package("pillow>=9.0,<13", "PIL.Image", "reads textures", True),
    Package("charset-normalizer>=3.0", "charset_normalizer",
            "reads .obj/.mtl files saved by older Windows exporters", True),
    Package("DracoPy>=1.4", "DracoPy", "reads Draco-compressed glTF", True),
    Package("tomli>=2.0", "tomli", "reads rules.toml on Python 3.9 and 3.10", True,
            max_python=(3, 11)),
    Package("watchdog>=3.0", "watchdog.observers", "faster folder watching", False),
    Package("fast-simplification>=0.1.7", "fast_simplification",
            "Polypaint bake: reduces the sculpt", False, min_python=BAKE_MIN_PYTHON),
    Package("xatlas>=0.0.9", "xatlas", "Polypaint bake: unwraps UVs", False,
            min_python=BAKE_MIN_PYTHON),
    Package("scipy>=1.7", "scipy.spatial", "Polypaint bake: samples the sculpt colours",
            False, min_python=BAKE_MIN_PYTHON),
)


def say(text: str = "") -> None:
    print(text, flush=True)


def loads(module: str, python: str = sys.executable) -> tuple[bool, str]:
    """Does *module* import in a fresh interpreter? (A fresh one, because a
    package installed a moment ago is not visible to this process's caches,
    and one that is installed but broken should fail here, not in a bake.)"""
    try:
        result = subprocess.run([python, "-c", f"import {module}"], capture_output=True,
                                text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, ""
    lines = [line for line in (result.stderr or "").strip().splitlines() if line.strip()]
    return False, lines[-1] if lines else f"exit {result.returncode}"


def pip_available(python: str = sys.executable) -> bool:
    try:
        return subprocess.run([python, "-m", "pip", "--version"], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pip_command(packages: list, python: str = sys.executable) -> list[str]:
    return [python, "-m", "pip", "install", *[p.pip_name for p in packages]]


def quoted(command: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part or "<" in part or ">" in part else part
                    for part in command)


def survey(version: tuple = sys.version_info[:2], python: str = sys.executable):
    """(working, missing, not_for_this_python) lists of Package."""
    working, missing, skipped = [], [], []
    for package in PACKAGES:
        if not package.applies(version):
            if package.max_python is None:      # too old for it, not too new
                skipped.append(package)
            continue
        ok, _ = loads(package.module, python)
        (working if ok else missing).append(package)
    return working, missing, skipped


def report(working, missing, skipped, version) -> None:
    for package in working:
        say(f"  OK       {package.name:22} {package.purpose}")
    for package in missing:
        tag = "MISSING " if package.needed else "not yet "
        say(f"  {tag} {package.name:22} {package.purpose}")
    for package in skipped:
        need = ".".join(map(str, package.min_python))
        say(f"  skipped  {package.name:22} needs Python {need}+ "
            f"(this is {version[0]}.{version[1]})")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install_extras",
        description="Install AssetGatekeeper's libraries into this Python and check them.")
    parser.add_argument("--check", action="store_true",
                        help="only report what is installed; change nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the pip command instead of running it")
    parser.add_argument("--needed-only", action="store_true",
                        help="install only what the gate needs, no extras")
    args = parser.parse_args(argv)

    version = sys.version_info[:2]
    say(f"Python {version[0]}.{version[1]} at {sys.executable}")
    if version < MIN_PYTHON:
        say()
        say("  AssetGatekeeper needs Python 3.9 or newer (3.12 recommended).")
        say("  Install it from https://www.python.org/downloads/ and run this again.")
        return 1
    say()

    working, missing, skipped = survey(version)
    if args.needed_only:
        missing = [p for p in missing if p.needed]
    report(working, missing, skipped, version)
    say()

    if not missing:
        say("Everything is installed and loads.")
        if skipped:
            say("The Polypaint bake needs Python 3.10 or newer: install Python 3.12 from")
            say("https://www.python.org/downloads/ to use it. Everything else works.")
        return 0

    command = pip_command(missing)
    if args.check:
        say("To install the missing ones:")
        say(f"    {quoted(command)}")
        return 1 if any(p.needed for p in missing) else 0
    if args.dry_run:
        say(quoted(command))
        return 0

    if not pip_available():
        say("pip is not available in this Python. Repair it with:")
        say(f'    "{sys.executable}" -m ensurepip --upgrade')
        say("then run this again.")
        return 1

    say(f"Installing {', '.join(p.name for p in missing)} (this can take a few minutes)...")
    say()
    code = subprocess.call(command)
    say()
    if code != 0:
        say(f"pip stopped with exit {code}; the lines above say why. Common causes:")
        say("  - no internet connection, or a proxy that blocks pypi.org")
        say("  - Python installed for all users: run this from an administrator window,")
        say(f'    or add --user:  {quoted(command[:4] + ["--user"] + command[4:])}')
        say("  - \"externally-managed-environment\" (Linux/macOS system Python): make a")
        say("    virtual environment first:  python3 -m venv .venv  then run this with")
        say("    .venv/bin/python (the .bat files use .venv by themselves)")

    still = []
    for package in missing:
        ok, why = loads(package.module)
        if not ok:
            still.append((package, why))
    if still:
        say("These still do not load:")
        for package, why in still:
            say(f"  {package.name}: {why}")
        return 1
    say("Done: everything is installed and loads.")
    if skipped:
        say("(The Polypaint bake needs Python 3.10 or newer; this Python is older.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
