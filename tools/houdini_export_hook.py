#!/usr/bin/env python3
r"""Houdini export hook for AssetGatekeeper (HAPI / Headless).

Runs *inside* Houdini's embedded Python — on a shelf button, from a ROP's
"Post-Render Script", or headless via ``hootl``/``hython`` — plus ad-hoc from a
terminal, where Houdini is absent and the geometry has already been exported.

Two facts drive the design:

1. **Houdini's interpreter does not have trimesh or Pillow**, and installing them
   into it is a bad idea (side effects on the studio-wide build). So verification
   shells out to a *separate* interpreter: ``AGK_PYTHON``, or ``sys.executable``
   when that already is a normal Python.
2. **A DCC must never block on a network.** Telemetry is off by default here;
   the hook only answers "did what I just wrote pass?".

Usage inside Houdini::

    import asset_gatekeeper_houdini as agk_hou
    agk_hou.verify_export(["/out/geo/monsters_boss.obj"])

Usage from a terminal::

    python tools/houdini_export_hook.py --outdir /out/geo --recursive
    python tools/houdini_export_hook.py asset.obj --notify

Install: copy to ``$HOUDINI_USER_PREF_DIR/scripts/python/asset_gatekeeper_houdini.py``,
then call it from a shelf tool or a ROP post-render script.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

TRACKED = (".obj", ".gltf", ".glb", ".fbx", ".png", ".tga", ".jpg", ".jpeg")
GATEKEEPER = Path(__file__).resolve().parent.parent / "asset_gatekeeper.py"

# Populated by install(): the gate's CLI prefix, resolved once.
_GATE: list[str] = []


def _inside_houdini() -> bool:
    try:
        __import__("hou")      # import probe only: this module runs outside Houdini too
        return True
    except Exception:
        return False


def clean_subprocess_env(extra: dict | None = None) -> dict:
    """Environment for the gate subprocess.

    A DCC owns its interpreter. Houdini (like Maya and Blender) exports PYTHONHOME
    and PYTHONPATH into every child process, so launching a *different* Python -
    the studio's AGK_PYTHON - inherits the DCC's standard library and dies with
    "SRE module mismatch" before reaching argparse. Dropping those three variables
    is what makes the shell-out portable across DCCs.
    """
    env = dict(os.environ)
    for leaked in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONEXECUTABLE"):
        env.pop(leaked, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra or {})
    return env



def python_for_gate() -> str:
    """The interpreter that can actually run the gate (has trimesh + Pillow)."""
    override = os.environ.get("AGK_PYTHON")
    if override:
        return override
    if _inside_houdini():
        # Houdini's bundled interpreter is not a safe place to run the gate.
        for candidate in ("python3", "python", "py"):
            found = shutil.which(candidate)
            if found:
                return found
        raise RuntimeError(
            "running inside Houdini and no external Python was found; "
            "set AGK_PYTHON to an interpreter with trimesh and Pillow")
    return sys.executable


def gatekeeper_command() -> list[str]:
    global _GATE
    if not _GATE:
        script = Path(os.environ.get("AGK_GATEKEEPER") or GATEKEEPER)
        if not script.is_file():
            raise RuntimeError(f"asset_gatekeeper.py not found at {script}")
        _GATE = [python_for_gate(), str(script)]
    return list(_GATE)


def verify_file(path: str | Path, extra_args: list[str] | None = None,
                timeout: float = 120.0) -> dict:
    """Gate one exported file. Returns {path, returncode, output, verdict}."""
    target = Path(path)
    if not target.is_file():
        return {"path": str(target), "returncode": 2, "output": "file not found",
                "verdict": None, "skipped": True}

    # --no-auto-heal is not negotiable in a DCC hook: the artist just wrote that
    # file, and a gate that rewrites it under them is indistinguishable from a bug.
    command = [*gatekeeper_command(), str(target), "--no-color", "--json",
               "--no-auto-heal", *(extra_args or [])]
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=timeout, encoding="utf-8",
                                   errors="replace", env=clean_subprocess_env())
    except subprocess.TimeoutExpired:
        return {"path": str(target), "returncode": 124,
                "output": f"gate timed out after {timeout:g}s", "verdict": None}
    except Exception as exc:                       # no interpreter, no script
        return {"path": str(target), "returncode": 2,
                "output": f"{type(exc).__name__}: {exc}", "verdict": None}

    verdict = None
    for line in completed.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                verdict = json.loads(line)
            except ValueError:
                pass
    return {"path": str(target), "returncode": completed.returncode,
            "output": (completed.stdout + completed.stderr).strip(), "verdict": verdict}


def verify_export(paths: list[str | Path], extra_args: list[str] | None = None,
                  notify: bool = False, raise_on_fail: bool = False) -> list[dict]:
    """Verify a batch of exports. Never raises unless `raise_on_fail` is set."""
    results = [verify_file(path, extra_args) for path in paths]
    failures = [item for item in results if item["returncode"] == 1]

    for item in results:
        verdict = item["verdict"] or {}
        status = verdict.get("status") or ("skipped" if item.get("skipped") else "error")
        name = Path(item["path"]).name
        problems = "; ".join(verdict.get("problems") or []) or item["output"][:200]
        print(f"[AssetGatekeeper] {status:<7} {name}"
              + (f"  {problems}" if status not in ("PASSED",) else ""))

    if notify and failures:
        _notify(f"AssetGatekeeper: {len(failures)} export(s) failed",
                failures[0].get("verdict", {}).get("problems", ["see console"])[0])
    if raise_on_fail and failures:
        raise SystemExit(1)
    return results


def _notify(title: str, body: str) -> None:
    """Best effort, and never fatal: a missing notifier is not an export error."""
    try:
        sys.path.insert(0, str(Path(gatekeeper_command()[1]).parent))
        import notifier
        toast = notifier.Notifier()
        toast.notify(title, body)
        toast.close(timeout=5.0)
    except Exception as exc:
        print(f"[AssetGatekeeper] toast skipped: {type(exc).__name__}: {exc}")


def install() -> None:
    """Houdini convenience: a shelf button body / ROP post-render snippet."""
    try:
        import hou
    except Exception:
        print("install() is only meaningful inside Houdini")
        return
    hou.ui.setStatusMessage("AssetGatekeeper hook ready: verify_export(paths)",
                            severity=hou.severityType.Message)


def _paths_from_arguments(options: argparse.Namespace) -> list[Path]:
    files: list[Path] = []
    for raw in options.paths:
        candidate = Path(raw)
        if candidate.is_dir():
            pattern = "**/*" if options.recursive else "*"
            files.extend(p for p in sorted(candidate.glob(pattern))
                         if p.suffix.lower() in TRACKED)
        else:
            files.append(candidate)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run AssetGatekeeper over Houdini exports.")
    parser.add_argument("paths", nargs="+", help="files or folders to verify")
    parser.add_argument("--recursive", "-r", action="store_true",
                        help="descend into subfolders")
    parser.add_argument("--notify", action="store_true",
                        help="raise a desktop toast when anything fails")
    parser.add_argument("--gate-args", default="",
                        help="extra arguments for asset_gatekeeper, space separated")
    options = parser.parse_args(argv)

    files = _paths_from_arguments(options)
    if not files:
        print("[AssetGatekeeper] nothing to verify")
        return 0
    extra = options.gate_args.split() if options.gate_args else None
    results = verify_export(files, extra, notify=options.notify)
    return 1 if any(item["returncode"] == 1 for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
