"""[lod] and [collision]: LOD levels and Unreal collision meshes (FBX and glTF).

    python tests/test_lod_collision.py

Every fixture is generated here: small ASCII FBX scenes and .gltf files with
their buffers inline. The real Unreal 5.3.2 exports in tests/fixtures/fbx_ue
(an LOD group with a UCX_ hull) are checked too: Unreal's own output must pass
the rules it is the reference for.
"""
import base64
import json
import math
import shutil
import struct
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asset_gatekeeper as agk  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
WORK = ROOT / "scratch" / "lod_collision_tests"
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)

failures = []


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(label)


# --------------------------------------------------------------------------- #
# Geometry: (vertices, polygons) with polygons as lists of vertex indices.

def box(size=(1.0, 1.0, 1.0), centre=(0.0, 0.0, 0.0)):
    sx, sy, sz = (s / 2 for s in size)
    cx, cy, cz = centre
    vertices = [(cx + x * sx, cy + y * sy, cz + z * sz)
                for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
    polygons = [[0, 1, 3, 2], [4, 6, 7, 5], [0, 4, 5, 1], [2, 3, 7, 6], [0, 2, 6, 4], [1, 5, 7, 3]]
    return vertices, polygons


def strip(quads):
    """A flat strip of quads: 2 triangles each (render meshes for LOD counts)."""
    vertices = [(float(i), float(j), 0.0) for i in range(quads + 1) for j in (0, 1)]
    polygons = [[2 * i, 2 * i + 2, 2 * i + 3, 2 * i + 1] for i in range(quads)]
    return vertices, polygons


def l_prism():
    """An L-shaped block: clearly not convex (the inner corner is a 1 m dent in 3 m)."""
    outline = [(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)]
    vertices = [(float(x), float(y), 0.0) for x, y in outline] + \
               [(float(x), float(y), 1.0) for x, y in outline]
    polygons = [[5, 4, 3, 2, 1, 0], [6, 7, 8, 9, 10, 11]]
    polygons += [[i, (i + 1) % 6, (i + 1) % 6 + 6, i + 6] for i in range(6)]
    return vertices, polygons


def cylinder(sides, radius=0.5, height=1.0):
    vertices = [(radius * math.cos(2 * math.pi * i / sides), radius * math.sin(2 * math.pi * i / sides), z)
                for z in (0.0, height) for i in range(sides)]
    polygons = [list(range(sides - 1, -1, -1)), list(range(sides, 2 * sides))]
    polygons += [[i, (i + 1) % sides, (i + 1) % sides + sides, i + sides] for i in range(sides)]
    return vertices, polygons


def plane():
    return [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)], [[0, 1, 2, 3]]


def triangles_of(polygons):
    return [(p[0], p[k], p[k + 1]) for p in polygons for k in range(1, len(p) - 1)]


# --------------------------------------------------------------------------- #
# ASCII FBX: Models (Mesh, LodGroup, Null), Geometry, an optional Skin.

def fbx_scene(path, objects):
    """objects: (name, kind, parent, geometry or None, skinned). kind Mesh / LodGroup / Null;
    parent is another object's name or None (scene root). Children keep list order (LOD order)."""
    ids, lines, links = {}, [], []
    next_id = [1000]

    def new_id():
        next_id[0] += 1
        return next_id[0]

    for name, kind, parent, geometry, skinned in objects:
        model = ids[name] = new_id()
        lines.append(f'\tModel: {model}, "Model::{name}", "{kind}" {{\n\t\tVersion: 232\n\t}}')
        links.append((model, ids[parent] if parent else 0))
        if geometry is not None:
            vertices, polygons = geometry
            geometry_id = new_id()
            flat = ",".join(repr(float(c)) for v in vertices for c in v)
            indices = ",".join(",".join(str(i) for i in p[:-1]) + f",{-p[-1] - 1}" for p in polygons)
            count = sum(len(p) for p in polygons)
            lines.append(f'\tGeometry: {geometry_id}, "Geometry::{name}", "Mesh" {{\n'
                         f'\t\tVertices: *{len(vertices) * 3} {{\n\t\t\ta: {flat}\n\t\t}}\n'
                         f'\t\tPolygonVertexIndex: *{count} {{\n\t\t\ta: {indices}\n\t\t}}\n\t}}')
            links.append((geometry_id, model))
            if skinned:
                skin, cluster = new_id(), new_id()
                lines.append(f'\tDeformer: {skin}, "Deformer::Skin", "Skin" {{\n\t}}')
                lines.append(f'\tDeformer: {cluster}, "SubDeformer::Bone", "Cluster" {{\n'
                             f'\t\tIndexes: *1 {{\n\t\t\ta: 0\n\t\t}}\n'
                             f'\t\tWeights: *1 {{\n\t\t\ta: 1.0\n\t\t}}\n\t}}')
                links += [(skin, geometry_id), (cluster, skin)]
    text = ("; FBX 7.3.0 project file\n"
            "FBXHeaderExtension:  {\n\tFBXHeaderVersion: 1003\n\tFBXVersion: 7300\n}\n"
            "Objects:  {\n" + "\n".join(lines) + "\n}\n"
            "Connections:  {\n" + "\n".join(f'\tC: "OO",{a},{b}' for a, b in links) + "\n}\n")
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# glTF with inline buffers. nodes: dicts with name, geometry, children, extensions.

def gltf_scene(path, nodes, roots, skinned=False):
    blob = bytearray()
    document = {"asset": {"version": "2.0", "generator": "test"}, "scene": 0,
                "scenes": [{"nodes": roots}], "nodes": [], "meshes": [], "accessors": [],
                "bufferViews": []}

    def view(data):
        while len(blob) % 4:
            blob.append(0)
        document["bufferViews"].append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)})
        blob.extend(data)
        return len(document["bufferViews"]) - 1

    for item in nodes:
        entry = {key: value for key, value in item.items() if key not in ("geometry", "mesh_name")}
        geometry = item.get("geometry")
        if geometry is not None:
            vertices, polygons = geometry
            triangles = triangles_of(polygons)
            positions = struct.pack(f"<{len(vertices) * 3}f", *[c for v in vertices for c in v])
            indices = struct.pack(f"<{len(triangles) * 3}I", *[i for t in triangles for i in t])
            lows = [min(v[k] for v in vertices) for k in range(3)]
            highs = [max(v[k] for v in vertices) for k in range(3)]
            document["accessors"].append({"bufferView": view(positions), "componentType": 5126,
                                          "count": len(vertices), "type": "VEC3",
                                          "min": lows, "max": highs})
            document["accessors"].append({"bufferView": view(indices), "componentType": 5125,
                                          "count": len(triangles) * 3, "type": "SCALAR"})
            attributes = {"POSITION": len(document["accessors"]) - 2}
            document["meshes"].append({"name": item.get("mesh_name", item.get("name", "")),
                                       "primitives": [{"attributes": attributes,
                                                       "indices": len(document["accessors"]) - 1}]})
            entry["mesh"] = len(document["meshes"]) - 1
        document["nodes"].append(entry)
    if skinned:
        document["nodes"].append({"name": "root_bone"})
        document["skins"] = [{"joints": [len(document["nodes"]) - 1]}]
        for entry in document["nodes"]:
            if "mesh" in entry:
                entry["skin"] = 0
    document["buffers"] = [{"byteLength": len(blob), "uri": "data:application/octet-stream;base64,"
                            + base64.b64encode(bytes(blob)).decode()}]
    if any("MSFT_lod" in (entry.get("extensions") or {}) for entry in document["nodes"]):
        document["extensionsUsed"] = ["MSFT_lod"]
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def check(path, budget):
    return agk.Gatekeeper(budget).check(Path(path))


def problems_of(path, budget):
    return " | ".join(check(path, budget).problems)


def rules(**tables):
    return agk.Budget.from_mapping({"mesh": {"max_material_slots": 8}, **tables})


# --------------------------------------------------------------------------- #
print("1. FBX: LOD levels from an LOD group, and from _LOD names")
lod3 = fbx_scene(WORK / "SM_Rock_lod3.fbx", [
    ("Rock", "LodGroup", None, None, False),
    ("Rock_LOD0", "Mesh", "Rock", strip(100), False),
    ("Rock_LOD1", "Mesh", "Rock", strip(50), False),
    ("Rock_LOD2", "Mesh", "Rock", strip(25), False),
    ("UCX_Rock_LOD0", "Mesh", None, box(), False),
])
summary = agk.read_fbx(lod3)
expect("an LOD group reads as three levels: 200 / 100 / 50 triangles",
       summary.lod_groups == (("Rock", (200, 100, 50), "group"),), summary.lod_groups)
expect("... and LOD0 alone is the triangle budget, as before", summary.triangles == 200, summary.triangles)
expect("a 3-level asset passes min_levels = 3",
       not problems_of(lod3, rules(lod={"min_levels": 3})), problems_of(lod3, rules(lod={"min_levels": 3})))
verdict = check(lod3, rules(lod={"min_levels": 4}))
expect("... and fails min_levels = 4, saying what it has and what to add",
       "3 LOD levels in 'Rock'; at least 4 are required: add LOD3" in " | ".join(verdict.problems),
       verdict.problems)
stats = dict(verdict.stats)
expect("the levels and their triangles are in the stats",
       stats.get("lod_levels") == 3 and stats.get("lod0_triangles") == 200
       and stats.get("lod1_triangles") == 100 and stats.get("lod2_triangles") == 50, verdict.stats)
expect("... and the count is in the one-line metric", "3 LODs" in verdict.metric, verdict.metric)
expect("halving levels pass max_ratio = 0.6",
       not problems_of(lod3, rules(lod={"max_ratio": 0.6})), problems_of(lod3, rules(lod={"max_ratio": 0.6})))

flat_ladder = fbx_scene(WORK / "SM_Tree_ratio.fbx", [
    ("Tree", "LodGroup", None, None, False),
    ("Tree_LOD0", "Mesh", "Tree", strip(100), False),
    ("Tree_LOD1", "Mesh", "Tree", strip(50), False),
    ("Tree_LOD2", "Mesh", "Tree", strip(40), False),
])
text = problems_of(flat_ladder, rules(lod={"max_ratio": 0.6}))
expect("a level that keeps 80% of the one above fails max_ratio = 0.6, with the numbers",
       "LOD2 of 'Tree' keeps 0.80 of LOD1's triangles (80 of 100); at most 0.6 is allowed: "
       "reduce it to 60 triangles or fewer" in text, text)
expect("... and only that level is named (LOD1 at 0.50 is fine)", "LOD1 of" not in text, text)

no_lods = fbx_scene(WORK / "SM_Crate_nolod.fbx", [("Crate", "Mesh", None, box(), False),
                                                 ("UCX_Crate", "Mesh", None, box(), False)])
verdict = check(no_lods, rules(lod={"min_levels": 2}))
expect("an FBX without LODs fails min_levels, saying it has LOD0 only and how to fix it",
       "no LODs (LOD0 only); at least 2 levels are required: add LOD1 (export an LOD group)"
       in " | ".join(verdict.problems), verdict.problems)
expect("... its stats say 1 level", dict(verdict.stats).get("lod_levels") == 1, verdict.stats)
expect("min_levels = 1 is met by any mesh", not problems_of(no_lods, rules(lod={"min_levels": 1})))

by_name = fbx_scene(WORK / "SM_Lamp_names.fbx", [("Lamp_LOD0", "Mesh", None, strip(40), False),
                                                 ("Lamp_LOD1", "Mesh", None, strip(20), False)])
verdict = check(by_name, rules(lod={"min_levels": 2}))
expect("without an LOD group, _LOD0/_LOD1 names count as levels",
       agk.read_fbx(by_name).lod_groups == (("Lamp", (80, 40), "names"),) and verdict.passed,
       (agk.read_fbx(by_name).lod_groups, verdict.problems))
expect("... with a note that Unreal reads an FBX's LODs only from an LOD group",
       any("Unreal reads an FBX's LODs only from an LOD group" in w for w in verdict.warnings),
       verdict.warnings)

print("2. LOD names: the chain rules")
chains = agk.lod_chains_by_name([("Rock", 300), ("Rock_LOD1", 150), ("rock_lod2", 70),
                                 ("Bush_LOD0", 10), ("Bush_LOD2", 5), ("Crate_LOD0", 12)])
expect("a plain <name> stands in for a missing _LOD0; names compare without case",
       ("Rock", (300, 150, 70)) in chains, chains)
expect("a chain stops at the first missing level (Bush: LOD0, then LOD2 -> 1 level, no chain)",
       not any(name == "Bush" for name, _ in chains), chains)
expect("a lone _LOD0 is a mesh without LODs", not any(name == "Crate" for name, _ in chains), chains)
expect("a namespace is not part of the name", agk.lod_chains_by_name(
    [("ns:Pot_LOD0", 4), ("ns:Pot_LOD1", 2)]) == [("Pot", (4, 2))])

print("3. glTF: MSFT_lod and _LOD node names")
msft = gltf_scene(WORK / "SM_Vase_msft.gltf", [
    {"name": "Vase", "geometry": strip(60), "extensions": {"MSFT_lod": {"ids": [1, 2]}}},
    {"name": "Vase_coarse", "geometry": strip(30)},
    {"name": "Vase_coarsest", "geometry": strip(10)},
], roots=[0])
structure = agk.gltf_structure(msft, agk.read_gltf_document(msft))
expect("MSFT_lod: the node and its ids are LOD0, LOD1, LOD2",
       structure["lod_groups"] == (("Vase", (120, 60, 20), "MSFT_lod"),), structure["lod_groups"])
expect("... the coarser levels are outside the scene, so the triangle budget is LOD0's",
       agk.count_triangles(msft) == 120, agk.count_triangles(msft))
expect("... a 3-level glTF passes min_levels = 3 and max_ratio = 0.5",
       check(msft, rules(lod={"min_levels": 3, "max_ratio": 0.5})).passed,
       check(msft, rules(lod={"min_levels": 3, "max_ratio": 0.5})).problems)
named = gltf_scene(WORK / "SM_Bench_names.gltf", [
    {"name": "Bench", "children": [1, 2]},
    {"name": "Bench_LOD0", "geometry": strip(50)},
    {"name": "Bench_LOD1", "geometry": strip(40)},
], roots=[0])
text = problems_of(named, rules(lod={"min_levels": 3, "max_ratio": 0.5}))
expect("_LOD node names: too few levels and too little reduction both fail, in one line",
       "2 LOD levels in 'Bench'; at least 3 are required: add LOD2" in text
       and "LOD1 of 'Bench' keeps 0.80 of LOD0's triangles (80 of 100)" in text, text)
plain = gltf_scene(WORK / "SM_Cup.gltf", [{"name": "Cup", "geometry": strip(4)}], roots=[0])
expect("a glTF without LODs fails min_levels, naming both glTF ways to add them",
       "add LOD1-LOD2 (add them as MSFT_lod levels or as nodes named <mesh>_LOD1, ...)"
       in problems_of(plain, rules(lod={"min_levels": 3})), problems_of(plain, rules(lod={"min_levels": 3})))
empty_level = fbx_scene(WORK / "SM_Pipe_empty.fbx", [
    ("Pipe", "LodGroup", None, None, False), ("Pipe_LOD0", "Mesh", "Pipe", strip(4), False),
    ("Pipe_LOD1", "Null", "Pipe", None, False)])
expect("an LOD level with no triangles is named, with what to do",
       "LOD1 of 'Pipe' has no triangles: re-export it or remove the level"
       in problems_of(empty_level, rules(lod={"max_ratio": 0.5})),
       problems_of(empty_level, rules(lod={"max_ratio": 0.5})))
obj = WORK / "SM_Stone.obj"
obj.write_text("o Stone\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
expect("an .obj cannot carry LODs, and the failure says what to export instead",
       ".obj cannot carry LODs: export FBX with an LOD group" in problems_of(obj, rules(lod={"min_levels": 2})),
       problems_of(obj, rules(lod={"min_levels": 2})))

print("4. Collision: require, and the skinned-mesh exemption")
bare = fbx_scene(WORK / "SM_Barrel_bare.fbx", [("Barrel", "Mesh", None, box(), False)])
text = problems_of(bare, rules(collision={"require": True}))
expect("a static FBX without collision fails require, with the name to use",
       "no collision: a static mesh needs a UCX_ (or UBX_/USP_/UCP_) mesh named after its render "
       "mesh, such as UCX_Barrel_00" in text, text)
expect("... and one with a UCX_ hull passes", check(no_lods, rules(collision={"require": True})).passed,
       check(no_lods, rules(collision={"require": True})).problems)
skinned = fbx_scene(WORK / "SK_Hero.fbx", [("Hero", "Mesh", None, box(), True)])
expect("a skinned FBX is exempt (it uses a physics asset)",
       agk.read_fbx(skinned).skinned and check(skinned, rules(collision={"require": True})).passed,
       check(skinned, rules(collision={"require": True})).problems)
skinned_gltf = gltf_scene(WORK / "SK_Hero.gltf", [{"name": "Hero", "geometry": box()}], roots=[0],
                          skinned=True)
expect("... so is a skinned glTF",
       "no collision" not in problems_of(skinned_gltf, rules(collision={"require": True})),
       problems_of(skinned_gltf, rules(collision={"require": True})))
bare_gltf = gltf_scene(WORK / "SM_Barrel.gltf", [{"name": "Barrel", "geometry": box()}], roots=[0])
expect("a static glTF without collision fails require",
       "no collision" in problems_of(bare_gltf, rules(collision={"require": True})))
with_hull = gltf_scene(WORK / "SM_Barrel_hull.gltf", [
    {"name": "Barrel", "geometry": box()}, {"name": "UCX_Barrel_00", "geometry": box((1.1, 1.1, 1.1))},
], roots=[0, 1])
expect("... and one with a UCX_ node passes",
       check(with_hull, rules(collision={"require": True})).passed,
       check(with_hull, rules(collision={"require": True})).problems)
verdict = check(obj, rules(collision={"require": True}))
expect("an .obj is not failed for collision it cannot carry, but says it was not checked",
       not any("collision" in p for p in verdict.problems)
       and any("collision not checked: .obj" in w for w in verdict.warnings), (verdict.problems, verdict.warnings))

print("5. Collision: names must match a render mesh")
expect("UCX_<mesh> and UCX_<mesh>_<suffix> match (Unreal's UCX_Tree_01_00 for Tree_01)",
       agk.collision_target("UCX_Tree_01", ["Tree_01"]) and agk.collision_target("UCX_Tree_01_00", ["Tree_01"])
       and agk.collision_target("UBX_SM_Door_3", ["SM_Door"]))
expect("... another name, a different case or a bare prefix does not",
       not agk.collision_target("UCX_Tree", ["Tree_01"]) and not agk.collision_target("UCX_tree_01", ["Tree_01"])
       and not agk.collision_target("UCX_", ["Tree"]))
orphan = fbx_scene(WORK / "SM_Chair_orphan.fbx", [("Chair", "Mesh", None, box(), False),
                                                  ("UCX_Chiar_00", "Mesh", None, box(), False)])
text = problems_of(orphan, rules(collision={"name_must_match": True}))
expect("a hull named after no render mesh fails, naming it and the mesh to name it after",
       "collision mesh 'UCX_Chiar_00' matches no render mesh, so Unreal ignores it on import: "
       "name it UCX_<mesh> or UCX_<mesh>_00 after 'Chair' (case matters)" in text, text)
expect("an empty [collision] table turns name matching on",
       "matches no render mesh" in problems_of(orphan, rules(collision={})))
expect("... and name_must_match = false turns it off",
       check(orphan, rules(collision={"name_must_match": False})).passed,
       check(orphan, rules(collision={"name_must_match": False})).problems)
ue_lod = FIXTURES / "fbx_ue" / "cube_lod_binary.fbx"
expect("Unreal's own export (UCX_cube_subd3_LOD0 beside LOD0 cube_subd3_LOD0) matches",
       check(ue_lod, rules(collision={})).passed, check(ue_lod, rules(collision={})).problems)
orphan_gltf = gltf_scene(WORK / "SM_Table.gltf", [
    {"name": "Table", "geometry": box()}, {"name": "UBX_Tabel", "geometry": box()},
    {"name": "USP_Tabel_01", "geometry": box()}], roots=[0, 1, 2])
text = problems_of(orphan_gltf, rules(collision={}))
expect("glTF: two orphans are one problem naming both",
       "collision meshes 'UBX_Tabel', 'USP_Tabel_01' match no render mesh, so Unreal ignores them"
       in text, text)
mesh_named = gltf_scene(WORK / "SM_Lamp_mesh.gltf", [
    {"name": "node0", "mesh_name": "Lamp", "geometry": box()},
    {"name": "UCX_Lamp", "geometry": box()}], roots=[0, 1])
expect("glTF: a hull may be named after the render node or its mesh",
       check(mesh_named, rules(collision={})).passed, check(mesh_named, rules(collision={})).problems)

print("6. Collision: hull count and vertices per hull")
many = fbx_scene(WORK / "SM_Arch_hulls.fbx", [("Arch", "Mesh", None, box((3, 1, 3)), False)] + [
    (f"UCX_Arch_{i:02d}", "Mesh", None, box((1, 1, 1), (i, 0, 0)), False) for i in range(5)] + [
    ("UBX_Arch_99", "Mesh", None, box(), False)])
text = problems_of(many, rules(collision={"max_hulls": 4}))
expect("5 UCX_ hulls fail max_hulls = 4 (the UBX_ box is not a hull)",
       "5 convex hulls (UCX_) exceed the 4 hull budget: merge or remove 1" in text, text)
expect("... and pass max_hulls = 5", check(many, rules(collision={"max_hulls": 5})).passed)
expect("the hull count is in the stats",
       dict(check(many, rules(collision={"max_hulls": 5})).stats).get("collision_hulls") == 5
       and dict(check(many, rules(collision={"max_hulls": 5})).stats).get("collision_shapes") == 6)
round_hull = fbx_scene(WORK / "SM_Pillar.fbx", [("Pillar", "Mesh", None, cylinder(40), False),
                                                ("UCX_Pillar", "Mesh", None, cylinder(40, 0.55), False)])
text = problems_of(round_hull, rules(collision={"max_hull_vertices": 32}))
expect("an 80-vertex hull fails max_hull_vertices = 32, with its count",
       "collision hull 'UCX_Pillar' (80 vertices) is over the 32-vertex hull limit: simplify it" in text, text)
expect("... and passes 255 (PhysX's cap)", check(round_hull, rules(collision={"max_hull_vertices": 255})).passed)
expect("Unreal's own 32-vertex hull reads as 32",
       dict(check(ue_lod, rules(collision={})).stats).get("max_hull_vertices") == 32)

print("7. Collision: UCX_ hulls must be convex")
for shape, sides in (("box", box()), ("40-sided cylinder", cylinder(40))):
    count, concavity, flat = agk.hull_shape(sides[0], triangles_of(sides[1]))
    expect(f"a {shape} is convex (concavity {concavity})", concavity == 0 and not flat, (count, concavity))
vertices, polygons = box()
flipped = [list(reversed(p)) if i % 2 else p for i, p in enumerate(polygons)]
expect("... whichever way its faces are wound", agk.hull_shape(vertices, triangles_of(flipped))[1] == 0)
count, concavity, flat = agk.hull_shape(*(lambda v, p: (v, triangles_of(p)))(*l_prism()))
expect("an L-shaped block is not: its dent is a third of its size", abs(concavity - 1 / 3) < 1e-6,
       concavity)
dented = [list(v) for v in box()[0]]
dented[7] = [0.5, 0.5, 0.49]                    # a top corner pushed in by 1 cm of a 1.7 m diagonal
expect("a dent under 1% of the hull's size is within tolerance",
       agk.hull_shape(dented, triangles_of(box()[1]))[1] <= agk.HULL_CONCAVITY_TOLERANCE,
       agk.hull_shape(dented, triangles_of(box()[1])))
concave = fbx_scene(WORK / "SM_Corner.fbx", [("Corner", "Mesh", None, l_prism(), False),
                                             ("UCX_Corner", "Mesh", None, l_prism(), False)])
text = problems_of(concave, rules(collision={}))
expect("an L-shaped UCX_ fails, saying Unreal fills the dent and how to split it",
       "collision hull 'UCX_Corner' is not convex (a dent 33% of its size): Unreal replaces it with its "
       "convex hull on import, filling the dent; split it into convex pieces (UCX_<mesh>_00, _01, ...)"
       in text, text)
expect("... convex = false lets it through", check(concave, rules(collision={"convex": False})).passed)
flat_hull = gltf_scene(WORK / "SM_Floor.gltf", [{"name": "Floor", "geometry": box()},
                                                {"name": "UCX_Floor", "geometry": plane()}], roots=[0, 1])
expect("a flat UCX_ (a plane) fails: no volume to build a hull from",
       "collision hull 'UCX_Floor' is flat (no volume)" in problems_of(flat_hull, rules(collision={})),
       problems_of(flat_hull, rules(collision={})))
concave_gltf = gltf_scene(WORK / "SM_Corner.gltf", [{"name": "Corner", "geometry": l_prism()},
                                                    {"name": "UCX_Corner_01", "geometry": l_prism()}],
                          roots=[0, 1])
expect("glTF hulls get the same convexity test",
       "'UCX_Corner_01' is not convex (a dent 33%" in problems_of(concave_gltf, rules(collision={})),
       problems_of(concave_gltf, rules(collision={})))
boxed = fbx_scene(WORK / "SM_Shelf.fbx", [("Shelf", "Mesh", None, l_prism(), False),
                                          ("UBX_Shelf", "Mesh", None, l_prism(), False)])
expect("UBX_/USP_/UCP_ shapes are fitted by Unreal, so they are not tested for convexity",
       check(boxed, rules(collision={})).passed, check(boxed, rules(collision={})).problems)
expect("ASCII and binary Unreal exports read the same collision",
       agk.read_fbx(ue_lod).collision_shapes
       == agk.read_fbx(FIXTURES / "fbx_ue" / "cube_lod_ascii.fbx").collision_shapes
       == (("UCX_cube_subd3_LOD0", 32, 0.0, False),), agk.read_fbx(ue_lod).collision_shapes)

print("8. Everything at once, on Unreal's own exports")
strict = rules(lod={"min_levels": 3, "max_ratio": 0.5},
               collision={"require": True, "max_hulls": 1, "max_hull_vertices": 32})
verdict = check(ue_lod, strict)
expect("Unreal's LOD-group export with its UCX_ hull passes every LOD and collision rule",
       verdict.passed, verdict.problems)
expect("... and its metric says so", verdict.metric.endswith("3 LODs / 1 collision"), verdict.metric)
for name in ("mats5_binary.fbx", "mats5_ascii_7700.fbx"):
    verdict = check(FIXTURES / "fbx_ue" / name, rules(collision={"require": True, "max_hull_vertices": 8}))
    expect(f"{name}: Unreal's 8-vertex box hull passes", verdict.passed, verdict.problems)

print("9. Off by default: no verdict changes")
generated = sorted(WORK.glob("*.fbx")) + sorted(WORK.glob("*.gltf")) + [obj]
shipped = sorted((FIXTURES / "fbx").glob("*.fbx")) + sorted((FIXTURES / "fbx_ue").glob("*.fbx"))
for budget, label in ((agk.Budget(), "Budget()"), (agk.Budget.from_mapping({}), "an empty rules file"),
                      (agk.Budget.from_mapping({"lod": {}}), "an empty [lod] table")):
    touched = []
    for path in generated + shipped:
        verdict = check(path, budget)
        words = " ".join(verdict.problems + verdict.warnings) + verdict.metric
        if ("LOD" in words or "collision" in words or "hull" in words
                or any(key.startswith(("lod", "collision")) or key == "max_hull_vertices"
                       for key, _ in verdict.stats)):
            touched.append(path.name)
    expect(f"{label}: no LOD or collision problem, note, metric or stat on {len(generated + shipped)} files",
           not touched, touched)
defaults = agk.Budget()
expect("every new Budget field is off",
       (defaults.lod_min_levels, defaults.lod_max_ratio, defaults.collision_require,
        defaults.collision_name_match, defaults.collision_max_hulls,
        defaults.collision_max_hull_vertices, defaults.collision_convex) == (0, 0.0, False, False, 0, 0, False))
expect("the startup banner is unchanged by default",
       "lod" not in agk.describe_budget(defaults) and "collision" not in agk.describe_budget(defaults))
expect("... and names the rules when they are on",
       "lod>=3" in agk.describe_budget(strict) and "collision=required,names,convex,hulls<=1,hull_verts<=32"
       in agk.describe_budget(strict), agk.describe_budget(strict))

print("10. Bad values stop the run with a message that says what is wanted")
bad = {
    "[lod] must be a table": {"lod": 3},
    "[lod].min_levels must be at least 0, got -1": {"lod": {"min_levels": -1}},
    "[lod].min_levels must be an integer, got 2.5": {"lod": {"min_levels": 2.5}},
    "[lod].max_ratio must be a fraction above 0 and at most 1, such as 0.6": {"lod": {"max_ratio": 1.5}},
    "[lod].max_ratio must be a number, got '0.5'": {"lod": {"max_ratio": "0.5"}},
    "[lod].max_ratio must be a fraction above 0 and at most 1": {"lod": {"max_ratio": float("nan")}},
    "unknown key(s) in [lod] (known: min_levels, max_ratio): levels": {"lod": {"levels": 3}},
    "[collision] must be a table": {"collision": True},
    "[collision].require must be true or false, got 'yes'": {"collision": {"require": "yes"}},
    "[collision].max_hulls must be at least 0, got -1": {"collision": {"max_hulls": -1}},
    "[collision].max_hull_vertices must be 0 (off) or at least 4: a convex hull needs 4 corners":
        {"collision": {"max_hull_vertices": 3}},
    "unknown key(s) in [collision] (known: require, name_must_match, max_hulls, max_hull_vertices, "
    "convex): covex": {"collision": {"covex": False}},
}
for wanted, document in bad.items():
    try:
        agk.Budget.from_mapping(document)
    except agk.ConfigError as exc:
        expect(f"{json.dumps(document, default=str)} -> {wanted[:60]}...", wanted in str(exc), str(exc))
    else:
        expect(f"{json.dumps(document, default=str)} is refused", False, "accepted")
try:
    agk.Budget(lod_min_levels=-2)
except agk.ConfigError as exc:
    expect("a Budget built in code is checked too", "min_levels must be a whole number" in str(exc), str(exc))
else:
    expect("a Budget built in code is checked too", False, "accepted")

print("11. Through the command line: rules.toml, one line per asset, an exception")
project = WORK / "project"
project.mkdir()
for path in (lod3, no_lods, orphan):
    shutil.copy(path, project / path.name)
today = date.today()
(project / "rules.toml").write_text(
    "[mesh]\nmax_material_slots = 8\n\n[lod]\nmin_levels = 3\nmax_ratio = 0.6\n\n"
    "[collision]\nrequire = true\n\n"
    "[[exceptions]]\npath = \"SM_Chair_orphan.fbx\"\nrule = \"lod_min_levels\"\nallow = 0\n"
    "reason = \"placeholder until the LOD pass\"\nowner = \"art\"\n"
    f"expires = {(today + timedelta(days=30)).isoformat()}\n", encoding="utf-8")
completed = subprocess.run([sys.executable, str(ROOT / "asset_gatekeeper.py"), str(project), "--once",
                            "--json", "--no-color", "--no-notify"], capture_output=True, text=True,
                           timeout=180)
rows = {}
for line in completed.stdout.splitlines():
    try:
        row = json.loads(line)
    except ValueError:
        continue
    if "file" in row:
        rows[Path(row["file"]).name] = row
expect("no traceback", "Traceback" not in completed.stderr, completed.stderr[-400:])
expect("the 3-level rock with its hull passes", rows.get("SM_Rock_lod3.fbx", {}).get("passed") is True,
       rows.get("SM_Rock_lod3.fbx"))
expect("the crate without LODs fails on LODs only",
       rows.get("SM_Crate_nolod.fbx", {}).get("problems") == [
           "no LODs (LOD0 only); at least 3 levels are required: add LOD1-LOD2 (export an LOD group)"],
       rows.get("SM_Crate_nolod.fbx"))
chair = rows.get("SM_Chair_orphan.fbx", {})
expect("the chair's LOD rule is waived by its exception, its misnamed hull still fails",
       len(chair.get("problems", [])) == 1 and "matches no render mesh" in chair["problems"][0],
       chair)
plain_run = subprocess.run([sys.executable, str(ROOT / "asset_gatekeeper.py"), str(project), "--once",
                            "--no-color", "--no-notify"], capture_output=True, text=True, timeout=180)
line = next((l for l in plain_run.stdout.splitlines() if "SM_Crate_nolod.fbx" in l), "")
expect("the plain report is one line naming the problem", "FAILED" in line and "no LODs (LOD0 only)" in line,
       plain_run.stdout[-600:])
expect("the startup banner shows the rules", "lod>=3" in plain_run.stdout + plain_run.stderr,
       (plain_run.stdout + plain_run.stderr)[:600])

print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
