"""Maya pre-flight window.

Run from the script editor:

    import sys; sys.path.append(r"<studio>/AssetGatekeeper/plugins/maya")
    import maya_preflight as p; p.show()

Or drop it on a shelf button. It counts faces with ``polyEvaluate`` (the same
number the Attribute Editor shows), materials from the shading engines actually
assigned, and texture sizes from the ``file`` nodes feeding them - no export.
"""
import os
import sys

import maya.cmds as cmds

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

WINDOW = "agkPreflightWindow"


def _ls(items, **flags):
    """cmds.ls of a list that may be empty (cmds.ls([]) is not reliably [])."""
    return (cmds.ls(items, **flags) or []) if items else []


def _shapes_under(node):
    """Mesh shapes at and below *node*, intermediate ("Orig") shapes left out."""
    return _ls(cmds.listRelatives(node, allDescendents=True, type="mesh", fullPath=True) or [],
               long=True, noIntermediate=True)


def _mesh_transforms(selection):
    shapes = []
    for node in selection:
        if cmds.nodeType(node) == "mesh":
            shapes.append(node)
            continue
        shapes.extend(cmds.listRelatives(node, shapes=True, type="mesh") or [])
        if cmds.nodeType(node) == "lodGroup":
            # A selected LOD group stands for its levels (and their collision).
            shapes.extend(_shapes_under(node))
    # A skinned mesh keeps its unskinned "Orig" shape as an intermediate object;
    # it is not exported and must not be counted twice.
    return list(dict.fromkeys(_ls(shapes, long=True, noIntermediate=True)))


def _short(node):
    """'|grp|ns:Rock' -> 'ns:Rock' (the gate drops the namespace itself)."""
    return str(node).rsplit("|", 1)[-1]


def _transform(shape):
    return (cmds.listRelatives(shape, parent=True, fullPath=True) or [shape])[0]


def _is_collision(shape):
    return core.is_collision_name(_short(_transform(shape)))


def _lod_levels(shapes):
    """{lodGroup: [level transform, ...]} for the LOD groups above *shapes*.

    Children of a lodGroup in outliner order are LOD0, LOD1, ...; a collision
    mesh parented to the group (Unreal's own export does that) is not a level.
    """
    groups = {}
    for shape in shapes:
        parts = str(shape).split("|")
        for depth in range(2, len(parts) - 1):
            ancestor = "|".join(parts[:depth])
            if ancestor in groups or cmds.nodeType(ancestor) != "lodGroup":
                continue
            children = cmds.listRelatives(ancestor, children=True, type="transform",
                                          fullPath=True) or []
            groups[ancestor] = [child for child in children
                                if not core.is_collision_name(_short(child))]
    return groups


def _engines_used(shape):
    """Shading groups holding this shape or its faces; an empty but still
    connected initialShadingGroup (left behind by per-face assignment) is
    not a material. Verified in Maya 2027: 5 face materials read as 6."""
    own = set(cmds.ls(shape, long=True) or [])
    own.update(cmds.listRelatives(shape, parent=True, fullPath=True) or [])
    used = []
    for engine in set(cmds.listConnections(shape, type="shadingEngine") or []):
        members = cmds.sets(engine, query=True) or []
        if members and own.intersection(cmds.ls(members, objectsOnly=True, long=True) or []):
            used.append(engine)
    return used


def _render_shapes(shapes):
    """The shapes the gate counts: not collision, not below LOD0 of an LOD group."""
    lower = set()
    for levels in _lod_levels(shapes).values():
        for level in levels[1:]:
            lower.update(_shapes_under(level))
    return [shape for shape in shapes if shape not in lower and not _is_collision(shape)]


def _selection():
    return cmds.ls(selection=True) or cmds.ls(type="mesh")


def scene_facts(selection=None):
    selection = selection or _selection()
    # Collision meshes and LOD1+ are not part of the triangle or material budget
    # in the gate, so they are not counted here either.
    shapes = _render_shapes(_mesh_transforms(selection))
    triangles = vertices = degenerate = 0
    materials = set()
    textures = []
    for shape in shapes:
        faces = cmds.polyEvaluate(shape, face=True) or 0
        triangles += cmds.polyEvaluate(shape, triangle=True) or faces * 2
        vertices += cmds.polyEvaluate(shape, vertex=True) or 0
        for engine in _engines_used(shape):
            for material in cmds.ls(cmds.listConnections(engine + ".surfaceShader"),
                                    materials=True) or []:
                materials.add(material)
                for history in cmds.listHistory(material) or []:
                    if cmds.nodeType(history) != "file":
                        continue
                    texture = cmds.getAttr(history + ".fileTextureName") or ""
                    width = cmds.getAttr(history + ".outSizeX") or 0
                    height = cmds.getAttr(history + ".outSizeY") or 0
                    textures.append((os.path.basename(texture) or history, int(width),
                                     int(height)))
    return triangles, vertices, len(materials), textures, degenerate


# --------------------------------------------------------------------------- #
# [lod], [collision], [skeleton], [lightmap]: read only while a rule is on
# --------------------------------------------------------------------------- #

def _mesh_fn(shape):
    """An OpenMaya 2 MFnMesh for *shape*, or None (no API 2 in this Maya)."""
    try:
        import maya.api.OpenMaya as om
    except ImportError:                                # pragma: no cover
        return None
    selection = om.MSelectionList()
    selection.add(shape)
    return om.MFnMesh(selection.getDagPath(0))


def _hull(shape):
    """(points, fan triangles) of a collision mesh, or (vertex count, None)."""
    fn = _mesh_fn(shape)
    if fn is None:
        return cmds.polyEvaluate(shape, vertex=True) or 0, None
    counts, corners = fn.getVertices()
    return ([(point.x, point.y, point.z) for point in fn.getPoints()],
            core.fan_triangles(list(counts), list(corners)))


def _collision_shapes(shapes, render):
    """UCX_/UBX_/USP_/UCP_ meshes selected, or next to a render mesh (same parent).

    Next to it, because an artist selects the mesh, not its hull; a misnamed
    hull beside it must still be reported."""
    found = [shape for shape in shapes if _is_collision(shape)]
    for parent in {(cmds.listRelatives(_transform(shape), parent=True, fullPath=True)
                    or [None])[0] for shape in render}:
        siblings = (cmds.listRelatives(parent, children=True, type="transform", fullPath=True)
                    if parent else cmds.ls(assemblies=True, long=True)) or []
        for sibling in siblings:
            if core.is_collision_name(_short(sibling)):
                found.extend(_shapes_under(sibling))
    return list(dict.fromkeys(found))


def _skin_clusters(shape):
    return _ls(cmds.listHistory(shape) or [], type="skinCluster")


def _skeleton(shapes):
    """Joints of the skinClusters on *shapes*, connected as the FBX export writes them.

    Bones per material section need every vertex weight (MFnSkinCluster); that is
    left to the gate, which reads them from the export."""
    bound = []
    for shape in shapes:
        for cluster in _skin_clusters(shape):
            bound.extend(_ls(cmds.skinCluster(cluster, query=True, influence=True) or [],
                             long=True))

    def parent_of(node):
        return (cmds.listRelatives(node, parent=True, fullPath=True) or [None])[0]

    def children_of(node):
        return cmds.listRelatives(node, children=True, fullPath=True) or []

    return {"bones": core.skeleton_from_hierarchy(
        bound, parent_of, children_of, lambda node: cmds.nodeType(node) == "joint",
        label=_short), "sections": None}


def _lightmap(shapes, channel):
    """(mesh, per-corner UVs of set *channel* or None, fan UV triangles) per static mesh.

    Maya exports UV sets in polyUVSet order, so set *channel* is the FBX UV
    layer of that index. UVs per face corner, as the FBX export writes them."""
    found = []
    for shape in shapes:
        if _skin_clusters(shape):
            continue                                   # skinned: not lightmapped
        name = _short(_transform(shape))
        sets = cmds.polyUVSet(shape, query=True, allUVSets=True) or []
        if channel >= len(sets):
            found.append((name, None, None))
            continue
        fn = _mesh_fn(shape)
        if fn is None:
            found.append((name, True, None))           # there, values not read
            continue
        us, vs = fn.getUVs(sets[channel])
        counts, ids = fn.getAssignedUVs(sets[channel])
        corners = [(us[index], vs[index]) for index in ids]
        triangles = core.fan_triangles(list(counts), corners)
        found.append((name, corners, triangles))
    return found


def structure_facts(budget, selection=None):
    """assess() keywords for [lod], [collision], [skeleton] and [lightmap].

    Nothing is read for a rule that is off. A host API that differs from the
    one this was written against leaves that rule to the gate (an info note
    from assess()) instead of breaking the panel."""
    on = core.rules_on(budget) if budget is not None else {}
    facts = {}
    if not any(on.values()):
        return facts
    shapes = _mesh_transforms(selection or _selection())
    render = _render_shapes(shapes)
    facts["render_names"] = sorted({_short(_transform(shape)) for shape in render})

    def triangles(items):
        return sum(cmds.polyEvaluate(shape, triangle=True) or 0 for shape in items)

    if on.get("lod"):
        try:
            groups = _lod_levels(shapes)
            facts["lods"] = ([(_short(group), tuple(
                triangles([shape for shape in _shapes_under(level) if not _is_collision(shape)])
                for level in levels), "group") for group, levels in groups.items() if levels]
                if groups else core.lods_by_name(
                    (_short(_transform(shape)), triangles([shape])) for shape in render))
        except Exception:
            pass
    if on.get("collision"):
        try:
            facts["collision"] = [(_short(_transform(shape)), *_hull(shape))
                                  for shape in _collision_shapes(shapes, render)]
            facts["skinned"] = any(_skin_clusters(shape) for shape in render)
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
    triangles, vertices, materials, textures, degenerate = scene_facts()
    budget = None
    problem = ""
    try:
        budget = core.load_budget(rules_path, preset=preset)
    except Exception as exc:
        problem = str(exc)
    scene_file = cmds.file(query=True, sceneName=True) or ""
    report = core.assess(asset=os.path.basename(scene_file) or "untitled",
                         triangles=triangles, vertices=vertices, materials=materials,
                         textures=textures, degenerate=degenerate,
                         folder=os.path.basename(os.path.dirname(scene_file)),
                         budget=budget, **structure_facts(budget))
    if problem:
        report.add("info", "no_rules", "budget unavailable: " + problem)
    return report


def show():
    report = run_preflight()
    if cmds.window(WINDOW, exists=True):
        cmds.deleteUI(WINDOW)
    cmds.window(WINDOW, title="Asset Gatekeeper", widthHeight=(520, 340))
    cmds.columnLayout(adjustableColumn=True, rowSpacing=4)
    cmds.text(label=report.headline(), align="left", font="boldLabelFont")
    if report.triangles is not None:
        cmds.text(label=f"{report.triangles:,} tris   "
                        f"{report.materials or 0} materials   "
                        f"{report.budget_summary}",
                  align="left", font="smallObliqueLabelFont")
    cmds.separator(height=6)
    cmds.scrollField(editable=False, wordWrap=True, height=240,
                     text=report.as_text())
    cmds.button(label="Re-run", height=28,
                command=lambda *_: (cmds.deleteUI(WINDOW), show()))
    cmds.showWindow(WINDOW)
    return report


if __name__ == "__main__":
    show()
