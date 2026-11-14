"""FBX, TGA/JPG, and the audit fixes that touch formats, references and reporting.

    python tests/test_formats.py

The FBX ground truth (tests/fixtures/fbx/truth.json) comes from Blender 4.2 itself:
make_fixtures.py builds each scene, counts it with Blender's own triangulation,
and exports binary FBX. Nothing here needs Blender to run - only to regenerate.
Hostile FBX files are built in-process, byte by byte.
"""
import contextlib
import io
import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
import zlib
from pathlib import Path
from unittest.mock import patch

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
for extra in (str(ROOT), str(ROOT / "server")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import asset_gatekeeper as agk  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "fbx"
WORK = ROOT / "scratch" / "format_tests"
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)

failures = []


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(label)


@contextlib.contextmanager
def captured_logs():
    lines = []

    class Handler(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = Handler()
    logger = logging.getLogger("assetgatekeeper")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def run_gate(*args):
    return subprocess.run([sys.executable, str(ROOT / "asset_gatekeeper.py"), *map(str, args),
                           "--no-color", "--no-notify"], capture_output=True, text=True, timeout=180)


def json_verdicts(completed):
    rows = {}
    for line in completed.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if "file" in row:
            rows[Path(row["file"]).name] = row
    return rows


# --------------------------------------------------------------------------- #
print("1. FBX measurements match Blender")
truth = json.loads((FIXTURES / "truth.json").read_text(encoding="utf-8"))
generous = agk.Budget(max_triangles=10_000_000, max_material_slots=64, max_bone_influences=64,
                      max_uv_channels=8, enforce_texture_naming=False)
for name, facts in sorted(truth.items()):
    path = FIXTURES / f"{name}.fbx"
    summary = agk.read_fbx(path)
    expect(f"{name}: {facts['triangles']} triangles", summary.triangles == facts["triangles"],
           summary.triangles)
    if facts["triangles"]:
        expect(f"{name}: {facts['materials']} material slot(s)",
               summary.material_slots == facts["materials"], summary.material_slots)
        expect(f"{name}: {facts['uv']} UV set(s)", summary.uv_channels == facts["uv"],
               summary.uv_channels)
        expect(f"{name}: {facts['influences']} max bone influence(s)",
               summary.max_influences == facts["influences"], summary.max_influences)
        expect(f"{name}: {facts['degenerate']} degenerate triangle(s)",
               summary.degenerate == facts["degenerate"], summary.degenerate)

verdict = agk.Gatekeeper(agk.Budget()).check(FIXTURES / "mats5.fbx")
expect("a 5-material FBX fails the default 3-slot budget",
       any("5 material slots" in p for p in verdict.problems), verdict.problems)
verdict = agk.Gatekeeper(agk.Budget(max_bone_influences=4)).check(FIXTURES / "skin6.fbx")
expect("a 6-influence skin fails a 4-influence budget",
       any("6 bone weights" in p for p in verdict.problems), verdict.problems)
verdict = agk.Gatekeeper(agk.Budget()).check(FIXTURES / "degenerate.fbx")
expect("a zero-area FBX face is reported", any("degenerate" in p for p in verdict.problems),
       verdict.problems)
verdict = agk.Gatekeeper(agk.Budget()).check(FIXTURES / "empty.fbx")
expect("an FBX with no mesh fails as empty", any("no triangles" in p for p in verdict.problems),
       verdict.problems)
verdict = agk.Gatekeeper(generous).check(FIXTURES / "linked3.fbx")
expect("linked duplicates count the shared mesh once (320, not 960)",
       verdict.metric.startswith("320 tris"), verdict.metric)

print("1b. FBX mesh memory and texel density (10 Nov 2026)")
# Measured for glTF and OBJ since 2 Nov; an FBX only warned "not measured for
# FBX yet". engine/fbx_preview.measure() places the FBX the way the preview
# does (transforms, axes, unit scale) and counts vertices as an engine builds
# them (a UV seam or a hard edge splits a vertex). Density is cross-checked
# against the glTF path: the same geometry converted to .glb and measured by
# mesh_facts() must give the same number.
sys.path.insert(0, str(ROOT / "engine"))
import fbx_preview  # noqa: E402
cube = fbx_preview.measure(FIXTURES / "mats5.fbx")
expect("a hard-edged, UV-mapped cube is 24 vertices (4 per face), not its 8 corners",
       cube["vertices"] == 24, cube)
expect("... its density with the assumed 1,024 px texture is 128 px/m (2 m cube, quarter-width UVs)",
       round(cube["texel_density"]) == 128 and cube["assumed_texture"], cube)
# Flat-shaded fixtures: every polygon corner is its own vertex.
for name, corners in (("cube_subd3.fbx", 1536), ("linked3.fbx", 960), ("uv3.fbx", 4)):
    facts = fbx_preview.measure(FIXTURES / name)
    expect(f"{name}: {corners} vertices (flat shading: one per polygon corner)",
           facts["vertices"] == corners, facts)
for name in ("cube_subd3.fbx", "linked3.fbx", "uv3.fbx", "skin4.fbx", "mats5.fbx"):
    facts = fbx_preview.measure(FIXTURES / name)
    glb = WORK / (Path(name).stem + "_measure.glb")
    fbx_preview.convert(FIXTURES / name, glb)
    as_gltf = agk.mesh_facts(glb, 1024)
    expect(f"{name}: density matches the glTF path ({facts['texel_density']:.1f} px/m)",
           as_gltf["texel_density"] and abs(facts["texel_density"] - as_gltf["texel_density"])
           < 0.01 * as_gltf["texel_density"], (facts, as_gltf))


def fake_scene(uvs):
    """Two triangles sharing an edge, smooth normals: a quad drawn with 6 corners."""
    corners = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 0, 0], [1, 1, 0], [0, 1, 0]], float)
    return {"positions": corners.astype(np.float32), "uvs": np.asarray(uvs, np.float32),
            "normals": np.tile([0, 0, 1], (6, 1)).astype(np.float32),
            "groups": [(0, np.array([[0, 1, 2], [3, 4, 5]], np.uint32))],
            "material_names": ["m"], "textures": [""], "colours": False, "info": {}}


import numpy as np  # noqa: E402
shared = [[0, 0], [1, 0], [1, 1], [0, 0], [1, 1], [0, 1]]
seam = [[0, 0], [1, 0], [1, 1], [0.5, 0.5], [1, 1], [0, 1]]
with patch.object(fbx_preview, "_collect", lambda path: fake_scene(shared)):
    welded = fbx_preview.measure(Path("quad.fbx"))
with patch.object(fbx_preview, "_collect", lambda path: fake_scene(seam)):
    split = fbx_preview.measure(Path("quad.fbx"))
expect("corners with the same position, normal and UV are one vertex (quad: 6 corners -> 4)",
       welded["vertices"] == 4, welded)
expect("... a UV seam splits the shared corner (-> 5)", split["vertices"] == 5, split)
textured = fbx_preview.measure(FIXTURES / "tex_relative.fbx")
expect("the texture beside the FBX sets the density (64 px wood: 1/16 of the assumed 1,024)",
       not textured["assumed_texture"]
       and abs(textured["texel_density"] * 16 - cube["texel_density"]) < 0.5, textured)
# 14 Nov 2026: an FBX that carries its textures inside it previewed in flat slot
# colours - the preview named the image by a path nothing was stored under.
packed_glb = Path(tempfile.mkdtemp()) / "packed.glb"
packed_info = fbx_preview.convert(FIXTURES / "tex_embedded.fbx", packed_glb)
packed_blob = packed_glb.read_bytes()
packed_json = json.loads(packed_blob[20:20 + struct.unpack("<I", packed_blob[12:16])[0]])
packed_image = (packed_json.get("images") or [{}])[0]
packed_view = packed_json["bufferViews"][packed_image.get("bufferView", 0)]
packed_start = 20 + struct.unpack("<I", packed_blob[12:16])[0] + 8 + packed_view["byteOffset"]
expect("a texture packed inside the FBX goes into the preview itself (no file beside it needed)",
       packed_info["packed_textures"] and "uri" not in packed_image
       and packed_image.get("mimeType") == "image/png"
       and packed_blob[packed_start:packed_start + 8] == b"\x89PNG\r\n\x1a\n"
       and packed_json["materials"][0]["pbrMetallicRoughness"].get("baseColorTexture") == {"index": 0}
       and packed_json["buffers"][0]["byteLength"] == len(packed_blob) - packed_start
       + packed_view["byteOffset"], packed_image)
linked_glb = packed_glb.with_name("linked.glb")
fbx_preview.convert(FIXTURES / "tex_relative.fbx", linked_glb)
linked_blob = linked_glb.read_bytes()
linked_json = json.loads(linked_blob[20:20 + struct.unpack("<I", linked_blob[12:16])[0]])
expect("... and a texture beside the FBX is still named by its path",
       linked_json["images"] == [{"uri": "tex_relative_wood_Albedo.png"}], linked_json.get("images"))
expect("a packed image a glTF cannot carry is left out, not written as broken bytes",
       fbx_preview._as_web_image(b"not an image") == b""
       and fbx_preview._image_type(b"\xff\xd8\xff\xe0") == "image/jpeg")
expect("an FBX with no mesh measures as nothing, not as an error",
       fbx_preview.measure(FIXTURES / "empty.fbx")["vertices"] == 0)
verdict = agk.Gatekeeper(agk.Budget(max_mesh_mb=0.0001, max_material_slots=8)).check(FIXTURES / "mats5.fbx")
expect("an FBX over a mesh-memory budget fails with its vertex count",
       any("estimated GPU memory exceeds" in p and "(24 vertices)" in p for p in verdict.problems)
       and not any("not measured" in w for w in verdict.warnings), (verdict.problems, verdict.warnings))
verdict = agk.Gatekeeper(agk.Budget(texel_density_min=256, texel_density_max=2048,
                                    max_material_slots=8)).check(FIXTURES / "mats5.fbx")
expect("an FBX under a texel-density minimum fails, naming the assumed texture",
       any("texel density 128 px/m is below the 256 px/m minimum (assuming a 1024px texture)" in p
           for p in verdict.problems), verdict.problems)
verdict = agk.Gatekeeper(agk.Budget(texel_density_min=4, texel_density_max=64)).check(FIXTURES / "tex_relative.fbx")
expect("... and one inside the range passes on that count, with the number in its stats",
       not any("texel density" in p for p in verdict.problems)
       and dict(verdict.stats).get("texel_density") == 8, (verdict.problems, verdict.stats))

# 11 Nov 2026: run as a script the gate is __main__, and fbx_preview's
# "import asset_gatekeeper" executed the whole file a second time (and parsed
# the FBX again). The gate now registers itself under its module name.
speed_dir = WORK / "one_import"
speed_dir.mkdir()
shutil.copy(FIXTURES / "cube_subd3.fbx", speed_dir / "SM_Cube.fbx")
(speed_dir / "rules.toml").write_text("[mesh]\nmax_mb = 64\n", encoding="utf-8")
timed = subprocess.run([sys.executable, "-X", "importtime", str(ROOT / "asset_gatekeeper.py"),
                        str(speed_dir), "--once", "--no-cache", "--no-color"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
second = [line for line in timed.stderr.splitlines()
          if line.startswith("import time:") and line.rstrip().endswith("| asset_gatekeeper")]
expect("measuring an FBX does not load the gate a second time", timed.returncode == 0
       and not second and "~51 KB" in timed.stdout + timed.stderr, second or timed.stderr[-300:])

print("2. FBX texture references")
expect("relative texture beside the FBX resolves",
       not agk.missing_references(FIXTURES / "tex_relative.fbx"),
       agk.missing_references(FIXTURES / "tex_relative.fbx"))
missing = agk.missing_references(FIXTURES / "tex_missing.fbx")
expect("a deleted texture is reported missing",
       [w for w, _ in missing] == ["tex_missing_wood_Albedo.png"], missing)
expect("an embedded texture is not an external reference",
       agk.collect_references(FIXTURES / "tex_embedded.fbx") == [],
       agk.collect_references(FIXTURES / "tex_embedded.fbx"))
audit_dir = WORK / "audit"
audit_dir.mkdir()
for name in ("tex_relative.fbx", "tex_relative_wood_Albedo.png", "tex_missing.fbx"):
    shutil.copy(FIXTURES / name, audit_dir / name)
completed = run_gate(audit_dir, "--audit")
expect("--audit exits 1 and names the FBX with the broken link",
       completed.returncode == 1 and "BROKEN  tex_missing.fbx" in completed.stdout + completed.stderr,
       (completed.stdout + completed.stderr)[-300:])
moved = WORK / "moved_texture"
(moved / "meshes").mkdir(parents=True)
(moved / "textures").mkdir()
shutil.copy(FIXTURES / "tex_relative.fbx", moved / "meshes" / "tex_relative.fbx")
shutil.copy(FIXTURES / "tex_relative_wood_Albedo.png", moved / "textures" / "elsewhere.png")
# The FBX also stores the absolute path it was exported from. Where that file
# still exists (the machine that made the fixture) the reference escapes the
# project; everywhere else it is missing. Either way the asset must fail.
moved_verdict = agk.Gatekeeper(agk.Budget(), root=moved).check(moved / "meshes" / "tex_relative.fbx")
expect("an FBX whose texture is not in the project fails",
       any("missing reference" in p or "outside the project" in p for p in moved_verdict.problems),
       moved_verdict.problems)

print("3. Hostile FBX files fail cleanly")
MAGIC = b"Kaydara FBX Binary  \x00\x1a\x00"


def fbx_file(records, version=7500):
    return MAGIC + struct.pack("<I", version) + records + b"\0" * 25 + b"\0" * 160


def record(name, props=b"", count=0, start=27):
    total = 25 + len(name) + len(props)
    return struct.pack("<QQQB", start + total, count, len(props), len(name)) + name + props


def nested(depth, start):
    header = 26
    if depth == 0:
        return struct.pack("<QQQB", start + header, 0, 0, 1) + b"N"
    child = nested(depth - 1, start + header)
    return struct.pack("<QQQB", start + header + len(child) + 25, 0, 0, 1) + b"N" + child + b"\0" * 25


sys.setrecursionlimit(max(sys.getrecursionlimit(), 20_000))
real = (FIXTURES / "cube_subd3.fbx").read_bytes()
bomb = zlib.compress(b"\0" * (8 << 20))
hostile = {
    "truncated.fbx": real[: len(real) // 2],
    "random.fbx": MAGIC + struct.pack("<I", 7400) + os.urandom(4000),
    "bomb.fbx": fbx_file(record(b"Bomb", b"d" + struct.pack("<III", 100_000_000, 1, len(bomb)) + bomb, 1)),
    "bad_offset.fbx": MAGIC + struct.pack("<I", 7500) + struct.pack("<QQQB", 10 ** 12, 0, 0, 1) + b"X" + b"\0" * 200,
    "deep.fbx": fbx_file(nested(3000, 27)),
    "ascii.fbx": b"; FBX 7.4.0 project file\nFBXHeaderExtension:  {\n}\n",
    "old.fbx": MAGIC + struct.pack("<I", 6100) + b"\0" * 64,
    "empty_file.fbx": b"",
    "not_fbx.fbx": b"\x89PNG\r\n\x1a\n" + os.urandom(64),
}
hostile_dir = WORK / "hostile"
hostile_dir.mkdir()
for name, data in hostile.items():
    (hostile_dir / name).write_bytes(data)
started = time.monotonic()
completed = run_gate(hostile_dir, "--once", "--json")
elapsed = time.monotonic() - started
rows = json_verdicts(completed)
expect("no traceback on hostile FBX", "Traceback" not in completed.stderr, completed.stderr[-400:])
expect("hostile corpus judged in under 30 s", elapsed < 30, f"{elapsed:.1f}s")
for name in hostile:
    row = rows.get(name)
    expect(f"{name} is FAILED, not skipped or passed", row is not None and row["status"] == "FAILED",
           row and row["problems"])
expect("an ASCII FBX with no version fails with a re-export hint",
       "re-export" in " ".join(rows.get("ascii.fbx", {}).get("problems", [])))
expect("the decompression bomb is refused by size, not by running out of memory",
       "too large" in " ".join(rows.get("bomb.fbx", {}).get("problems", [])))

# A .glb's JSON is read a dozen times per check; each read used to load the
# whole file, binary chunk and all.
_json = json.dumps({"asset": {"version": "2.0"}}).encode() + b"  "
_bin = b"\0" * (4 << 20)
_glb = WORK / "big_bin.glb"
_glb.write_bytes(b"glTF" + struct.pack("<II", 2, 28 + len(_json) + len(_bin))
                 + struct.pack("<II", len(_json), 0x4E4F534A) + _json
                 + struct.pack("<II", len(_bin), 0x004E4942) + _bin)
_read = {"bytes": 0}
_real_open = Path.open


def _counting_open(self, *args, **kwargs):
    handle = _real_open(self, *args, **kwargs)
    if self == _glb:
        _real_read = handle.read

        def read(*a):
            data = _real_read(*a)
            _read["bytes"] += len(data)
            return data
        handle.read = read
    return handle


_real_read_bytes = Path.read_bytes
with patch.object(Path, "open", _counting_open), \
        patch.object(Path, "read_bytes", lambda self: (_read.__setitem__("bytes", _read["bytes"] + self.stat().st_size)
                                                       if self == _glb else None) or _real_read_bytes(self)):
    _document = agk.read_gltf_document(_glb)
expect("a .glb's JSON is read without reading its binary chunk",
       _document == {"asset": {"version": "2.0"}} and _read["bytes"] < 1024, _read)
for _data, _why in ((b"", "not a GLB container"), (b"glTF\x02\0\0\0\xff\xff\xff\xff", "no JSON chunk")):
    _glb.write_bytes(_data)
    try:
        agk.read_gltf_document(_glb)
        expect(f"a broken .glb still says what is wrong ({_why})", False)
    except agk.AssetError as exc:
        expect(f"a broken .glb still says what is wrong ({_why})", _why in str(exc), str(exc))

print("4. TGA and JPG textures")
try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None
tex = WORK / "textures"
tex.mkdir()
Image.new("RGB", (512, 512), (120, 110, 100)).save(tex / "rock_Albedo.tga")
Image.new("RGB", (300, 300), (120, 110, 100)).save(tex / "npot_Albedo.tga")
Image.new("RGB", (4096, 4096), (120, 110, 100)).save(tex / "huge_Albedo.jpg", quality=70)
Image.new("RGB", (256, 256), (0, 0, 0)).save(tex / "black_Albedo.jpg")
Image.new("RGB", (256, 256), (120, 110, 100)).save(tex / "wood_Albedo.jpeg")
Image.new("RGB", (256, 256), (120, 110, 100)).save(tex / "fake_Albedo.tga", format="JPEG")
Image.new("RGB", (256, 256), (120, 110, 100)).save(tex / "fake_Albedo.jpg", format="PNG")
noise = Image.effect_noise((512, 512), 60).convert("RGB")
for suffix, fmt in ((".png", "PNG"), (".jpg", "JPEG"), (".tga", "TGA")):
    whole = tex / f"whole_Roughness{suffix}"
    noise.save(whole, format=fmt)
    data = whole.read_bytes()
    (tex / f"cut_Roughness{suffix}").write_bytes(data[: len(data) // 2])
rows = json_verdicts(run_gate(tex, "--once", "--json"))
expect("a 512 TGA passes", rows.get("rock_Albedo.tga", {}).get("passed") is True, rows.get("rock_Albedo.tga"))
expect("a .jpeg passes", rows.get("wood_Albedo.jpeg", {}).get("passed") is True)
expect("an NPOT TGA fails", "not a power of two" in " ".join(rows.get("npot_Albedo.tga", {}).get("problems", [])))
expect("a 4096 JPG fails the 2048 limit", "exceeds" in " ".join(rows.get("huge_Albedo.jpg", {}).get("problems", [])))
expect("a black JPG albedo fails the PBR floor", "PBR floor" in " ".join(rows.get("black_Albedo.jpg", {}).get("problems", [])))
expect("a JPEG named .tga is caught", "actually JPEG" in " ".join(rows.get("fake_Albedo.tga", {}).get("problems", [])))
expect("a PNG named .jpg is caught", "actually PNG" in " ".join(rows.get("fake_Albedo.jpg", {}).get("problems", [])))
for suffix in (".png", ".jpg", ".tga"):
    expect(f"an intact {suffix} passes", rows.get(f"whole_Roughness{suffix}", {}).get("passed") is True)
    expect(f"a truncated {suffix} fails instead of passing on its header",
           "truncated" in " ".join(rows.get(f"cut_Roughness{suffix}", {}).get("problems", [])),
           rows.get(f"cut_Roughness{suffix}"))

print("5. Healing never orphans a reference")
spec = __import__("importlib.util").util.spec_from_file_location("agk_healer_formats", ROOT / "healer.py")
healer_module = __import__("importlib.util").util.module_from_spec(spec)
sys.modules[spec.name] = healer_module
spec.loader.exec_module(healer_module)
heal_budget = agk.Budget()


def heal_run(root, texture):
    healer = healer_module.Healer(heal_budget, root=root,
                                  resolve_references=agk.resolve_references)
    return agk.Gatekeeper(heal_budget, healer=healer).check(texture)


binary_dir = WORK / "binary_ref"
binary_dir.mkdir()
Image.new("RGB", (64, 64), (120, 110, 100)).save(binary_dir / "wood.png")


def glb_pointing_at(path, uri):
    document = json.dumps({"asset": {"version": "2.0"}, "images": [{"uri": uri}]}).encode()
    document += b" " * (-len(document) % 4)
    body = struct.pack("<II", len(document), 0x4E4F534A) + document
    path.write_bytes(struct.pack("<III", 0x46546C67, 2, 12 + len(body)) + body)


glb_pointing_at(binary_dir / "crate.glb", "wood.png")
heal_run(binary_dir, binary_dir / "wood.png")
expect("a texture a binary .glb points at is not renamed",
       (binary_dir / "wood.png").exists() and not (binary_dir / "wood_Albedo.png").exists())
other = WORK / "binary_other"
(other / "a").mkdir(parents=True)
(other / "b").mkdir()
Image.new("RGB", (64, 64), (120, 110, 100)).save(other / "a" / "wood.png")
Image.new("RGB", (64, 64), (120, 110, 100)).save(other / "b" / "wood.png")
glb_pointing_at(other / "b" / "crate.glb", "wood.png")
heal_run(other, other / "a" / "wood.png")
expect("a .glb naming a *different* wood.png does not block the rename",
       (other / "a" / "wood_Albedo.png").exists(), sorted(p.name for p in (other / "a").iterdir()))
outside = WORK / "outside_ref"
(outside / "Textures").mkdir(parents=True)
(outside / "Meshes").mkdir()
Image.new("RGB", (64, 64), (120, 110, 100)).save(outside / "Textures" / "wood.png")
(outside / "Meshes" / "crate.mtl").write_text("newmtl a\nmap_Kd ../Textures/wood.png\n", encoding="utf-8")
heal_run(outside / "Textures", outside / "Textures" / "wood.png")
expect("watching only Textures/ does not rename a texture a mesh outside points at",
       (outside / "Textures" / "wood.png").exists()
       and "wood.png" in (outside / "Meshes" / "crate.mtl").read_text(encoding="utf-8"))
alone = WORK / "unreferenced"
(alone / "Textures").mkdir(parents=True)
Image.new("RGB", (64, 64), (120, 110, 100)).save(alone / "Textures" / "wood.png")
heal_run(alone / "Textures", alone / "Textures" / "wood.png")
expect("an unreferenced texture is still renamed", (alone / "Textures" / "wood_Albedo.png").exists())

print("6. Remote rules: unreachable vs invalid, exit 2")
cache_dir = WORK / "remote"
cache_dir.mkdir()


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


for label, effect, word in (
        ("network down", urllib.error.URLError("refused"), "unreachable"),
        ("invalid budget", FakeResponse(b'{"mesh": {"max_triangles": -1}}'), "invalid"),
        ("not a table", FakeResponse(b"[1, 2]"), "invalid")):
    kwargs = {"side_effect": effect} if isinstance(effect, Exception) else {"return_value": effect}
    with patch("urllib.request.urlopen", **kwargs):
        try:
            agk.fetch_remote_rules("https://studio.example/rules.json", cache_dir / "cold.json")
            expect(f"{label}: refused without a cache", False)
        except agk.ConfigError as exc:
            expect(f"{label}: says '{word}'", f"remote rules {word}" in str(exc), str(exc)[:120])
(cache_dir / "a.obj").write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nf 1 2 3\n", encoding="utf-8")
with patch("urllib.request.urlopen", return_value=FakeResponse(b'{"mesh": {"max_triangles": -1}}')):
    code = agk.main([str(cache_dir), "--once", "--no-color", "--no-notify",
                     "--remote-rules", "https://studio.example/rules.json"])
expect("invalid remote rules exit 2 from main(), not a traceback", code == 2, code)

print("7. Telemetry failures are visible once")
with captured_logs() as lines:
    telemetry = agk.Telemetry("http://127.0.0.1:9/api/telemetry", timeout=0.5)
    for index in range(3):
        telemetry.submit(agk.Verdict(cache_dir / f"m{index}.obj"))
    telemetry.close(timeout=10)
warnings = [line for line in lines if "not being recorded" in line]
expect("one warning for three failed posts", len(warnings) == 1, warnings)
expect("a closing count of undelivered verdicts",
       any("3 verdict(s) not delivered" in line for line in lines), lines[-3:])

print("8. --workstation-url and --workstation-token reach the server")
import asset_health_server as ahs  # noqa: E402
seen = {}


def fake_build(*args, **kwargs):
    seen.update(kwargs)
    raise SystemExit(0)


with patch.object(ahs, "build_server", side_effect=fake_build):
    try:
        ahs.main(["--port", "0", "--db", str(WORK / "ws_flag.db"), "--quiet", "--no-seed",
                  "--workstation-url", "http://review-box:8100", "--workstation-token", "t0k"])
    except SystemExit:
        pass
expect("the URL is passed on", seen.get("workstation_url") == "http://review-box:8100", seen)
expect("the token is passed on", seen.get("workstation_token") == "t0k", seen)

print("9. Unreal's blocking import path fails closed")
fake_unreal = types.ModuleType("unreal")
fake_unreal.imported = []
fake_unreal.log = fake_unreal.log_warning = fake_unreal.log_error = lambda message: None


class _Tools:
    def import_asset_tasks(self, tasks):
        fake_unreal.imported.extend(task.filename for task in tasks)


fake_unreal.AssetToolsHelpers = types.SimpleNamespace(get_asset_tools=lambda: _Tools())
sys.modules["unreal"] = fake_unreal
sys.path.insert(0, str(ROOT / "plugins" / "unreal"))
import import_gatekeeper as ig  # noqa: E402

Task = types.SimpleNamespace
ue = WORK / "unreal"
ue.mkdir()
shutil.copy(FIXTURES / "cube_subd3.fbx", ue / "good.fbx")
shutil.copy(FIXTURES / "mats5.fbx", ue / "heavy.fbx")
(ue / "click.wav").write_bytes(b"RIFF....WAVE")
configured = {"ASSETGATEKEEPER_PYTHON": sys.executable, "ASSETGATEKEEPER_HOME": str(ROOT)}


def ue_import(files, env):
    fake_unreal.imported.clear()
    with patch.dict(os.environ, env, clear=False):
        for key in ("ASSETGATEKEEPER_PYTHON", "ASSETGATEKEEPER_HOME"):
            if key not in env:
                os.environ.pop(key, None)
        try:
            ig.import_with_gatekeeper([Task(filename=str(ue / name)) for name in files])
            return "imported", list(fake_unreal.imported)
        except ig.GatekeeperBlocked as blocked:
            return "blocked", blocked.problems


expect("a good FBX imports", ue_import(["good.fbx"], configured)[0] == "imported")
outcome = ue_import(["heavy.fbx"], configured)
expect("an over-budget FBX is blocked, naming the file",
       outcome[0] == "blocked" and outcome[1][0].startswith("heavy.fbx:"), outcome)
expect("non-art files are not gated", ue_import(["click.wav"], {})[0] == "imported")
outcome = ue_import(["good.fbx"], {})
expect("an unconfigured gate blocks gated files instead of waving them through",
       outcome[0] == "blocked" and "not configured" in outcome[1][0], outcome)

# Verified in UE 5.3.2: an editor started by another DCC inherits its PYTHONHOME,
# and the gate's own interpreter then died "SRE module mismatch" on every file.
bogus_home = WORK / "someone_elses_python"
bogus_home.mkdir()
outcome = ue_import(["good.fbx"], {**configured, "PYTHONHOME": str(bogus_home)})
expect("an inherited PYTHONHOME (editor launched by another DCC) does not block a good FBX",
       outcome[0] == "imported", outcome)
child = ig.child_environment({"PYTHONHOME": "x", "PYTHONPATH": os.pathsep.join(
    [str(Path(sys.prefix) / "Lib"), str(WORK / "studio_site")])})
expect("the gate's environment drops PYTHONHOME and the host's own PYTHONPATH, keeps the studio's",
       "PYTHONHOME" not in child and child.get("PYTHONPATH") == str(WORK / "studio_site"), child)

fake_unreal.StaticMesh = type("StaticMesh", (), {})
import inspect  # noqa: E402
import init_unreal  # noqa: E402
expect("init_unreal's post-import handler takes OnAssetPostImport's (factory, created_object)",
       list(inspect.signature(init_unreal._on_post_import).parameters) == ["factory", "created_object"],
       str(inspect.signature(init_unreal._on_post_import)))

# Drag-and-drop rejection (verified in UE 5.3.2; these pin the logic without it).
class _FakeLibrary:
    def __init__(self, existing=(), referencers=None):
        self.assets = set(existing)
        self.referencers = referencers or {}
        self.calls = []

    def does_asset_exist(self, path):
        return path in self.assets

    def rename_asset(self, source, target):
        self.calls.append(("rename", source, target))
        self.assets.discard(source)
        self.assets.add(target)
        return True

    def delete_asset(self, path):
        self.calls.append(("delete", path))
        self.assets.discard(path)
        return True

    def find_package_referencers_for_asset(self, path, load_assets=False):
        return self.referencers.get(path, [])


told = []
init_unreal.notify = lambda title, message: told.append(f"{title}: {message}")
library = _FakeLibrary({"/Game/Props/rock", "/Game/_AssetGatekeeper_Rejected/rock"})
fake_unreal.EditorAssetLibrary = library
with patch.dict(os.environ, {}, clear=False):
    os.environ.pop("ASSETGATEKEEPER_UNREAL_ON_FAIL", None)
    expect("drag-and-drop failures are quarantined by default", init_unreal.on_fail_mode() == "quarantine")
    os.environ["ASSETGATEKEEPER_UNREAL_ON_FAIL"] = "Nuke"
    expect("an unknown mode falls back to quarantine", init_unreal.on_fail_mode() == "quarantine")
outcome = init_unreal.reject("/Game/Props/rock", "quarantine", ["240,000 triangles exceeds the 50,000 budget"])
expect("quarantine moves the asset out, without overwriting an earlier reject",
       outcome == "quarantined" and library.calls == [("rename", "/Game/Props/rock", "/Game/_AssetGatekeeper_Rejected/rock_2")],
       library.calls)
expect("the artist is told where it went and why",
       told and "rock_2" in told[-1] and "240,000 triangles" in told[-1], told)
library = _FakeLibrary({"/Game/Props/rock"}, {"/Game/Props/rock": ["/Game/Maps/Level01"]})
fake_unreal.EditorAssetLibrary = library
outcome = init_unreal.reject("/Game/Props/rock", "delete", ["5 material slots exceeds the 3 slot budget"])
expect("an asset something references is never moved or deleted",
       outcome == "warned-referenced" and library.calls == [], (outcome, library.calls))
library = _FakeLibrary({"/Game/Props/crate"})
fake_unreal.EditorAssetLibrary = library
expect("delete mode deletes", init_unreal.reject("/Game/Props/crate", "delete", ["x"]) == "deleted"
       and ("delete", "/Game/Props/crate") in library.calls, library.calls)


# Verified in UE 5.3.2 by hand: one drag-and-drop reported the mesh twice, and
# the second report logged "could NOT be moved" after the first had moved it.
class _FakeMesh(fake_unreal.StaticMesh):
    def __init__(self, path):
        self.path = path

    def get_path_name(self):
        return self.path + "." + self.path.rsplit("/", 1)[-1]

    def get_name(self):
        return self.path.rsplit("/", 1)[-1]


saved_budget, saved_check = init_unreal.load_budget, init_unreal.check_static_mesh
init_unreal.load_budget = lambda: {"max_triangles": 50000}
init_unreal.check_static_mesh = lambda asset, budget: ["240,000 triangles exceeds the 50,000 budget"]
library = _FakeLibrary({"/Game/DragTest/SM_2_TooHeavy"})
fake_unreal.EditorAssetLibrary = library
told.clear()
mesh = _FakeMesh("/Game/DragTest/SM_2_TooHeavy")
init_unreal._on_post_import(None, mesh)
init_unreal._on_post_import(None, mesh)          # the engine's second report
expect("a mesh reported twice by one import is moved once",
       [c for c in library.calls if c[0] == "rename"] == [
           ("rename", "/Game/DragTest/SM_2_TooHeavy", "/Game/_AssetGatekeeper_Rejected/SM_2_TooHeavy")],
       library.calls)
expect("... and the artist gets one message, not a false 'could NOT be moved'",
       len(told) == 1 and "could NOT" not in told[0], told)
init_unreal.load_budget, init_unreal.check_static_mesh = saved_budget, saved_check
told.clear()
expect("rejecting an asset that is already gone is quiet",
       init_unreal.reject("/Game/DragTest/SM_2_TooHeavy", "quarantine", ["x"]) == "gone" and not told, told)
ticks = []
fake_unreal.register_slate_post_tick_callback = lambda fn: ticks.append(fn) or len(ticks)
fake_unreal.unregister_slate_post_tick_callback = lambda handle: None
library = _FakeLibrary({"/Game/DragTest/SM_3_FiveMaterials"})
fake_unreal.EditorAssetLibrary = library
init_unreal._schedule("/Game/DragTest/SM_3_FiveMaterials", "quarantine", ["5 material slots"])
init_unreal._schedule("/Game/DragTest/SM_3_FiveMaterials", "quarantine", ["5 material slots"])
init_unreal._process_pending()
expect("the same asset queued twice in one tick is handled once",
       len(library.calls) == 1 and len(told) == 1, (library.calls, told))
del fake_unreal.register_slate_post_tick_callback, fake_unreal.unregister_slate_post_tick_callback


class _FakeEvent:
    def __init__(self):
        self.handlers = []

    def contains_callable(self, handler):
        return handler in self.handlers

    def add_callable_unique(self, handler):
        if handler not in self.handlers:
            self.handlers.append(handler)


subsystem = types.SimpleNamespace(on_asset_pre_import=_FakeEvent(), on_asset_post_import=_FakeEvent())
fake_unreal.ImportSubsystem = object
fake_unreal.get_editor_subsystem = lambda cls: subsystem
first = init_unreal.ensure_bound(force=True)
subsystem.on_asset_post_import.handlers.clear()          # the engine drops a hook
again = init_unreal.ensure_bound(force=True)
expect("hooks that the engine drops are re-attached by the watchdog",
       first == ["on_asset_pre_import", "on_asset_post_import"] and again == ["on_asset_post_import"],
       (first, again))
expect("a bound hook is not bound twice", init_unreal.ensure_bound(force=True) == [])

print("9b. Unreal Data Validation (G6): the rules on save and on Validate Changelist")
import agk_validator  # noqa: E402
import asset_gatekeeper as _agk  # noqa: E402
_default = _agk.Budget()
expect("init_unreal says why it did not register validation in an editor without it",
       init_unreal.register_validation() is False)
expect("a mesh over budget gets the gate's own wording",
       agk_validator.mesh_problems({"triangles": 60_000, "material_slots": 4, "uv_channels": 2}, _default)
       == ["60,000 triangles exceeds the 50,000 budget (+10,000)",
           "4 material slots exceeds the 3 slot budget (draw calls)"])
expect("UV channels count too", agk_validator.mesh_problems(
    {"triangles": 10, "material_slots": 1, "uv_channels": 6}, _default)
    == ["6 UV channels exceeds the 4 channel budget"])
expect("a mesh within budget passes",
       agk_validator.mesh_problems({"triangles": 4_000, "material_slots": 2, "uv_channels": 1}, _default) == [])
_ue_rules = WORK / "ue_rules.toml"
_ue_rules.write_text('[mesh]\nmax_triangles = 50000\n\n[[exceptions]]\npath = "Characters/Boss/*"\n'
                     'rule = "max_triangles"\nallow = 80000\nreason = "hero boss"\nowner = "lena"\n'
                     'expires = 2026-12-01\n', encoding="utf-8")
agk_validator._cache.clear()
with patch.dict(os.environ, {"ASSETGATEKEEPER_RULES": str(_ue_rules)}):
    _budget, _exceptions = agk_validator.load_rules(_agk)
_boss = {"triangles": 72_500, "material_slots": 2, "uv_channels": 1}
expect("an [[exceptions]] path matches the content path below /Game",
       agk_validator.mesh_problems(_boss, _budget, _exceptions, "/Game/Characters/Boss/SM_Boss.SM_Boss",
                                   today=_agk.date(2026, 11, 2)) == [])
expect("... and not another folder",
       agk_validator.mesh_problems(_boss, _budget, _exceptions, "/Game/Props/SM_Statue.SM_Statue",
                                   today=_agk.date(2026, 11, 2))[0].startswith("72,500 triangles"))
_late = agk_validator.mesh_problems(_boss, _budget, _exceptions, "/Game/Characters/Boss/SM_Boss",
                                    today=_agk.date(2026, 12, 2))
expect("... and the day after it ends, the failure says the exception expired",
       len(_late) == 2 and "expired on 2026-12-01" in _late[1], _late)
expect("a texture: power of two, size and naming, as the gate words them",
       agk_validator.texture_problems({"width": 1000, "height": 600, "name": "T_Rock"}, _default, _agk)
       == ["1000x600 is not a power of two",
           "name does not end with a standard suffix "
           f"({_agk.describe_suffixes(_agk.Budget().texture_suffixes)})"]
       and agk_validator.texture_problems({"width": 4096, "height": 4096, "name": "T_Rock_Albedo"},
                                          _default, _agk)[0].startswith("4096x4096 exceeds")
       and agk_validator.texture_problems({"width": 2048, "height": 1024, "name": "T_Rock_Albedo"},
                                          _default, _agk) == [])


class _ValidatorBase:
    def __init__(self):
        self.log = []

    def asset_fails(self, asset, message, errors):
        self.log.append(("fail", message))
        return list(errors) + [message]

    def asset_passes(self, asset):
        self.log.append(("pass", None))

    def asset_warning(self, asset, message):
        self.log.append(("warn", message))


class _ValidatorSubsystem:
    def __init__(self):
        self.validators = []

    def add_validator(self, validator):
        self.validators.append(validator)


class _UvSubsystem:
    def get_num_uv_channels(self, mesh, lod):
        return mesh.uvs


_validation = _ValidatorSubsystem()
fake_unreal.EditorValidatorBase = _ValidatorBase
fake_unreal.EditorValidatorSubsystem = _ValidatorSubsystem
fake_unreal.StaticMeshEditorSubsystem = _UvSubsystem
fake_unreal.Texture2D = type("Texture2D", (), {})
fake_unreal.uclass = lambda: (lambda cls: cls)
fake_unreal.ufunction = lambda **kw: (lambda fn: fn)
fake_unreal.DataValidationResult = types.SimpleNamespace(VALID="VALID", INVALID="INVALID",
                                                         NOT_VALIDATED="NOT_VALIDATED")
fake_unreal.get_editor_subsystem = lambda cls: (_validation if cls is _ValidatorSubsystem
                                                else _UvSubsystem() if cls is _UvSubsystem else subsystem)


class _UeMesh(fake_unreal.StaticMesh):
    def __init__(self, path, triangles, slots, uvs=1):
        self.path, self.triangles, self.slots, self.uvs = path, triangles, slots, uvs

    def get_path_name(self):
        return self.path + "." + self.path.rsplit("/", 1)[-1]

    def get_num_lods(self):
        return 1

    def get_num_triangles(self, lod):
        return self.triangles

    def get_editor_property(self, name):
        return [object()] * self.slots


with patch.dict(os.environ, {"ASSETGATEKEEPER_UNREAL_VALIDATE": "off"}):
    expect("ASSETGATEKEEPER_UNREAL_VALIDATE=off leaves validation to the import check",
           agk_validator.register_validator(fake_unreal) is None)
agk_validator._cache.clear()
with patch.dict(os.environ, {"ASSETGATEKEEPER_RULES": ""}):
    _validator = agk_validator.register_validator(fake_unreal)
    expect("the validator registers with the editor's validator subsystem, once",
           _validator is not None and _validation.validators == [_validator]
           and agk_validator.register_validator(fake_unreal) is _validator)
    _heavy, _light = _UeMesh("/Game/Props/SM_Heavy", 60_000, 2), _UeMesh("/Game/Props/SM_Light", 800, 1)
    expect("it validates static meshes and textures, nothing else",
           _validator.can_validate_asset(_heavy) and _validator.can_validate_asset(fake_unreal.Texture2D())
           and not _validator.can_validate_asset(object()))
    _result, _errors = _validator.validate_loaded_asset(_heavy, [])
    expect("an over-budget mesh is INVALID with the gate's reason in the Message Log",
           _result == "INVALID" and _errors == ["AssetGatekeeper: 60,000 triangles exceeds the 50,000 budget (+10,000)"],
           (_result, _errors))
    _validator.log.clear()
    _result, _errors = _validator.validate_loaded_asset(_light, [])
    expect("a mesh within budget is VALID", _result == "VALID" and _validator.log == [("pass", None)])

    class _Unreadable(_UeMesh):
        def get_num_triangles(self, lod):
            raise RuntimeError("API drift")
    _result, _errors = _validator.validate_loaded_asset(_Unreadable("/Game/Props/SM_Odd", 0, 1), [])
    expect("a mesh whose triangle count cannot be read fails, never passes as 0 triangles",
           _result == "INVALID" and "NOT checked" in _errors[0], (_result, _errors))
    _saved_engine = agk_validator.engine_module

    def _no_engine():
        raise ImportError("asset_gatekeeper")
    agk_validator.engine_module = _no_engine
    _validator.log.clear()
    _result, _ = _validator.validate_loaded_asset(_heavy, [])
    expect("on a machine without the gate it says NOT checked, never VALID",
           _result == "NOT_VALIDATED" and "NOT checked" in _validator.log[0][1], _validator.log)
    agk_validator.engine_module = _saved_engine
_broken_rules = WORK / "ue_broken.toml"
_broken_rules.write_text("[mesh]\nmax_triangles = 'lots'\n", encoding="utf-8")
agk_validator._cache.clear()
with patch.dict(os.environ, {"ASSETGATEKEEPER_RULES": str(_broken_rules)}):
    _result, _errors = _validator.validate_loaded_asset(_light, [])
expect("a broken rules file fails validation with the reason (it does not pass assets unchecked)",
       _result == "INVALID" and "could not check" in _errors[0], _errors)
agk_validator._cache.clear()

# Blender add-on (verified in Blender 4.0.2; these pin the fixes without it).
fake_bpy = types.ModuleType("bpy")
fake_bpy.types = types.SimpleNamespace(PropertyGroup=object, Operator=object, Panel=object)
fake_bpy.props = types.SimpleNamespace(StringProperty=lambda **kw: None,
                                       PointerProperty=lambda **kw: None)
fake_bpy.data = types.SimpleNamespace(filepath="")
sys.modules["bpy"] = fake_bpy
sys.path.insert(0, str(ROOT / "plugins" / "blender"))
import addon as blender_addon  # noqa: E402
lines = blender_addon.wrap_text("159,200 triangles exceeds the 50,000 budget (+109,200)", 190)
expect("the Blender panel wraps a long reason instead of cutting it to '159,200 tri...'",
       len(lines) > 1 and " ".join(lines) == "159,200 triangles exceeds the 50,000 budget (+109,200)", lines)
child = blender_addon.child_environment({"PYTHONHOME": "C:/Houdini/python313", "PATH": "x"})
expect("the Blender add-on drops an inherited PYTHONHOME before running the engine",
       "PYTHONHOME" not in child and child.get("PATH") == "x", child)
expect("the Blender add-on reports version 0.1.0", blender_addon.bl_info["version"] == (0, 1, 0))

print("9b. ASCII FBX, collision meshes and LOD groups (files exported by Unreal 5.3.2)")
UE_FIXTURES = ROOT / "tests" / "fixtures" / "fbx_ue"
ue_truth = json.loads((UE_FIXTURES / "truth.json").read_text(encoding="utf-8"))
for name, want in ue_truth.items():
    if name.startswith("_"):
        continue
    summary = agk.read_fbx(UE_FIXTURES / name)
    got = {"triangles": summary.triangles, "materials": summary.material_slots,
           "collision": summary.collision_meshes, "lower_lod": summary.lower_lod_meshes}
    expect(f"{name} matches Unreal ({want['triangles']} tris, {want['materials']} mat)", got == want,
           f"got {got}")
binary = agk.read_fbx(UE_FIXTURES / "cube_lod_binary.fbx")
text = agk.read_fbx(UE_FIXTURES / "cube_lod_ascii.fbx")
expect("ASCII and binary exports of the same mesh measure the same", binary == text, (binary, text))
ascii_dir = WORK / "ascii_gate"
ascii_dir.mkdir()
shutil.copy(UE_FIXTURES / "mats5_ascii_7300.fbx", ascii_dir / "mats5_ascii.fbx")
completed = run_gate(ascii_dir, "--once", "--json")
rows = json_verdicts(completed)
expect("the gate fails an ASCII FBX on its materials, not on its format",
       "5 material slots" in " ".join(rows.get("mats5_ascii.fbx", {}).get("problems", [])),
       rows.get("mats5_ascii.fbx"))
original = (UE_FIXTURES / "mats5_ascii_7300.fbx").read_bytes()
broken = {
    "ascii_truncated.fbx": original[: len(original) // 2],
    "ascii_short_array.fbx": original.replace(b"Vertices: *24", b"Vertices: *2400", 1),
    "ascii_old.fbx": original.replace(b"FBXVersion: 7300", b"FBXVersion: 6100", 1),
    "ascii_garbage.fbx": b"; FBX 7.3.0 project file\n" + bytes(range(256)) * 20,
    "ascii_huge_count.fbx": original.replace(b"Vertices: *24", b"Vertices: *999999999999", 1),
}
broken_dir = WORK / "ascii_hostile"
broken_dir.mkdir()
for name, data in broken.items():
    (broken_dir / name).write_bytes(data)
completed = run_gate(broken_dir, "--once", "--json")
rows = json_verdicts(completed)
expect("no traceback on broken ASCII FBX", "Traceback" not in completed.stderr, completed.stderr[-400:])
for name in broken:
    row = rows.get(name)
    expect(f"{name} is FAILED, not skipped or passed", row is not None and row["status"] == "FAILED",
           row and row["problems"])
expect("a pre-2011 ASCII FBX gets the re-export hint",
       "too old" in " ".join(rows.get("ascii_old.fbx", {}).get("problems", [])))

# 3ds Max pre-flight against a stand-in pymxs (no Max here: the developer's test plan
# covers the real thing). Pins what is counted, not how Max reports it.
class _MaxClass:
    def __init__(self, name):
        self.name = name


class _MaxNode:
    def __init__(self, cls, superclass, triangles, material=None, name="SM_Node"):
        self.cls, self.superclass, self.triangles, self.material = cls, superclass, triangles, material
        self.name = name


class _MaxSelection(list):
    @property
    def count(self):
        return len(self)


max_rt = types.SimpleNamespace()
for _name in ("Sphere", "Box", "Editable_Poly", "Targetobject", "BoneGeometry", "Multimaterial",
              "StandardMaterial", "Physical_Material", "Bitmaptexture", "GeometryClass", "Camera"):
    setattr(max_rt, _name, _MaxClass(_name))
max_rt.classOf = lambda item: getattr(item, "cls", None)
max_rt.superClassOf = lambda item: getattr(item, "superclass", None)
max_rt.getPolygonCount = lambda node: [node.triangles, node.triangles // 2]
physical = types.SimpleNamespace(cls=max_rt.Physical_Material, name="rock_mat",
                                 base_color_map=types.SimpleNamespace(
                                     cls=max_rt.Bitmaptexture, filename="C:/maps/rock_Albedo.png",
                                     name="rock", bitmap=types.SimpleNamespace(width=4096, height=4096)))
multi = types.SimpleNamespace(cls=max_rt.Multimaterial, materialList=[
    types.SimpleNamespace(cls=max_rt.StandardMaterial, name="a"), None,
    types.SimpleNamespace(cls=max_rt.StandardMaterial, name="b")])
max_rt.selection = _MaxSelection([
    _MaxNode(max_rt.Sphere, max_rt.GeometryClass, 200_000, physical),     # unconverted primitive
    _MaxNode(max_rt.Editable_Poly, max_rt.GeometryClass, 1_000, multi),
    _MaxNode(max_rt.Targetobject, max_rt.GeometryClass, 12),             # a camera target
    _MaxNode(max_rt.Camera, max_rt.Camera, 0),
])
max_rt.objects = list(max_rt.selection)
fake_pymxs = types.ModuleType("pymxs")
fake_pymxs.runtime = max_rt
sys.modules["pymxs"] = fake_pymxs
sys.path.insert(0, str(ROOT / "plugins" / "max"))
import max_preflight  # noqa: E402
max_triangles, _, max_materials, max_textures, _, _ = max_preflight.scene_facts()
expect("Max: an unconverted primitive (Sphere) is measured, a camera target is not",
       max_triangles == 201_000, max_triangles)
expect("Max: Physical Material and Multi/Sub-Object slots are counted",
       max_materials == 3, max_materials)
expect("Max: a Physical Material's base colour map is found",
       ("rock_Albedo.png", 4096, 4096) in max_textures, max_textures)

# DCC Pythons (Unreal 5.3: 3.9, Blender 4.0: 3.10, Maya 2022-24) have no tomllib:
# the engine must still read rules.toml there, through the bundled tomli.
probe = ("import sys\nsys.modules['tomllib'] = None\nsys.modules['tomli'] = None\n"
         f"sys.path.insert(0, {str(ROOT)!r})\n"
         "import asset_gatekeeper as a\nfrom pathlib import Path\n"
         "print(a.tomllib.__name__, a.Budget.from_mapping(a.parse_rules(Path(sys.argv[1]))).max_triangles)\n")
vendor_rules = WORK / "vendor_rules.toml"
vendor_rules.write_text("[mesh]\nmax_triangles = 1234\n", encoding="utf-8")
completed = subprocess.run([sys.executable, "-c", probe, str(vendor_rules)],
                           capture_output=True, text=True, timeout=120)
expect("without tomllib or tomli (DCC Pythons) rules.toml is read by the bundled tomli",
       completed.stdout.split() == ["agk_vendor.tomli", "1234"], completed.stdout + completed.stderr[-300:])

# Maya: an empty initialShadingGroup left connected after per-face assignment is
# not a material (verified in Maya 2027: 5 face materials read as 6).
class _FakeMayaCmds:
    members = {"agk_SG0": ["SM_mats.f[0:3]"], "agk_SG1": ["SM_mats.f[4:5]"], "initialShadingGroup": []}

    def listConnections(self, node, type=None):
        return list(self.members)

    def sets(self, engine, query=True):
        return self.members[engine]

    def ls(self, items, long=False, objectsOnly=False, **kwargs):
        items = [items] if isinstance(items, str) else items
        return ["|SM_mats|SM_matsShape" if "SM_mats" in item else item for item in items]

    def listRelatives(self, node, parent=False, fullPath=False, **kwargs):
        return ["|SM_mats"]


fake_maya = types.ModuleType("maya")
fake_maya.cmds = _FakeMayaCmds()
sys.modules["maya"] = fake_maya
sys.modules["maya.cmds"] = fake_maya.cmds
sys.path.insert(0, str(ROOT / "plugins" / "maya"))
import gatekeeper_maya  # noqa: E402
import maya_preflight  # noqa: E402
expect("Maya shelf check: an empty, still-connected initialShadingGroup is not a material",
       gatekeeper_maya.shading_engines_used("|SM_mats|SM_matsShape") == {"agk_SG0", "agk_SG1"})
expect("Maya pre-flight: the same", sorted(maya_preflight._engines_used("|SM_mats|SM_matsShape"))
       == ["agk_SG0", "agk_SG1"])

# Maya exec()s userSetup.py with no __file__ (verified in Maya 2027): the setup
# used to die on NameError, so no menu or shelf button ever appeared.
deferred = []
fake_maya.cmds.about = lambda batch=False: False
fake_maya.cmds.evalDeferred = lambda fn, lowestPriority=False: deferred.append(fn)
fake_maya.cmds.warning = lambda text: deferred.append(("warning", text))
fake_maya.mel = types.SimpleNamespace(eval=lambda text: "")
sys.modules["maya.mel"] = fake_maya.mel
user_setup_globals = {"__name__": "__main__"}
exec(compile((ROOT / "plugins" / "maya" / "userSetup.py").read_text(encoding="utf-8"),
             "userSetup.py", "exec"), user_setup_globals)
expect("Maya userSetup.py runs without __file__ and defers building its menu and shelf button",
       len(deferred) == 1 and callable(deferred[0]), deferred)
expect("userSetup.py finds gatekeeper_maya.py's folder on sys.path",
       user_setup_globals["_scripts_dir"]().replace("\\", "/").endswith("plugins/maya"),
       user_setup_globals["_scripts_dir"]())

print("9c. DCC panels: [lod], [collision], [skeleton], [lightmap] judged as the gate judges the export")
# One scene, described once, written two ways: as the FBX the artist would
# export (judged by the gate) and as each host's live scene (judged by the
# panel through preflight_core). The panel's messages must be the gate's.
import dataclasses  # noqa: E402
import importlib.util  # noqa: E402

from plugins import preflight_core  # noqa: E402
import preflight_panel  # noqa: E402

PANEL_WORK = WORK / "panel_rules"
PANEL_WORK.mkdir()


def _box(size=1.0, x=0.0):
    corners = [(x + a * size, b * size, c * size) for a in (0, 1) for b in (0, 1) for c in (0, 1)]
    return corners, [[0, 1, 3, 2], [4, 6, 7, 5], [0, 4, 5, 1], [2, 3, 7, 6], [0, 2, 6, 4], [1, 5, 7, 3]]


def _l_prism():
    """An L-shaped block: concave (a 1 m dent in a 3 m hull), 12 vertices."""
    outline = [(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)]
    vertices = [(float(x), float(y), z) for z in (0.0, 1.0) for x, y in outline]
    polygons = [[5, 4, 3, 2, 1, 0], [6, 7, 8, 9, 10, 11]]
    polygons += [[i, (i + 1) % 6, (i + 1) % 6 + 6, i + 6] for i in range(6)]
    return vertices, polygons


def _corner_uvs(polygons, shift=0.0):
    """One UV per face corner: every face mapped to the whole 0-1 square (so the
    faces overlap), the first corner moved by *shift* (outside 0-1 when > 0)."""
    square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    uvs = [square[k % 4] for polygon in polygons for k in range(len(polygon))]
    if shift:
        uvs[0] = (uvs[0][0] + shift, uvs[0][1])
    return uvs


def _mesh(name, geometry, parent=None, kind="Mesh", uv_sets=(), material="crate_mat"):
    vertices, polygons = geometry
    return {"name": name, "kind": kind, "parent": parent, "vertices": vertices,
            "polygons": polygons, "uv_sets": [list(uvs) for uvs in uv_sets], "material": material}


def write_fbx(path, objects, bones=(), skin=None):
    """ASCII FBX 7.4. objects: _mesh() dicts (kind LodGroup/Null: no geometry);
    bones: [(name, parent bone index or None)]; skin: (mesh name, [(bone index,
    vertex ids, weights)])."""
    ids, lines, links = {}, [], []
    counter = [1000]

    def new_id():
        counter[0] += 1
        return counter[0]

    materials = {}
    for item in objects:
        model = ids[item["name"]] = new_id()
        lines.append(f'\tModel: {model}, "Model::{item["name"]}", "{item["kind"]}" {{\n\t}}')
        links.append((model, ids[item["parent"]] if item["parent"] else 0))
        if item["kind"] != "Mesh":
            continue
        geometry = item["geometry_id"] = new_id()
        flat = []
        for polygon in item["polygons"]:
            flat += list(polygon[:-1]) + [-polygon[-1] - 1]
        body = [f'\tGeometry: {geometry}, "Geometry::{item["name"]}", "Mesh" {{',
                f"\t\tVertices: *{len(item['vertices']) * 3} {{ a: "
                + ",".join(repr(float(c)) for v in item["vertices"] for c in v) + " }",
                f"\t\tPolygonVertexIndex: *{len(flat)} {{ a: {','.join(map(str, flat))} }}"]
        for index, uvs in enumerate(item["uv_sets"]):
            body += [f"\t\tLayerElementUV: {index} {{", '\t\t\tMappingInformationType: "ByPolygonVertex"',
                     '\t\t\tReferenceInformationType: "Direct"',
                     f"\t\t\tUV: *{len(uvs) * 2} {{ a: " + ",".join(repr(float(c)) for uv in uvs for c in uv)
                     + " }", "\t\t}"]
        lines += body + ["\t}"]
        links.append((geometry, model))
        if item["material"]:
            if item["material"] not in materials:
                materials[item["material"]] = new_id()
                lines.append(f'\tMaterial: {materials[item["material"]]}, '
                             f'"Material::{item["material"]}", "" {{\n\t}}')
            links.append((materials[item["material"]], model))
    bone_ids = []
    for name, parent in bones:
        bone_ids.append(new_id())
        lines.append(f'\tModel: {bone_ids[-1]}, "Model::{name}", "LimbNode" {{\n\t}}')
    for (name, parent), bone_id in zip(bones, bone_ids):
        links.append((bone_id, 0 if parent is None else bone_ids[parent]))
    if skin:
        owner, clusters = skin
        skin_id = new_id()
        lines.append(f'\tDeformer: {skin_id}, "Deformer::Skin", "Skin" {{\n\t}}')
        links.append((skin_id, next(item["geometry_id"] for item in objects if item["name"] == owner)))
        for bone, vertex_ids, weights in clusters:
            cluster = new_id()
            lines += [f'\tDeformer: {cluster}, "SubDeformer::Cluster", "Cluster" {{',
                      f"\t\tIndexes: *{len(vertex_ids)} {{ a: {','.join(map(str, vertex_ids))} }}",
                      f"\t\tWeights: *{len(weights)} {{ a: {','.join(repr(float(w)) for w in weights)} }}",
                      "\t}"]
            links += [(cluster, skin_id), (bone_ids[bone], cluster)]
    text = ("; FBX 7.4.0 project file\nFBXHeaderExtension:  {\n\tFBXVersion: 7400\n}\n"
            "Objects:  {\n" + "\n".join(lines) + "\n}\nConnections:  {\n"
            + "\n".join(f'\tC: "OO",{a},{b}' for a, b in links) + "\n}\n")
    path.write_text(text, encoding="utf-8")
    return path


#: Everything but the four structure tables generous, so the gate's verdict on
#: the export holds only what those tables say.
LENIENT = {"max_triangles": 1_000_000, "max_material_slots": 64, "max_uv_channels": 8,
           "enforce_texture_naming": False, "allow_degenerate": True}


def structure_budget(lod=None, collision=None, skeleton=None, lightmap=None):
    document = {key: value for key, value in (("lod", lod), ("collision", collision),
                                              ("skeleton", skeleton), ("lightmap", lightmap))
                if value is not None}
    return dataclasses.replace(agk.Budget.from_mapping(document), **LENIENT)


def gate_says(path, budget):
    verdict = agk.Gatekeeper(budget).check(path)
    return sorted(verdict.problems), sorted(verdict.warnings)


def panel_says(report):
    return (sorted(f.message for f in report.findings if f.severity == "error"),
            sorted(f.message for f in report.findings if f.severity == "warning"))


def same_verdict(label, report, path, budget):
    gate = gate_says(path, budget)
    panel = panel_says(report)
    expect(f"{label}: the panel says what the gate says ({len(gate[0])} error(s), "
           f"{len(gate[1])} warning(s))", panel == gate and gate[0], f"panel {panel} / gate {gate}")


# The crate: LOD0 and LOD1 that saves nothing, a concave hull named after LOD0,
# a stray hull, a lightmap set with a UV outside 0-1 and overlapping faces on
# LOD0, and no lightmap set on LOD1.
CRATE_LOD0 = _box()
CRATE_LOD1 = _box()
CRATE_HULL = _l_prism()
STRAY_HULL = _box(0.5, 3.0)
CRATE_UV = [_corner_uvs(CRATE_LOD0[1]), _corner_uvs(CRATE_LOD0[1], shift=1.5)]
crate_budget = structure_budget(lod={"min_levels": 3, "max_ratio": 0.6},
                                collision={"max_hull_vertices": 8},
                                lightmap={"require": True, "max_overlap": 0})


def crate_objects(group):
    """The crate as exported: under an LOD group (Maya) or by _LOD<n> names (Max, Blender)."""
    parent = "Crate" if group else None
    objects = [_mesh("Crate", ([], []), kind="LodGroup")] if group else []
    objects += [_mesh("Crate_LOD0", CRATE_LOD0, parent, uv_sets=CRATE_UV),
                _mesh("Crate_LOD1", CRATE_LOD1, parent, uv_sets=CRATE_UV[:1]),
                _mesh("UCX_Crate_LOD0", CRATE_HULL, parent, material=None),
                _mesh("UCX_Crat", STRAY_HULL, parent, material=None)]
    return objects


crate_group_fbx = write_fbx(PANEL_WORK / "crate_group.fbx", crate_objects(group=True))
crate_names_fbx = write_fbx(PANEL_WORK / "crate_names.fbx", crate_objects(group=False))

# The character: a skinned body, two roots, a three-bone chain, one section
# weighted to three bones; no collision (exempt), no lightmap (skinned).
BODY = _box()
BONES = [("root", None), ("spine", 0), ("head", 1), ("prop_root", None)]
BODY_CLUSTERS = [(0, [0, 1], [1.0, 1.0]), (1, [2, 3], [1.0, 1.0]), (2, [4], [1.0]), (3, [5], [1.0])]
body_fbx = write_fbx(PANEL_WORK / "body.fbx", [_mesh("Body", BODY, material="skin_mat")],
                     bones=BONES, skin=("Body", BODY_CLUSTERS))
body_budget = structure_budget(skeleton={"max_bones": 3, "single_root": True, "max_depth": 2,
                                         "max_bones_per_section": 2},
                               collision={"require": True},
                               lightmap={"require": True})


def faces_of(geometry):
    return preflight_core.fan_triangles([len(p) for p in geometry[1]], [i for p in geometry[1] for i in p])


def uv_triangles(geometry, uvs):
    corners = preflight_core.fan_triangles([len(p) for p in geometry[1]], list(range(len(uvs))))
    return [[uvs[a], uvs[b], uvs[c]] for a, b, c in corners]


def tris(geometry):
    return sum(len(p) - 2 for p in geometry[1])


# -- the core alone: facts as an adapter would pass them ---------------------- #
core_crate = preflight_core.assess(
    "crate_group.fbx", triangles=tris(CRATE_LOD0), budget=crate_budget,
    lods=[("Crate", (tris(CRATE_LOD0), tris(CRATE_LOD1)), "group")],
    collision=[("UCX_Crate_LOD0", CRATE_HULL[0], faces_of(CRATE_HULL)),
               ("UCX_Crat", STRAY_HULL[0], faces_of(STRAY_HULL))],
    render_names=["Crate_LOD0"],
    lightmap=[("Crate_LOD0", CRATE_UV[1], uv_triangles(CRATE_LOD0, CRATE_UV[1]))])
same_verdict("core, LOD group + collision + lightmap", core_crate, crate_group_fbx, crate_budget)
core_names = preflight_core.assess(
    "crate_names.fbx", triangles=tris(CRATE_LOD0) + tris(CRATE_LOD1), budget=crate_budget,
    lods=preflight_core.lods_by_name([("Crate_LOD0", tris(CRATE_LOD0)), ("Crate_LOD1", tris(CRATE_LOD1))]),
    collision=[("UCX_Crate_LOD0", CRATE_HULL[0], faces_of(CRATE_HULL)),
               ("UCX_Crat", STRAY_HULL[0], faces_of(STRAY_HULL))],
    render_names=["Crate_LOD0", "Crate_LOD1"],
    lightmap=[("Crate_LOD0", CRATE_UV[1], uv_triangles(CRATE_LOD0, CRATE_UV[1])),
              ("Crate_LOD1", None, None)])
same_verdict("core, LODs by name (with the gate's FBX note)", core_names, crate_names_fbx, crate_budget)
core_body = preflight_core.assess(
    "body.fbx", triangles=tris(BODY), budget=body_budget, collision=[], render_names=["Body"],
    skinned=True, lightmap=[],
    skeleton={"bones": [(name, None if parent is None else BONES[parent][0]) for name, parent in BONES],
              "sections": [("skin_mat", 4)]})
same_verdict("core, skeleton (bones, roots, depth, per section)", core_body, body_fbx, body_budget)
clean_fbx = write_fbx(PANEL_WORK / "clean.fbx", [_mesh("Barrel", _box(), uv_sets=[
    _corner_uvs(_box()[1]), [((k % 4) // 2 * 0.1 + f * 0.15, (k % 2) * 0.1) for f in range(6) for k in range(4)]]),
    _mesh("UCX_Barrel_00", _box(), material=None)])
clean_budget = structure_budget(collision={"require": True}, lightmap={"require": True})
clean_report = preflight_core.assess("clean.fbx", triangles=12, budget=clean_budget,
                                     collision=[("UCX_Barrel_00", _box()[0], faces_of(_box()))],
                                     render_names=["Barrel"], lightmap=[("Barrel", [(0.5, 0.5)] * 24, None)])
expect("core: a clean asset passes in the panel as at the gate",
       clean_report.ok and gate_says(clean_fbx, clean_budget) == ([], []),
       (clean_report.as_text(), gate_says(clean_fbx, clean_budget)))

# Rules off: the new inputs add nothing.
plain = preflight_core.assess("x", triangles=12, budget=agk.Budget())
everything = preflight_core.assess(
    "x", triangles=12, budget=agk.Budget(), lods=[], collision=[("UCX_Nothing", 4)],
    render_names=["X"], skinned=False, skeleton={"bones": [("a", None), ("b", None)], "sections": None},
    lightmap=[("X", None, None)])
expect("rules off: structure facts add no finding",
       [f.as_dict() for f in everything.findings] == [f.as_dict() for f in plain.findings] == [],
       everything.as_text())
expect("rules off: nothing is noted either (no 'not checked' for rules nobody asked for)",
       not preflight_core.assess("x", triangles=12, budget=agk.Budget()).findings)

# Data not available: an info note, never an error and never a pass.
unread = preflight_core.assess("x", triangles=12, budget=dataclasses.replace(crate_budget, max_bones=10))
codes = sorted((f.severity, f.code) for f in unread.findings)
expect("rules on, nothing supplied: one info note per rule, no error",
       unread.ok and codes == [("info", "collision"), ("info", "lightmap"), ("info", "lod"),
                               ("info", "skeleton")], codes)
expect("... saying the gate checks it on export",
       all(f.message.endswith("not checked in the panel; the gate checks it on export")
           for f in unread.findings), [f.message for f in unread.findings])
partial = preflight_core.assess(
    "x", triangles=12, budget=dataclasses.replace(crate_budget, max_bones_per_section=1),
    lods=[("Crate", (12, 6, 3), "group")], render_names=["Crate"],
    collision=[("UCX_Crate", 8)],
    skeleton={"bones": [("root", None)], "sections": None},
    lightmap=[("Crate", [(0.5, 0.5)] * 3, None)])
notes = sorted(f.message for f in partial.findings if f.severity == "info")
expect("partial facts: hull convexity, bones per section and overlap are notes, not verdicts",
       partial.ok and len(notes) == 3 and any("convexity of 'UCX_Crate'" in n for n in notes)
       and any("max_bones_per_section" in n for n in notes)
       and any("max_overlap" in n for n in notes), partial.as_text())
present = preflight_core.assess("x", triangles=12, budget=crate_budget, lods=[("C", (12, 6, 3), "group")],
                                collision=[], render_names=["C"], lightmap=[("C", True, None)])
expect("a lightmap set that is there but unread is not 'missing' (no false fail)",
       not any(f.code == "lightmap" and f.severity == "error" for f in present.findings)
       and any("lightmap UVs of 'C'" in f.message for f in present.findings), present.as_text())
stand_in = types.SimpleNamespace(max_triangles=100, max_material_slots=3, max_texture_edge=2048,
                                  lod_min_levels=3)
foreign = preflight_core.assess("x", triangles=12, budget=stand_in, lods=[])
expect("a budget the gate did not build: the rule is a note, not a guess",
       foreign.ok and [f.code for f in foreign.findings] == ["lod"]
       and "not checked in the panel" in foreign.findings[0].message, foreign.as_text())


# -- Maya: a stand-in maya.cmds + maya.api.OpenMaya over a small DAG ---------- #
class _MayaScene:
    """Just enough of maya.cmds for maya_preflight: a DAG of full paths."""

    def __init__(self):
        self.types, self.meshes, self.clusters, self.selected = {}, {}, {}, []

    def add(self, path, kind, geometry=None, uv_sets=(), skin=None):
        self.types[path] = kind
        if geometry is not None:
            shape = f"{path}|{path.rsplit('|', 1)[-1]}Shape"
            self.types[shape] = "mesh"
            self.meshes[shape] = {"geometry": geometry, "uv_sets": list(uv_sets), "skin": skin}
        return path

    def _full(self, name):
        if name in self.types:
            return name
        return next((path for path in self.types if path.rsplit("|", 1)[-1] == name), name)

    def _children(self, path):
        return [item for item in self.types if item.rsplit("|", 1)[0] == path and item != path]

    def nodeType(self, node):
        return self.types.get(self._full(node), "skinCluster" if node in self.clusters else "")

    def ls(self, items=None, selection=False, type=None, long=False, noIntermediate=False,
           assemblies=False, **flags):
        if selection:
            return list(self.selected)
        if assemblies:
            return [path for path in self.types if path.count("|") == 1]
        if items is None:
            return [path.rsplit("|", 1)[-1] for path, kind in self.types.items() if kind == type]
        items = [items] if isinstance(items, str) else list(items)
        if type is not None:
            return [item for item in items if self.nodeType(item) == type]
        return [self._full(item) for item in items]

    def listRelatives(self, node, shapes=False, type=None, allDescendents=False, children=False,
                      parent=False, fullPath=False, **flags):
        path = self._full(node)
        if parent:
            up = path.rsplit("|", 1)[0]
            return [up] if up else []
        found = ([item for item in self.types if item.startswith(path + "|")] if allDescendents
                 else self._children(path))
        if shapes:
            found = [item for item in found if self.types[item] == "mesh"]
        if type == "mesh":
            found = [item for item in found if self.types[item] == "mesh"]
        elif type == "transform":
            found = [item for item in found if self.types[item] in ("transform", "joint", "lodGroup")]
        return found

    def polyEvaluate(self, shape, face=False, triangle=False, vertex=False):
        vertices, polygons = self.meshes[self._full(shape)]["geometry"]
        return len(polygons) if face else (sum(len(p) - 2 for p in polygons) if triangle
                                           else len(vertices))

    def listConnections(self, *args, **flags):
        return []

    def listHistory(self, shape):
        skin = self.meshes[self._full(shape)]["skin"]
        return [skin] if skin else []

    def skinCluster(self, cluster, query=True, influence=True):
        return [path.rsplit("|", 1)[-1] for path in self.clusters[cluster]]

    def polyUVSet(self, shape, query=True, allUVSets=True):
        return [f"map{index + 1}" for index in range(len(self.meshes[self._full(shape)]["uv_sets"]))]

    def file(self, query=True, sceneName=True):
        return "/proj/props/crate.ma"


class _MayaPoint:
    def __init__(self, xyz):
        self.x, self.y, self.z = xyz


def _open_maya(scene):
    om = types.ModuleType("maya.api.OpenMaya")

    class MSelectionList(list):
        def add(self, name):
            self.append(scene._full(name))

        def getDagPath(self, index):
            return self[index]

    class MFnMesh:
        def __init__(self, path):
            self.data = scene.meshes[path]

        def getVertices(self):
            polygons = self.data["geometry"][1]
            return [len(p) for p in polygons], [i for p in polygons for i in p]

        def getPoints(self, *space):
            return [_MayaPoint(xyz) for xyz in self.data["geometry"][0]]

        def _set(self, name):
            return self.data["uv_sets"][int(name[3:]) - 1]

        def getUVs(self, name):
            uvs = self._set(name)
            return [u for u, _v in uvs], [v for _u, v in uvs]

        def getAssignedUVs(self, name):
            return [len(p) for p in self.data["geometry"][1]], list(range(len(self._set(name))))

    om.MSelectionList, om.MFnMesh = MSelectionList, MFnMesh
    return om


def maya_crate():
    scene = _MayaScene()
    group = scene.add("|Crate", "lodGroup")
    scene.add(group + "|Crate_LOD0", "transform", CRATE_LOD0, CRATE_UV)
    scene.add(group + "|Crate_LOD1", "transform", CRATE_LOD1, CRATE_UV[:1])
    scene.add(group + "|UCX_Crate_LOD0", "transform", CRATE_HULL)
    scene.add(group + "|UCX_Crat", "transform", STRAY_HULL)
    scene.selected = ["Crate_LOD0"]                  # the artist selects the mesh only
    return scene


def maya_body():
    scene = _MayaScene()
    scene.add("|Body", "transform", BODY, skin="skinCluster1")
    scene.add("|root", "joint")
    scene.add("|root|spine", "joint")
    scene.add("|root|spine|head", "joint")
    scene.add("|prop_root", "joint")
    scene.clusters["skinCluster1"] = ["|root", "|root|spine", "|root|spine|head", "|prop_root"]
    scene.selected = ["Body"]
    return scene


_saved_cmds = maya_preflight.cmds
_maya_api = types.ModuleType("maya.api")
sys.modules["maya.api"] = fake_maya.api = _maya_api


def maya_report(scene, budget, open_maya=True):
    maya_preflight.cmds = scene
    if open_maya:
        sys.modules["maya.api.OpenMaya"] = _maya_api.OpenMaya = _open_maya(scene)
    else:
        sys.modules["maya.api.OpenMaya"] = None             # import fails: no API 2
    try:
        triangles, vertices, materials, textures, degenerate = maya_preflight.scene_facts()
        return triangles, preflight_core.assess(
            "crate.ma", triangles=triangles, budget=budget,
            **maya_preflight.structure_facts(budget))
    finally:
        maya_preflight.cmds = _saved_cmds
        sys.modules.pop("maya.api.OpenMaya", None)


maya_triangles, maya_crate_report = maya_report(maya_crate(), crate_budget)
expect("Maya: LOD1 and the UCX_ hulls are not in the triangle count (as in the gate)",
       maya_triangles == 12, maya_triangles)
same_verdict("Maya, lodGroup + UCX_ hulls next to the mesh + UV set map2", maya_crate_report,
             crate_group_fbx, crate_budget)
no_section = dataclasses.replace(body_budget, max_bones_per_section=0)
_, maya_body_report = maya_report(maya_body(), no_section)
same_verdict("Maya, skinCluster joints (skinned: collision exempt, no lightmap)", maya_body_report,
             body_fbx, no_section)
_, maya_section = maya_report(maya_body(), body_budget)
expect("Maya: bones per section is a note (left to the gate), not a pass or a fail",
       any(f.severity == "info" and "max_bones_per_section" in f.message
           for f in maya_section.findings)
       and not any(f.severity == "error" and "material section" in f.message
                   for f in maya_section.findings),
       maya_section.as_text())
_, maya_off = maya_report(maya_crate(), structure_budget())
expect("Maya: rules off -> nothing new", not maya_off.findings, maya_off.as_text())
_, maya_no_api = maya_report(maya_crate(), crate_budget, open_maya=False)
expect("Maya without OpenMaya 2: hull convexity and lightmap values are notes, no false verdict",
       not any(f.severity == "error" and ("not convex" in f.message or "outside 0-1" in f.message)
               for f in maya_no_api.findings)
       and any("convexity of" in f.message for f in maya_no_api.findings)
       and any("lightmap UVs of 'Crate_LOD0'" in f.message for f in maya_no_api.findings),
       maya_no_api.as_text())


class _BrokenCmds(_MayaScene):
    def polyUVSet(self, *args, **flags):
        raise RuntimeError("polyUVSet changed")


broken = maya_crate()
broken.__class__ = _BrokenCmds
_, maya_broken = maya_report(broken, crate_budget)
expect("Maya: a host API that fails leaves that rule to the gate (info), the rest still judged",
       any(f.severity == "info" and f.code == "lightmap" for f in maya_broken.findings)
       and any(f.code == "lod" and f.severity == "error" for f in maya_broken.findings),
       maya_broken.as_text())


# -- 3ds Max: a stand-in pymxs runtime with nodes, TriMeshes and a Skin ------- #
class _MaxPoint:
    def __init__(self, *xyz):
        self.x, self.y, self.z = (list(xyz) + [0.0, 0.0])[:3]


class _MaxTriMesh:
    """snapshotAsMesh(): fan triangles; map channel k + 1 holds UV set k, per corner."""

    def __init__(self, geometry, uv_sets):
        self.vertices, polygons = geometry
        self.faces = faces_of(geometry)
        self.maps = [None] + [(uvs, preflight_core.fan_triangles([len(p) for p in polygons],
                                                                list(range(len(uvs)))))
                              for uvs in uv_sets]


def _max_runtime(nodes):
    rt = types.SimpleNamespace()
    for name in ("GeometryClass", "Multimaterial", "Bitmaptexture", "Skin", "BoneGeometry",
                 "Editable_Poly"):
        setattr(rt, name, _MaxClass(name))
    by_handle = {node.inode.handle: node for node in nodes}
    rt.classOf = lambda item: getattr(item, "cls", None)
    rt.superClassOf = lambda item: getattr(item, "superclass", None)
    rt.getPolygonCount = lambda node: [len(faces_of(node.geometry)), len(node.geometry[0])]
    rt.selection = _MaxSelection([node for node in nodes if node.selected])
    rt.objects = list(nodes)
    rt.rootNode = types.SimpleNamespace(children=[node for node in nodes if node.parent is None])
    rt.maxFilePath, rt.maxFileName = "C:/proj/props/", "crate.max"
    rt.maxOps = types.SimpleNamespace(getNodeByHandle=lambda handle: by_handle[handle])
    rt.snapshotAsMesh = lambda node: _MaxTriMesh(node.geometry, node.uv_sets)
    rt.delete = lambda item: None
    rt.getNumVerts = lambda mesh: len(mesh.vertices)
    rt.getVert = lambda mesh, index: _MaxPoint(*mesh.vertices[index - 1])
    rt.getNumFaces = lambda mesh: len(mesh.faces)
    rt.getFace = lambda mesh, index: _MaxPoint(*(corner + 1 for corner in mesh.faces[index - 1]))
    rt.meshop = types.SimpleNamespace(
        getNumMaps=lambda mesh: len(mesh.maps),
        getMapSupport=lambda mesh, channel: mesh.maps[channel] is not None,
        getNumMapVerts=lambda mesh, channel: len(mesh.maps[channel][0]),
        getMapVert=lambda mesh, channel, index: _MaxPoint(*mesh.maps[channel][0][index - 1]),
        getMapFace=lambda mesh, channel, index: _MaxPoint(
            *(corner + 1 for corner in mesh.maps[channel][1][index - 1])))
    rt.skinOps = types.SimpleNamespace(GetBoneNodes=lambda modifier: modifier.bones)
    return rt


class _MaxSceneNode:
    def __init__(self, name, geometry=None, parent=None, uv_sets=(), selected=False, bone=False):
        self.name, self.geometry, self.parent, self.uv_sets = name, geometry, parent, list(uv_sets)
        self.selected, self.children, self.modifiers, self.material = selected, [], [], None
        self.inode = types.SimpleNamespace(handle=id(self))
        self.cls = "bone" if bone else "poly"
        self.superclass = None if bone else "geometry"
        if parent is not None:
            parent.children.append(self)


def max_report(nodes, budget):
    rt = _max_runtime(nodes)
    for node in nodes:      # the stand-in classes, now that the runtime exists
        node.cls = rt.BoneGeometry if node.cls == "bone" else rt.Editable_Poly
        node.superclass = None if node.superclass is None else rt.GeometryClass
        for modifier in node.modifiers:
            modifier.cls = rt.Skin
    saved, max_preflight.rt = max_preflight.rt, rt
    try:
        triangles, _v, _m, _t, _d, names = max_preflight.scene_facts()
        return triangles, preflight_core.assess("crate.max", triangles=triangles, budget=budget,
                                                mesh_names=names,
                                                **max_preflight.structure_facts(budget))
    finally:
        max_preflight.rt = saved


max_crate = [_MaxSceneNode("Crate_LOD0", CRATE_LOD0, uv_sets=CRATE_UV, selected=True),
             _MaxSceneNode("Crate_LOD1", CRATE_LOD1, uv_sets=CRATE_UV[:1], selected=True),
             _MaxSceneNode("UCX_Crate_LOD0", CRATE_HULL), _MaxSceneNode("UCX_Crat", STRAY_HULL)]
max_triangles, max_crate_report = max_report(max_crate, crate_budget)
expect("Max: UCX_ hulls are not in the triangle count; LODs by name are (as in the gate)",
       max_triangles == 24, max_triangles)
same_verdict("Max, _LOD<n> names + UCX_ hulls beside the meshes + map channel 2", max_crate_report,
             crate_names_fbx, crate_budget)
max_bones = {}
for _name, _parent in BONES:
    max_bones[_name] = _MaxSceneNode(_name, parent=None if _parent is None else max_bones[BONES[_parent][0]],
                                     bone=True)
max_body = _MaxSceneNode("Body", BODY, selected=True)
max_body.modifiers.append(types.SimpleNamespace(bones=list(max_bones.values())))
_, max_body_report = max_report([max_body, *max_bones.values()], no_section)
same_verdict("Max, Skin modifier bones (skinned: collision exempt, no lightmap)", max_body_report,
             body_fbx, no_section)
_, max_off = max_report([_MaxSceneNode("Crate_LOD0", CRATE_LOD0, selected=True)], structure_budget())
expect("Max: rules off -> nothing new", not max_off.findings, max_off.as_text())


# -- Blender: a stand-in bpy.context over evaluated meshes -------------------- #
class _BlenderCollection(list):
    def foreach_get(self, attribute, target):
        flat = []
        for item in self:
            value = getattr(item, attribute)
            flat.extend(value if isinstance(value, (tuple, list)) else [value])
        target[:] = flat


class _BlenderMesh:
    def __init__(self, geometry, uv_sets=(), weights=None):
        vertices, polygons = geometry
        weights = weights or {}
        self.vertices = _BlenderCollection(
            types.SimpleNamespace(index=index, co=tuple(map(float, point)),
                                   groups=[types.SimpleNamespace(group=group, weight=1.0)
                                           for group in weights.get(index, ())])
            for index, point in enumerate(vertices))
        self.polygons = _BlenderCollection(types.SimpleNamespace(loop_total=len(p), material_index=0,
                                                                  area=1.0) for p in polygons)
        self.loops = _BlenderCollection(types.SimpleNamespace(vertex_index=i) for p in polygons for i in p)
        self.loop_triangles = faces_of(geometry)
        self.uv_layers = [types.SimpleNamespace(uv=_BlenderCollection(
            types.SimpleNamespace(vector=tuple(uv)) for uv in uvs)) for uvs in uv_sets]


class _BlenderObject:
    def __init__(self, name, geometry=None, uv_sets=(), kind="MESH", material="crate_mat"):
        self.name, self.type, self.parent, self.parent_type = name, kind, None, "OBJECT"
        self.geometry, self.uv_sets, self.weights = geometry, uv_sets, {}
        self.modifiers, self.vertex_groups, self.children, self.users_collection = [], [], [], []
        self.material_slots = [types.SimpleNamespace(material=types.SimpleNamespace(
            name=material, use_nodes=False))] if material else []

    def evaluated_get(self, depsgraph):
        return self

    def to_mesh(self):
        return _BlenderMesh(self.geometry, self.uv_sets, self.weights)

    def to_mesh_clear(self):
        pass


def blender_report(objects, selected, budget):
    collection = list(objects)
    for obj in objects:
        obj.users_collection = [types.SimpleNamespace(objects=collection)]
    context = types.SimpleNamespace(selected_objects=selected,
                                     scene=types.SimpleNamespace(objects=objects, name="crate"),
                                     evaluated_depsgraph_get=lambda: None)
    fake_bpy.context = context
    try:
        triangles, _v, _m, _t, _d = preflight_panel.scene_facts()
        return triangles, preflight_core.assess("crate.blend", triangles=triangles, budget=budget,
                                                **preflight_panel.structure_facts(budget))
    finally:
        del fake_bpy.context


blender_crate = [_BlenderObject("Crate_LOD0", CRATE_LOD0, CRATE_UV),
                 _BlenderObject("Crate_LOD1", CRATE_LOD1, CRATE_UV[:1]),
                 _BlenderObject("UCX_Crate_LOD0", CRATE_HULL, material=None),
                 _BlenderObject("UCX_Crat", STRAY_HULL, material=None)]
blender_triangles, blender_crate_report = blender_report(blender_crate, blender_crate[:2], crate_budget)
expect("Blender: UCX_ hulls are not in the triangle count; LODs by name are (as in the gate)",
       blender_triangles == 24, blender_triangles)
same_verdict("Blender, _LOD<n> names + UCX_ hulls in the collection + UV layer 1",
             blender_crate_report, crate_names_fbx, crate_budget)


class _BlenderBone:
    def __init__(self, name, parent):
        self.name, self.parent, self.children = name, parent, []
        if parent is not None:
            parent.children.append(self)


blender_bones = {}
for _name, _parent in BONES:
    blender_bones[_name] = _BlenderBone(_name, None if _parent is None else blender_bones[BONES[_parent][0]])
armature = _BlenderObject("Armature", kind="ARMATURE", material=None)
armature.data = types.SimpleNamespace(bones=list(blender_bones.values()))
blender_body = _BlenderObject("Body", BODY, material="skin_mat")
blender_body.modifiers = [types.SimpleNamespace(type="ARMATURE", object=armature)]
blender_body.vertex_groups = [types.SimpleNamespace(index=i, name=name) for i, (name, _p) in enumerate(BONES)]
blender_body.weights = {vertex: [bone] for bone, vertices, _w in BODY_CLUSTERS for vertex in vertices}
_, blender_body_report = blender_report([blender_body, armature], [blender_body], body_budget)
same_verdict("Blender, armature deform bones incl. bones per material section", blender_body_report,
             body_fbx, body_budget)
_, blender_off = blender_report(blender_crate, blender_crate[:2], structure_budget())
expect("Blender: rules off -> nothing new", not blender_off.findings, blender_off.as_text())


# -- Houdini: the Python SOP over stand-in geometry with a name attribute ------ #
_sop_spec = importlib.util.spec_from_file_location("agk_sop_panel_rules",
                                                   ROOT / "plugins" / "houdini" / "sop_gatekeeper.py")
sop = importlib.util.module_from_spec(_sop_spec)
_sop_spec.loader.exec_module(sop)


class _HouAttrib:
    def __init__(self, name, values=()):
        self._name, self._values = name, list(values)

    def name(self):
        return self._name

    def strings(self):
        return list(dict.fromkeys(self._values))


class _HouPoint:
    def __init__(self, number, xyz):
        self._number, self._xyz = number, xyz

    def number(self):
        return self._number

    def position(self):
        return self._xyz


class _HouVertex:
    def __init__(self, point, uvs):
        self._point, self._uvs = point, uvs

    def point(self):
        return self._point

    def attribValue(self, attribute):
        return self._uvs[attribute.name()]


class _HouPrim:
    def __init__(self, name, points, uvs):
        self._name, self._points = name, points
        self._vertices = [_HouVertex(point, {key: values[k] for key, values in uvs.items()})
                          for k, point in enumerate(points)]

    def type(self):
        return "primType.Polygon"

    def numVertices(self):
        return len(self._points)

    def isClosed(self):
        return True

    def points(self):
        return self._points

    def vertices(self):
        return self._vertices

    def attribValue(self, attribute):
        return self._name


class _HouGeometry:
    def __init__(self, pieces, uv_names=("uv", "uv2"), captured=False):
        self._prims, self.uv = [], [_HouAttrib(name) for name in uv_names]
        self.captured = captured
        number = 0
        for name, (vertices, polygons), uv_sets in pieces:
            points = []
            for xyz in vertices:
                points.append(_HouPoint(number, xyz))
                number += 1
            corner = 0
            for polygon in polygons:
                uvs = {attribute.name(): uv_sets[i][corner:corner + len(polygon)]
                       for i, attribute in enumerate(self.uv) if i < len(uv_sets)}
                self._prims.append(_HouPrim(name, [points[i] for i in polygon], uvs))
                corner += len(polygon)
        self.names = _HouAttrib("name", [prim._name for prim in self._prims])

    def prims(self):
        return self._prims

    def points(self):
        return [point for prim in self._prims for point in prim.points()]

    def findPrimAttrib(self, name):
        return self.names if name == "name" else None

    def findVertexAttrib(self, name):
        return next((attribute for attribute in self.uv if attribute.name() == name), None)

    def findPointAttrib(self, name):
        return _HouAttrib(name) if name == "boneCapture" and self.captured else None

    def vertexAttribs(self):
        return self.uv

    def pointAttribs(self):
        return ()

    primAttribs = pointAttribs


houdini_crate = [("Crate_LOD0", CRATE_LOD0, CRATE_UV), ("Crate_LOD1", CRATE_LOD1, CRATE_UV),
                 ("UCX_Crate_LOD0", CRATE_HULL, [_corner_uvs(CRATE_HULL[1])] * 2),
                 ("UCX_Crat", STRAY_HULL, [_corner_uvs(STRAY_HULL[1])] * 2)]
# A Houdini attribute is on every prim, so here LOD1 carries the lightmap set too.
houdini_fbx = write_fbx(PANEL_WORK / "crate_houdini.fbx", [
    _mesh("Crate_LOD0", CRATE_LOD0, uv_sets=CRATE_UV), _mesh("Crate_LOD1", CRATE_LOD1, uv_sets=CRATE_UV),
    _mesh("UCX_Crate_LOD0", CRATE_HULL, material=None), _mesh("UCX_Crat", STRAY_HULL, material=None)])
houdini_budget = dataclasses.replace(crate_budget, max_bones=10)
sop_notes: list = []
sop_problems = sop.analyse(_HouGeometry(houdini_crate), houdini_budget, sop_notes, node_name="crate")
gate_problems, gate_warnings = gate_says(houdini_fbx, houdini_budget)
expect("Houdini SOP: the badge carries what the gate says on the export (name attribute pieces)",
       sorted(sop_problems) == sorted(gate_problems + gate_warnings) and gate_problems,
       f"SOP {sorted(sop_problems)} / gate {sorted(gate_problems + gate_warnings)}")
expect("Houdini SOP: an uncaptured stream has no skeleton to check (nothing noted)",
       not any("skeleton" in note for note in sop_notes), sop_notes)
captured_notes: list = []
sop.analyse(_HouGeometry(houdini_crate, captured=True), houdini_budget, captured_notes, node_name="crate")
expect("Houdini SOP: a captured stream leaves [skeleton] to the gate (a note, not a badge)",
       any("skeleton ([skeleton]) not checked in the panel" in note for note in captured_notes),
       captured_notes)
expect("Houdini SOP: UCX_ pieces are not counted as triangles",
       not any("triangles exceeds" in problem for problem in sop.analyse(
           _HouGeometry(houdini_crate), dataclasses.replace(structure_budget(), max_triangles=24))),
       sop.analyse(_HouGeometry(houdini_crate), dataclasses.replace(structure_budget(), max_triangles=24)))
off_notes: list = []
expect("Houdini SOP: rules off -> nothing new",
       sop.analyse(_HouGeometry(houdini_crate), structure_budget(), off_notes) == [] and not off_notes,
       off_notes)
one_uv_fbx = write_fbx(PANEL_WORK / "crate_one_uv.fbx", [
    _mesh(name, geometry, uv_sets=uvs[:1], material=None if name.startswith("UCX_") else "crate_mat")
    for name, geometry, uvs in houdini_crate])
lightmap_only = structure_budget(lightmap={"require": True})
no_uv2 = sop.analyse(_HouGeometry(houdini_crate, uv_names=("uv",)), lightmap_only, node_name="crate")
expect("Houdini SOP: no uv2 attribute -> every static piece lacks the lightmap set, as the gate says",
       sorted(no_uv2) == gate_says(one_uv_fbx, lightmap_only)[0] and no_uv2, no_uv2)

print("10. Hooks and tools know the new formats")
sys.path.insert(0, str(ROOT / "vcs_hooks"))
import git_pre_commit  # noqa: E402
import p4_pre_submit  # noqa: E402
for suffix in (".fbx", ".tga", ".jpg", ".jpeg"):
    expect(f"git pre-commit gates {suffix}", suffix in git_pre_commit.ASSET_SUFFIXES)
    expect(f"Perforce trigger gates {suffix}", suffix in p4_pre_submit.ASSET_SUFFIXES)
    expect(f"the gate tracks {suffix}", suffix in agk.TRACKED_SUFFIXES)
push_hook = (ROOT / "vcs_hooks" / "git_pre_push.sh").read_text(encoding="utf-8")
_push_suffixes = {"." + item for item in push_hook.split('ASSET_EXTENSIONS="', 1)[1].split('"', 1)[0].split()}
for _hook, _suffixes in (("git pre-commit", git_pre_commit.ASSET_SUFFIXES),
                         ("Perforce trigger", p4_pre_submit.ASSET_SUFFIXES),
                         ("git pre-push", _push_suffixes)):
    expect(f"{_hook} gates every format the gate checks (.dds, .tif, .exr, .webp, .ktx2 too)",
           set(agk.TRACKED_SUFFIXES) <= set(_suffixes), sorted(set(agk.TRACKED_SUFFIXES) - set(_suffixes)))
expect("git pre-push gates fbx/tga/jpg", " fbx " in push_hook and " tga " in push_hook and " jpg " in push_hook)

print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
