"""Run the gate over hundreds of public real-world models and compare its counts.

The 6 Nov 2026 audit found four wrong verdicts that no hand-made test file
showed (Draco, packed .glb textures, small-but-valid triangles, a crash on one
damaged file). This script repeats that test so a change that breaks counting on
real files is caught before a release:

    python tools/corpus_check.py                 # fetch the models, check, compare
    python tools/corpus_check.py --corpus DIR    # reuse a folder fetched earlier

It sparse-clones three public collections (about 2.5 GB): KhronosGroup's
glTF-Sample-Assets, three.js examples/models and assimp test/models. Every
mesh is checked in its own process (a crash or hang is reported, never fatal),
and glTF/GLB triangle counts are compared with an independent reader below
that honours every primitive mode. If the optional ``ufbx`` package is
installed, FBX and OBJ counts are compared with it too.

Exit status: 0 when nothing crashed, nothing timed out and every comparable
count agreed; 1 otherwise. A short report is printed and, with --json, saved.
"""
from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = (
    ("khronos", "https://github.com/KhronosGroup/glTF-Sample-Assets.git",
     ["/Models/*/glTF-Binary/*", "/Models/*/glTF/*"]),
    ("threejs", "https://github.com/mrdoob/three.js.git", ["/examples/models/"]),
    ("assimp", "https://github.com/assimp/assimp.git",
     ["/test/models/FBX/", "/test/models/OBJ/", "/test/models/glTF2/", "/test/models/glTF/"]),
)
MESHES = (".fbx", ".obj", ".gltf", ".glb")


def fetch(corpus: Path) -> None:
    corpus.mkdir(parents=True, exist_ok=True)
    for name, url, patterns in SOURCES:
        target = corpus / name
        if (target / ".git").is_dir():
            continue
        print(f"fetching {name} ...", flush=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse",
                        url, str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "sparse-checkout", "set", "--no-cone", *patterns],
                       check=True)


def gltf_document(path: Path) -> dict:
    if path.suffix.lower() == ".gltf":
        return json.loads(path.read_text(encoding="utf-8-sig"))
    blob = path.read_bytes()
    offset = 12
    while offset + 8 <= len(blob):
        length, kind = struct.unpack_from("<II", blob, offset)
        offset += 8
        if kind == 0x4E4F534A:
            return json.loads(blob[offset:offset + length])
        offset += length
    raise ValueError("no JSON chunk")


def reference_gltf(path: Path) -> int | None:
    """Unique triangles of the default scene, every primitive mode honoured.
    Written independently of the gate on purpose."""
    try:
        document = gltf_document(path)
    except Exception:
        return None
    if not isinstance(document, dict) or not isinstance(document.get("meshes", []), list):
        return None
    accessors = document.get("accessors", [])

    def triangles(primitive: dict) -> int:
        mode = primitive.get("mode", 4)
        accessor = primitive.get("indices")
        if accessor is None:
            accessor = primitive.get("attributes", {}).get("POSITION")
        if accessor is None or mode not in (4, 5, 6):
            return 0
        count = accessors[accessor].get("count", 0)
        return count // 3 if mode == 4 else max(count - 2, 0)

    per_mesh = [sum(triangles(p) for p in mesh.get("primitives", []))
                for mesh in document.get("meshes", [])]
    nodes = document.get("nodes", [])
    scenes = document.get("scenes", [])
    if not scenes:
        return sum(per_mesh)
    scene = scenes[document.get("scene", 0)] if document.get("scene", 0) < len(scenes) else scenes[0]
    used: set[int] = set()
    stack = [(root, frozenset()) for root in scene.get("nodes", [])]
    while stack:
        index, seen = stack.pop()
        if index in seen or index >= len(nodes):
            continue
        node = nodes[index]
        if "mesh" in node:
            used.add(node["mesh"])
        stack.extend((child, seen | {index}) for child in node.get("children", []))
    return sum(per_mesh[i] for i in used if i < len(per_mesh))


def reference_ufbx(path: Path) -> int | None:
    """Triangles from ufbx in a child process (its binding can crash)."""
    code = ("import sys, ufbx, os\n"
            "s = ufbx.load_file(sys.argv[1]); m = s.meshes; n = len(m); t = 0\n"
            "for k in range(n): t += m[k].num_triangles\n"
            "print(t, flush=True); os._exit(0)\n")
    try:
        done = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True,
                              text=True, timeout=60)
        return int(done.stdout.strip()) if done.stdout.strip().isdigit() else None
    except Exception:
        return None


def run_gate(path: Path, timeout: float) -> dict:
    started = time.time()
    try:
        done = subprocess.run([sys.executable, str(ROOT / "asset_gatekeeper.py"), str(path),
                               "--json", "--no-color", "--no-cache"], capture_output=True,
                              text=True, timeout=timeout, cwd=str(path.parent))
    except subprocess.TimeoutExpired:
        return {"path": str(path), "outcome": "timeout", "seconds": timeout}
    verdict = None
    for line in done.stdout.splitlines():
        if line.startswith("{"):
            try:
                verdict = json.loads(line)
            except ValueError:
                pass
    outcome = "crash" if verdict is None or "Traceback" in done.stderr else "ok"
    return {"path": str(path), "outcome": outcome, "seconds": round(time.time() - started, 2),
            "verdict": verdict, "stderr": done.stderr[-2000:] if outcome == "crash" else ""}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--corpus", type=Path, default=ROOT / ".agk" / "corpus",
                        help="where the models are (fetched there if missing)")
    parser.add_argument("--jobs", type=int, default=4, help="files checked at a time")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds per file")
    parser.add_argument("--json", type=Path, help="also save the full results here")
    args = parser.parse_args(argv)

    fetch(args.corpus)
    files = sorted(p for p in args.corpus.rglob("*")
                   if p.suffix.lower() in MESHES and ".git" not in p.parts)
    print(f"checking {len(files)} models with {args.jobs} at a time ...", flush=True)
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(lambda p: run_gate(p, args.timeout), files))

    import importlib.util
    have_ufbx = importlib.util.find_spec("ufbx") is not None
    compared = agreed = 0
    disagreements = []
    for result in results:
        path = Path(result["path"])
        stats = (result.get("verdict") or {}).get("stats", {})
        if "triangles" not in stats:
            continue
        if path.suffix.lower() in (".gltf", ".glb"):
            reference = reference_gltf(path)
        elif have_ufbx:
            reference = reference_ufbx(path)
        else:
            reference = None
        if reference is None:
            continue
        compared += 1
        if stats["triangles"] == reference:
            agreed += 1
        else:
            disagreements.append((str(path.relative_to(args.corpus)), stats["triangles"], reference))

    crashes = [r for r in results if r["outcome"] == "crash"]
    timeouts = [r for r in results if r["outcome"] == "timeout"]
    print()
    print(f"models     {len(results)}")
    print(f"crashes    {len(crashes)}")
    for result in crashes[:20]:
        print(f"           {result['path']}: {(result['stderr'].strip().splitlines() or [''])[-1][:160]}")
    print(f"timeouts   {len(timeouts)}")
    print(f"counts     {agreed} of {compared} agree with the independent reader"
          + ("" if have_ufbx else " (glTF only: pip install ufbx to compare FBX/OBJ too)"))
    for name, gate, reference in disagreements[:30]:
        print(f"           {name}: gate {gate:,}, reference {reference:,}")
    passed = sum(1 for r in results if (r.get("verdict") or {}).get("passed"))
    print(f"verdicts   {passed} passed, {len(results) - passed} failed (default rules)")
    if args.json:
        args.json.write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0 if not crashes and not timeouts and not disagreements else 1


if __name__ == "__main__":
    raise SystemExit(main())
