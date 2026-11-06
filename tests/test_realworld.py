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
    source = (ROOT / "asset_gatekeeper.py").read_text(encoding="utf-8")
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
finally:
    shutil.rmtree(WORK, ignore_errors=True)

print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
