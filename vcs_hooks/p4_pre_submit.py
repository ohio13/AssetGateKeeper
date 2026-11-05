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
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

ASSET_SUFFIXES = {".obj", ".gltf", ".glb", ".fbx", ".png", ".tga", ".jpg", ".jpeg"}
#: Files an asset carries that the gate does not measure on their own. With
#: --require-approval they still need an approved version holding these exact
#: bytes: a changelist that only edits rock.bin changes an approved rock.gltf.
APPROVAL_ONLY_SUFFIXES = {".bin", ".mtl", ".tif", ".tiff", ".dds", ".exr", ".hdr"}
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


def changelist_files(changelist: str,
                     revisions: Optional[dict] = None) -> list[tuple[str, str]]:
    """Return [(depot_path, action)] for the content-bearing files in a CL.

    ``p4 describe -s`` is the portable formulation; the text is parsed rather
    than version-pinned so this keeps working across server versions. When
    ``revisions`` is given it is filled with ``{depot_path: "3"}`` - the
    revision each file becomes, which the Art Workstation shows as ``#3``.
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
                path, _, rev = revisioned.partition("#")
                if action and path:
                    files.append((path, action))
                    if revisions is not None and rev.isdigit():
                        revisions[path] = rev
    return files


def fetch_revision(changelist: str, depot_path: str, destination: Path) -> None:
    """Stream a changelist rev out of the depot into ``destination``.

    ``@=<change>`` selects the revision this change received. A plain
    ``@<change>`` resolves against the change's *time* instead, which is empty in
    a pending changelist and can pick a neighbouring revision on a busy server.
    """
    p4("p4", "print", "-q", "-o", str(destination), f"{depot_path}@={changelist}")


#: Actions whose revision `p4 describe` shows as the one being replaced while
#: the change is still pending (an edit of #1 lists as #1 until it commits).
_PENDING_SHOWS_OLD_REV = {"edit", "integrate", "merge"}


def submitted_revision(changelist: str, depot_path: str, described: str, action: str) -> str:
    """The revision this change creates, e.g. "2" - or "" when unknown.

    In a change-content trigger the change is still pending, and `p4 describe`
    lists an edit of #1 as "#1" (the developer, 5 Nov: the Workstation showed #1 for a
    file that became #2). `p4 files <path>@=<change>` names the new one.
    """
    try:
        output = p4("p4", "files", f"{depot_path}@={changelist}")
        first = output.strip().splitlines()[0] if output.strip() else ""
        rev = first.split("#", 1)[1].split(" ", 1)[0] if "#" in first else ""
        if rev.isdigit():
            return rev
    except (RuntimeError, OSError, IndexError):
        pass
    # Without `p4 files`: an add/branch is #1 either way; an edit's number is
    # not known for sure, and a wrong number is worse than none.
    return described if action not in _PENDING_SHOWS_OLD_REV else ""


def fetch_head(depot_path: str, destination: Path) -> None:
    """A file already in the depot, at its submitted head revision."""
    p4("p4", "print", "-q", "-o", str(destination), f"{depot_path}#head")


#: Assets whose references the gate reads (an .obj's .mtl and its maps, a
#: .gltf's buffers and images). FBX texture paths are usually absolute DCC
#: paths that no depot folder can supply, so they are left to the gate.
REFERENCING_SUFFIXES = {".obj", ".gltf"}


_GATE_MODULE: dict = {}


def _gate_module():
    """The gate itself, for its reference parser - or None if it cannot load."""
    try:
        script = Path(gatekeeper_command()[1]).resolve()
    except Exception:            # the gate run itself will report a broken install
        return None
    if script in _GATE_MODULE:
        return _GATE_MODULE[script]
    module = None
    name = "agk_gate_for_p4"
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, script)
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            # Registered before it runs: its dataclasses look themselves up there.
            sys.modules[name] = module
            spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        module = None
    _GATE_MODULE[script] = module
    return module


def depot_path_of(local: Path, temp_root: Path) -> Optional[str]:
    """The depot path a temp copy stands for, or None outside the depot layout."""
    try:
        relative = Path(os.path.normpath(local)).relative_to(temp_root)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) < 2 or parts[0] in ("_rules",):
        return None
    return "//" + "/".join(parts)


def fetch_references(meshes: list[Path], temp_root: Path, verbose: bool = False) -> list[str]:
    """Bring in what the changelist's meshes point at but do not carry.

    Every file of the changelist is fetched first, so a texture submitted with
    its .obj is next to it before the .obj is gated (it used to be gated the
    moment it was fetched, before its texture: the developer's 4 Nov submit of an .obj,
    .mtl and texture was refused for a "missing reference" to the texture in
    the same changelist). A reference the changelist does not carry - the
    texture submitted last week - is fetched at its depot head. One that is in
    neither stays missing, and the gate fails it: that is a real broken link.
    Two rounds, because an .mtl fetched in the first names textures of its own.
    Returns the depot paths fetched.
    """
    gate_module = _gate_module()
    if gate_module is None:
        return []
    fetched: list[str] = []
    tried: set[str] = set()
    for _round in range(2):
        wanted = []
        for mesh in meshes:
            if mesh.suffix.lower() not in REFERENCING_SUFFIXES or not mesh.is_file():
                continue
            try:
                missing = gate_module.missing_references(mesh)
            except Exception:
                continue
            for _written, target in missing:
                local = Path(os.path.normpath(target))
                depot = depot_path_of(local, temp_root)
                if depot and depot not in tried:
                    tried.add(depot)
                    wanted.append((depot, local))
        if not wanted:
            break
        for depot, local in wanted:
            local.parent.mkdir(parents=True, exist_ok=True)
            try:
                fetch_head(depot, local)
            except (RuntimeError, OSError):
                continue                       # not in the depot either: the gate says so
            if local.is_file():
                fetched.append(depot)
                if verbose:
                    log(f"AssetGatekeeper: fetched {depot}#head (referenced, not in this change)")
    return fetched


RULES_FILENAME = "rules.toml"


def depot_rules_for(depot_path: str, destination_root: Path,
                    cache: dict) -> tuple[Optional[Path], Optional[str]]:
    """The nearest ``rules.toml`` above a depot file, fetched at ``#head``.

    The asset is gated from a temp copy, and the gate reads ``rules.toml`` from
    the folder it is given - which, in a temp folder, never has one. So the
    studio's budget was silently replaced by the shipped defaults at submit
    time. The project's file is fetched from the depot instead, at its
    *submitted* head revision: a rules.toml edited in the same changelist does
    not loosen its own gate.

    Returns the local copy and the depot folder it came from: ``[[exceptions]]``
    paths are relative to that folder, and the temp copy's layout is not.
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
            found = (target, folder)
            break
        folder = folder.rsplit("/", 1)[0]
    else:
        found = (None, None)
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


def _workstation_call(base: str, token: str, path: str, data: bytes | None = None,
                      timeout: float = 15.0) -> dict:
    request = urllib.request.Request(base.rstrip("/") + path, data=data,
                                     method="POST" if data is not None else "GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


#: Perforce stores these as text and rewrites their line endings on the way
#: out: on a Windows server ``p4 print`` hands the trigger CRLF, while the file
#: the artist dropped on the Workstation (and had approved) is usually LF.
TEXT_SUFFIXES = {".obj", ".mtl", ".gltf"}


def content_variants(local: Path) -> list[bytes]:
    """The exact bytes first, then - for text assets only - the same text with
    LF and with CRLF line endings. Binary files have one form: a different
    byte is a different file."""
    data = local.read_bytes()
    variants = [data]
    if local.suffix.lower() in TEXT_SUFFIXES:
        lf = data.replace(b"\r\n", b"\n")
        crlf = lf.replace(b"\n", b"\r\n")
        for candidate in (lf, crlf):
            if candidate not in variants:
                variants.append(candidate)
    return variants


def approval_problem(base: str, token: str, local: Path) -> str:
    """Why this exact file may not be submitted yet, or "" when it is approved.

    "Exact" means the same content: for a text asset the line endings Perforce
    rewrote do not count (the developer, 5 Nov: an approved .obj and .mtl were refused
    because the trigger's copy had CRLF and the approved one LF). Fails closed:
    if the workstation cannot be asked, the submit is refused - a gate that
    waves files through when its reviewer is down is not a gate.
    """
    answer: dict = {}
    for content in content_variants(local):
        digest = hashlib.sha256(content).hexdigest()
        try:
            answer = _workstation_call(base, token, f"/api/approvals/{digest}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return f"could not ask the Art Workstation at {base} whether it is approved ({exc})"
        if answer.get("approved"):
            return ""
    return ("not approved in the Art Workstation: "
            + str(answer.get("reason") or "no approval for this exact file"))


def send_for_review(base: str, token: str, local: Path, depot_path: str,
                    changelist: str, rev: str = "",
                    sidecars: Optional[list[tuple[str, Path]]] = None) -> str:
    """Best effort: put a submitted asset in the workstation's review queue.

    ``rev`` is shown as ``//depot/...#3 @18`` (it used to be left out). The
    files a mesh references (``sidecars``: [(name as written, local copy)])
    go into the same asset, so it renders textured and its .mtl/.bin are part
    of the version a reviewer approves.
    """
    fields = {"filename": local.name, "source_path": depot_path,
              "changelist": changelist, "source": "perforce"}
    if rev:
        fields["source_rev"] = f"#{rev}"
    query = urllib.parse.urlencode(fields)
    try:
        answer = _workstation_call(base, token, f"/api/assets?{query}", data=local.read_bytes(),
                                   timeout=30)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"not sent to the Art Workstation ({exc})"
    attached = 0
    asset_id = answer.get("id")
    for written, path in (sidecars or []) if asset_id is not None else []:
        side_query = urllib.parse.urlencode({"filename": written})
        try:
            _workstation_call(base, token, f"/api/assets/{asset_id}/files?{side_query}",
                              data=path.read_bytes(), timeout=30)
            attached += 1
        except (urllib.error.URLError, OSError, ValueError):
            continue
    extra = f" with {attached} file(s) alongside" if attached else ""
    return (f"sent to the Art Workstation as {answer.get('filename')} "
            f"v{answer.get('version')}{extra}")


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
    parser.add_argument("--workstation", default=os.environ.get("AGK_WORKSTATION", ""),
                        metavar="URL",
                        help="Art Workstation to send passing assets to for review (or "
                             "AGK_WORKSTATION); never blocks a submit on its own")
    parser.add_argument("--workstation-token",
                        default=os.environ.get("AGK_WORKSTATION_TOKEN", ""),
                        help="the workstation's --token (or AGK_WORKSTATION_TOKEN)")
    parser.add_argument("--require-approval", action="store_true",
                        default=os.environ.get("AGK_REQUIRE_APPROVAL", "").strip().lower()
                        in ("1", "true", "yes", "on"),
                        help="also refuse any asset whose exact content is not Approved in "
                             "the workstation (needs --workstation)")
    parser.add_argument("--verbose", action="store_true")
    options = parser.parse_args(argv)
    if options.require_approval and not options.workstation:
        log("AssetGatekeeper: --require-approval needs --workstation (or AGK_WORKSTATION)")
        return 1
    configure_connection(options.port, options.p4user, options.tickets, options.p4_exe)

    # Built here, not inside gate(), so what a trigger passes to the gate is
    # visible in one place — and provably includes the no-heal lockdown.
    extra: list[str] = ["--no-color", "--no-auto-heal"]
    if options.rules:
        extra += ["--rules", options.rules]

    revisions: dict = {}
    try:
        files = changelist_files(options.changelist, revisions)
    except (RuntimeError, OSError) as exc:
        # Fail closed: an unreadable changelist must not become an implicit pass.
        log(f"AssetGatekeeper: cannot read changelist {options.changelist}: {exc}")
        return 1

    wanted = ASSET_SUFFIXES | (APPROVAL_ONLY_SUFFIXES if options.require_approval else set())
    candidates = [(path, action) for path, action in files
                  if action in CONTENT_ACTIONS and Path(path).suffix.lower() in wanted]
    if not candidates:
        if options.verbose:
            log(f"AssetGatekeeper: no tracked assets in changelist {options.changelist}")
        return 0

    temp_root = Path(tempfile.mkdtemp(prefix="agk-p4-"))
    failures: list[str] = []
    passed: list[tuple[Path, str]] = []
    rules_cache: dict = {}
    try:
        # Laid out exactly as in the depot (//depot/art/x.obj -> <temp>/depot/art/x.obj),
        # so relative texture/buffer references resolve, including ../ ones.
        # EVERY file is fetched before ANY is gated: gating inside this loop
        # checked an .obj before its texture had arrived (4 Nov, false refusal).
        # The .mtl/.bin a mesh names come from this changelist too, even when
        # they are not checked themselves (no --require-approval).
        checked = {path for path, _ in candidates}
        carried = [(path, action) for path, action in files
                   if action in CONTENT_ACTIONS and path not in checked
                   and Path(path).suffix.lower() in APPROVAL_ONLY_SUFFIXES]
        local_of: dict[str, Path] = {}
        for depot_path, action in candidates + carried:
            destination = temp_root / depot_path.lstrip("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                fetch_revision(options.changelist, depot_path, destination)
            except (RuntimeError, OSError) as exc:
                if depot_path in checked:
                    failures.append(f"{depot_path}: could not fetch from depot ({exc})")
                continue                       # an unchecked sidecar: the gate reports it missing
            local_of[depot_path] = destination
        fetch_references(list(local_of.values()), temp_root, options.verbose)

        for depot_path, action in candidates:
            destination = local_of.get(depot_path)
            if destination is None:
                continue                       # its fetch failure is already recorded
            if Path(depot_path).suffix.lower() not in ASSET_SUFFIXES:
                # Not measured by the gate; only its approval is in question.
                problem = approval_problem(options.workstation, options.workstation_token,
                                           destination)
                if problem:
                    failures.append(f"{depot_path} ({action})\n    {problem}")
                continue
            per_file = list(extra)
            if not options.rules:
                rules_file, rules_folder = depot_rules_for(depot_path, temp_root, rules_cache)
                if rules_file is not None:
                    per_file += ["--rules", str(rules_file)]
                    # [[exceptions]] name paths below the rules file's folder; the
                    # temp copy is laid out differently, so say where it really is.
                    per_file += ["--exception-path", depot_path[len(rules_folder):].lstrip("/")]
                elif options.verbose:
                    log(f"AssetGatekeeper: no rules.toml above {depot_path}; shipped defaults")
            completed = gate(destination, per_file)
            if completed.returncode == 0:
                if options.require_approval:
                    problem = approval_problem(options.workstation, options.workstation_token,
                                               destination)
                    if problem:
                        failures.append(f"{depot_path} ({action})\n    {problem}")
                        continue
                if options.verbose:
                    log(f"AssetGatekeeper: PASSED {depot_path}")
                passed.append((destination, depot_path))
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
        # Only a submit that will go through is queued for review. Meshes first,
        # carrying the files they reference; a texture that went in with its
        # mesh is not sent again as an asset of its own.
        if not failures and options.workstation and not options.require_approval:
            gate_module = _gate_module()
            carried: set[Path] = set()
            ordered = sorted(passed, key=lambda item: item[0].suffix.lower()
                             not in REFERENCING_SUFFIXES | {".fbx", ".glb"})
            for local, depot_path in ordered:
                if local in carried:
                    if options.verbose:
                        log(f"AssetGatekeeper: {depot_path}: sent with the mesh that uses it")
                    continue
                sidecars: list[tuple[str, Path]] = []
                if gate_module is not None and local.suffix.lower() in REFERENCING_SUFFIXES:
                    try:
                        pairs = gate_module.resolve_references(local)
                    except Exception:
                        pairs = []
                    for written, target in pairs:
                        target = Path(os.path.normpath(target))
                        if target.is_file() and depot_path_of(target, temp_root):
                            sidecars.append((written.replace("\\", "/"), target))
                            carried.add(target)
                action = dict(candidates).get(depot_path, "")
                rev = submitted_revision(options.changelist, depot_path,
                                         revisions.get(depot_path, ""), action)
                note = send_for_review(options.workstation, options.workstation_token,
                                       local, depot_path, options.changelist, rev, sidecars)
                if options.verbose:
                    log(f"AssetGatekeeper: {depot_path}: {note}")
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    if not failures:
        return 0

    header = (f"Submit rejected by AssetGatekeeper: {len(failures)} of {len(candidates)} "
              + ("asset(s) are over budget or not approved." if options.require_approval
                 else "asset(s) exceed the studio budget."))
    body = "\n".join(f"  - {item}" for item in failures)
    hint = ("\nNothing was healed or modified. Fix the assets and resubmit, or ask a "
            "technical lead to change rules.toml.")
    if options.require_approval:
        hint += ("\nApproval: upload the file to the Art Workstation and ask a reviewer "
                 "to approve that exact version.")
    log((header + "\n" + body + hint)[:MAX_MESSAGE])
    log(f"\n(submitted by {options.user or 'unknown'}, changelist {options.changelist})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
