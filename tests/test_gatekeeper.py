"""Sanity checks for asset_gatekeeper: new rules, fallbacks, edge cases."""
import shutil
import sys
import time
from pathlib import Path

# Allow `python tests/test_gatekeeper.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asset_gatekeeper as agk  # noqa: E402

# Group 8 prints non-ASCII asset names. A cp1252 console/pipe would raise
# UnicodeEncodeError and abort the run half-way, so force UTF-8 output here.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

DROP = Path("dropbox")
failures = []


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not condition:
        failures.append(label)


gate = agk.Gatekeeper(agk.Budget())
problems = lambda name: gate.check(DROP / name).problems  # noqa: E731

print("1. glTF / GLB triangle counting via the dependency-free fallback")
saved = agk.trimesh
agk.trimesh = None
try:
    expect("boss_statue.gltf = 72,500", agk.count_triangles(DROP / "boss_statue.gltf") == 72_500)
    expect("prop_crate.glb = 1,800", agk.count_triangles(DROP / "prop_crate.glb") == 1_800)
    expect("heavy_prop.glb = 80,000", agk.count_triangles(DROP / "heavy_prop.glb") == 80_000)
finally:
    agk.trimesh = saved

print("2. Material slot counting")
for name, expected in (
    ("crate_1mtl.obj", 1),
    ("crate_4mtl.obj", 4),
    ("crate_nomtl.obj", 1),   # faces with no usemtl => default material
    ("lantern.gltf", 1),
    ("boss_statue.gltf", 4),
    ("prop_crate.glb", 2),
):
    actual = agk.count_material_slots(DROP / name)
    expect(f"{name} = {expected} slot(s)", actual == expected, f"got {actual}")

print("3. Naming pattern")
pattern = agk.build_naming_pattern(agk.Budget.texture_suffixes)
for stem, should_match in (
    ("bark_Albedo", True),
    ("bark_albedo", True),      # case-insensitive
    ("bark_Normal_02", True),   # numbered variant
    ("rock_Roughness_3", True),
    ("bark_diffuse", False),
    ("albedo", False),          # missing separator
    ("bark_Albedo_extra", False),
    ("bark_AlbedoTex", False),
):
    expect(f"'{stem}' {'matches' if should_match else 'rejected'}",
           bool(pattern.search(stem)) is should_match)

print("4. Power-of-two test")
for value, expected in ((0, False), (1, True), (256, True), (512, True), (1024, True),
                        (2048, True), (300, False), (1000, False), (4096, True)):
    expect(f"is_power_of_two({value}) is {expected}", agk.is_power_of_two(value) is expected)

print("5. Rule outcomes (one line, all violations)")
expect("bark_Albedo.png passes", not problems("bark_Albedo.png"))
expect("bark_Normal_02.png passes", not problems("bark_Normal_02.png"))
expect("atlas_max.png fails naming only", problems("atlas_max.png") == (
    "name does not end with a standard suffix (_Albedo, _Normal, _Roughness)",))
expect("hull_Albedo.png fails resolution only", len(problems("hull_Albedo.png")) == 1,
       str(problems("hull_Albedo.png")))
expect("ui_icon.png reports 2 violations", len(problems("ui_icon.png")) == 2)
expect("boss_statue.gltf reports 2 violations (tris + slots)", len(problems("boss_statue.gltf")) == 2)
expect("crate_1mtl.obj passes", not problems("crate_1mtl.obj"))
expect("crate_nomtl.obj passes", not problems("crate_nomtl.obj"))
expect("crate_4mtl.obj fails on slots only", len(problems("crate_4mtl.obj")) == 1)
expect("prop_crate.glb passes", not problems("prop_crate.glb"))

print("6. Rule toggles")
lenient = agk.Gatekeeper(agk.Budget(require_power_of_two=False))
expect("--allow-npot silences POT failure",
       not any("power of two" in p for p in lenient.check(DROP / "ui_icon.png").problems))
unchecked = agk.Gatekeeper(agk.Budget(enforce_texture_naming=False))
expect("--no-naming silences naming failure",
       not any("suffix" in p for p in unchecked.check(DROP / "atlas_max.png").problems))
tight = agk.Gatekeeper(agk.Budget(max_material_slots=1))
expect("max_material_slots=1 rejects a 2-slot mesh",
       any("material slots" in p for p in tight.check(DROP / "prop_crate.glb").problems))

print("7. Broken exports fail fast instead of passing")
scratch = Path("scratch")
scratch.mkdir(exist_ok=True)
for name, blob in (("empty_Albedo.png", b""),
                   ("empty.obj", b""),
                   ("truncated_Albedo.png", b"\x89PNG\r\n\x1a\n")):
    (scratch / name).write_bytes(blob)

for name in ("empty_Albedo.png", "empty.obj", "truncated_Albedo.png"):
    path = scratch / name
    start = time.monotonic()
    stable = agk.wait_until_stable(path)
    elapsed = time.monotonic() - start
    verdict = gate.check(path)
    expect(f"{name} settles quickly", stable and elapsed < 3.0, f"{elapsed:.2f}s")
    expect(f"{name} fails with a reason", not verdict.passed and verdict.reason, verdict.tag)

print("8. Unicode folder + filenames")
uni = scratch / "导出_assets"
uni.mkdir(exist_ok=True)
shutil.copy(DROP / "crate_4mtl.obj", uni / "墙体.obj")
shutil.copy(DROP / "bark_Albedo.png", uni / "树皮_Albedo.png")
for path in sorted(uni.iterdir()):
    verdict = gate.check(path)
    print(f"     {verdict.path.name}: {verdict.tag} {verdict.metric}")
    expect(f"unicode asset analysed ({path.name})", bool(verdict.metric))

print("9. rules.toml configuration")
cfg = scratch / "rules_demo"
cfg.mkdir(exist_ok=True)
(cfg / "rules.toml").write_text(
    "[mesh]\nmax_triangles = 10\nmax_material_slots = 1\n\n"
    "[texture]\nmax_edge = 512\npower_of_two = false\nnaming = false\n"
    "suffixes = [\"_Diffuse\"]\n",
    encoding="utf-8",
)

parse = lambda *extra: agk.build_parser().parse_args([str(cfg), *extra])  # noqa: E731
budget, source, overrides = agk.resolve_budget(cfg, parse())
expect("rules.toml is discovered automatically", source is not None and source.name == "rules.toml")
expect("file values are applied", (budget.max_triangles, budget.max_texture_edge,
                                  budget.max_material_slots) == (10, 512, 1), str(budget))
expect("booleans from file are applied",
       budget.require_power_of_two is False and budget.enforce_texture_naming is False)
expect("suffix list from file is applied", budget.texture_suffixes == ("_Diffuse",))
expect("no CLI overrides reported", overrides == [])

strict = agk.Gatekeeper(budget)
expect("12-tri cube fails a rules.toml budget of 10",
       not strict.check(DROP / "crate_1mtl.obj").passed)
expect("NPOT texture accepted when file says power_of_two=false",
       not any("power of two" in p for p in strict.check(DROP / "ui_icon.png").problems))
expect("naming follows the file's suffix list",
       not strict.check(DROP / "bark_diffuse.png").problems)

budget2, _, overrides2 = agk.resolve_budget(cfg, parse("--triangles", "99999"))
expect("CLI overrides rules.toml", budget2.max_triangles == 99_999 and "max_triangles" in overrides2)
expect("CLI only overrides what was passed", budget2.max_texture_edge == 512)

pot_cfg = scratch / "rules_pot"
pot_cfg.mkdir(exist_ok=True)
(pot_cfg / "rules.toml").write_text("[texture]\npower_of_two = true\nnaming = true\n", encoding="utf-8")
from_file, _, _ = agk.resolve_budget(pot_cfg, agk.build_parser().parse_args([str(pot_cfg)]))
expect("file can turn power_of_two back on", from_file.require_power_of_two is True)
forced_off, _, off_overrides = agk.resolve_budget(
    pot_cfg, agk.build_parser().parse_args([str(pot_cfg), "--allow-npot"]))
expect("--allow-npot beats the file",
       forced_off.require_power_of_two is False and off_overrides == ["require_power_of_two"],
       str(off_overrides))

missing = scratch / "no_rules_here"
missing.mkdir(exist_ok=True)
default_budget, no_source, _ = agk.resolve_budget(missing, agk.build_parser().parse_args([str(missing)]))
expect("missing rules.toml falls back to defaults",
       no_source is None and default_budget == agk.Budget())

json_rules = scratch / "rules.json"
json_rules.write_text('{"mesh": {"max_triangles": 777}}', encoding="utf-8")
json_budget, json_source, _ = agk.resolve_budget(
    missing, agk.build_parser().parse_args([str(missing), "--rules", str(json_rules)]))
expect("--rules accepts a JSON file", json_budget.max_triangles == 777, str(json_source))

bom_cfg = scratch / "rules_bom"
bom_cfg.mkdir(exist_ok=True)
(bom_cfg / "rules.toml").write_bytes(b"\xef\xbb\xbf[mesh]\nmax_triangles = 64\n")
bom_budget, _, _ = agk.resolve_budget(bom_cfg, agk.build_parser().parse_args([str(bom_cfg)]))
expect("rules.toml written with a BOM still loads (Notepad / PowerShell)",
       bom_budget.max_triangles == 64, str(bom_budget.max_triangles))

for label, blob in (
    ("unknown key is rejected", "[mesh]\nmax_triange = 10\n"),
    ("wrong type is rejected", "[mesh]\nmax_triangles = \"lots\"\n"),
    ("bad section is rejected", "[meshes]\nmax_triangles = 10\n"),
    ("malformed toml is rejected", "[mesh\nmax_triangles = 10\n"),
):
    (cfg / "rules.toml").write_text(blob, encoding="utf-8")
    try:
        agk.resolve_budget(cfg, parse())
        expect(label, False, "no error raised")
    except agk.ConfigError as exc:
        expect(label, True, str(exc).split(":")[-1].strip()[:48])

# leave a valid file behind for the CLI demo
(cfg / "rules.toml").write_text("[mesh]\nmax_triangles = 10\n", encoding="utf-8")

print("10. deep data profiling")
expect("uv channels: rig_ok has 2", agk.count_uv_channels(DROP / "rig_ok.gltf") == 2)
expect("uv channels: uv_heavy has 5", agk.count_uv_channels(DROP / "uv_heavy.gltf") == 5)
expect("uv channels: obj without vt has 0", agk.count_uv_channels(DROP / "crate_1mtl.obj") == 0)
expect("influences: rig_ok has 4", agk.max_bone_influences(DROP / "rig_ok.gltf") == 4)
expect("influences: rig_heavy has 6", agk.max_bone_influences(DROP / "rig_heavy.gltf") == 6)
expect("influences: obj has none", agk.max_bone_influences(DROP / "crate_1mtl.obj") == 0)
expect("degenerate: broken_glass.obj has 1",
       agk.count_degenerate_triangles(DROP / "broken_glass.obj") == 1)
expect("degenerate: shattered.gltf has 1",
       agk.count_degenerate_triangles(DROP / "shattered.gltf") == 1)
expect("degenerate: clean mesh has 0",
       agk.count_degenerate_triangles(DROP / "crate_1mtl.obj") == 0)
expect("rig_ok.gltf passes every deep rule", not problems("rig_ok.gltf"))
expect("rig_heavy.gltf fails on influences",
       any("bone weights" in p for p in problems("rig_heavy.gltf")))
expect("uv_heavy.gltf fails on UV channels",
       any("UV channels" in p for p in problems("uv_heavy.gltf")))
expect("broken_glass.obj fails on degenerate triangles",
       any("degenerate" in p for p in problems("broken_glass.obj")))
lenient_geo = agk.Gatekeeper(agk.Budget(allow_degenerate=True))
expect("allow_degenerate=True tolerates zero-area faces",
       not any("degenerate" in p for p in lenient_geo.check(DROP / "broken_glass.obj").problems))
strict_rig = agk.Gatekeeper(agk.Budget(max_bone_influences=2))
expect("max_bone_influences=2 rejects a 4-influence mesh",
       any("bone weights" in p for p in strict_rig.check(DROP / "rig_ok.gltf").problems))

loose_rig_cfg = scratch / "rules_rig"
loose_rig_cfg.mkdir(exist_ok=True)
(loose_rig_cfg / "rules.toml").write_text(
    "[mesh]\nmax_bone_influences = 8\nmax_uv_channels = 6\nallow_degenerate = true\n", encoding="utf-8")
rig_budget, _, _ = agk.resolve_budget(loose_rig_cfg, agk.build_parser().parse_args([str(loose_rig_cfg)]))
expect("rules.toml sets the deep budgets",
       (rig_budget.max_bone_influences, rig_budget.max_uv_channels, rig_budget.allow_degenerate)
       == (8, 6, True), str(rig_budget))

print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
