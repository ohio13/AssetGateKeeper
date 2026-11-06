"""Regression tests for what the 6 Nov real-world model test found.

460 public models (Khronos glTF samples, three.js and assimp test files) were
run through the gate and compared with an independent reader. Each check here
rebuilds the smallest file that shows one of the problems found, so the suite
needs no third-party files.

Run: python tests/test_realworld.py
"""
import base64
import io
import json
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asset_gatekeeper as agk  # noqa: E402

try:
    from PIL import Image
except ImportError:                        # pragma: no cover - Pillow ships with the gate
    Image = None

ROOT = Path(__file__).resolve().parent.parent
failures = []


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not condition:
        failures.append(label)


WORK = Path(tempfile.mkdtemp(prefix="agk_realworld_"))


# --- builders --------------------------------------------------------------- #

def gltf_document(positions, indices=None, mode=None, nodes=None, images=None, extensions=None,
                  extra_meshes=0):
    """A glTF 2.0 document with one mesh (optionally repeated by several nodes)."""
    pos = struct.pack(f"<{len(positions) * 3}f", *[c for p in positions for c in p])
    blob = pos
    views = [{"buffer": 0, "byteOffset": 0, "byteLength": len(pos)}]
    accessors = [{"bufferView": 0, "componentType": 5126, "count": len(positions), "type": "VEC3",
                  "min": [min(p[i] for p in positions) for i in range(3)],
                  "max": [max(p[i] for p in positions) for i in range(3)]}]
    primitive = {"attributes": {"POSITION": 0}}
    if indices is not None:
        idx = struct.pack(f"<{len(indices)}I", *indices)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(idx)})
        blob += idx
        accessors.append({"bufferView": 1, "componentType": 5125, "count": len(indices),
                          "type": "SCALAR"})
        primitive["indices"] = 1
    if mode is not None:
        primitive["mode"] = mode
    meshes = [{"primitives": [primitive]}] + [{"primitives": [dict(primitive)]}] * extra_meshes
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes or [{"mesh": 0}])))}],
        "nodes": nodes or [{"mesh": 0}],
        "meshes": meshes,
        "buffers": [{"byteLength": len(blob)}],
        "bufferViews": views,
        "accessors": accessors,
    }
    if images:
        document["images"] = images
    if extensions:
        document["extensionsUsed"] = list(extensions)
    return document, blob


def write_gltf(path, document, blob):
    document = json.loads(json.dumps(document))
    document["buffers"][0]["uri"] = ("data:application/octet-stream;base64,"
                                     + base64.b64encode(blob).decode())
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def write_glb(path, document, blob, json_bytes=None):
    body = json_bytes if json_bytes is not None else json.dumps(document).encode()
    body += b" " * (-len(body) % 4)
    blob += b"\0" * (-len(blob) % 4)
    out = struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(body) + 8 + len(blob))
    out += struct.pack("<II", len(body), 0x4E4F534A) + body
    out += struct.pack("<II", len(blob), 0x004E4942) + blob
    path.write_bytes(out)
    return path


def png_bytes(width, height):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (90, 90, 90)).save(buffer, "PNG")
    return buffer.getvalue()


TRIANGLE = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]


def run_gate(*args):
    return subprocess.run([sys.executable, str(ROOT / "asset_gatekeeper.py"), *map(str, args),
                           "--no-color", "--no-cache"], capture_output=True, text=True,
                          cwd=str(WORK), timeout=300)


try:
    print("1. One damaged file never stops a folder check")
    folder = WORK / "crash"
    folder.mkdir()
    document, blob = gltf_document(TRIANGLE * 8, indices=[0, 1, 65535, 3, 4, 5])
    write_gltf(folder / "SM_IndexOutOfRange.gltf", document, blob)
    document, blob = gltf_document(TRIANGLE, indices=[0, 1, 2])
    write_gltf(folder / "SM_Good.gltf", document, blob)
    write_glb(folder / "SM_BadText.glb", document, blob, json_bytes=b'{"asset": "\xff\xfe"}')
    for jobs in ("1", "2"):
        done = run_gate(folder, "--once", "--jobs", jobs)
        text = done.stdout + done.stderr
        expect(f"--jobs {jobs}: no traceback", "Traceback" not in text, text[-300:])
        expect(f"--jobs {jobs}: the good file is still reported", "PASSED  SM_Good.gltf" in text,
               text[-300:])
        expect(f"--jobs {jobs}: all three files reported", "3 asset(s)" in text, text[-200:])
    verdict = agk.Gatekeeper(agk.Budget()).check(folder / "SM_IndexOutOfRange.gltf")
    expect("out-of-range index explains itself",
           any("damaged" in p for p in verdict.problems), verdict.problems)
    verdict = agk.Gatekeeper(agk.Budget()).check(folder / "SM_BadText.glb")
    expect("corrupt GLB text is a normal failure, not an exception",
           not verdict.passed and any("damaged glTF" in p for p in verdict.problems),
           verdict.problems)

    class Boom(agk.Gatekeeper):
        def _inspect(self, path):
            raise RuntimeError("")
    verdict = Boom(agk.Budget()).check(folder / "SM_Good.gltf")
    expect("any unexpected error becomes an ERROR verdict with a readable reason",
           verdict.unreadable and verdict.problems == ("could not be checked (RuntimeError)",),
           verdict.problems)

    print("2. glTF triangles are counted from the file, strips and fans included")
    quad = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
    for mode, expected in ((4, 1), (5, 2), (6, 2), (1, 0), (0, 0)):
        document, blob = gltf_document(quad, indices=[0, 1, 2, 3][:3 if mode == 4 else 4], mode=mode)
        path = write_gltf(WORK / f"mode{mode}.gltf", document, blob)
        expect(f"mode {mode} counts {expected}", agk.count_triangles(path) == expected,
               agk.count_triangles(path))
    document, blob = gltf_document(quad, indices=[0, 1, 2, 3], mode=6)
    verdict = agk.Gatekeeper(agk.Budget()).check(write_gltf(WORK / "SM_Fan.gltf", document, blob))
    expect("a fan-only mesh is not called empty",
           not any("no triangles" in p for p in verdict.problems), verdict.problems)
    nodes = [{"mesh": 0}, {"mesh": 0, "translation": [2, 0, 0]}, {"mesh": 0, "translation": [4, 0, 0]}]
    document, blob = gltf_document(TRIANGLE, indices=[0, 1, 2], nodes=nodes)
    path = write_gltf(WORK / "SM_Repeated.gltf", document, blob)
    unique, drawn = agk.gltf_triangle_counts(json.loads(path.read_text()))
    expect("a mesh placed three times counts once against the budget", (unique, drawn) == (1, 3),
           (unique, drawn))
    stats = dict(agk.Gatekeeper(agk.Budget()).check(path).stats)
    expect("the drawn total is kept in the stats", stats.get("drawn_triangles") == 3, stats)
    saved = agk.trimesh
    agk.trimesh = None
    try:
        expect("the count does not depend on trimesh", agk.count_triangles(path) == 1)
    finally:
        agk.trimesh = saved
    cyclic = dict(document, nodes=[{"mesh": 0, "children": [1]}, {"children": [0]}],
                  scenes=[{"nodes": [0]}])
    expect("a cyclic node graph does not loop", agk.gltf_triangle_counts(cyclic) == (1, 1))
    old = {"asset": {"version": "1.0"}, "meshes": {"m": {}}}
    old_path = WORK / "SM_Old.gltf"
    old_path.write_text(json.dumps(old), encoding="utf-8")
    verdict = agk.Gatekeeper(agk.Budget()).check(old_path)
    expect("glTF 1.0 says what to do", any("re-export it as glTF 2.0" in p for p in verdict.problems),
           verdict.problems)

    print("3. Draco/meshopt that cannot be decoded skips shape checks instead of failing them")
    flat = [(0, 0, 0), (1, 0, 0), (2, 0, 0)]              # a genuinely flat triangle
    document, blob = gltf_document(flat, indices=[0, 1, 2],
                                   extensions=["KHR_draco_mesh_compression"])
    path = write_gltf(WORK / "SM_Draco.gltf", document, blob)
    with patch.object(agk, "_dracopy_available", lambda: False):
        verdict = agk.Gatekeeper(agk.Budget()).check(path)
    expect("no DracoPy: no degenerate failure", not any("degenerate" in p for p in verdict.problems),
           verdict.problems)
    expect("no DracoPy: a warning says how to fix it",
           any("pip install DracoPy" in w for w in verdict.warnings), verdict.warnings)
    with patch.object(agk, "_dracopy_available", lambda: True):
        verdict = agk.Gatekeeper(agk.Budget()).check(path)
    expect("with DracoPy the shape checks run", any("degenerate" in p for p in verdict.problems),
           verdict.problems)
    document["extensionsRequired"] = ["EXT_meshopt_compression"]
    document["extensionsUsed"] = ["EXT_meshopt_compression"]
    verdict = agk.Gatekeeper(agk.Budget()).check(write_gltf(WORK / "SM_Meshopt.gltf", document, blob))
    expect("meshopt: counted, shape checks skipped with a reason",
           dict(verdict.stats).get("triangles") == 1
           and any("meshopt" in w for w in verdict.warnings), (verdict.problems, verdict.warnings))
    expect("pyproject installs DracoPy", "DracoPy" in (ROOT / "pyproject.toml").read_text())

    print("4. Textures packed inside .glb / data: URIs are checked")
    if Image is not None:
        big, odd = png_bytes(4096, 16), png_bytes(100, 64)
        document, blob = gltf_document(TRIANGLE, indices=[0, 1, 2])
        views = document["bufferViews"]
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(big)})
        blob += big
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(odd)})
        blob += odd
        document["images"] = [{"bufferView": len(views) - 2, "mimeType": "image/png", "name": "Big"},
                              {"bufferView": len(views) - 1, "mimeType": "image/png"}]
        document["buffers"][0]["byteLength"] = len(blob)
        verdict = agk.Gatekeeper(agk.Budget()).check(write_glb(WORK / "SM_Packed.glb", document, blob))
        text = "; ".join(verdict.problems)
        expect("a packed 4096 texture fails the 2048 limit",
               "embedded texture 'Big' 4096x16 exceeds the 2048x2048" in text, text)
        expect("a packed 100x64 texture fails power-of-two",
               "embedded texture 'image 2 of 2' 100x64 is not a power of two" in text, text)
        expect("the stats count the packed textures", dict(verdict.stats).get("embedded_textures") == 2)
        document, blob = gltf_document(TRIANGLE, indices=[0, 1, 2],
                                       images=[{"uri": "data:image/png;base64,"
                                                + base64.b64encode(big).decode()}])
        verdict = agk.Gatekeeper(agk.Budget()).check(write_gltf(WORK / "SM_DataUri.gltf", document, blob))
        expect("a data: URI texture is checked too", any("4096x16" in p for p in verdict.problems),
               verdict.problems)
        ktx2 = b"\xabKTX 20\xbb\r\n\x1a\n" + struct.pack("<IIIII", 0, 1, 8192, 4096, 0) + b"\0" * 64
        expect("KTX2 size read from its header", agk.image_size_from_bytes(ktx2) == (8192, 4096))
    else:                                                   # pragma: no cover
        print("  skip  Pillow is not installed")

    print("5. Degenerate means flat or collapsed, whatever the scale")
    tiny = [(0.0, 0.0, 0.0), (1e-4, 0.0, 0.0), (0.0, 1e-4, 0.0)]       # 0.1 mm, well shaped
    document, blob = gltf_document(tiny, indices=[0, 1, 2])
    path = write_gltf(WORK / "SM_Tiny.gltf", document, blob)
    expect("a well-shaped 0.1 mm triangle is not degenerate", agk.count_degenerate_triangles(path) == 0)
    document, blob = gltf_document(flat, indices=[0, 1, 2])
    expect("a flat triangle is degenerate",
           agk.count_degenerate_triangles(write_gltf(WORK / "SM_Flat.gltf", document, blob)) == 1)
    document, blob = gltf_document(TRIANGLE, indices=[0, 1, 1])
    expect("a repeated corner is degenerate",
           agk.count_degenerate_triangles(write_gltf(WORK / "SM_Repeat.gltf", document, blob)) == 1)
    sliver = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.5, 1e-9, 0.0)]
    document, blob = gltf_document(sliver, indices=[0, 1, 2])
    expect("a sliver (height a billionth of its length) is degenerate",
           agk.count_degenerate_triangles(write_gltf(WORK / "SM_Sliver.gltf", document, blob)) == 1)
    expect("degenerate_epsilon still works as an extra area floor",
           agk.count_degenerate_triangles(path, epsilon=1e-8) == 1)
    rules = WORK / "ratio.toml"
    rules.write_text("[mesh]\ndegenerate_ratio = 0.5\n", encoding="utf-8")
    expect("degenerate_ratio is read from the rules file",
           agk.load_rules(rules)[0].degenerate_ratio == 0.5)
    import healer
    obj = WORK / "SM_Heal.obj"
    obj.write_text("v 0 0 0\nv 0.0001 0 0\nv 0 0.0001 0\nv 2 0 0\nf 1 2 3\nf 1 2 4\n", encoding="utf-8")
    note = healer.strip_degenerate_obj(obj)
    expect("the healer strips what the gate flags, and only that",
           note == "Stripped 1 degenerate face" and "f 1 2 3" in obj.read_text(), (note, obj.read_text()))

    print("6. FBX that other tools read is not called corrupt")
    nodes = agk.parse_fbx_ascii(b'; FBX 7.4.0 project file\nFBXHeaderExtension:  {\n\tFBXVersion: 7400\n}\n'
                                b'Props: {\n\tP: "Original|ApplicationActiveProject", '
                                b'"KString", "", "", "D:\\"\n\tP: "Q", "a &quot;b&quot;"\n}\n')
    props = [child.props for node in nodes if node.name == "Props" for child in node.children]
    expect("a string ending in a backslash keeps its closing quote",
           props[0][-1] == "D:\\" and len(props) == 2, props)
    expect("&quot; becomes a quote", props[1][-1] == 'a "b"', props)
    expect("byte arrays of type 'c' are understood", ord("c") in agk._FBX_ARRAY_TYPES)
finally:
    shutil.rmtree(WORK, ignore_errors=True)

print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
