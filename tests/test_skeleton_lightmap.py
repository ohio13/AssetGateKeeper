"""[skeleton] and [lightmap]: bone budgets and authored lightmap UV checks.

    python tests/test_skeleton_lightmap.py

Every fixture is built here: glTF files in code (a base64 buffer, no binary
files), ASCII FBX text for the FBX cases, plus the Blender-made FBX fixtures in
tests/fixtures/fbx (skin4/skin6: four and six unparented bones; uv3: a plane
with three identical 0-1 UV sets). Both rule tables are off by default, and the
last section checks that nothing reads a skeleton or a lightmap then.
"""
import base64
import json
import shutil
import sys
import time
from pathlib import Path
from unittest.mock import patch

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

import asset_gatekeeper as agk  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "fbx"
WORK = ROOT / "scratch" / "skeleton_lightmap_tests"
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)

failures = []


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def config_error(document):
    """The ConfigError message a rules document raises, or '' when it loads."""
    try:
        agk.Budget.from_mapping(document)
    except agk.ConfigError as exc:
        return str(exc)
    return ""


def budget_error(**fields):
    try:
        agk.Budget(**fields)
    except agk.ConfigError as exc:
        return str(exc)
    return ""


# --------------------------------------------------------------------------- #
# glTF built in code
# --------------------------------------------------------------------------- #

class GltfBuilder:
    """Accessors packed into one base64 buffer; meshes, nodes and skins as given."""

    def __init__(self):
        self.blob = bytearray()
        self.views, self.accessors = [], []

    def add(self, values, component=5126, kind="VEC2", normalized=False):
        dtype = {5126: "<f4", 5125: "<u4", 5123: "<u2", 5121: "<u1"}[component]
        width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[kind]
        array = np.asarray(values, dtype=dtype).reshape(-1, width)
        while len(self.blob) % 4:
            self.blob.append(0)
        self.views.append({"buffer": 0, "byteOffset": len(self.blob), "byteLength": array.nbytes})
        self.blob += array.tobytes()
        accessor = {"bufferView": len(self.views) - 1, "componentType": component,
                    "count": len(array), "type": kind}
        if normalized:
            accessor["normalized"] = True
        if kind == "VEC3" and component == 5126:
            accessor["min"] = array.min(axis=0).tolist()
            accessor["max"] = array.max(axis=0).tolist()
        self.accessors.append(accessor)
        return len(self.accessors) - 1

    def write(self, path, meshes, nodes, skins=(), materials=()):
        document = {
            "asset": {"version": "2.0", "generator": "test"},
            "scene": 0, "scenes": [{"nodes": [i for i, n in enumerate(nodes) if n.pop("_root", True)]}],
            "nodes": nodes, "meshes": meshes,
            "buffers": [{"byteLength": len(self.blob),
                         "uri": "data:application/octet-stream;base64,"
                                + base64.b64encode(bytes(self.blob)).decode()}],
            "bufferViews": self.views, "accessors": self.accessors,
        }
        if skins:
            document["skins"] = list(skins)
        if materials:
            document["materials"] = [{"name": name} for name in materials]
        path.write_text(json.dumps(document), encoding="utf-8")
        return path


def grid(n, size=1.0):
    """An n x n grid of quads: positions (z=0), 0-1 UVs, triangle indices."""
    xs = np.linspace(0.0, 1.0, n + 1)
    uu, vv = np.meshgrid(xs, xs, indexing="xy")
    uv = np.c_[uu.ravel(), vv.ravel()]
    positions = np.c_[uv * size, np.zeros(len(uv))]
    tris = []
    for j in range(n):
        for i in range(n):
            a, b, c, d = j * (n + 1) + i, j * (n + 1) + i + 1, (j + 1) * (n + 1) + i + 1, (j + 1) * (n + 1) + i
            tris += [(a, b, c), (a, c, d)]
    return positions, uv, np.asarray(tris)


def per_quad_uvs(n):
    """Unwelded n x n quads, each quad mapped to the whole 0-1 square (fully overlapping)."""
    positions, uvs, tris = [], [], []
    for j in range(n):
        for i in range(n):
            base = len(positions)
            positions += [(i, j, 0), (i + 1, j, 0), (i + 1, j + 1, 0), (i, j + 1, 0)]
            uvs += [(0, 0), (1, 0), (1, 1), (0, 1)]
            tris += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
    return np.asarray(positions, float), np.asarray(uvs, float), np.asarray(tris)


def static_gltf(path, lightmap=None, uv0=None, n=4, mode=4, quantized=False, lightmap_set=1):
    """A static grid mesh. ``lightmap`` replaces its TEXCOORD_1 (None = grid UVs,
    False = no lightmap set at all)."""
    builder = GltfBuilder()
    positions, uv, tris = grid(n)
    attributes = {"POSITION": builder.add(positions, kind="VEC3")}
    attributes["TEXCOORD_0"] = builder.add(uv if uv0 is None else uv0)
    if lightmap is not False:
        values = uv if lightmap is None else lightmap
        if quantized:
            attributes[f"TEXCOORD_{lightmap_set}"] = builder.add(
                np.round(np.asarray(values) * 65535), component=5123, normalized=True)
        else:
            attributes[f"TEXCOORD_{lightmap_set}"] = builder.add(values)
    primitive = {"attributes": attributes, "mode": mode}
    if mode == 4:
        primitive["indices"] = builder.add(tris.ravel(), component=5125, kind="SCALAR")
    else:
        # A strip over the first row of quads: 0, n+1, 1, n+2, ...
        strip = [k for i in range(n + 1) for k in (i, n + 1 + i)]
        primitive["indices"] = builder.add(strip, component=5125, kind="SCALAR")
    return builder.write(path, [{"name": "Wall", "primitives": [primitive]}],
                         [{"mesh": 0, "name": "Wall"}])


def skinned_gltf(path, joints_parent, sections, weights_dtype=5126, skin_joints=None,
                 static_also=False, pad_joint=0):
    """A skinned mesh whose primitives are weighted to the given joints.

    joints_parent: parent joint of each joint (None = root), in skin order.
    sections: [(material name, [joint indices weighting it])]; each section is
    one primitive of a 1 x len(joints) strip, every vertex weighted equally to
    the section's joints (up to 4; more go to a second JOINTS_1/WEIGHTS_1 set).
    Unused slots of a set point at ``pad_joint`` with weight 0.
    """
    builder = GltfBuilder()
    nodes = [{"name": "Body", "mesh": 0, "skin": 0}]
    joint_nodes = []
    for index, _parent in enumerate(joints_parent):
        nodes.append({"name": f"bone{index}", "_root": False})
        joint_nodes.append(len(nodes) - 1)
    for index, parent in enumerate(joints_parent):
        if parent is None:
            nodes[joint_nodes[index]]["_root"] = True
        else:
            nodes[joint_nodes[parent]].setdefault("children", []).append(joint_nodes[index])
    primitives = []
    for material, (_name, joints) in enumerate(sections):
        positions, _uv, tris = grid(1)
        positions = positions + [material * 2, 0, 0]
        attributes = {"POSITION": builder.add(positions, kind="VEC3")}
        for set_index in range(0, max(1, len(joints)), 4):
            chunk = list(joints[set_index:set_index + 4])
            weight = [1.0 / len(joints)] * len(chunk)
            chunk += [pad_joint] * (4 - len(chunk))
            weight += [0.0] * (4 - len(weight))
            attributes[f"JOINTS_{set_index // 4}"] = builder.add([chunk] * 4, component=5121, kind="VEC4")
            if weights_dtype == 5121:
                attributes[f"WEIGHTS_{set_index // 4}"] = builder.add(
                    [[round(w * 255) for w in weight]] * 4, component=5121, kind="VEC4", normalized=True)
            else:
                attributes[f"WEIGHTS_{set_index // 4}"] = builder.add([weight] * 4, kind="VEC4")
        primitives.append({"attributes": attributes, "material": material,
                           "indices": builder.add(tris.ravel(), component=5125, kind="SCALAR")})
    meshes = [{"name": "Body", "primitives": primitives}]
    if static_also:
        positions, uv, tris = grid(2)
        meshes.append({"name": "Prop", "primitives": [{
            "attributes": {"POSITION": builder.add(positions, kind="VEC3"),
                           "TEXCOORD_0": builder.add(uv)},
            "indices": builder.add(tris.ravel(), component=5125, kind="SCALAR")}]})
        nodes.append({"name": "Prop", "mesh": 1})
    skin = {"joints": joint_nodes if skin_joints is None else skin_joints}
    return builder.write(path, meshes, nodes, [skin], [name for name, _ in sections])


def check(path, **fields):
    return agk.Gatekeeper(agk.Budget(**fields)).check(Path(path))


def has(verdict, text):
    return any(text in problem for problem in verdict.problems)


# --------------------------------------------------------------------------- #
# ASCII FBX built in code
# --------------------------------------------------------------------------- #

def fbx_ascii(path, polygons, vertices, uv_sets=(), materials=(), polygon_materials=None,
              bones=(), clusters=()):
    """A minimal ASCII FBX 7.4: one mesh model, optional UV sets, materials and a skin.

    uv_sets: per-corner UV lists (ByPolygonVertex, Direct), one per set.
    bones: [(name, parent index or None)] LimbNode models.
    clusters: [(bone index, vertex indices, weights)] of one Skin deformer.
    """
    flat = []
    for polygon in polygons:
        flat += list(polygon[:-1]) + [-polygon[-1] - 1]
    lines = ["; FBX 7.4.0 project file", "FBXHeaderExtension:  {", "\tFBXVersion: 7400", "}",
             "Objects:  {",
             '\tGeometry: 100, "Geometry::Mesh", "Mesh" {',
             f"\t\tVertices: *{len(vertices) * 3} {{ a: {','.join(repr(float(c)) for v in vertices for c in v)} }}",
             f"\t\tPolygonVertexIndex: *{len(flat)} {{ a: {','.join(map(str, flat))} }}"]
    for index, corners in enumerate(uv_sets):
        values = ",".join(repr(float(c)) for uv in corners for c in uv)
        lines += [f"\t\tLayerElementUV: {index} {{", f'\t\t\tName: "UV{index}"',
                  '\t\t\tMappingInformationType: "ByPolygonVertex"',
                  '\t\t\tReferenceInformationType: "Direct"',
                  f"\t\t\tUV: *{len(corners) * 2} {{ a: {values} }}", "\t\t}"]
    if polygon_materials is not None:
        lines += ["\t\tLayerElementMaterial: 0 {", '\t\t\tMappingInformationType: "ByPolygon"',
                  '\t\t\tReferenceInformationType: "IndexToDirect"',
                  f"\t\t\tMaterials: *{len(polygon_materials)} {{ a: {','.join(map(str, polygon_materials))} }}",
                  "\t\t}"]
    lines.append("\t}")
    lines.append('\tModel: 200, "Model::Thing", "Mesh" {\n\t}')
    for index, name in enumerate(materials):
        lines.append(f'\tMaterial: {300 + index}, "Material::{name}", "" {{\n\t}}')
    for index, (name, _parent) in enumerate(bones):
        lines.append(f'\tModel: {400 + index}, "Model::{name}", "LimbNode" {{\n\t}}')
    if clusters:
        lines.append('\tDeformer: 500, "Deformer::Skin", "Skin" {\n\t}')
        for index, (_bone, vertex_ids, weights) in enumerate(clusters):
            lines += [f'\tDeformer: {600 + index}, "SubDeformer::Cluster{index}", "Cluster" {{',
                      f"\t\tIndexes: *{len(vertex_ids)} {{ a: {','.join(map(str, vertex_ids))} }}",
                      f"\t\tWeights: *{len(weights)} {{ a: {','.join(repr(float(w)) for w in weights)} }}",
                      "\t}"]
    lines.append("}")
    links = ["Connections:  {", '\tC: "OO",200,0', '\tC: "OO",100,200']
    links += [f'\tC: "OO",{300 + index},200' for index in range(len(materials))]
    for index, (_name, parent) in enumerate(bones):
        links.append(f'\tC: "OO",{400 + index},{0 if parent is None else 400 + parent}')
    if clusters:
        links.append('\tC: "OO",500,100')
        for index, (bone, _ids, _weights) in enumerate(clusters):
            links += [f'\tC: "OO",{600 + index},500', f'\tC: "OO",{400 + bone},{600 + index}']
    links.append("}")
    path.write_text("\n".join(lines + links) + "\n", encoding="utf-8")
    return path


# Two quads sharing an edge (vertices 2, 3).
STRIP_VERTICES = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0), (2, 1, 0)]
STRIP_POLYGONS = [(0, 1, 2, 3), (1, 4, 5, 2)]


# --------------------------------------------------------------------------- #
print("1. Rules: [skeleton] and [lightmap] are off by default and validated")
defaults = agk.Budget()
expect("a default budget checks no skeleton and no lightmap",
       not defaults.skeleton_checked and not defaults.lightmap_checked)
expect("an empty rules file leaves both off",
       not agk.Budget.from_mapping({}).skeleton_checked
       and not agk.Budget.from_mapping({}).lightmap_checked)
loaded = agk.Budget.from_mapping({"skeleton": {"max_bones": 256, "max_bones_per_section": 75,
                                               "max_depth": 20, "single_root": True},
                                  "lightmap": {"require": True, "uv_channel": 1,
                                               "max_overlap": 0.5, "resolution": 512}})
expect("[skeleton] keys land on the budget",
       (loaded.max_bones, loaded.max_bones_per_section, loaded.max_bone_depth,
        loaded.single_root_bone) == (256, 75, 20, True), loaded)
expect("[lightmap] keys land on the budget",
       (loaded.lightmap_require, loaded.lightmap_uv_channel, loaded.lightmap_max_overlap,
        loaded.lightmap_resolution) == (True, 1, 0.5, 512), loaded)
expect("inside_unit_square comes on with require", loaded.lightmap_inside_unit_square)
only_overlap = agk.Budget.from_mapping({"lightmap": {"max_overlap": 0}})
expect("... and with max_overlap alone (max_overlap = 0: no overlap allowed)",
       only_overlap.lightmap_inside_unit_square and only_overlap.lightmap_max_overlap == 0.0
       and not only_overlap.lightmap_require)
expect("require = false on its own switches nothing on",
       not agk.Budget.from_mapping({"lightmap": {"require": False}}).lightmap_checked)
expect("inside_unit_square = false is respected",
       not agk.Budget.from_mapping({"lightmap": {"require": True,
                                                 "inside_unit_square": False}}).lightmap_inside_unit_square)
for document, wanted in (
        ({"skeleton": {"max_bones": -1}}, "[skeleton].max_bones must be at least 0"),
        ({"skeleton": {"max_bones": 2.5}}, "[skeleton].max_bones must be an integer"),
        ({"skeleton": {"single_root": 1}}, "[skeleton].single_root must be true or false"),
        ({"skeleton": {"max_bone": 5}}, "unknown key(s) in [skeleton]: max_bone"),
        ({"skeleton": 5}, "[skeleton] must be a table"),
        ({"lightmap": {"max_overlap": 150}}, "[lightmap].max_overlap must be a percentage from 0 to 100"),
        ({"lightmap": {"max_overlap": -1}}, "leave it out to skip the overlap check"),
        ({"lightmap": {"max_overlap": float("nan")}}, "[lightmap].max_overlap must be a percentage"),
        ({"lightmap": {"uv_channel": 9}}, "[lightmap].uv_channel must be 0-7"),
        ({"lightmap": {"resolution": 8}}, "[lightmap].resolution must be at least 16"),
        ({"lightmap": {"resolution": 8192}}, "[lightmap].resolution must be 16-4096"),
        ({"lightmap": {"require": "yes"}}, "[lightmap].require must be true or false"),
        ({"lightmap": {"overlap": 0}}, "unknown key(s) in [lightmap]: overlap"),
        ({"lightmap": []}, "[lightmap] must be a table")):
    message = config_error(document)
    expect(f"{json.dumps(document)} -> ConfigError naming the key", wanted in message, message)
for fields, wanted in (({"max_bones": -1}, "max_bones must be at least 0"),
                       ({"lightmap_max_overlap": 200.0}, "lightmap_max_overlap must be a percentage"),
                       ({"lightmap_resolution": 10}, "lightmap_resolution must be at least 16"),
                       ({"lightmap_uv_channel": 8}, "lightmap_uv_channel must be at most 7")):
    message = budget_error(**fields)
    expect(f"Budget({fields}) is refused", wanted in message, message)

rules = WORK / "rules.toml"
rules.write_text("[skeleton]\nmax_bones_per_section = 75\nsingle_root = true\n\n"
                 "[lightmap]\nrequire = true\nmax_overlap = 1.5\n", encoding="utf-8")
from_file, _where = agk.load_rules(rules)
expect("a rules.toml with both tables loads",
       from_file.max_bones_per_section == 75 and from_file.single_root_bone
       and from_file.lightmap_require and from_file.lightmap_max_overlap == 1.5, from_file)
line = agk.describe_budget(from_file)
expect("the start-up line names the skeleton and lightmap rules",
       "bones/section<=75" in line and "single-root" in line and "lightmap=uv1(required)" in line
       and "overlap<=1.5%" in line, line)
plain = agk.describe_budget(agk.Budget())
expect("... and says nothing of them when they are off", "bones" not in plain and "lightmap" not in plain, plain)

document = {"skeleton": {"max_bones": 200},
            "platforms": {"mobile": {"skeleton": {"max_bones_per_section": 75}}}}
mobile = agk.Budget.from_mapping(agk.apply_platform(document, "mobile"))
expect("[platforms.mobile.skeleton] tightens the per-section limit and keeps max_bones",
       mobile.max_bones_per_section == 75 and mobile.max_bones == 200, mobile)
expect("... without editing the project's own [skeleton] table",
       document["skeleton"] == {"max_bones": 200})
message = ""
try:
    agk.apply_platform({"platforms": {"mobile": {"skeleton": {"single_root": True}}}}, "mobile")
except agk.ConfigError as exc:
    message = str(exc)
expect("a platform profile cannot switch single_root (switches stay in [skeleton])",
       "single_root" in message, message)
tagged = agk.build_tag_budgets({"skeleton": {"max_bones": 100},
                                "tags": {"hero": {"skeleton": {"max_bones": 400}}}})
expect("[tags.hero.skeleton] gives the hero tag its own bone budget",
       tagged["hero"].max_bones == 400, tagged)
waiver = agk.parse_exceptions({"exceptions": [{"path": "Hero/*", "rule": "max_bones", "allow": 500,
                                               "reason": "hero", "owner": "lead",
                                               "expires": "2099-01-01"}]})
expect("an [[exceptions]] entry may relax max_bones", waiver and waiver[0].allow == 500, waiver)

# --------------------------------------------------------------------------- #
print("2. Skeleton: glTF")
chain = [None, 0, 1, 2]                      # bone0 -> bone1 -> bone2 -> bone3
body = skinned_gltf(WORK / "chain.gltf", chain, [("Skin", [0, 1]), ("Cloth", [1, 2, 3])])
facts = agk.skeleton_facts(body)
expect("4 joints, one root, 4 deep", facts["bones"] == 4 and facts["roots"] == ["bone0"]
       and facts["depth"] == 4 and facts["deepest"] == "bone3", facts)
expect("bones per section are counted per material (Skin 2, Cloth 3)",
       sorted(facts["sections"]) == [("Cloth", 3), ("Skin", 2)], facts["sections"])
verdict = check(body, max_bones=4, max_bones_per_section=3, single_root_bone=True, max_bone_depth=4)
expect("within every skeleton limit: passes", verdict.passed, verdict.problems)
stats = dict(verdict.stats)
expect("... with bones, root_bones, bone_depth and max_bones_per_section in its stats",
       (stats.get("bones"), stats.get("root_bones"), stats.get("bone_depth"),
        stats.get("max_bones_per_section")) == (4, 1, 4, 3), verdict.stats)
expect("... and the bone count in its metric", "4 bones" in verdict.metric, verdict.metric)
verdict = check(body, max_bones=3)
expect("max_bones = 3 fails a 4-bone skeleton with the number",
       has(verdict, "4 bones in the skeleton exceeds the 3 bone budget (+1)"), verdict.problems)
verdict = check(body, max_bones_per_section=2)
expect("max_bones_per_section = 2 names the 3-bone section and says what to do",
       has(verdict, "material section 'Cloth' is skinned to 3 bones, over the 2 bones-per-section limit")
       and has(verdict, "weight it to fewer bones"), verdict.problems)
verdict = check(body, max_bones_per_section=1)
expect("two sections over the limit are one line naming the worst",
       has(verdict, "2 material sections are skinned to more than 1 bones (worst 'Cloth': 3)")
       and len(verdict.problems) == 1, verdict.problems)
verdict = check(body, max_bone_depth=3)
expect("max_depth = 3 fails a 4-deep chain, naming its last bone",
       has(verdict, "bone chain is 4 bones deep (root to 'bone3'), over the 3 depth limit"),
       verdict.problems)
forest = skinned_gltf(WORK / "forest.gltf", [None, None, 0], [("Skin", [0, 1, 2])])
verdict = check(forest, single_root_bone=True)
expect("two root joints fail single_root, naming both",
       has(verdict, "skeleton has 2 root bones ('bone0', 'bone1')"), verdict.problems)
expect("... and the same file passes with single_root off", check(forest).passed, check(forest).problems)
# The two unused slots of each vertex name bone3 with weight 0: it must not count.
zero = skinned_gltf(WORK / "zero_weight.gltf", chain, [("Skin", [0, 1])], pad_joint=3)
facts = agk.skeleton_facts(zero)
expect("a joint listed with zero weight does not weight the section",
       facts["sections"] == [("Skin", 2)], facts["sections"])
quantized = skinned_gltf(WORK / "ubyte_weights.gltf", chain, [("Skin", [0, 1, 2])], weights_dtype=5121)
expect("normalised 8-bit weights are read (3 bones)",
       agk.skeleton_facts(quantized)["sections"] == [("Skin", 3)], agk.skeleton_facts(quantized))
wide = skinned_gltf(WORK / "six.gltf", [None] + [0] * 5, [("Skin", [0, 1, 2, 3, 4, 5])])
expect("six joints over JOINTS_0 + JOINTS_1 count as six in the section",
       agk.skeleton_facts(wide)["sections"] == [("Skin", 6)], agk.skeleton_facts(wide))
extra = skinned_gltf(WORK / "unweighted_bones.gltf", [None, 0, 0, 1], [("Skin", [0])])
facts = agk.skeleton_facts(extra)
expect("unweighted joints still count as bones of the skeleton (4), not of the section (1)",
       facts["bones"] == 4 and facts["sections"] == [("Skin", 1)], facts)
static = static_gltf(WORK / "static.gltf")
verdict = check(static, max_bones=1, single_root_bone=True, max_bones_per_section=1)
expect("a mesh with no skin passes every skeleton rule and reports no bones",
       verdict.passed and "bones" not in dict(verdict.stats), (verdict.problems, verdict.stats))
squeezed = json.loads(body.read_text())
for primitive in squeezed["meshes"][0]["primitives"]:
    primitive["extensions"] = {"KHR_draco_mesh_compression": {"bufferView": 0, "attributes": {}}}
(WORK / "draco_skin.gltf").write_text(json.dumps(squeezed), encoding="utf-8")
verdict = check(WORK / "draco_skin.gltf", max_bones_per_section=1, max_bones=3)
expect("Draco-compressed weights: no per-section verdict from placeholder data, a warning instead; "
       "max_bones still applies (from the JSON)",
       not has(verdict, "material section") and has(verdict, "4 bones in the skeleton")
       and any("bones per material section not counted" in w for w in verdict.warnings),
       (verdict.problems, verdict.warnings))
(WORK / "draco_skin.gltf").unlink()
broken = json.loads(body.read_text())
broken["skins"][0]["joints"] = [1, 2, 99, 3]
(WORK / "bad_joint.gltf").write_text(json.dumps(broken), encoding="utf-8")
facts = agk.skeleton_facts(WORK / "bad_joint.gltf")
expect("a joint index past the node list is not a bone and does not crash",
       facts["bones"] == 3, facts)

# --------------------------------------------------------------------------- #
print("3. Skeleton: FBX")
facts = agk.skeleton_facts(FIXTURES / "skin4.fbx")
expect("skin4.fbx (Blender): 4 bones, each its own root under the armature object",
       facts["bones"] == 4 and len(facts["roots"]) == 4 and facts["depth"] == 1, facts)
verdict = check(FIXTURES / "skin4.fbx", single_root_bone=True)
expect("... so single_root fails it with Unreal's reason and the fix",
       has(verdict, "skeleton has 4 root bones ('b0', 'b1', 'b2' and 1 more)")
       and has(verdict, "parent every bone under a single root bone"), verdict.problems)
expect("skin6.fbx: 6 bones; max_bones = 6 passes, 5 fails",
       check(FIXTURES / "skin6.fbx", max_bones=6, max_bone_influences=8).passed
       and has(check(FIXTURES / "skin6.fbx", max_bones=5, max_bone_influences=8),
               "6 bones in the skeleton exceeds the 5 bone budget"))
rig = fbx_ascii(WORK / "rig.fbx", STRIP_POLYGONS, STRIP_VERTICES, materials=("Skin", "Cloth"),
                polygon_materials=[0, 1],
                bones=[("root", None), ("spine", 0), ("head", 1), ("tail", 0)],
                clusters=[(0, [0, 1], [1.0, 1.0]), (1, [2, 3], [1e-6, 1e-6]), (2, [4, 5], [1.0, 1.0])])
facts = agk.skeleton_facts(rig)
expect("ASCII FBX rig: 4 LimbNodes, one root, 3 deep (tail is bound to nothing but still a bone)",
       facts["bones"] == 4 and facts["roots"] == ["root"] and facts["depth"] == 3
       and facts["deepest"] == "head", facts)
expect("... per material: Skin is weighted by root, Cloth by root (shared vertex 1) and head; "
       "spine's 1e-6 weights do not count",
       sorted(facts["sections"]) == [("Cloth", 2), ("Skin", 1)], facts["sections"])
verdict = check(rig, max_bones_per_section=1, single_root_bone=True, max_bone_depth=3, max_bones=4)
expect("max_bones_per_section = 1 fails the Cloth section only",
       len(verdict.problems) == 1 and "'Cloth' is skinned to 2 bones" in verdict.problems[0],
       verdict.problems)
expect("an FBX with no skin has no skeleton", agk.skeleton_facts(FIXTURES / "mats5.fbx") is None)

# --------------------------------------------------------------------------- #
print("4. Lightmap overlap: the rasteriser")


def quads(n, lo=0.0, hi=1.0, alternate=False):
    xs = np.linspace(lo, hi, n + 1)
    out = []
    for i in range(n):
        for j in range(n):
            a, b, c, d = (xs[i], xs[j]), (xs[i + 1], xs[j]), (xs[i + 1], xs[j + 1]), (xs[i], xs[j + 1])
            out += [(a, b, d), (b, c, d)] if alternate and (i + j) % 2 else [(a, b, c), (a, c, d)]
    return np.asarray(out, float)


covered, overlapped, res = agk.lightmap_coverage(quads(8), 256)
expect("a clean 8x8 grid covers all 65,536 texels and overlaps none (edges through texel centres)",
       (covered, overlapped, res) == (65536, 0, 256), (covered, overlapped, res))
expect("... with the diagonals alternating as well",
       agk.lightmap_coverage(quads(8, alternate=True), 256)[:2] == (65536, 0))
expect("... and on an odd 7x7 grid whose edges fall between texel centres",
       agk.lightmap_coverage(quads(7), 256)[:2] == (65536, 0))
mixed = quads(8)
mixed[::2] = mixed[::2][:, [0, 2, 1]]
expect("mirrored (clockwise) triangles mixed in change nothing",
       agk.lightmap_coverage(mixed, 256)[:2] == (65536, 0))
centre = np.array([128.5, 128.5]) / 256
angles = np.linspace(0, 2 * np.pi, 13)
fan = np.array([(centre, centre + 0.2 * np.array([np.cos(a), np.sin(a)]),
                 centre + 0.2 * np.array([np.cos(b), np.sin(b)])) for a, b in zip(angles[:-1], angles[1:])])
expect("a 12-triangle fan around a texel centre overlaps nowhere, not even at its hub",
       agk.lightmap_coverage(fan, 256)[1] == 0, agk.lightmap_coverage(fan, 256))
left = quads(1)
left[..., 0] *= 0.5
right = left.copy()
right[..., 0] += 0.5
expect("two islands that touch along an edge do not overlap",
       agk.lightmap_coverage(np.concatenate([left, right]), 256)[:2] == (65536, 0))
shifted = left.copy()
shifted[..., 0] += 0.25
covered, overlapped, _res = agk.lightmap_coverage(np.concatenate([left, shifted]), 256)
expect("two half-squares offset by a quarter overlap on exactly 1/3 of the covered texels",
       (covered, overlapped) == (49152, 16384), (covered, overlapped))
covered, overlapped, _res = agk.lightmap_coverage(np.concatenate([quads(4), quads(4)]), 256)
expect("the same layout twice overlaps everywhere", covered == overlapped == 65536)
expect("zero-area and non-finite triangles cover nothing",
       agk.lightmap_coverage(np.array([[[0, 0], [1, 1], [0.5, 0.5]], [[0, 0], [np.nan, 0], [0, 1]]]), 256)
       == (0, 0, 256))
big = quads(224)
start = time.perf_counter()
result = agk.lightmap_coverage(big, 256)
elapsed = time.perf_counter() - start
expect(f"100,352 triangles at 256x256 in {elapsed:.2f} s (clean: full, no overlap)",
       result == (65536, 0, 256) and elapsed < 10, (result, elapsed))
dense = np.tile(np.array([[[0, 0], [1, 0], [0, 1]]], float), (20000, 1, 1))
start = time.perf_counter()
result = agk.lightmap_coverage(dense, 256)
elapsed = time.perf_counter() - start
expect(f"20,000 triangles each over half the square: the grid shrinks to stay fast "
       f"({result[2]}x{result[2]}, {elapsed:.2f} s)", result[2] < 256 and result[0] == result[1]
       and elapsed < 10, (result, elapsed))

# --------------------------------------------------------------------------- #
print("5. Lightmap UVs: glTF")
clean = static_gltf(WORK / "clean.gltf")
verdict = check(clean, lightmap_require=True, lightmap_inside_unit_square=True, lightmap_max_overlap=0.0)
expect("a grid lightmap inside 0-1 with no overlap passes require, inside and max_overlap = 0",
       verdict.passed, verdict.problems)
stats = dict(verdict.stats)
expect("... reporting its texels and zero overlap in stats",
       stats.get("lightmap_texels") == 65536 and stats.get("lightmap_overlap_texels") == 0
       and stats.get("lightmap_uvs_outside") == 0, verdict.stats)
missing = static_gltf(WORK / "no_lightmap.gltf", lightmap=False)
verdict = check(missing, lightmap_require=True)
expect("require fails a static mesh without TEXCOORD_1, naming it and the fix",
       has(verdict, "no lightmap UV set (UV channel 1, the second set) on static mesh 'Wall'")
       and has(verdict, "add a UV set unwrapped for lightmaps"), verdict.problems)
expect("... and require off lets it pass", check(missing, lightmap_inside_unit_square=True).passed)
expect("uv_channel = 0 reads TEXCOORD_0 instead",
       check(missing, lightmap_require=True, lightmap_uv_channel=0, lightmap_max_overlap=0.0).passed)
_positions, uv, _tris = grid(4)
outside = static_gltf(WORK / "outside.gltf", lightmap=uv + [0.5, 0.0])
verdict = check(outside, lightmap_inside_unit_square=True)
expect("a lightmap shifted past u = 1 fails inside_unit_square with a count and the worst UV",
       has(verdict, "10 of 25 lightmap UVs (UV channel 1, the second set) are outside 0-1 "
                    "(worst at u 1.500, v 0.000)")
       and has(verdict, "move every lightmap island inside the 0-1 square"), verdict.problems)
expect("... and passes with inside_unit_square off", check(outside, lightmap_max_overlap=50.0).passed)
noise = static_gltf(WORK / "noise.gltf", lightmap=np.clip(uv * 1.00001, 0, 1.00001))
expect("float noise (1.00001) is inside the tolerance", check(noise, lightmap_inside_unit_square=True).passed,
       check(noise, lightmap_inside_unit_square=True).problems)
positions, tiled, tris = per_quad_uvs(4)
builder = GltfBuilder()
primitive = {"attributes": {"POSITION": builder.add(positions, kind="VEC3"),
                            "TEXCOORD_0": builder.add(tiled), "TEXCOORD_1": builder.add(tiled)},
             "indices": builder.add(tris.ravel(), component=5125, kind="SCALAR")}
overlapping = builder.write(WORK / "overlapping.gltf", [{"name": "Crate", "primitives": [primitive]}],
                            [{"mesh": 0}])
verdict = check(overlapping, lightmap_max_overlap=0.0)
expect("16 quads each mapped to the whole square fail max_overlap = 0 at 100%",
       has(verdict, "lightmap UVs (UV channel 1, the second set) overlap on 100% of their texels "
                    "in 'Crate' (65,536 of 65,536 on a 256x256 grid), over the 0% limit")
       and has(verdict, "repack the lightmap UVs"), verdict.problems)
expect("... with the overlap in its metric", "lightmap 100% overlap" in verdict.metric, verdict.metric)
half = np.concatenate([uv * [0.5, 1.0], uv * [0.5, 1.0] + [0.25, 0.0]])
positions, _uv, tris = grid(4)
builder = GltfBuilder()
primitive = {"attributes": {"POSITION": builder.add(np.concatenate([positions, positions + [2, 0, 0]]), kind="VEC3"),
                            "TEXCOORD_1": builder.add(half)},
             "indices": builder.add(np.concatenate([tris, tris + len(positions)]).ravel(),
                                    component=5125, kind="SCALAR")}
partial = builder.write(WORK / "partial.gltf", [{"name": "Pair", "primitives": [primitive]}], [{"mesh": 0}])
expect("a 33% overlap passes max_overlap = 40 and fails max_overlap = 30",
       check(partial, lightmap_max_overlap=40.0).passed
       and has(check(partial, lightmap_max_overlap=30.0), "overlap on 33% of their texels"),
       check(partial, lightmap_max_overlap=30.0).problems)
expect("... and the grid follows resolution (64x64)",
       has(check(partial, lightmap_max_overlap=30.0, lightmap_resolution=64), "on a 64x64 grid"))
collapsed = static_gltf(WORK / "collapsed.gltf", lightmap=np.zeros((25, 2)))
expect("a collapsed lightmap (every UV at 0,0) fails the overlap check instead of passing it",
       has(check(collapsed, lightmap_max_overlap=0.0), "cover no texel of a 256x256 grid"))
packed = static_gltf(WORK / "quantized.gltf", quantized=True)
expect("a quantized (normalised 16-bit) TEXCOORD_1 reads as 0-1",
       check(packed, lightmap_inside_unit_square=True, lightmap_max_overlap=0.0).passed,
       check(packed, lightmap_inside_unit_square=True, lightmap_max_overlap=0.0).problems)
strip = static_gltf(WORK / "strip.gltf", mode=5)
facts = agk.lightmap_facts(strip, 1, 256)
expect("a triangle strip is rasterised (one row of the grid: a quarter of the texels)",
       facts["overlap"] and facts["overlap"][2] == 65536 // 4 and facts["overlap"][1] == 0, facts)
skinned = skinned_gltf(WORK / "skinned_and_prop.gltf", chain, [("Skin", [0, 1])], static_also=True)
verdict = check(skinned, lightmap_require=True)
expect("require skips the skinned mesh and names only the static one",
       has(verdict, "on static mesh 'Prop'") and not has(verdict, "'Body'"), verdict.problems)
expect("... and a fully skinned asset is never asked for a lightmap",
       check(body, lightmap_require=True, lightmap_max_overlap=0.0).passed)
draco = json.loads(clean.read_text())
draco["meshes"][0]["primitives"][0]["extensions"] = {"KHR_draco_mesh_compression": {"bufferView": 0}}
draco["extensionsUsed"] = ["KHR_draco_mesh_compression"]
(WORK / "draco.gltf").write_text(json.dumps(draco), encoding="utf-8")
with patch.object(agk, "_dracopy_available", lambda: True):
    verdict = check(WORK / "draco.gltf", lightmap_max_overlap=0.0)
expect("a Draco primitive is not rasterised from placeholder data: a warning says so",
       verdict.passed and any("lightmap UVs not fully checked" in w for w in verdict.warnings),
       (verdict.problems, verdict.warnings))
(WORK / "draco.gltf").unlink()          # a fake Draco file: no use to section 7

# --------------------------------------------------------------------------- #
print("6. Lightmap UVs: FBX and OBJ")
verdict = check(FIXTURES / "uv3.fbx", lightmap_require=True, lightmap_max_overlap=0.0)
expect("uv3.fbx (Blender, 3 UV sets) has a clean second set: passes",
       verdict.passed and dict(verdict.stats).get("lightmap_texels") == 65536,
       (verdict.problems, verdict.stats))
verdict = check(FIXTURES / "mats5.fbx", lightmap_require=True, max_material_slots=8)
expect("mats5.fbx (one UV set) fails require, naming its mesh",
       has(verdict, "no lightmap UV set (UV channel 1, the second set) on static mesh 'Cube'"),
       verdict.problems)
corners = [(0, 0), (0.5, 0), (0.5, 1), (0, 1), (0.5, 0), (1, 0), (1, 1), (0.5, 1)]
clean_fbx = fbx_ascii(WORK / "clean.fbx", STRIP_POLYGONS, STRIP_VERTICES, uv_sets=(corners, corners))
expect("an ASCII FBX whose two quads sit side by side in the second set passes",
       check(clean_fbx, lightmap_require=True, lightmap_max_overlap=0.0).passed,
       check(clean_fbx, lightmap_require=True, lightmap_max_overlap=0.0).problems)
stacked = [(0, 0), (1, 0), (1, 1), (0, 1)] * 2
stacked_fbx = fbx_ascii(WORK / "stacked.fbx", STRIP_POLYGONS, STRIP_VERTICES, uv_sets=(corners, stacked))
verdict = check(stacked_fbx, lightmap_max_overlap=0.0)
expect("... and one whose quads are stacked in the second set fails at 100% (the first set is not read)",
       has(verdict, "overlap on 100% of their texels in 'Thing'"), verdict.problems)
wide_fbx = fbx_ascii(WORK / "wide.fbx", STRIP_POLYGONS, STRIP_VERTICES,
                     uv_sets=(corners, [(u * 2, v) for u, v in corners]))
verdict = check(wide_fbx, lightmap_inside_unit_square=True)
expect("an FBX lightmap reaching u = 2 fails inside_unit_square",
       has(verdict, "lightmap UVs (UV channel 1, the second set) are outside 0-1 (worst at u 2.000"),
       verdict.problems)
skin_fbx = check(rig, lightmap_require=True)
expect("a skinned FBX is not asked for a lightmap", skin_fbx.passed, skin_fbx.problems)
obj = WORK / "crate.obj"
obj.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\n"
               "f 1/1 2/2 3/3\nf 1/1 3/3 4/4\n", encoding="utf-8")
verdict = check(obj, lightmap_require=True)
expect("an OBJ (one UV set) cannot hold UV channel 1: require says so and how to fix it",
       has(verdict, "an OBJ file holds one UV set") and has(verdict, "uv_channel = 0"), verdict.problems)
verdict = check(obj, lightmap_require=True, lightmap_uv_channel=0, lightmap_max_overlap=0.0)
expect("... and with uv_channel = 0 its single set is checked like any other",
       verdict.passed and dict(verdict.stats).get("lightmap_texels") == 65536,
       (verdict.problems, verdict.stats))
expect("require off: an OBJ is not failed for its single set",
       check(obj, lightmap_inside_unit_square=True).passed)

# --------------------------------------------------------------------------- #
print("7. Off by default: no skeleton or lightmap is read, no verdict changes")


def refuse(*_args, **_kwargs):
    raise AssertionError("read although every [skeleton]/[lightmap] rule is off")


assets = sorted(FIXTURES.glob("*.fbx")) + sorted(WORK.glob("*.gltf")) + sorted(WORK.glob("*.fbx")) + [obj]
before = {path.name: agk.Gatekeeper(agk.Budget()).check(path) for path in assets}
with patch.object(agk, "skeleton_facts", refuse), patch.object(agk, "lightmap_facts", refuse):
    after = {path.name: agk.Gatekeeper(agk.Budget()).check(path) for path in assets}
expect(f"the default budget never reads a skeleton or a lightmap ({len(assets)} assets)",
       all(not verdict.unreadable for verdict in after.values()),
       [name for name, verdict in after.items() if verdict.unreadable])
expect("... and every default verdict is free of skeleton and lightmap wording",
       not any("bone" in problem.lower() and "influence" not in problem.lower()
               or "lightmap" in problem.lower()
               for verdict in after.values() for problem in verdict.problems),
       [verdict.problems for verdict in after.values()])
expect("... with no new stats",
       not any(key in dict(verdict.stats) for verdict in after.values()
               for key in ("bones", "lightmap_texels", "lightmap_missing", "lightmap_uvs_outside")))
expect("... identical to the verdicts of the same run without the readers patched out",
       all(before[name].tag == after[name].tag for name in before))

print()
print("FAILURES:", failures if failures else "none")
