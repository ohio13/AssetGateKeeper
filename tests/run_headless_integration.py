#!/usr/bin/env python3
"""
Headless DCC integration harness for AssetGatekeeper
====================================================

Unit tests prove the *rules* are right. This harness proves the thing that
actually breaks in production: whether a **real exporter** writing a **real file**
into a watched folder gets flagged. It drives headless DCC command lines, waits for
the gatekeeper to react, and asserts the verdict — the same way a build engineer
would wire it into a nightly job.

It is deliberately standalone (no pytest, no fixtures framework): build engineers
copy it into a studio pipeline and run one command.

What it does
------------
1. Self-check: the gatekeeper runs and its dependencies import.
2. **Blender**: `blender --background --python <script>` builds a UV sphere of
   ~180,000 triangles, triangulates it, exports ``dense_blender_sphere.gltf``
   straight into the watch folder, and the harness asserts the gatekeeper fails it
   on the real watchdog path (queue + stability window, not a simulated event).
3. **Unreal**: builds the exact ``UnrealEditor-Cmd.exe -run=pythonscript`` command
   and the actor script, prints both, and runs them when you pass a ``.uproject``
   and ``--run-unreal``.
4. Writes both DCC snippets to disk so they can be pasted into CI.

Usage
-----
    # auto-detect Blender, run the real end-to-end export
    python tests/run_headless_integration.py

    # no DCC installed? prove the whole pipeline with a pure-Python 180k-tri mesh
    python tests/run_headless_integration.py --synthetic

    # point at a specific editor build, keep the artefacts, write the snippets out
    python tests/run_headless_integration.py \\
        --blender "C:\\Program Files\\Blender Foundation\\Blender 4.2\\blender.exe" --keep

    # Unreal: print the command only
    python tests/run_headless_integration.py --unreal-project "C:\\Game\\MyGame.uproject"
    # Unreal: actually execute it
    python tests/run_headless_integration.py --unreal-project "C:\\Game\\MyGame.uproject" --run-unreal

Exit codes
----------
    0  every check that ran passed (DCC not installed counts as SKIPPED, not failure)
    1  a check failed — an export was not flagged as expected
    2  environment problem (gatekeeper missing, Python lacks dependencies, bad path)

Environment variables
---------------------
    BLENDER_EXE      headless Blender executable (overridden by --blender)
    UE_EDITOR_EXE    UnrealEditor-Cmd.exe (overridden by --unreal-editor)
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEKEEPER = REPO_ROOT / "asset_gatekeeper.py"

DEFAULT_TARGET_TRIANGLES = 180_000
SPHERE_NAME = "dense_blender_sphere.gltf"
SYNTHETIC_NAME = "dense_synthetic_sphere.gltf"
UNREAL_NAME = "dense_unreal_mesh.gltf"

# The gate's own default budget; kept here so the assertion does not depend on it.
DEFAULT_TRIANGLE_BUDGET = 50_000


# --------------------------------------------------------------------------- #
# DCC snippets — the actual code that runs inside Blender / Unreal
# --------------------------------------------------------------------------- #

BLENDER_EXPORT_SCRIPT = r'''# blender_export_dense.py
# Run:  blender.exe --background --factory-startup --python blender_export_dense.py -- <outdir> <triangles>
# Creates a UV sphere of roughly <triangles> triangles and exports it as glTF.
import math
import os
import sys

import bpy

argv = sys.argv
args = argv[argv.index("--") + 1:] if "--" in argv else []
outdir = args[0] if args else "."
target = int(args[1]) if len(args) > 1 else __TARGET__

# A UV sphere triangulates to 2 * segments * (ring_count - 1) triangles, so this
# pair is the analytic solution for the target; the loop below only corrects for
# API differences between Blender versions.
segments, rings = 400, 226
tris = 0

bpy.ops.wm.read_factory_settings(use_empty=True)

for _attempt in range(4):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=segments, ring_count=rings, radius=1.0)
    obj = bpy.context.active_object
    # Triangulate and apply, so the exporter cannot hand back quads and under-count.
    modifier = obj.modifiers.new(name="triangulate", type="TRIANGULATE")
    bpy.ops.object.modifier_apply(modifier=modifier.name)
    tris = sum(len(poly.vertices) - 2 for poly in obj.data.polygons)

    if target * 0.97 <= tris <= target * 1.03:
        break

    print(f"[assetgatekeeper] {tris:,} tris at segments={segments} rings={rings}, rescaling")
    bpy.ops.object.delete(use_global=False)
    scale = math.sqrt(target / max(tris, 1))
    segments = max(8, int(round(segments * scale)))
    rings = max(6, int(round(rings * scale)))

path = os.path.join(outdir, "__NAME__")
bpy.ops.export_scene.gltf(
    filepath=path,
    export_format="GLTF_SEPARATE",   # writes .gltf + .bin, exactly like a manual export
    use_selection=False,
)
print(f"[assetgatekeeper] blender wrote {path} with {tris:,} triangles")
'''


UNREAL_EXPORT_SCRIPT = r'''# ue_export_dense.py
# Run:  UnrealEditor-Cmd.exe <Project.uproject> -run=pythonscript -script="ue_export_dense.py"
#       -unattended -nosplash -nopause -nullrhi
# Requires the "Python Editor Script Plugin" and "glTF Exporter" plugins enabled
# in the project. Exports one mesh asset into the gatekeeper's watch folder.
import unreal

ASSET_PATH = "__ASSET_PATH__"          # e.g. "/Game/QA/SM_DenseQA"
OUTPUT_DIR = r"__OUTPUT_DIR__"         # the folder AssetGatekeeper is watching
OUTPUT_NAME = "__NAME__"

mesh = unreal.load_asset(ASSET_PATH)
if mesh is None:
    raise RuntimeError(f"asset not found: {ASSET_PATH} (did you enable the exporter plugin?)")

task = unreal.AssetExportTask()
task.object = mesh
task.filename = f"{OUTPUT_DIR}\\{OUTPUT_NAME}"
task.selected = False
task.replace_identical = True
task.automated = True
task.prompt = False
task.write_empty_files = False
task.exporter = unreal.GLTFExporter()      # requires the glTF Exporter plugin

if not unreal.Exporter.run_asset_export_task(task):
    raise RuntimeError(f"glTF export failed for {ASSET_PATH}")

unreal.log(f"[assetgatekeeper] unreal wrote {task.filename}")
'''


def render_snippets(outdir: Path, asset_path: str = "/Game/QA/SM_DenseQA") -> dict[str, Path]:
    """Write both DCC scripts to disk with this machine's paths baked in."""
    outdir.mkdir(parents=True, exist_ok=True)
    blender_path = outdir / "blender_export_dense.py"
    unreal_path = outdir / "ue_export_dense.py"

    blender_path.write_text(
        BLENDER_EXPORT_SCRIPT.replace("__TARGET__", str(DEFAULT_TARGET_TRIANGLES))
                              .replace("__NAME__", SPHERE_NAME),
        encoding="utf-8",
    )
    unreal_path.write_text(
        UNREAL_EXPORT_SCRIPT.replace("__ASSET_PATH__", asset_path)
                            .replace("__OUTPUT_DIR__", str(outdir.parent))
                            .replace("__NAME__", UNREAL_NAME),
        encoding="utf-8",
    )
    return {"blender": blender_path, "unreal": unreal_path}


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def find_blender(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    env = os.environ.get("BLENDER_EXE")
    if env and Path(env).is_file():
        return Path(env)

    found = shutil.which("blender")
    if found:
        return Path(found)

    patterns = (
        r"C:\Program Files\Blender Foundation\Blender*\blender.exe",
        "/Applications/Blender.app/Contents/MacOS/Blender",
        "/snap/bin/blender",
        "/usr/bin/blender",
        "/usr/local/bin/blender",
        str(Path.home() / "blender*" / "blender"),
    )
    for pattern in patterns:
        hits = sorted(glob.glob(pattern), reverse=True)  # newest version first
        if hits:
            return Path(hits[0])
    return None


def find_unreal_editor(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    env = os.environ.get("UE_EDITOR_EXE")
    if env and Path(env).is_file():
        return Path(env)

    patterns = (
        r"C:\Program Files\Epic Games\UE_*\Engine\Binaries\Win64\UnrealEditor-Cmd.exe",
        "/Users/Shared/Epic Games/UE_*/Engine/Binaries/Mac/UnrealEditor-Cmd",
        str(Path.home() / "UnrealEngine/Engine/Binaries/Linux/UnrealEditor-Cmd"),
    )
    for pattern in patterns:
        hits = sorted(glob.glob(pattern), reverse=True)
        if hits:
            return Path(hits[0])
    return None


def unreal_command(editor: Path, project: Path, script: Path) -> str:
    """The exact one-liner a build engineer pastes into a nightly job."""
    return (
        f'"{editor}" "{project}" -run=pythonscript -script="{script}" '
        f'-unattended -nosplash -nopause -nullrhi'
    )


# --------------------------------------------------------------------------- #
# A dependency-free-enough synthetic sphere (for machines with no DCC)
# --------------------------------------------------------------------------- #

def _sphere_topology(target: int) -> tuple[int, int, int]:
    """Pick segments/rings so 2 * segments * (rings - 1) is as close to target as possible."""
    best = (400, 226, 2 * 400 * 225)
    for rings in range(4, 2001):
        segments = max(3, round(target / (2 * (rings - 1))))
        triangles = 2 * segments * (rings - 1)
        if abs(triangles - target) < abs(best[2] - target):
            best = (segments, rings, triangles)
            if triangles == target:
                break
    return best


def write_synthetic_sphere(path: Path, target: int = DEFAULT_TARGET_TRIANGLES) -> int:
    """Write a real indexed UV-sphere glTF with no DCC installed. Returns triangles."""
    import numpy as np

    segments, rings, triangles = _sphere_topology(target)

    latitudes = np.arange(1, rings) * np.pi / rings
    longitudes = np.arange(segments) * 2.0 * np.pi / segments
    grid = np.stack(np.meshgrid(latitudes, longitudes, indexing="ij"), axis=-1).reshape(-1, 2)

    interior = np.stack(
        (np.sin(grid[:, 0]) * np.cos(grid[:, 1]),
         np.sin(grid[:, 0]) * np.sin(grid[:, 1]),
         np.cos(grid[:, 0])),
        axis=1,
    ).astype(np.float32)

    positions = np.vstack(([0.0, 0.0, 1.0], interior, [0.0, 0.0, -1.0])).astype(np.float32)
    north, south = 0, len(positions) - 1
    ring = lambda i: 1 + i * segments  # noqa: E731

    indices: list[int] = []
    for j in range(segments):  # north cap
        indices += [north, ring(0) + j, ring(0) + (j + 1) % segments]
    for j in range(segments):  # south cap
        indices += [south, ring(rings - 2) + (j + 1) % segments, ring(rings - 2) + j]
    for i in range(rings - 2):  # quad bands -> two triangles each
        for j in range(segments):
            a, b = ring(i) + j, ring(i) + (j + 1) % segments
            c, d = ring(i + 1) + j, ring(i + 1) + (j + 1) % segments
            indices += [a, c, d, a, d, b]

    index_array = np.asarray(indices, dtype=np.uint32)
    assert len(index_array) == triangles * 3, (len(index_array) // 3, triangles)

    pos_bytes, idx_bytes = positions.tobytes(), index_array.tobytes()
    payload = pos_bytes + idx_bytes
    document = {
        "asset": {"version": "2.0", "generator": "AssetGatekeeper synthetic sphere"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(positions), "type": "VEC3",
             "min": positions.min(axis=0).tolist(), "max": positions.max(axis=0).tolist()},
            {"bufferView": 1, "componentType": 5125, "count": len(index_array), "type": "SCALAR"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bytes)},
            {"buffer": 0, "byteOffset": len(pos_bytes), "byteLength": len(idx_bytes)},
        ],
        "buffers": [{"byteLength": len(payload),
                     "uri": "data:application/octet-stream;base64," + base64.b64encode(payload).decode()}],
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return triangles


# --------------------------------------------------------------------------- #
# Driving the gatekeeper in watch mode
# --------------------------------------------------------------------------- #

class WatchSession:
    """Runs ``asset_gatekeeper.py <folder>`` and collects its stdout."""

    def __init__(self, python: Path, folder: Path) -> None:
        self.python = python
        self.folder = folder
        self.lines: list[str] = []
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen[str]] = None
        self._reader: Optional[threading.Thread] = None

    def __enter__(self) -> "WatchSession":
        self._process = subprocess.Popen(
            [str(self.python), str(GATEKEEPER), str(self.folder), "--no-color"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        return self

    def _pump(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            with self._lock:
                self.lines.append(line.rstrip())
            print(f"    | {line.rstrip()}")

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.lines)

    def wait_for(self, pattern: str, timeout: float) -> Optional[str]:
        """Return the first line matching ``pattern``, or None on timeout."""
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            lines = self.snapshot()
            for line in lines[seen:]:
                if re.search(pattern, line):
                    return line
            seen = len(lines)
            if self._process is not None and self._process.poll() is not None:
                return None
            time.sleep(0.2)
        return None

    def __exit__(self, *exc: object) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._reader is not None:
            self._reader.join(timeout=5)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

@dataclass
class Result:
    name: str
    status: str          # PASSED / FAILED / SKIPPED
    detail: str = ""
    extra: dict[str, str] = field(default_factory=dict)


def _argv_for(executable: Path, args: Sequence[str]) -> list[str]:
    """Run .cmd/.bat through cmd.exe — CreateProcess cannot exec them directly."""
    if executable.suffix.lower() in {".cmd", ".bat"}:
        return ["cmd", "/c", str(executable), *args]
    return [str(executable), *args]


def _run(argv: Sequence[str], timeout: float, echo: bool = False) -> subprocess.CompletedProcess:
    completed = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout)
    if echo and completed.stdout:
        for line in completed.stdout.splitlines()[-15:]:
            print(f"    | {line}")
    return completed


def check_environment(python: Path) -> Result:
    print("[1/4] environment")
    if not GATEKEEPER.is_file():
        return Result("gatekeeper script", "FAILED", f"missing: {GATEKEEPER}")
    probe = _run([str(python), "-c",
                  "import watchdog, trimesh, PIL; print('deps ok')"], timeout=120)
    if probe.returncode != 0:
        return Result("gatekeeper script", "FAILED",
                      f"{python} cannot import dependencies: {probe.stderr.strip()[:160]}")
    print(f"    python : {python}")
    print(f"    gate   : {GATEKEEPER}")
    return Result("gatekeeper script", "PASSED", f"{python.name}, dependencies importable")


def check_blender(python: Path, blender: Optional[Path], workdir: Path,
                  target: int, timeout: float, synthetic: bool) -> Result:
    name = "synthetic 180k export" if synthetic else "blender headless export"
    if synthetic:
        print("[2/4] synthetic export (no DCC)")
    else:
        print("[2/4] blender headless export")

    asset_name = SYNTHETIC_NAME if synthetic else SPHERE_NAME
    asset_path = workdir / asset_name

    if not synthetic and blender is None:
        print("    SKIPPED: no Blender found.")
        print("    Install Blender, or pass --blender <path>, or run --synthetic to test")
        print("    the pipeline without a DCC. Set BLENDER_EXE to pin a version.")
        return Result(name, "SKIPPED", "Blender not found; use --blender or --synthetic")

    with WatchSession(python, workdir) as watch:
        if watch.wait_for(r"watching ", timeout=60) is None:
            return Result(name, "FAILED", "gatekeeper watcher never started")

        if synthetic:
            started = time.monotonic()
            triangles = write_synthetic_sphere(asset_path, target)
            print(f"    wrote {asset_name} ({triangles:,} tris) in "
                  f"{time.monotonic() - started:.1f}s")
        else:
            assert blender is not None
            snippet = render_snippets(workdir / "dcc_snippets")["blender"]
            argv = _argv_for(blender, ["--background", "--factory-startup",
                                       "--python", str(snippet),
                                       "--", str(workdir), str(target)])
            print(f"    {' '.join(str(p) for p in argv)}")
            completed = _run(argv, timeout=timeout, echo=True)
            if completed.returncode != 0:
                return Result(name, "FAILED",
                              f"blender exited {completed.returncode}: "
                              f"{(completed.stderr or '').strip()[-200:]}")
            if not asset_path.is_file():
                return Result(name, "FAILED", f"blender did not write {asset_path}")

        line = watch.wait_for(re.escape(asset_name), timeout=timeout)
        if line is None:
            return Result(name, "FAILED", f"gatekeeper never reported {asset_name}")

    match = re.search(r"([\d,]+) tris", line)
    reported = int(match.group(1).replace(",", "")) if match else -1

    ok = "FAILED" in line and reported >= DEFAULT_TRIANGLE_BUDGET
    detail = line.strip()
    if not ok:
        detail = f"expected a FAILED verdict above {DEFAULT_TRIANGLE_BUDGET:,} tris, got: {line}"
    return Result(name, "PASSED" if ok else "FAILED", detail,
                  {"asset": asset_name, "reported": f"{reported:,}"} if reported > 0 else {})


def check_unreal(workdir: Path, editor: Optional[Path], project: Optional[Path],
                 run: bool, timeout: float) -> Result:
    print("[3/4] unreal headless export")
    script = render_snippets(workdir / "dcc_snippets", asset_path="/Game/QA/SM_DenseQA")["unreal"]

    if editor is None or project is None:
        print("    command (dry run):")
        print(f'    "C:\\Program Files\\Epic Games\\UE_5.4\\Engine\\Binaries\\Win64\\UnrealEditor-Cmd.exe" '
              f'"C:\\Game\\MyGame.uproject" -run=pythonscript -script="{script}" '
              f'-unattended -nosplash -nopause -nullrhi')
        print("    SKIPPED: pass --unreal-project <file.uproject> (and --run-unreal to execute)")
        return Result("unreal headless export", "SKIPPED", f"script written to {script}")

    command = unreal_command(editor, project, script)
    print(f"    {command}")
    if not run:
        return Result("unreal headless export", "SKIPPED", "command printed (add --run-unreal)")

    completed = _run(_argv_for(editor, [str(project), "-run=pythonscript",
                                        f"-script={script}", "-unattended", "-nosplash",
                                        "-nopause", "-nullrhi"]),
                     timeout=timeout, echo=True)
    if completed.returncode != 0:
        return Result("unreal headless export", "FAILED",
                      f"editor exited {completed.returncode}")
    written = workdir / UNREAL_NAME
    if not written.is_file():
        return Result("unreal headless export", "FAILED", f"no {UNREAL_NAME} produced")
    return Result("unreal headless export", "PASSED", f"{UNREAL_NAME} written")


def check_verdicts(python: Path, workdir: Path, expected: Path) -> Result:
    print("[4/4] --once sweep re-run (CI path)")
    if not expected.is_file():
        print(f"    SKIPPED: no {expected.name} to re-check")
        return Result("--once CI sweep", "SKIPPED", "no dense export was produced")
    completed = _run([str(python), str(GATEKEEPER), str(workdir), "--once", "--no-color"],
                     timeout=300)
    tail = [line for line in completed.stdout.splitlines() if line.strip()][-4:]
    for line in tail:
        print(f"    | {line}")
    if completed.returncode == 0:
        return Result("--once CI sweep", "FAILED",
                      "exit 0, but a dense export was in the folder")
    return Result("--once CI sweep", "PASSED", f"exit {completed.returncode} (failures detected)")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drive headless Blender/Unreal exports through AssetGatekeeper.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Snippets for both DCCs are written to <workdir>/dcc_snippets/.",
    )
    parser.add_argument("--blender", help="path to blender executable (else auto-detect)")
    parser.add_argument("--unreal-editor", help="path to UnrealEditor-Cmd.exe")
    parser.add_argument("--unreal-project", help="path to the .uproject to open")
    parser.add_argument("--run-unreal", action="store_true", help="actually execute the editor")
    parser.add_argument("--synthetic", action="store_true",
                        help="skip the DCC and generate a 180k-tri sphere in pure Python")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET_TRIANGLES,
                        help="triangle count for the dense asset (default: %(default)s)")
    parser.add_argument("--workdir", help="watch folder to use (default: scratch/headless_<ts>)")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter that has watchdog+trimesh+Pillow (default: this one)")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="seconds to wait for one DCC export (default: %(default)s)")
    parser.add_argument("--keep", action="store_true", help="keep the watch folder afterwards")
    args = parser.parse_args(argv)

    python = Path(args.python)
    workdir = Path(args.workdir) if args.workdir else REPO_ROOT / "scratch" / f"headless_{int(time.time())}"
    workdir.mkdir(parents=True, exist_ok=True)

    if not python.is_file():
        print(f"error: interpreter not found: {python}", file=sys.stderr)
        return 2

    results: list[Result] = [check_environment(python)]
    if results[0].status == "FAILED":
        print(f"\nenvironment error: {results[0].detail}", file=sys.stderr)
        return 2

    blender = None if args.synthetic else find_blender(args.blender)
    results.append(check_blender(python, blender, workdir, args.target, args.timeout, args.synthetic))
    results.append(check_unreal(workdir,
                                find_unreal_editor(args.unreal_editor),
                                Path(args.unreal_project) if args.unreal_project else None,
                                args.run_unreal, args.timeout))
    results.append(check_verdicts(python, workdir,
                                  workdir / (SYNTHETIC_NAME if args.synthetic else SPHERE_NAME)))

    print("\n" + "=" * 78)
    for result in results:
        print(f"  {result.status:<7} {result.name:<28} {result.detail}")
        for key, value in result.extra.items():
            print(f"          {key}: {value}")
    print("=" * 78)

    failed = [r for r in results if r.status == "FAILED"]
    skipped = [r for r in results if r.status == "SKIPPED"]
    print(f"{len(results) - len(failed) - len(skipped)} passed, {len(failed)} failed, "
          f"{len(skipped)} skipped")

    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"artefacts kept in {workdir}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
