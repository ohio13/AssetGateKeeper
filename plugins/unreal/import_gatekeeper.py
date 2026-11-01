"""
AssetGatekeeper — blocking import wrapper for Unreal
====================================================

The drag-and-drop path can only be *warned* about from Python (see
``init_unreal.py``). This module is the hard gate for everything a build engineer
controls: batch imports, nightly asset syncs, custom import toolbar buttons.

    import unreal
    from import_gatekeeper import import_with_gatekeeper, GatekeeperBlocked

    tasks = [ ... unreal.AssetImportTask() objects ... ]
    try:
        import_with_gatekeeper(tasks)      # validates first, then imports
    except GatekeeperBlocked as blocked:
        unreal.log_error(f"{len(blocked.problems)} file(s) rejected")

Validation is delegated to the CLI engine for every source type it measures
(FBX, OBJ, glTF, GLB, PNG, TGA, JPG), so there is exactly one implementation of the rules — the same one the
artist's watch folder and CI use.

Configuration
-------------
``ASSETGATEKEEPER_PYTHON``  interpreter with trimesh + Pillow
``ASSETGATEKEEPER_HOME``    folder containing asset_gatekeeper.py
``ASSETGATEKEEPER_RULES``   optional explicit rules.toml
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import unreal

ENV_PYTHON = "ASSETGATEKEEPER_PYTHON"
ENV_ENGINE = "ASSETGATEKEEPER_HOME"
ENV_RULES = "ASSETGATEKEEPER_RULES"

_warned = {"misconfigured": False}

#: Source formats the gate measures. Anything else (audio, .uasset, USD, ...) is
#: not this module's business and imports exactly as before.
GATED_SUFFIXES = (".obj", ".gltf", ".glb", ".fbx", ".png", ".tga", ".jpg", ".jpeg")


class GatekeeperBlocked(RuntimeError):
    """Raised instead of importing files that violate the budget."""

    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def _engine_command(path):
    python_exe = os.environ.get(ENV_PYTHON)
    home = os.environ.get(ENV_ENGINE)
    if not python_exe or not home:
        if not _warned["misconfigured"]:
            unreal.log_warning(
                "[AssetGatekeeper] ASSETGATEKEEPER_PYTHON / ASSETGATEKEEPER_HOME are "
                "not set: gated imports are blocked until they are.")
            _warned["misconfigured"] = True
        return "AssetGatekeeper is not configured (set ASSETGATEKEEPER_PYTHON and " \
               "ASSETGATEKEEPER_HOME)"

    engine = os.path.join(home, "asset_gatekeeper.py")
    if not os.path.isfile(engine):
        unreal.log_error(f"[AssetGatekeeper] engine missing: {engine}")
        return f"AssetGatekeeper engine missing: {engine}"

    command = [python_exe, engine, path, "--json"]
    rules = os.environ.get(ENV_RULES)
    if rules:
        command += ["--rules", rules]
    return command


#: Variables that point a child interpreter at *some other* Python install.
#: Inherited by the gate's interpreter (a different version), they make it load
#: the wrong standard library and die before measuring anything.
_INHERITED_PYTHON_VARS = ("PYTHONHOME", "PYTHONEXECUTABLE", "PYTHONSTARTUP",
                          "__PYVENV_LAUNCHER__")


def _under(path, roots):
    try:
        candidate = os.path.normcase(os.path.abspath(path))
    except (TypeError, ValueError):
        return False
    return any(candidate == root or candidate.startswith(root + os.sep) for root in roots)


def child_environment(environ=None):
    """The environment for the gate's own interpreter.

    The gate runs in the studio's Python (``ASSETGATEKEEPER_PYTHON``). Whatever
    launched the editor - a pipeline launcher, another DCC - may have left
    PYTHONHOME pointing at *its* Python. Verified in UE 5.3.2 with the editor
    started from Houdini 22: the inherited ``PYTHONHOME=<houdini>/python313`` made
    every gated import fail with ``AssertionError: SRE module mismatch`` (a 3.11
    interpreter loading a 3.13 standard library), so good assets were blocked
    along with bad ones. Interpreter variables and PYTHONPATH entries inside
    Unreal's own Python install are dropped; the studio's own PYTHONPATH entries
    (site-packages with trimesh, say) are kept.
    """
    env = dict(os.environ if environ is None else environ)
    for key in _INHERITED_PYTHON_VARS:
        env.pop(key, None)
    roots = {os.path.normcase(os.path.abspath(root))
             for root in (sys.prefix, sys.base_prefix, sys.exec_prefix) if root}
    kept = [entry for entry in env.get("PYTHONPATH", "").split(os.pathsep)
            if entry and not _under(entry, roots)]
    if kept:
        env["PYTHONPATH"] = os.pathsep.join(kept)
    else:
        env.pop("PYTHONPATH", None)
    return env


def engine_problems(path):
    """Run the CLI engine against one source file and return its problems.

    Fails closed: this is the *blocking* import path, so "the gate could not say"
    blocks the file with the reason, instead of importing it unchecked. (It used
    to return no problems for a missing engine, a crash, or a format the engine
    skipped - which is how every .fbx went through unvalidated.)
    """
    name = os.path.basename(path)
    command = _engine_command(path)
    if isinstance(command, str):
        return [f"{name}: {command}"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=180,
                                   env=child_environment())
    except (OSError, subprocess.SubprocessError) as error:
        unreal.log_error(f"[AssetGatekeeper] engine failed to run: {error}")
        return [f"{name}: the gate could not run ({error})"]

    for line in reversed(completed.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "problems" in payload:
                return [f"{name}: {problem}" for problem in payload.get("problems", [])]
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    return [f"{name}: the gate gave no verdict (exit {completed.returncode})"
            + (f": {detail[-1][:200]}" if detail else "")]


def validate_sources(tasks):
    """Problems across every task's source file (empty list = all clear)."""
    problems = []
    for task in tasks:
        filename = getattr(task, "filename", "") or ""
        if not filename or not filename.lower().endswith(GATED_SUFFIXES):
            continue
        if not os.path.isfile(filename):
            problems.append(f"{os.path.basename(filename)}: source file not found")
            continue
        problems.extend(engine_problems(filename))
    return problems


def import_with_gatekeeper(tasks, rules_path=None):
    """Validate, then import. Raises ``GatekeeperBlocked`` instead of importing."""
    if rules_path:
        os.environ[ENV_RULES] = rules_path

    problems = validate_sources(tasks)
    if problems:
        unreal.log_error("[AssetGatekeeper] import blocked:")
        for problem in problems:
            unreal.log_error(f"   - {problem}")
        raise GatekeeperBlocked(problems)

    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks(tasks)
    unreal.log(f"[AssetGatekeeper] {len(tasks)} task(s) imported")
    return True
