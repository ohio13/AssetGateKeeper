#!/usr/bin/env python3
r"""Perforce server-side submit trigger for AssetGatekeeper.

Installed on the P4 server as a ``change-content`` trigger, this runs *after* the
new content has been received into the depot but *before* the submit commits.
The ``change-submit`` hook is too early: at that point the revisions exist only
in a pending changelist and ``p4 print @=<change>`` has nothing to resolve.
If any asset in the changelist blows a budget, the trigger
exits non-zero and P4V shows the reason straight to the artist - the file never
enters the depot.

Server trigger line (``p4 triggers``), one line, tabs between fields::

    AssetGatekeeper change-content //depot/... "python C:\p4\hooks\p4_pre_submit.py %changelist% %user% --port %serverport% --p4user agk-trigger --tickets C:\p4\hooks\p4tickets.txt"

The trigger runs inside the Perforce service, which has no P4PORT, P4USER or
login of its own - so without ``--port`` the trigger's own ``p4`` calls dial
the default ``perforce:1666`` and every submit is refused. ``--p4user`` and
``--tickets`` name a login (``p4 login -a`` with ``P4TICKETS`` set to that file)
that can read the depot. Each can also come from ``AGK_P4PORT``,
``AGK_P4USER``, ``AGK_P4TICKETS`` and ``AGK_P4`` (path to p4.exe).

Behaviour
    * only inspects ``add``/``edit``/``branch``/``move/add``/``integrate`` revs of
      .obj/.gltf/.glb/.png — deleted files have nothing to inspect;
    * materialises each rev with ``p4 print -q -o`` at ``@=<change>`` into a temp
      folder and gates that copy, so the check reflects the *received* content,
      not the shelf;
    * **never heals.** A trigger that rewrites files behind an artist's back is not
      a gate, it is a liability. ``--no-auto-heal`` is passed explicitly so a
      project rules.toml cannot switch it on either;
    * fails closed: any unexpected error rejects the submit rather than waving it
      through.

Exit codes: 0 pass, 1 rejected (budget or hook error), 2 configuration problem.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

ASSET_SUFFIXES = {".obj", ".gltf", ".glb", ".fbx", ".png", ".tga", ".jpg", ".jpeg"}
# Actions that put bytes into the depot. "delete" and "move/delete" carry none.
CONTENT_ACTIONS = {"add", "edit", "branch", "move/add", "integrate", "merge"}

MAX_MESSAGE = 4000   # P4V truncates anyway; keep the rejection readable


def log(message: str) -> None:
    print(message, flush=True)


# How the trigger reaches the server. Filled by configure_connection().
P4_EXE = "p4"
P4_GLOBAL: list[str] = []
P4_ENV: Optional[dict] = None


def configure_connection(port: str = "", user: str = "", tickets: str = "",
                         executable: str = "") -> None:
    """Point every p4 call at the right server and login.

    A trigger inherits the service's environment, not an artist's, so nothing
    can be assumed: the address, the user and the tickets file are passed in.
    """
    global P4_EXE, P4_GLOBAL, P4_ENV
    P4_EXE = executable or os.environ.get("AGK_P4") or "p4"
    port = port or os.environ.get("AGK_P4PORT", "")
    user = user or os.environ.get("AGK_P4USER", "")
    tickets = tickets or os.environ.get("AGK_P4TICKETS", "")
    P4_GLOBAL = []
    if port:
        P4_GLOBAL += ["-p", port]
    if user:
        P4_GLOBAL += ["-u", user]
    P4_ENV = None
    if tickets:
        P4_ENV = dict(os.environ, P4TICKETS=tickets)


def p4(*args: str, timeout: float = 120.0) -> str:
    """Run a p4 command and return stdout, raising on a non-zero exit.

    ``args`` starts with the literal ``"p4"``; it is swapped for the configured
    executable and the connection flags are inserted after it.
    """
    if args and args[0] == "p4":
        args = (P4_EXE, *P4_GLOBAL, *args[1:])
    completed = subprocess.run(args, capture_output=True, text=True, env=P4_ENV,
                               encoding="utf-8", errors="replace", timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args[:3])} failed ({completed.returncode}): "
            f"{(completed.stderr or completed.stdout).strip()[:400]}"
        )
    return completed.stdout


def changelist_files(changelist: str) -> list[tuple[str, str]]:
    """Return [(depot_path, action)] for the content-bearing files in a CL.

    ``p4 describe -s`` is the portable formulation; the text is parsed rather
    than version-pinned so this keeps working across server versions.
    """
    output = p4("p4", "describe", "-s", changelist)
    files: list[tuple[str, str]] = []
    in_files = False
    for line in output.splitlines():
        if line.startswith("Affected files ..."):
            in_files = True
            continue
        if in_files:
            stripped = line.strip()
            if stripped.startswith("Differences ..."):
                break
            # Real `p4 describe` output puts a blank line between the header and
            # the first entry; a blank line is not the end of the list.
            if not stripped:
                continue
            # "... //depot/path/file.png#3 add"
            if stripped.startswith("..."):
                body = stripped[3:].strip()
                if not body:
                    continue
                revisioned, _, action = body.rpartition(" ")
                path = revisioned.split("#")[0]
                if action and path:
                    files.append((path, action))
    return files


def fetch_revision(changelist: str, depot_path: str, destination: Path) -> None:
    """Stream a changelist rev out of the depot into ``destination``.

    ``@=<change>`` selects the revision this change received. A plain
    ``@<change>`` resolves against the change's *time* instead, which is empty in
    a pending changelist and can pick a neighbouring revision on a busy server.
    """
    p4("p4", "print", "-q", "-o", str(destination), f"{depot_path}@={changelist}")


RULES_FILENAME = "rules.toml"


def depot_rules_for(depot_path: str, destination_root: Path, cache: dict) -> Optional[Path]:
    """The nearest ``rules.toml`` above a depot file, fetched at ``#head``.

    The asset is gated from a temp copy, and the gate reads ``rules.toml`` from
    the folder it is given - which, in a temp folder, never has one. So the
    studio's budget was silently replaced by the shipped defaults at submit
    time. The project's file is fetched from the depot instead, at its
    *submitted* head revision: a rules.toml edited in the same changelist does
    not loosen its own gate.
    """
    folder = depot_path.rsplit("/", 1)[0]
    walked = []
    while folder.startswith("//") and folder.count("/") >= 2:     # up to //depot itself
        if folder in cache:
            found = cache[folder]
            break
        walked.append(folder)
        target = destination_root / "_rules" / f"{len(cache) + len(walked)}" / RULES_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            p4("p4", "print", "-q", "-o", str(target), f"{folder}/{RULES_FILENAME}#head")
        except (RuntimeError, OSError):             # no rules.toml at this level
            folder = folder.rsplit("/", 1)[0]
            continue
        if target.is_file():
            found = target
            break
        folder = folder.rsplit("/", 1)[0]
    else:
        found = None
    for item in walked:
        cache[item] = found
    return found


def gatekeeper_command() -> list[str]:
    """Resolve how to invoke the gate. Overridable for non-standard installs."""
    python = os.environ.get("AGK_PYTHON") or sys.executable
    script = os.environ.get("AGK_GATEKEEPER") or str(
        Path(__file__).resolve().parent.parent / "asset_gatekeeper.py")
    if not Path(script).is_file():
        raise RuntimeError(
            f"asset_gatekeeper.py not found at {script}; set AGK_GATEKEEPER to its path"
        )
    return [python, script]


def gate(path: Path, extra: list[str]) -> subprocess.CompletedProcess:
    """Gate one file. Single-file mode gives one verdict and one exit code."""
    command = [*gatekeeper_command(), str(path), *extra]
    return subprocess.run(command, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="p4_pre_submit.py",
        description="Reject Perforce submits whose assets exceed the studio budget.")
    parser.add_argument("changelist", help="changelist number (%%changelist%%)")
    parser.add_argument("user", nargs="?", default="", help="submitting user (%%user%%)")
    parser.add_argument("--rules", help="rules file to use instead of <asset folder>/rules.toml")
    parser.add_argument("--port", default="",
                        help="server address for the trigger's own p4 calls (%%serverport%%)")
    parser.add_argument("--p4user", default="",
                        help="Perforce user the trigger reads the depot as")
    parser.add_argument("--tickets", default="",
                        help="P4TICKETS file holding that user's ticket (p4 login -a)")
    parser.add_argument("--p4", dest="p4_exe", default="", help="path to p4 / p4.exe")
    parser.add_argument("--verbose", action="store_true")
    options = parser.parse_args(argv)
    configure_connection(options.port, options.p4user, options.tickets, options.p4_exe)

    # Built here, not inside gate(), so what a trigger passes to the gate is
    # visible in one place — and provably includes the no-heal lockdown.
    extra: list[str] = ["--no-color", "--no-auto-heal"]
    if options.rules:
        extra += ["--rules", options.rules]

    try:
        files = changelist_files(options.changelist)
    except (RuntimeError, OSError) as exc:
        # Fail closed: an unreadable changelist must not become an implicit pass.
        log(f"AssetGatekeeper: cannot read changelist {options.changelist}: {exc}")
        return 1

    candidates = [(path, action) for path, action in files
                  if action in CONTENT_ACTIONS and Path(path).suffix.lower() in ASSET_SUFFIXES]
    if not candidates:
        if options.verbose:
            log(f"AssetGatekeeper: no tracked assets in changelist {options.changelist}")
        return 0

    temp_root = Path(tempfile.mkdtemp(prefix="agk-p4-"))
    failures: list[str] = []
    rules_cache: dict = {}
    try:
        # Keep the depot's folder shape so relative texture/buffer references in a
        # .gltf still resolve when the copy is inspected.
        common = os.path.commonprefix([path for path, _ in candidates])
        common_root = common.rsplit("/", 1)[0] if "/" in common else ""

        for depot_path, action in candidates:
            relative = depot_path[len(common_root):].lstrip("/") if common_root else depot_path.lstrip("/")
            destination = temp_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                fetch_revision(options.changelist, depot_path, destination)
            except (RuntimeError, OSError) as exc:
                failures.append(f"{depot_path}: could not fetch from depot ({exc})")
                continue

            per_file = list(extra)
            if not options.rules:
                rules_file = depot_rules_for(depot_path, temp_root, rules_cache)
                if rules_file is not None:
                    per_file += ["--rules", str(rules_file)]
                elif options.verbose:
                    log(f"AssetGatekeeper: no rules.toml above {depot_path}; shipped defaults")
            completed = gate(destination, per_file)
            if completed.returncode == 0:
                if options.verbose:
                    log(f"AssetGatekeeper: PASSED {depot_path}")
                continue
            if completed.returncode == 2:
                failures.append(f"{depot_path}: gate setup error\n{completed.stderr.strip()[:400]}")
                continue
            # Exit code 1: a real budget failure. Surface the tool's own verdict line.
            verdict = ""
            for line in (completed.stdout + completed.stderr).splitlines():
                if "FAILED" in line or "PASSED" in line:
                    verdict = line.strip()
            detail = verdict or (completed.stdout + completed.stderr).strip()[:400]
            failures.append(f"{depot_path} ({action})\n    {detail}")
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    if not failures:
        return 0

    header = (f"Submit rejected by AssetGatekeeper: {len(failures)} of {len(candidates)} "
              f"asset(s) exceed the studio budget.")
    body = "\n".join(f"  - {item}" for item in failures)
    hint = ("\nNothing was healed or modified. Fix the assets and resubmit, or ask a "
            "technical lead to change rules.toml.")
    log((header + "\n" + body + hint)[:MAX_MESSAGE])
    log(f"\n(submitted by {options.user or 'unknown'}, changelist {options.changelist})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
