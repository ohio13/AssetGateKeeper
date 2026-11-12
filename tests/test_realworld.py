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
    expect("a mesh placed three times: 1 unique, 3 drawn", (unique, drawn) == (1, 3),
           (unique, drawn))
    stats = dict(agk.Gatekeeper(agk.Budget()).check(path).stats)
    expect("the budget counts every copy, as Unreal 5.3 imports them (CesiumMilkTruck: 3,624)",
           stats.get("triangles") == 3 and stats.get("unique_triangles") == 1, stats)
    saved = agk.trimesh
    agk.trimesh = None
    try:
        expect("the count does not depend on trimesh", agk.count_triangles(path) == 3)
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
    expect("any Draco file warns that Unreal 5.3 imports nothing from it",
           any("Unreal Engine 5.3 imports nothing" in w
               for w in agk.Gatekeeper(agk.Budget()).check(WORK / "SM_Draco.gltf").warnings))

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
        many_document, many_blob = gltf_document(TRIANGLE, indices=[0, 1, 2])
        many_views = many_document["bufferViews"]
        many_images = []
        for number in range(6):
            data = png_bytes(100, 64)
            many_views.append({"buffer": 0, "byteOffset": len(many_blob), "byteLength": len(data)})
            many_blob += data
            many_images.append({"bufferView": len(many_views) - 1, "name": f"tex{number}"})
        many_document["images"] = many_images
        many_document["buffers"][0]["byteLength"] = len(many_blob)
        verdict = agk.Gatekeeper(agk.Budget()).check(write_glb(WORK / "SM_Many.glb", many_document, many_blob))
        expect("many packed textures become one line, not one each",
               verdict.problems == ("6 embedded textures are not powers of two ('tex0' 100x64, "
                                    "'tex1' 100x64, 'tex2' 100x64 and 3 more)",), verdict.problems)
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
    flat_path = WORK / "SM_Flat.gltf"
    for mode, failed, warned in (("block", True, False), ("warn", False, True), ("allow", False, False)):
        rules.write_text(f'[mesh]\ndegenerate = "{mode}"\n', encoding="utf-8")
        verdict = agk.Gatekeeper(agk.load_rules(rules)[0]).check(flat_path)
        expect(f'degenerate = "{mode}"',
               any("degenerate" in p for p in verdict.problems) == failed
               and any("degenerate" in w for w in verdict.warnings) == warned,
               (verdict.problems, verdict.warnings))
    rules.write_text('[mesh]\ndegenerate = "sometimes"\n', encoding="utf-8")
    try:
        agk.load_rules(rules)
        expect("an unknown degenerate mode is refused", False)
    except agk.ConfigError:
        expect("an unknown degenerate mode is refused", True)
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

    print("7. Texture names: the map names real exports use pass by default")
    pattern = agk.build_naming_pattern(agk.Budget().texture_suffixes)
    for name in ("Bishop_black_base_color", "BoomBox_baseColor", "Corset_occlusionRoughnessMetallic",
                 "T_Rock_D", "rock-normal", "Lantern_emissive", "Sofa_Metallic", "crate_Albedo_02"):
        expect(f"{name} passes", bool(pattern.search(name)))
    for name in ("CesiumLogoFlat", "mosquito0", "rockAlbedo", "final_final_v2"):
        expect(f"{name} still fails", not pattern.search(name))
    narrow = agk.build_naming_pattern(("_Albedo",))
    expect("a studio list still narrows it", not narrow.search("rock_Normal") and narrow.search("rock_albedo"))
    expect("the FAILED line lists a few and counts the rest",
           agk.describe_suffixes(agk.Budget().texture_suffixes).endswith("and 22 more"))

    print("8. Mesh names: one optional rule, the same in the gate and every DCC")
    document, blob = gltf_document(TRIANGLE, indices=[0, 1, 2])
    plain = write_gltf(WORK / "Rock.gltf", document, blob)
    expect("off by default", not any("mesh name" in p for p in agk.Gatekeeper(agk.Budget()).check(plain).problems))
    rules = WORK / "names.toml"
    rules.write_text('[mesh]\nname_prefixes = ["SM_", "SK_"]\n', encoding="utf-8")
    budget = agk.load_rules(rules)[0]
    expect("read from [mesh] name_prefixes", budget.mesh_name_prefixes == ("SM_", "SK_"))
    verdict = agk.Gatekeeper(budget).check(plain)
    expect("the gate fails a file without the prefix",
           "mesh name 'Rock' does not start with SM_ or SK_" in verdict.problems, verdict.problems)
    good = write_gltf(WORK / "sm_Rock.gltf", document, blob)
    expect("case-insensitive", not any("mesh name" in p for p in agk.Gatekeeper(budget).check(good).problems))
    sys.path.insert(0, str(ROOT))
    from plugins import preflight_core
    report = preflight_core.assess(asset="x", triangles=10, materials=1, budget=budget,
                                   mesh_names=["Teapot001", "SM_Rock"])
    names = [f.message for f in report.findings if f.code == "mesh_name"]
    expect("the DCC core applies the same rule (3ds Max)",
           names == ["mesh name 'Teapot001' does not start with SM_ or SK_"], names)
    report = preflight_core.assess(asset="x", triangles=10, materials=1, budget=agk.Budget(),
                                   mesh_names=["Teapot001"])
    expect("and stays quiet when the rule is off", not report.errors)
    maya = (ROOT / "plugins" / "maya" / "gatekeeper_maya.py").read_text(encoding="utf-8")
    expect("Maya uses the shared rule, not an underscore test",
           "mesh_name_problem" in maya and 'if "_" not in name' not in maya)
    expect("Maya's dialog says 'failed the checks'", "failed the checks" in maya and "exceed the budget" not in maya)
    blender = (ROOT / "plugins" / "blender" / "addon.py").read_text(encoding="utf-8")
    expect("Blender names its temp export after the object", "def export_file_name" in blender)
    max_text = (ROOT / "plugins" / "max" / "max_preflight.py").read_text(encoding="utf-8")
    expect("3ds Max passes object names to the core", "mesh_names=names" in max_text)
    expect("3ds Max shows a warning icon when blocked", "QMessageBox.Warning" in max_text)
    expect("3ds Max can install an AGK button",
           (ROOT / "plugins" / "max" / "install_max_button.py").is_file() and "macroScript" in max_text)

    print("9. More texture formats are checked")
    expect("tif, webp, dds, exr and ktx2 are tracked",
           {".tif", ".tiff", ".webp", ".dds", ".exr", ".ktx2"} <= agk.TRACKED_SUFFIXES)
    if Image is not None:
        for suffix, image_format in ((".tif", "TIFF"), (".webp", "WEBP")):
            path = WORK / f"T_Big_Normal{suffix}"
            Image.new("RGB", (4096, 64)).save(path, image_format)
            verdict = agk.Gatekeeper(agk.Budget()).check(path)
            expect(f"{suffix} is measured", any("4096x64 exceeds" in p for p in verdict.problems),
                   verdict.problems)
    ktx2 = WORK / "T_Big_Normal.ktx2"
    ktx2.write_bytes(b"\xabKTX 20\xbb\r\n\x1a\n" + struct.pack("<IIIII", 0, 1, 8192, 8192, 0) + b"\0" * 64)
    expect("ktx2 is measured from its header",
           agk.Gatekeeper(agk.Budget()).check(ktx2).metric.startswith("8192x8192"))

    def exr_attribute(name, kind, value):
        return name + b"\0" + kind + b"\0" + struct.pack("<i", len(value)) + value
    exr = WORK / "T_Sky_Emissive.exr"
    exr.write_bytes(b"\x76\x2f\x31\x01" + struct.pack("<I", 2)
                    + exr_attribute(b"dataWindow", b"box2i", struct.pack("<4i", 0, 0, 1023, 511)) + b"\0")
    expect("exr is measured from its header", agk.measure_texture(exr) == (1024, 512))
    junk = WORK / "T_Junk_Normal.exr"
    junk.write_bytes(b"not an exr")
    expect("a damaged exr fails cleanly", not agk.Gatekeeper(agk.Budget()).check(junk).passed)

    print("10. A missing file is reported once")
    source = "".join(part.read_text(encoding="utf-8") for part in (ROOT / "agk_gate").glob("*.py"))
    expect("missing references are de-duplicated", "seen_written" in source)

    print("11. AssetHealth: earlier history can join a project, and a project can be removed")
    import server.asset_health_server as health
    db = WORK / "health.db"
    health.init_db(db)
    payload = {"file": "SM_Old.fbx", "name": "SM_Old.fbx", "passed": False, "status": "FAILED",
               "metric": "60,000 tris", "problems": ["60,000 triangles exceeds the 50,000 budget"]}
    for _ in range(3):
        health.store_verdict(db, payload)
    project_id, _token = health.create_project(db, "Trial")
    expect("results from before projects are counted", health.legacy_verdict_count(db) == 3)
    expect("a project is found by name, any case", health.find_project(db, "trial")["id"] == project_id)
    expect("--adopt-legacy moves them in", health.adopt_legacy(db, project_id) == 3
           and health.legacy_verdict_count(db) == 0)
    health.add_exception(db, project_id, {"path": "SM_*", "rule": "max_triangles", "allow": 80000,
                                          "reason": "test", "owner": "alex", "expires": "2099-01-01"})
    expect("--remove-project keeps the results as history", health.remove_project(db, project_id) == 3
           and health.project_count(db) == 0 and health.legacy_verdict_count(db) == 3)
    expect("... and drops the project's exceptions", health.list_exceptions(db, project_id) == [])
    expect("the 401 page says what to do", "needs a token" in health.UNAUTHORISED_HELP)

    print("12. PostgreSQL copies keep exceptions; a fresh schema records its migrations")
    from enterprise import store as enterprise_store
    expect("migrate --sqlite copies exceptions and project roots",
           {"exceptions", "project_roots"} <= set(enterprise_store.MIGRATION_ORDER))
    expect("a migration creates the exceptions table",
           (ROOT / "migrations" / "0003_exceptions.up.sql").is_file())
    source = WORK / "copy_source.db"
    health.init_db(source)
    project_id, _token = health.create_project(source, "Copied")
    health.add_exception(source, project_id, {"path": "SM_*", "rule": "max_triangles", "allow": 80000,
                                              "reason": "test", "owner": "alex", "expires": "2099-01-01"})
    target = WORK / "copy_target.db"
    done = subprocess.run([sys.executable, "-m", "enterprise.cli", "migrate", "--sqlite", str(source),
                           "--dsn", str(target), "--init-schema"], capture_output=True, text=True,
                          cwd=str(ROOT), timeout=300)
    expect("the exception arrives in the copy", "exceptions               1" in done.stdout, done.stdout[-400:])
    expect("--init-schema records the migrations it contains", "recorded 3 migration(s)" in done.stdout)
    plan = subprocess.run([sys.executable, "-m", "enterprise.cli", "migrate", "--dsn", str(target),
                           "--plan"], capture_output=True, text=True, cwd=str(ROOT), timeout=300)
    expect("so the first preview has nothing to apply", "nothing to apply" in plan.stdout, plan.stdout)
    fresh = WORK / "fresh.db"
    plan = subprocess.run([sys.executable, "-m", "enterprise.cli", "migrate", "--dsn", str(fresh),
                           "--plan"], capture_output=True, text=True, cwd=str(ROOT), timeout=300)
    expect("a preview says 'would apply'", plan.stdout.startswith("would apply"), plan.stdout)

    print("13. GitHub annotations without paid code scanning")
    verdict = agk.Verdict(Path("assets/SM_Rock, big.fbx"), ("60,000 triangles exceeds the 50,000 budget",))
    text = agk.github_report([verdict, agk.Verdict(Path("ok.fbx"))])
    expect("one ::error line per problem, file name escaped",
           text == "::error file=assets/SM_Rock%2C big.fbx,title=AssetGatekeeper::"
                   "60,000 triangles exceeds the 50,000 budget\n", text)
    workflow = (ROOT / ".github" / "workflows" / "asset_gatekeeper.yml").read_text(encoding="utf-8")
    expect("the workflow prints them, and uses codeql-action v4 with actions: read",
           "github:gate-annotations.txt" in workflow and "upload-sarif@v4" in workflow
           and "actions: read" in workflow and "@v3" not in workflow)

    print("14. Naming conventions: presets, several at once, custom patterns, and suggestions")
    def naming_budget(text):
        path = WORK / "naming.toml"
        path.write_text(text, encoding="utf-8")
        return agk.load_rules(path)[0]
    unreal = naming_budget('[naming]\npreset = "unreal"\n')
    expect("unreal: T_Rock_D passes", not agk.texture_name_problem("T_Rock_D", unreal))
    expect("unreal: Rock_D fails (no T_)", agk.texture_name_problem("Rock_D", unreal))
    expect("unreal: SM_Rock passes and Rock fails as a mesh",
           not agk.mesh_name_problem("SM_Rock", unreal) and agk.mesh_name_problem("Rock", unreal))
    mixed = naming_budget('[naming]\npreset = ["unreal", "gltf"]\n')
    expect("two presets: a name passes when it follows either",
           not agk.texture_name_problem("T_Rock_N", mixed)
           and not agk.texture_name_problem("Helmet_baseColor", mixed)
           and agk.texture_name_problem("Helmet_final", mixed))
    custom = naming_budget('[naming]\ntexture_pattern = "^TX_[A-Z][A-Za-z]+_(Diff|Nrm)$"\n'
                           'mesh_pattern = "^GEO_[a-z0-9_]+$"\n')
    expect("a custom texture regex", not agk.texture_name_problem("TX_Rock_Diff", custom)
           and agk.texture_name_problem("TX_rock_Diff", custom))
    expect("a custom mesh regex", not agk.mesh_name_problem("GEO_rock_01", custom)
           and agk.mesh_name_problem("GEO_Rock", custom))
    for bad, why in (('[naming]\npreset = "unrael"\n', "unknown preset"),
                     ('[naming]\ntexture_pattern = "("\n', "bad regex"),
                     ('[naming]\nprefix = ["T_"]\n', "unknown key")):
        try:
            naming_budget(bad)
            expect(f"{why} is refused", False)
        except agk.ConfigError:
            expect(f"{why} is refused", True)
    expect("without [naming] nothing changes", agk.Budget().naming_conventions == ())
    if Image is not None:
        folder = WORK / "names"
        folder.mkdir()
        for stem in ("T_Rock_D", "T_Rock_N", "T_Wall_D", "T_Wall_N", "T_Wall_Grime", "T_Rock_Grime"):
            Image.new("RGB", (64, 64), (90, 90, 90)).save(folder / f"{stem}.png")
        document, blob = gltf_document(TRIANGLE, indices=[0, 1, 2])
        for stem in ("SM_Rock", "SM_Wall", "SM_Crate"):
            write_gltf(folder / f"{stem}.gltf", document, blob)
        text = agk.suggest_naming(folder)
        expect("the suggestion picks the preset the files follow", 'preset = ["unreal"]' in text, text)
        expect("... adds endings the preset lacks", '"_Grime"' in text, text)
        expect("... and says the result passes them all",
               "6 of 6 textures and 3 of 3 meshes" in text, text)
        done = run_gate(folder, "--suggest-naming")
        expect("--suggest-naming prints the block and writes nothing",
               done.returncode == 0 and "[naming]" in done.stdout
               and not (folder / "rules.toml").exists(), done.stdout[-300:])

    print("15. ZBrush Polypaint: recognised, explained, and baked into a game asset")
    # 9 Nov 2026: a ZBrush sculpt showed blue in Houdini and grey in Maya, 3ds
    # Max, Blender and the review page. Its colour was #MRGB comment lines.

    def painted_sphere(rings=24, segments=48, style="mrgb"):
        """A sphere: red top half, blue bottom half, as ZBrush (or xyzrgb) writes it."""
        import math
        vertices, colours, faces = [], [], []
        for i in range(rings + 1):
            theta = math.pi * i / rings
            for j in range(segments):
                phi = 2 * math.pi * j / segments
                vertices.append((math.sin(theta) * math.cos(phi), math.cos(theta),
                                 math.sin(theta) * math.sin(phi)))
                colours.append((255, 0, 0) if math.cos(theta) > 0 else (0, 0, 255))
        for i in range(rings):
            for j in range(segments):
                a, b = i * segments + j, i * segments + (j + 1) % segments
                c, d = a + segments, b + segments
                faces += [(a, c, b), (b, c, d)]    # two per quad; poles collapse one
        lines = ["# File exported by ZBrush version 2022"]
        for (x, y, z), (r, g, bl) in zip(vertices, colours):
            lines.append(f"v {x:.6f} {y:.6f} {z:.6f}" + (f" {r / 255:.3f} {g / 255:.3f} {bl / 255:.3f}"
                                                         if style == "xyzrgb" else ""))
        if style == "mrgb":
            groups = [f"ff{r:02x}{g:02x}{bl:02x}" for r, g, bl in colours]
            for start in range(0, len(groups), 64):
                lines.append("#MRGB " + "".join(groups[start:start + 64]))
        lines += [f"f {a + 1} {b + 1} {c + 1}" for a, b, c in faces]
        return "\n".join(lines) + "\n", len(faces)

    paint = WORK / "polypaint"
    paint.mkdir()
    text, sphere_tris = painted_sphere()
    (paint / "SM_Ball_HP.obj").write_text(text, encoding="utf-8")
    (paint / "SM_Plain.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    (paint / "SM_Inline.obj").write_text(painted_sphere(style="xyzrgb")[0], encoding="utf-8")
    expect("ZBrush #MRGB lines are recognised as Polypaint",
           agk.obj_colour_source(paint / "SM_Ball_HP.obj") == "polypaint")
    expect("colours on the vertex lines are recognised too",
           agk.obj_colour_source(paint / "SM_Inline.obj") == "vertex colours")
    expect("an .obj without colours is left alone", agk.obj_colour_source(paint / "SM_Plain.obj") == "")
    done = run_gate(paint / "SM_Ball_HP.obj")
    expect("the gate explains that the colour is per vertex and how to bake it",
           "ZBrush Polypaint" in done.stdout and "--bake-polypaint" in done.stdout, done.stdout[-400:])
    expect("... and says nothing about a plain .obj",
           "Polypaint" not in run_gate(paint / "SM_Plain.obj").stdout)
    sys.path.insert(0, str(ROOT / "engine"))
    import polypaint  # noqa: E402
    read = polypaint.read_obj(paint / "SM_Ball_HP.obj")
    expect("the colours are read one per vertex, in vertex order",
           len(read.colours) == len(read.vertices) and tuple(read.colours[0]) == (1.0, 0.0, 0.0)
           and tuple(read.colours[-1]) == (0.0, 0.0, 1.0), str(read.colours[:1]))
    expect("bake output names follow the source: _HP becomes _LP",
           polypaint.output_names(Path("SM_Hero_HP_03.obj"))
           == ("SM_Hero_LP_03.glb", "T_Hero_LP_03_BaseColor.png"))
    # 12 Nov 2026: Samus's N64 bake had a black triangle on each side of the
    # head - a fold the reducer left (two triangles on the same three points,
    # facing opposite ways) - and much of her face shaded from behind, where one
    # averaged normal per vertex spanned a sharp fold.
    import numpy as _npp
    _pos = _npp.array([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0)], float)
    _faces = _npp.array([(0, 1, 2), (0, 2, 1), (1, 4, 2), (1, 4, 2), (0, 3, 1)])
    _kept = polypaint._drop_stacked(_pos, _faces)
    expect("a fold (same triangle twice, facing opposite ways) is dropped whole",
           not any(sorted(face) == [0, 1, 2] for face in _kept.tolist()), _kept.tolist())
    expect("... a plain copy keeps one, and other triangles stay",
           sum(sorted(face) == [1, 2, 4] for face in _kept.tolist()) == 1
           and [0, 3, 1] in _kept.tolist() and len(_kept) == 2, _kept.tolist())
    # A sharp ridge: two faces at 90 degrees share an edge. One normal per
    # vertex would point between them; per corner, each face keeps its own.
    _ridge = _npp.array([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)], float)
    _ridge_faces = _npp.array([(0, 1, 2), (0, 3, 1)])
    _normals = polypaint._corner_normals(_ridge, _ridge_faces)
    expect("across a sharp edge each triangle keeps its own normal",
           _npp.allclose(_normals[0], [0, 0, 1]) and _npp.allclose(_normals[1], [0, 1, 0]),
           _normals.round(2).tolist())
    _gentle = _npp.array([(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, -1, 0.3)], float)
    _gentle_normals = polypaint._corner_normals(_gentle, _npp.array([(0, 1, 2), (0, 3, 1)]))
    expect("... and a gentle bend is smoothed (the shared corners agree)",
           _npp.allclose(_gentle_normals[0][0], _gentle_normals[1][0])
           and _npp.allclose(_gentle_normals[0][1], _gentle_normals[1][2]),
           _gentle_normals.round(2).tolist())
    _p2, _uv2, _n2, _f2 = polypaint._split_by_normal(_ridge, _npp.zeros((4, 2)), _ridge_faces, _normals)
    expect("a vertex on a hard edge gets a copy per normal",
           len(_p2) == 6 and _npp.allclose(_n2[_f2[0]], _normals[0]), len(_p2))
    import importlib
    def _bake_ready():
        try:
            for _name in ("fast_simplification", "xatlas", "scipy.spatial"):
                importlib.import_module(_name)
            return True
        except Exception:                 # missing, or (3.9) installed but broken
            return False
    if _bake_ready():
        done = run_gate(paint / "SM_Ball_HP.obj", "--bake-polypaint", "--bake-triangles", "600",
                        "--bake-texture", "256")
        # The source sphere's poles hold 96 collapsed triangles (as many ZBrush
        # meshes do): the source fails for them, the baked asset must not.
        expect("--bake-polypaint makes a game asset that passes the folder's rules",
               done.returncode == 0 and "PASSED  SM_Ball_LP.glb" in done.stdout
               and "PASSED  T_Ball_LP_BaseColor.png" in done.stdout, done.stdout[-600:] + done.stderr[-300:])
        from PIL import Image as _Image
        baked = _Image.open(paint / "T_Ball_LP_BaseColor.png").convert("RGB")
        import numpy as _np
        _pixels = _np.asarray(baked).reshape(-1, 3).astype(int)
        histogram = {"red": int(((_pixels[:, 0] > 200) & (_pixels[:, 2] < 60)).sum()),
                     "blue": int(((_pixels[:, 2] > 200) & (_pixels[:, 0] < 60)).sum())}
        expect("the texture carries the sculpt's colours (red and blue halves)",
               histogram["red"] > 4000 and histogram["blue"] > 4000, str(histogram))
        import trimesh as _trimesh
        made = _trimesh.load(paint / "SM_Ball_LP.glb", force="mesh")
        expect("the game asset is reduced to the budget and has UVs",
               len(made.faces) <= 600 and getattr(made.visual, "uv", None) is not None,
               str(len(made.faces)))
        expect("the source is never changed", (paint / "SM_Ball_HP.obj").read_text(encoding="utf-8") == text)

        # 10 Nov 2026: closest-surface sampling, a normal map, an N64 preset.
        def read_glb_parts(path):
            """(json document, {accessor index: numpy array}, [image bytes]) of a .glb."""
            data = path.read_bytes()
            json_length = int.from_bytes(data[12:16], "little")
            document = json.loads(data[20:20 + json_length])
            binary = data[28 + json_length:]
            kinds = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
            types = {5126: _np.float32, 5123: _np.uint16, 5125: _np.uint32}
            arrays = {}
            for index, accessor in enumerate(document["accessors"]):
                view = document["bufferViews"][accessor["bufferView"]]
                width = kinds[accessor["type"]]
                values = _np.frombuffer(binary, types[accessor["componentType"]],
                                        count=accessor["count"] * width,
                                        offset=view.get("byteOffset", 0))
                arrays[index] = values.reshape(accessor["count"], width) if width > 1 else values
            images = []
            for image in document.get("images", []):
                view = document["bufferViews"][image["bufferView"]]
                start = view.get("byteOffset", 0)
                images.append(binary[start:start + view["byteLength"]])
            return document, arrays, images

        def dome_sculpt(cells=90):
            """A 2 x 2 sheet with a smooth dome in the middle, coloured by position:
            the 'sculpt' whose colour and shape a bake must reproduce."""
            lines, faces = ["# File exported by ZBrush version 2022"], []
            groups = []
            for i in range(cells + 1):
                for j in range(cells + 1):
                    x, z = -1 + 2 * j / cells, -1 + 2 * i / cells
                    r2 = (x * x + z * z) / 0.36
                    y = 0.25 * (1 - r2) ** 2 if r2 < 1 else 0.0
                    lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
                    red, green = int(60 + 150 * (x + 1) / 2), int(60 + 150 * (z + 1) / 2)
                    groups.append(f"ff{red:02x}{green:02x}80")
            for start in range(0, len(groups), 64):
                lines.append("#MRGB " + "".join(groups[start:start + 64]))
            for i in range(cells):
                for j in range(cells):
                    a = i * (cells + 1) + j
                    b, c, d = a + 1, a + cells + 1, a + cells + 2
                    faces += [(a, c, b), (b, c, d)]       # normals face +y
            lines += [f"f {a + 1} {b + 1} {c + 1}" for a, b, c in faces]
            return "\n".join(lines) + "\n"

        def dome_normal(x, z):
            r2 = (x * x + z * z) / 0.36
            inside = r2 < 1
            slope = _np.where(inside, 0.25 * 2 * (1 - r2) * (-2 / 0.36), 0.0)
            n = _np.stack([-slope * x, _np.ones_like(x), -slope * z], axis=1)
            return n / _np.linalg.norm(n, axis=1, keepdims=True)

        dome = WORK / "dome"
        dome.mkdir()
        (dome / "SM_Dome_HP.obj").write_text(dome_sculpt(), encoding="utf-8")
        done = run_gate(dome / "SM_Dome_HP.obj", "--bake-polypaint", "--bake-triangles", "120",
                        "--bake-texture", "512")
        expect("the bake writes a normal map beside the BaseColor, and both pass the gate",
               done.returncode == 0 and "PASSED  T_Dome_LP_Normal.png" in done.stdout
               and "PASSED  T_Dome_LP_BaseColor.png" in done.stdout
               and "PASSED  SM_Dome_LP.glb" in done.stdout, done.stdout[-700:] + done.stderr[-300:])
        document, arrays, images = read_glb_parts(dome / "SM_Dome_LP.glb")
        material = document["materials"][0]
        attributes = document["meshes"][0]["primitives"][0]["attributes"]
        expect("... and the .glb carries it: normalTexture, TANGENT, metallic 0",
               "normalTexture" in material and "TANGENT" in attributes and len(images) == 2
               and material["pbrMetallicRoughness"]["metallicFactor"] == 0.0, str(material))
        # Decode the normal map the way a glTF viewer does (B = cross(N, T) * w,
        # green up) at points across the reduced mesh, and compare with the
        # dome's true normal there. Flipping green must do clearly worse.
        primitive = document["meshes"][0]["primitives"][0]
        positions = arrays[attributes["POSITION"]].astype(float)
        normals = arrays[attributes["NORMAL"]].astype(float)
        tangents = arrays[attributes["TANGENT"]].astype(float)
        uvs = arrays[attributes["TEXCOORD_0"]].astype(float)
        triangles = arrays[primitive["indices"]].reshape(-1, 3).astype(int)
        normal_map = _np.asarray(_Image.open(io.BytesIO(images[1])).convert("RGB"), float) / 255
        base_map = _np.asarray(_Image.open(io.BytesIO(images[0])).convert("RGB"), float) / 255
        rng = _np.random.default_rng(7)
        picks = rng.integers(0, len(triangles), 4000)
        weights = rng.dirichlet((2, 2, 2), 4000)
        corners = triangles[picks]

        def blend(values):
            return _np.einsum("nk,nkc->nc", weights, values[corners])
        point, uv = blend(positions), blend(uvs)
        n, t4 = blend(normals), blend(tangents)
        n /= _np.linalg.norm(n, axis=1, keepdims=True)
        t = t4[:, :3]
        handed = _np.where(t4[:, 3] < 0, -1.0, 1.0)[:, None]
        size = normal_map.shape[0]
        rows = _np.clip((uv[:, 1] * size).astype(int), 0, size - 1)     # glTF: v down
        cols = _np.clip((uv[:, 0] * size).astype(int), 0, size - 1)
        texel = normal_map[rows, cols] * 2 - 1
        truth = dome_normal(point[:, 0], point[:, 2])

        def error(green):
            world = (texel[:, :1] * t + green * texel[:, 1:2] * _np.cross(n, t) * handed
                     + texel[:, 2:3] * n)
            world /= _np.linalg.norm(world, axis=1, keepdims=True)
            return float(_np.degrees(_np.arccos(_np.clip((world * truth).sum(1), -1, 1))).mean())
        plain = float(_np.degrees(_np.arccos(_np.clip((n * truth).sum(1), -1, 1))).mean())
        right, flipped = error(1.0), error(-1.0)
        expect("the normal map gives back the sculpt's shape (glTF/OpenGL, green up)",
               right < 2.0 and right < plain * 0.6 and flipped > right * 2,
               f"mapped {right:.2f} deg, mesh alone {plain:.2f}, green flipped {flipped:.2f}")
        expected = _np.stack([(60 + 150 * (point[:, 0] + 1) / 2) / 255,
                              (60 + 150 * (point[:, 2] + 1) / 2) / 255,
                              _np.full(len(point), 128 / 255)], axis=1)
        colour_error = float(_np.abs(base_map[rows, cols] - expected).mean())
        expect("the colours are the sculpt's at each point (blended across its triangle)",
               colour_error < 0.03, f"mean error {colour_error:.4f}")

        sys.path.insert(0, str(ROOT / "engine"))
        settings = polypaint.resolve_settings("n64", max_triangles=400, max_texture=32)
        expect("the n64 preset: ~1,200 tris, 64 px, 16 colours, no normal map, a face "
               "texture - within the rules",
               polypaint.resolve_settings("n64") == {"preset": "n64", "triangles": 1200,
                                                     "texture": 64, "normal_map": False,
                                                     "colours": 16, "face_texture": True,
                                                     "face_texture_note": ""}
               and settings["triangles"] == 400 and settings["texture"] == 32,
               str(settings))
        expect("... and anything given explicitly wins",
               polypaint.resolve_settings("n64", texture=128, colours=0)["texture"] == 128
               and polypaint.resolve_settings("n64", colours=0)["colours"] == 0)
        try:
            polypaint.resolve_settings("ps9")
            refused = False
        except polypaint.PolypaintError:
            refused = True
        expect("an unknown preset is refused", refused)
        for _edge, _why in ((8, "at least 16 px (got 8)"), (100, "a power of two (got 100)")):
            try:
                polypaint.bake(paint / "SM_Ball_HP.obj", WORK / "unused", 600, _edge)
                _said = "baked"
            except polypaint.PolypaintError as exc:
                _said = str(exc)
            expect(f"a {_edge} px bake texture is refused saying why ({_why})", _why in _said, _said)

        def _out_of_memory(*_args, **_kwargs):
            raise MemoryError("Unable to allocate 16.0 GiB")
        _args = agk.build_parser().parse_args([str(paint / "SM_Ball_HP.obj"), "--bake-polypaint",
                                               "--bake-texture", "65536"])
        with patch.object(agk.polypaint_module(), "bake", _out_of_memory):
            try:
                _code = agk.run_bake_polypaint(paint / "SM_Ball_HP.obj",
                                               agk.Budget(max_texture_edge=65536), _args)
            except MemoryError as exc:
                _code = repr(exc)
        expect("a bake that runs out of memory (--bake-texture 65536) exits 2 with advice, "
               "not a traceback", _code == 2, _code)
        import contextlib
        import logging

        @contextlib.contextmanager
        def _captured_log():
            class _Capture(logging.Handler):
                text = ""

                def emit(self, record):
                    _Capture.text += record.getMessage() + "\n"
            handler = _Capture()
            agk.LOG.addHandler(handler)
            try:
                yield handler
            finally:
                agk.LOG.removeHandler(handler)

        # 11 Nov 2026: asking for more than the folder allows used to bake (an
        # 8192 bake: 85 s, 4 GB) and then fail the gate; it is refused first.
        _baked = []
        _args = agk.build_parser().parse_args([str(paint / "SM_Ball_HP.obj"), "--bake-polypaint",
                                               "--bake-texture", "4096", "--bake-triangles", "9000"])
        with patch.object(agk.polypaint_module(), "bake", lambda *a, **k: _baked.append(1)), \
                _captured_log() as _log:
            _code = agk.run_bake_polypaint(paint / "SM_Ball_HP.obj",
                                           agk.Budget(max_texture_edge=2048, max_triangles=5000), _args)
        expect("a bake asked for more than the folder's limits is refused before it starts",
               _code == 2 and not _baked
               and "--bake-texture 4096 is larger than the 2048 px texture limit here" in _log.text
               and "--bake-triangles 9,000 is more than the 5,000 triangle budget here" in _log.text,
               (_code, _log.text[-400:]))
        retro = WORK / "retro"
        retro.mkdir()
        (retro / "SM_Ball_HP.obj").write_text(text, encoding="utf-8")
        (retro / "rules.toml").write_text("[mesh]\nmax_triangles = 1200\nmax_material_slots = 2\n"
                                          "[texture]\nmax_edge = 64\npower_of_two = true\n"
                                          "pbr = false\n", encoding="utf-8")
        done = run_gate(retro / "SM_Ball_HP.obj", "--bake-polypaint", "--bake-preset", "n64")
        expect("--bake-preset n64 under N64 rules passes them, with no normal map",
               done.returncode == 0 and "PASSED  SM_Ball_LP.glb" in done.stdout
               and "PASSED  T_Ball_LP_BaseColor.png" in done.stdout
               and not (retro / "T_Ball_LP_Normal.png").exists()
               and "the texture budget here" not in done.stdout + done.stderr,
               done.stdout[-600:] + done.stderr[-300:])
        palette = _Image.open(retro / "T_Ball_LP_BaseColor.png")
        made = _trimesh.load(retro / "SM_Ball_LP.glb", force="mesh")
        document, _, images = read_glb_parts(retro / "SM_Ball_LP.glb")
        expect("... 64x64 in at most 16 colours, at most 1,200 triangles, nearest filtering",
               palette.size == (64, 64) and len(palette.convert("RGB").getcolors(4096)) <= 16
               and len(made.faces) <= 1200 and len(images) == 1
               and "normalTexture" not in document["materials"][0]
               and document["samplers"][0]["magFilter"] == 9728,
               f"{palette.size} {len(made.faces)} {document['samplers']}")
        done = run_gate(paint / "SM_Ball_HP.obj", "--bake-polypaint", "--bake-triangles", "600",
                        "--bake-texture", "256", "--bake-no-normal", "--bake-colours", "16")
        expect("--bake-no-normal and --bake-colours 16 do what they say",
               done.returncode == 0 and len(_Image.open(paint / "T_Ball_LP_BaseColor.png")
                                            .convert("RGB").getcolors(4096)) <= 16
               and "BaseColor in 16 colours" in done.stdout + done.stderr
               and "+ Normal" not in done.stdout + done.stderr,
               done.stdout[-500:] + done.stderr[-300:])

        # 10 Nov 2026: at 64 px the whole test figure shared one texture and her
        # face got about 6 x 6 texels (eyes were dark blobs). The n64 preset now
        # gives the face a 64 px texture of its own, as a second material.
        def figure(rings=90, segments=180):
            """A blue body and a skin head with two dark eyes and red lips, facing +z;
            each part a UV sphere whose pole fans repeat one point (open edges to a
            reducer, as a UV seam or separate pieces are)."""
            import math
            vertices, colours, faces = [], [], []

            def sphere(cx, cy, cz, rx, ry, rz, paint):
                base = len(vertices)
                for i in range(rings + 1):
                    theta = math.pi * i / rings
                    for j in range(segments):
                        phi = 2 * math.pi * j / segments
                        x, y = math.sin(theta) * math.cos(phi), math.cos(theta)
                        z = math.sin(theta) * math.sin(phi)
                        vertices.append((cx + rx * x, cy + ry * y, cz + rz * z))
                        colours.append(paint(x, y, z))
                for i in range(rings):
                    for j in range(segments):
                        a = base + i * segments + j
                        b = base + i * segments + (j + 1) % segments
                        faces.extend([(a, a + segments, b), (b, a + segments, b + segments)])

            def head(x, y, z):
                if z > 0 and any((x - ex) ** 2 + (y - 0.15) ** 2 < 0.13 ** 2 for ex in (-0.35, 0.35)):
                    return (20, 20, 25)
                if z > 0 and abs(x) < 0.3 and abs(y + 0.4) < 0.07:
                    return (200, 30, 40)
                return (230, 200, 90) if y > 0.55 else (235, 190, 160)

            sphere(0, 0.6, 0, 0.6, 0.45, 0.55, lambda x, y, z: (40, 70, 200))
            sphere(0, -1.4, 0, 0.45, 1.2, 0.4, lambda x, y, z: (40, 70, 200))
            sphere(0, 1.55, 0, 0.32, 0.4, 0.34, head)
            lines = ["# File exported by ZBrush version 2022"]
            lines += [f"v {x:.5f} {y:.5f} {z:.5f}" for x, y, z in vertices]
            groups = [f"ff{r:02x}{g:02x}{b:02x}" for r, g, b in colours]
            lines += ["#MRGB " + "".join(groups[k:k + 64]) for k in range(0, len(groups), 64)]
            lines += [f"f {a + 1} {b + 1} {c + 1}" for a, b, c in faces]
            return "\n".join(lines) + "\n"

        hero = WORK / "hero"
        hero.mkdir()
        figure_text = figure()
        (hero / "SM_Hero_HP.obj").write_text(figure_text, encoding="utf-8")
        (hero / "rules.toml").write_text("[mesh]\nmax_triangles = 1200\nmax_material_slots = 2\n"
                                         "[texture]\nmax_edge = 64\npower_of_two = true\n"
                                         "pbr = false\n", encoding="utf-8")
        done = run_gate(hero / "SM_Hero_HP.obj", "--bake-polypaint", "--bake-preset", "n64")
        expect("n64: the face gets its own texture, and everything passes N64 rules",
               done.returncode == 0 and "PASSED  SM_Hero_LP.glb" in done.stdout
               and "PASSED  T_Hero_LP_BaseColor.png" in done.stdout
               and "PASSED  T_Hero_LP_Face_BaseColor.png" in done.stdout
               and "the face has its own 64x64 texture" in done.stdout + done.stderr,
               done.stdout[-700:] + done.stderr[-300:])
        document, _, images = read_glb_parts(hero / "SM_Hero_LP.glb")
        primitives = document["meshes"][0]["primitives"]
        made = _trimesh.load(hero / "SM_Hero_LP.glb", force="mesh")
        expect("... one mesh, two materials, two embedded textures, at most 1,200 triangles",
               len(primitives) == 2 and len(document["materials"]) == 2 and len(images) == 2
               and {p["material"] for p in primitives} == {0, 1}
               and document["materials"][1]["name"] == "T_Hero_LP_Face"
               and len(made.faces) <= 1200,
               f"{len(primitives)} {len(document['materials'])} {len(images)} {len(made.faces)}")
        def shading_faults(path):
            """(triangles with a corner shaded from behind, triangles lying on another)."""
            document_, arrays_, _ = read_glb_parts(path)
            behind, stacked = 0, 0
            for primitive in (p for mesh in document_["meshes"] for p in mesh["primitives"]):
                P = arrays_[primitive["attributes"]["POSITION"]].astype(float)
                N = arrays_[primitive["attributes"]["NORMAL"]].astype(float)
                F = arrays_[primitive["indices"]].astype(int).reshape(-1, 3)
                fn = _np.cross(P[F[:, 1]] - P[F[:, 0]], P[F[:, 2]] - P[F[:, 0]])
                behind += int((_np.einsum("ikj,ij->ik", N[F], fn).min(axis=1) < 0).sum())
                keys = {tuple(sorted(map(tuple, corners)))
                        for corners in _np.round(P[F] * 1e5).astype(_np.int64).tolist()}
                stacked += len(F) - len(keys)
            return behind, stacked

        _faults = shading_faults(hero / "SM_Hero_LP.glb")
        expect("n64 bake: no triangle shaded from behind, none lying on another",
               _faults == (0, 0), f"{_faults[0]} shaded from behind, {_faults[1]} stacked")

        def blade_sculpt(rings=60, segments=80):
            """A head with a thin blade behind it (a ponytail, an ear): reduced
            hard, the blade's rim is shared by its two sides, and one averaged
            normal there shaded 16 of 148 triangles from behind (12 Nov 2026)."""
            import math
            vertices, colours, faces = [], [], []

            def sphere(cx, cy, cz, rx, ry, rz, colour):
                base = len(vertices)
                for i in range(rings + 1):
                    theta = math.pi * i / rings
                    for j in range(segments):
                        phi = 2 * math.pi * j / segments
                        vertices.append((cx + rx * math.sin(theta) * math.cos(phi),
                                         cy + ry * math.cos(theta),
                                         cz + rz * math.sin(theta) * math.sin(phi)))
                        colours.append(colour)
                for i in range(rings):
                    for j in range(segments):
                        a = base + i * segments + j
                        b = base + i * segments + (j + 1) % segments
                        faces.extend([(a, a + segments, b), (b, a + segments, b + segments)])

            sphere(0, 0, 0, 0.5, 0.5, 0.5, (200, 160, 140))
            sphere(0, 0.2, -0.6, 0.35, 0.9, 0.03, (230, 210, 120))
            lines = ["# File exported by ZBrush version 2022"]
            lines += [f"v {x:.5f} {y:.5f} {z:.5f}" for x, y, z in vertices]
            groups = [f"ff{r:02x}{g:02x}{b:02x}" for r, g, b in colours]
            lines += ["#MRGB " + "".join(groups[k:k + 64]) for k in range(0, len(groups), 64)]
            lines += [f"f {a + 1} {b + 1} {c + 1}" for a, b, c in faces]
            return "\n".join(lines) + "\n"

        blade = WORK / "blade"
        blade.mkdir()
        (blade / "SM_Blade_HP.obj").write_text(blade_sculpt(), encoding="utf-8")
        polypaint.bake(blade / "SM_Blade_HP.obj", blade, triangles=150, texture=64,
                       normal_map=False, colours=16)
        _faults = shading_faults(blade / "SM_Blade_LP.glb")
        expect("a thin blade reduced hard: no triangle shaded from behind (was 16 of 148)",
               _faults == (0, 0), f"{_faults[0]} shaded from behind, {_faults[1]} stacked")
        face_pixels = _np.asarray(_Image.open(hero / "T_Hero_LP_Face_BaseColor.png").convert("RGB"))
        dark_face = int((face_pixels.max(axis=2) < 70).sum())
        done = run_gate(hero / "SM_Hero_HP.obj", "--bake-polypaint", "--bake-preset", "n64",
                        "--bake-no-face-texture")
        one_pixels = _np.asarray(_Image.open(hero / "T_Hero_LP_BaseColor.png").convert("RGB"))
        dark_one = int((one_pixels.max(axis=2) < 70).sum())
        document, _, images = read_glb_parts(hero / "SM_Hero_LP.glb")
        expect("--bake-no-face-texture: one material, one texture",
               done.returncode == 0 and len(document["materials"]) == 1 and len(images) == 1,
               done.stdout[-400:])
        expect("... and the face texture left by the bake before is named as unused, not deleted",
               (hero / "T_Hero_LP_Face_BaseColor.png").is_file()
               and "T_Hero_LP_Face_BaseColor.png is from an earlier bake" in done.stdout + done.stderr,
               done.stdout[-400:] + done.stderr[-300:])
        expect("the eyes get at least 4x the texels with a face texture",
               dark_face >= 4 * max(dark_one, 1), f"{dark_face} vs {dark_one}")
        expect("the reducer meets the budget on a sculpt with repeated points (was 4,628 of 1,200)",
               len(_trimesh.load(hero / "SM_Hero_LP.glb", force="mesh").faces) <= 1200)
        (hero / "rules.toml").write_text("[mesh]\nmax_triangles = 1200\nmax_material_slots = 1\n"
                                         "[texture]\nmax_edge = 64\npower_of_two = true\n"
                                         "pbr = false\n", encoding="utf-8")
        for stale in hero.glob("*_LP*"):
            stale.unlink()
        done = run_gate(hero / "SM_Hero_HP.obj", "--bake-polypaint", "--bake-preset", "n64")
        expect("rules with one material slot: one texture, and the log says why",
               done.returncode == 0 and not (hero / "T_Hero_LP_Face_BaseColor.png").exists()
               and "allow 1 material slot" in done.stdout + done.stderr,
               done.stdout[-500:] + done.stderr[-300:])
        plain = WORK / "plain"
        plain.mkdir()
        (plain / "SM_Ball_HP.obj").write_text(text, encoding="utf-8")
        done = run_gate(plain / "SM_Ball_HP.obj", "--bake-polypaint", "--bake-preset", "n64")
        expect("a sculpt without colour detail keeps one texture, and says so",
               done.returncode == 0 and not (plain / "T_Ball_LP_Face_BaseColor.png").exists()
               and "too little colour detail" in done.stdout + done.stderr,
               done.stdout[-400:] + done.stderr[-300:])
        centre = polypaint._face_centre(*[getattr(polypaint.read_obj(hero / "SM_Hero_HP.obj"), k)
                                          for k in ("vertices", "faces", "colours")])
        expect("the face is found where the eyes and lips are (front of the head)",
               centre is not None and 1.2 < centre[1] < 1.8 and centre[2] > 0.15
               and abs(centre[0]) < 0.15, str(centre))

        # What the test sculpt did to the first two versions: a head sculpted far denser
        # than the body, a suit with coarse two-colour panel lines (the first
        # version's busiest edges, so the "face" landed on her chest) and a
        # blue collar ring round the neck (colour spread per cell pulled the
        # centre down to her chin). Counting colours per cell finds the face.
        def hero_like():
            import math
            vertices, colours, faces = [], [], []

            def sphere(cx, cy, cz, rx, ry, rz, rings, segments, paint):
                base = len(vertices)
                for i in range(rings + 1):
                    theta = math.pi * i / rings
                    for j in range(segments):
                        phi = 2 * math.pi * j / segments
                        x, y = math.sin(theta) * math.cos(phi), math.cos(theta)
                        z = math.sin(theta) * math.sin(phi)
                        vertices.append((cx + rx * x, cy + ry * y, cz + rz * z))
                        colours.append(paint(x, y, z))
                for i in range(rings):
                    for j in range(segments):
                        a = base + i * segments + j
                        b = base + i * segments + (j + 1) % segments
                        faces.extend([(a, a + segments, b), (b, a + segments, b + segments)])

            def suit(x, y, z):
                return (40, 70, 200) if int((y + 1) * 8) % 2 else (120, 170, 240)

            def blend(base, paint, inside):
                return tuple(round(p * inside + q * (1 - inside)) for p, q in zip(paint, base))

            def soft(distance, radius, width=0.06):
                """1 inside, 0 outside, a soft airbrushed edge as Polypaint has."""
                return min(1.0, max(0.0, (radius + width - distance) / (2 * width)))

            def head(x, y, z):
                if y < -0.7:                    # the suit's collar, a hard ring
                    return (40, 70, 200) if y < -0.8 else (240, 240, 250)
                colour = (230, 200, 90) if y > 0.55 else (235, 190, 160)
                if z <= 0:
                    return colour
                for ex in (-0.35, 0.35):
                    d = math.hypot(x - ex, y - 0.15)
                    colour = blend(colour, (250, 250, 250), soft(d, 0.13))   # white
                    colour = blend(colour, (60, 140, 90), soft(d, 0.08))     # iris
                    colour = blend(colour, (10, 10, 10), soft(d, 0.04))      # pupil
                colour = blend(colour, (110, 80, 40), soft(abs(y - 0.33), 0.02)
                               * soft(abs(abs(x) - 0.35), 0.15))             # brows
                return blend(colour, (200, 30, 40), soft(abs(y + 0.4), 0.05)
                             * soft(abs(x), 0.28))                           # lips

            sphere(0, 0.6, 0, 0.6, 0.45, 0.55, 40, 80, suit)
            sphere(0, -1.4, 0, 0.45, 1.2, 0.4, 60, 60, suit)
            sphere(0, 1.55, 0, 0.32, 0.4, 0.34, 160, 320, head)
            return (_np.array(vertices, float), _np.array(faces, _np.int64),
                    _np.array(colours, _np.float32) / 255.0)

        centre = polypaint._face_centre(*hero_like())
        expect("a dense head, a striped suit and a collar ring: the face is still found "
               "between the eyes and the lips (not the chest, not the chin)",
               centre is not None and 1.3 < centre[1] < 1.75 and centre[2] > 0.15
               and abs(centre[0]) < 0.15, str(centre))
    else:
        print("  skip baking: pip install fast-simplification xatlas scipy")
finally:
    shutil.rmtree(WORK, ignore_errors=True)

print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
