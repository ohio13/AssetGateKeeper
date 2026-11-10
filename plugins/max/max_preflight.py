"""Autodesk 3ds Max pre-flight rollup.

Run from the MAXScript listener:

    python.ExecuteFile @"<studio>\\AssetGatekeeper\\plugins\\max\\max_preflight.py"

or paste the one-liner in the module docstring of plugins/README.md. Press "Run
pre-flight" and the rollup reports the same verdict the gate will report at
submit, computed from the editable scene.
"""
import os
import sys

from pymxs import runtime as rt

_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (_HERE, os.path.dirname(_HERE),
                   os.path.dirname(os.path.dirname(_HERE))):
    if os.path.isdir(os.path.join(_candidate, "plugins")) or \
            os.path.isfile(os.path.join(_candidate, "asset_gatekeeper.py")):
        if _candidate not in sys.path:
            sys.path.insert(0, _candidate)
        break

try:
    from plugins import preflight_core as core
except ImportError:                                   # pragma: no cover
    import preflight_core as core                     # type: ignore[no-redef]


def _not_render_geometry():
    """Geometry-class helpers that are not meshes an engine receives."""
    names = ("Targetobject", "BoneGeometry", "Biped_Object", "CATBone", "HubObject")
    return tuple(cls for cls in (getattr(rt, name, None) for name in names) if cls is not None)


def _selected_geometry():
    """Every geometry node, whatever its class.

    This used to keep only Editable_Poly / Editable_Mesh / PolyMeshObject, so an
    unconverted primitive (a Sphere, a Box with no modifiers - classOf is Sphere)
    was skipped and read as 0 triangles. getPolygonCount works on any geometry.
    """
    selection = list(rt.selection) if rt.selection.count else list(rt.objects)
    skip = _not_render_geometry()
    return [node for node in selection
            if rt.superClassOf(node) == rt.GeometryClass and rt.classOf(node) not in skip]


#: Texture slots by material type: Standard (legacy) and Physical (the default
#: material since 3ds Max 2017). Other types still count as a material.
_MAP_SLOTS = ("diffuseMap", "bumpMap", "specularMap", "base_color_map", "bump_map",
              "roughness_map", "metalness_map", "norm_map")


def _leaf_materials(material):
    """A Multimaterial's sub-materials, or the material itself."""
    if material is None:
        return []
    if rt.classOf(material) == rt.Multimaterial:
        return [slot for slot in material.materialList if slot is not None]
    return [material]


def scene_facts():
    triangles = vertices = degenerate = 0
    materials = set()
    textures = []
    names = []
    for node in _selected_geometry():
        if core.is_collision_name(str(node.name)):
            continue            # UCX_/UBX_/USP_/UCP_: never in the gate's budget either
        names.append(str(node.name))
        triangles += int(rt.getPolygonCount(node)[0] or 0)
        vertices += int(rt.getPolygonCount(node)[1] or 0)
        # Any material type counts: only Standard/Multi used to, so Physical
        # Material (the default since Max 2017) and Arnold read as 0 materials.
        for slot in _leaf_materials(node.material):
            materials.add(str(slot.name))
            for map_slot in _MAP_SLOTS:
                try:
                    bitmap = getattr(slot, map_slot, None)
                except Exception:
                    bitmap = None
                if bitmap is not None and rt.classOf(bitmap) == rt.Bitmaptexture:
                    filename = str(bitmap.filename or "")
                    size = bitmap.bitmap
                    width = int(size.width) if size is not None else 0
                    height = int(size.height) if size is not None else 0
                    textures.append((os.path.basename(filename) or str(bitmap.name),
                                     width, height))
    return triangles, vertices, len(materials), textures, degenerate, names


# --------------------------------------------------------------------------- #
# [lod], [collision], [skeleton], [lightmap]: read only while a rule is on
# --------------------------------------------------------------------------- #

#: Above this many faces a mesh's lightmap UVs are not read value by value (one
#: pymxs call per vertex); the gate reads them from the export instead.
MAX_FACES_FOR_UV_READ = 20_000
#: A collision hull with more vertices than this is counted, not read.
MAX_HULL_VERTS_READ = 5_000
_BONE_CLASSES = ("BoneGeometry", "Biped_Object", "CATBone", "HubObject")


def _handle(node):
    """A node's handle: pymxs wrappers are not reliably hashable, handles are."""
    return int(node.inode.handle)


def _node(handle):
    return rt.maxOps.getNodeByHandle(handle)


def _snapshot(node):
    """The node as a world-space TriMesh (what the FBX exporter triangulates)."""
    return rt.snapshotAsMesh(node)


def _release(mesh):
    try:
        rt.delete(mesh)                    # a snapshot TriMesh is freed explicitly
    except Exception:
        pass


def _hull(node):
    """(points, triangles) of a collision mesh, or (vertex count, None)."""
    mesh = _snapshot(node)
    try:
        count = int(rt.getNumVerts(mesh))
        if count > MAX_HULL_VERTS_READ:
            return count, None
        points = []
        for index in range(1, count + 1):
            point = rt.getVert(mesh, index)
            points.append((float(point.x), float(point.y), float(point.z)))
        triangles = []
        for index in range(1, int(rt.getNumFaces(mesh)) + 1):
            face = rt.getFace(mesh, index)           # 1-based vertex indices
            triangles.append((int(face.x) - 1, int(face.y) - 1, int(face.z) - 1))
        return points, triangles
    finally:
        _release(mesh)


def _neighbours(node):
    parent = node.parent
    return list(parent.children) if parent is not None else list(rt.rootNode.children)


def _collision_nodes(nodes, render):
    """UCX_/UBX_/USP_/UCP_ nodes selected, or next to a render mesh (same parent)."""
    found = {}
    for node in list(nodes) + [item for mesh in render for item in _neighbours(mesh)]:
        if core.is_collision_name(str(node.name)):
            found.setdefault(_handle(node), node)
    return list(found.values())


def _skins(node):
    skin = getattr(rt, "Skin", None)
    if skin is None:
        return []
    return [modifier for modifier in node.modifiers if rt.classOf(modifier) == skin]


def _skin_bones(modifier):
    """The bones a Skin modifier binds. skinOps.GetBoneNodes where this Max has it
    (capability probe: not in every version); else the bone names, looked up.
    Unverified in Max: skinOps calls in older versions wanted the modifier shown
    in the Modify panel; a failure here leaves [skeleton] to the gate."""
    ops = rt.skinOps
    try:
        getter = getattr(ops, "GetBoneNodes", None)
    except Exception:
        getter = None
    if callable(getter):
        return [bone for bone in getter(modifier) if bone is not None]
    names = [str(ops.GetBoneName(modifier, index, 0))
             for index in range(1, int(ops.GetNumberBones(modifier)) + 1)]
    return [bone for bone in (rt.getNodeByName(name) for name in names) if bone is not None]


def _skeleton(render):
    bound = [_handle(bone) for node in render for modifier in _skins(node)
             for bone in _skin_bones(modifier)]
    classes = tuple(cls for cls in (getattr(rt, name, None) for name in _BONE_CLASSES)
                    if cls is not None)

    def parent_of(handle):
        parent = _node(handle).parent
        return _handle(parent) if parent is not None else None

    def children_of(handle):
        return [_handle(child) for child in _node(handle).children]

    return {"bones": core.skeleton_from_hierarchy(
        bound, parent_of, children_of, lambda handle: rt.classOf(_node(handle)) in classes,
        label=lambda handle: str(_node(handle).name)), "sections": None}


def _lightmap(render, channel):
    """(mesh, UVs or None, UV triangles) per static mesh.

    The FBX exporter writes the map channels a mesh has, from channel 1 up, as
    UV sets 0, 1, ...: UV set *channel* is the (channel + 1)-th supported map
    channel (unverified in Max; the gate reads the exported file). The UVs are
    the map vertices the faces use; the gate counts the exported face corners,
    so "N of M lightmap UVs" can count differently, never with another verdict."""
    found = []
    for node in render:
        if _skins(node):
            continue                                   # skinned: not lightmapped
        mesh = _snapshot(node)
        try:
            supported = [index for index in range(1, int(rt.meshop.getNumMaps(mesh)))
                         if rt.meshop.getMapSupport(mesh, index)]
            if channel >= len(supported):
                found.append((str(node.name), None, None))
                continue
            faces = int(rt.getNumFaces(mesh))
            if faces > MAX_FACES_FOR_UV_READ:
                found.append((str(node.name), True, None))
                continue
            map_channel = supported[channel]
            verts = []
            for index in range(1, int(rt.meshop.getNumMapVerts(mesh, map_channel)) + 1):
                uv = rt.meshop.getMapVert(mesh, map_channel, index)
                verts.append((float(uv.x), float(uv.y)))
            used, triangles = set(), []
            for index in range(1, faces + 1):
                face = rt.meshop.getMapFace(mesh, map_channel, index)
                corners = (int(face.x) - 1, int(face.y) - 1, int(face.z) - 1)
                used.update(corners)
                triangles.append([verts[corner] for corner in corners])
            found.append((str(node.name), [verts[corner] for corner in sorted(used)], triangles))
        finally:
            _release(mesh)
    return found


def structure_facts(budget):
    """assess() keywords for [lod], [collision], [skeleton] and [lightmap].

    Nothing is read for a rule that is off. 3ds Max has no LOD group the FBX
    exporter is known to write from Python here, so LODs are read from
    <name>_LOD<n> names. An API difference leaves the rule to the gate (an info
    note from assess()) instead of breaking the check."""
    on = core.rules_on(budget) if budget is not None else {}
    facts = {}
    if not any(on.values()):
        return facts
    nodes = _selected_geometry()
    render = [node for node in nodes if not core.is_collision_name(str(node.name))]
    facts["render_names"] = sorted({str(node.name) for node in render})
    if on.get("lod"):
        try:
            facts["lods"] = core.lods_by_name((str(node.name), int(rt.getPolygonCount(node)[0] or 0))
                                              for node in render)
        except Exception:
            pass
    if on.get("collision"):
        try:
            facts["collision"] = [(str(node.name), *_hull(node))
                                  for node in _collision_nodes(nodes, render)]
            facts["skinned"] = any(_skins(node) for node in render)
        except Exception:
            facts.pop("collision", None)
    if on.get("skeleton"):
        try:
            facts["skeleton"] = _skeleton(render)
        except Exception:
            pass
    if on.get("lightmap"):
        try:
            facts["lightmap"] = _lightmap(render, int(budget.lightmap_uv_channel))
        except Exception:
            pass
    return facts


def run_preflight(rules_path=None, preset=None):
    triangles, vertices, materials, textures, degenerate, names = scene_facts()
    budget = None
    problem = ""
    try:
        budget = core.load_budget(rules_path, preset=preset)
    except Exception as exc:
        problem = str(exc)
    scene_file = str(rt.maxFilePath or "")
    report = core.assess(asset=os.path.basename(str(rt.maxFileName or "")) or "untitled",
                         triangles=triangles, vertices=vertices, materials=materials,
                         textures=textures, degenerate=degenerate,
                         folder=os.path.basename(scene_file), budget=budget,
                         mesh_names=names, **structure_facts(budget))
    if problem:
        report.add("info", "no_rules", "budget unavailable: " + problem)
    return report


def _qt_box(text, blocked):
    """A Qt message box with a warning icon when blocked. None if Qt is unavailable."""
    try:
        from PySide6 import QtWidgets          # 3ds Max 2025+
    except ImportError:
        try:
            from PySide2 import QtWidgets      # 3ds Max 2021-2024
        except ImportError:
            return None
    try:
        from qtmax import GetQMaxMainWindow
        parent = GetQMaxMainWindow()
    except Exception:
        parent = None
    box = QtWidgets.QMessageBox(parent)
    box.setWindowTitle("Asset Gatekeeper")
    box.setIcon(QtWidgets.QMessageBox.Warning if blocked else QtWidgets.QMessageBox.Information)
    box.setText(text)
    box.exec() if hasattr(box, "exec") else box.exec_()
    return True


def show():
    """Show the report: a warning icon when it is blocked, information when clean.

    MAXScript's messageBox always shows the blue (i), so a BLOCKED result looked
    like a notice; Qt is used where available, messageBox otherwise."""
    report = run_preflight()
    text = report.as_text()
    if _qt_box(text, bool(report.errors)) is None:
        rt.messageBox(text, title="Asset Gatekeeper")
    return report


def install_button():
    """Add an "AGK Pre-flight" action (category AssetGatekeeper) that runs this file.

    Run once: python.ExecuteFile @"<...>\\plugins\\max\\install_max_button.py".
    Max keeps the action across restarts; drag it onto a toolbar or menu from
    Customize > Customize User Interface (category AssetGatekeeper).
    """
    script = os.path.abspath(__file__).replace("\\", "/")
    rt.execute('macroScript AGK_Preflight category:"AssetGatekeeper" '
               'buttonText:"AGK" tooltip:"AssetGatekeeper pre-flight check" '
               '( python.ExecuteFile @"' + script + '" )')
    return True


if __name__ == "__main__":
    show()
