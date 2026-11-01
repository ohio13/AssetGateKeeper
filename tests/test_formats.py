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
expect("ASCII FBX gets a re-export hint",
       "binary FBX" in " ".join(rows.get("ascii.fbx", {}).get("problems", [])))
expect("the decompression bomb is refused by size, not by running out of memory",
       "too large" in " ".join(rows.get("bomb.fbx", {}).get("problems", [])))

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

print("10. Hooks and tools know the new formats")
sys.path.insert(0, str(ROOT / "vcs_hooks"))
import git_pre_commit  # noqa: E402
import p4_pre_submit  # noqa: E402
for suffix in (".fbx", ".tga", ".jpg", ".jpeg"):
    expect(f"git pre-commit gates {suffix}", suffix in git_pre_commit.ASSET_SUFFIXES)
    expect(f"Perforce trigger gates {suffix}", suffix in p4_pre_submit.ASSET_SUFFIXES)
    expect(f"the gate tracks {suffix}", suffix in agk.TRACKED_SUFFIXES)
push_hook = (ROOT / "vcs_hooks" / "git_pre_push.sh").read_text(encoding="utf-8")
expect("git pre-push gates fbx/tga/jpg", " fbx " in push_hook and " tga " in push_hook and " jpg " in push_hook)

print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
