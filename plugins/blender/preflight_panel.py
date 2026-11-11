"""Blender pre-flight panel.

Install: Edit > Preferences > Add-ons > Install... > this file, tick
"AssetGatekeeper Pre-flight". It appears in the 3D Viewport sidebar (N panel) as
"Asset Gatekeeper". Set ``ASSETGATEKEEPER_HOME`` to the AssetGatekeeper folder:
an installed add-on is copied into Blender's add-ons folder, away from the
engine it needs.

It reads the *scene*, not an export: polygon and material counts come from the
evaluated depsgraph, so a modifier stack, a subdivision surface or a geo-nodes
setup is measured as the artist will see it. Nothing is written to disk until the
artist exports, which is the point - the warning arrives before the export, not
after the submit.
"""
import os
import sys
import textwrap

import bpy

# Without bl_info Blender copies the file on "Install..." and then never lists
# it, so there is nothing to tick (verified in Blender 4.0.2).
bl_info = {
    "name": "AssetGatekeeper Pre-flight",
    "author": "AssetGatekeeper contributors",
    "version": (0, 1, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Asset Gatekeeper",
    "description": "Live triangle/material/texture budget for the selection, before export",
    "category": "Pipeline",
}

ENV_HOME = "ASSETGATEKEEPER_HOME"

# The repository (running from a checkout) or ASSETGATEKEEPER_HOME (installed:
# Blender copies the add-on into its own folder, where the engine is not). The
# installed copy used to fail on enable with "No module named 'preflight_core'".
_HERE = os.path.dirname(os.path.abspath(__file__))
_candidates = [_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))]
if os.environ.get(ENV_HOME):
    _candidates.insert(0, os.environ[ENV_HOME])
for _candidate in _candidates:
    if os.path.isdir(os.path.join(_candidate, "plugins")) or \
            os.path.isfile(os.path.join(_candidate, "asset_gatekeeper.py")):
        if _candidate not in sys.path:
            sys.path.insert(0, _candidate)
        break

core = None
_IMPORT_PROBLEM = ""
try:
    from plugins import preflight_core as core
except ImportError:                                   # pragma: no cover - addon path
    try:
        import preflight_core as core                 # type: ignore[no-redef]
    except ImportError as _exc:
        _IMPORT_PROBLEM = (f"AssetGatekeeper not found ({_exc}). Set {ENV_HOME} to the "
                           "AssetGatekeeper folder and restart Blender.")


def scene_facts():
    """(triangles, vertices, materials, textures, degenerate) for selected objects.

    ``materials`` counts *distinct* materials, not slots: three props sharing one
    material is one draw call, and the engine counts distinct materials in the
    exported file. Summing slots made this panel disagree with ``preflight_panel``
    and with the gate, on the same scene, by a factor of the object count.
    """
    triangles = vertices = degenerate = 0
    material_names = set()
    textures = []
    seen = set()
    context = bpy.context
    depsgraph = context.evaluated_depsgraph_get()
    targets = context.selected_objects or [o for o in context.scene.objects
                                           if o.type == "MESH"]
    for obj in targets:
        if obj.type != "MESH" or core.is_collision_name(obj.name):
            continue                 # UCX_/UBX_/USP_/UCP_ are not in the gate's budget
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            count = len(mesh.polygons)
            triangles += len(mesh.loop_triangles) or (len(mesh.loops) - 2 * count)
            vertices += len(mesh.vertices)
            degenerate += _zero_area_polygons(mesh, count)
        finally:
            evaluated.to_mesh_clear()
        for slot in obj.material_slots:
            if slot.material is not None:
                material_names.add(slot.material.name)
            material = slot.material
            if material is None or not material.use_nodes:
                continue
            for node in material.node_tree.nodes:
                if node.type != "TEX_IMAGE" or node.image is None:
                    continue
                image = node.image
                if image.name in seen:
                    continue
                seen.add(image.name)
                textures.append((image.name, image.size[0], image.size[1]))
    return triangles, vertices, len(material_names), textures, degenerate


def _zero_area_polygons(mesh, count) -> int:
    """Zero-area faces, read in one call (a Python loop over 80k faces took
    seconds and ran on every sidebar redraw)."""
    if not count:
        return 0
    try:
        import numpy as np

        areas = np.empty(count, dtype=np.float32)
        mesh.polygons.foreach_get("area", areas)
        return int(np.count_nonzero(areas <= 1e-9))
    except ImportError:                                # pragma: no cover
        return sum(1 for polygon in mesh.polygons if polygon.area <= 1e-9)


# --------------------------------------------------------------------------- #
# [lod], [collision], [skeleton], [lightmap]: read only while a rule is on
# --------------------------------------------------------------------------- #

def _targets(context):
    return context.selected_objects or [o for o in context.scene.objects if o.type == "MESH"]


def _polygons(mesh):
    """(corner counts, corner -> vertex index) of a mesh, read in two calls."""
    import numpy as np

    counts = np.empty(len(mesh.polygons), dtype=np.int64)
    mesh.polygons.foreach_get("loop_total", counts)
    corners = np.empty(len(mesh.loops), dtype=np.int64)
    mesh.loops.foreach_get("vertex_index", corners)
    return counts.tolist(), corners.tolist()


def _evaluated_mesh_data(obj, depsgraph, read):
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        return read(mesh)
    finally:
        evaluated.to_mesh_clear()


def _hull(mesh):
    import numpy as np

    points = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", points)
    counts, corners = _polygons(mesh)
    return points.reshape(-1, 3).tolist(), core.fan_triangles(counts, corners)


def _armature(obj):
    """The armature object deforming *obj* (Armature modifier, or an armature parent)."""
    for modifier in getattr(obj, "modifiers", ()):
        if modifier.type == "ARMATURE" and getattr(modifier, "object", None) is not None:
            return modifier.object
    parent = getattr(obj, "parent", None)
    if parent is not None and parent.type == "ARMATURE" and \
            getattr(obj, "parent_type", "") == "ARMATURE":
        return parent
    return None


def _skeleton(render, depsgraph):
    """Bones as the FBX export writes them: every bone is a joint (LimbNode); the
    skeleton is the bones connected to one that weights a vertex (a vertex group
    of its name). The exporter's "Only Deform Bones" option would drop some: the
    gate reads the exported file."""
    import numpy as np

    bound, bones_of, sections = [], {}, {}
    for obj in render:
        armature = _armature(obj)
        if armature is None:
            continue
        for bone in armature.data.bones:
            bones_of[(armature.name, bone.name)] = bone
        names = {group.index: group.name for group in obj.vertex_groups}

        def read(mesh, armature=armature, names=names, obj=obj):
            weighted = {}
            for vertex in mesh.vertices:
                for item in vertex.groups:
                    name = names.get(item.group)
                    if name is not None and item.weight > 1e-4 and \
                            (armature.name, name) in bones_of:
                        weighted.setdefault(name, set()).add(vertex.index)
            counts, corners = _polygons(mesh)
            slots = np.empty(len(mesh.polygons), dtype=np.int64)
            mesh.polygons.foreach_get("material_index", slots)
            per_corner = np.repeat(slots, counts) if len(slots) else slots
            corners = np.asarray(corners, dtype=np.int64)
            for slot in np.unique(slots).tolist():
                used = set(corners[per_corner == slot].tolist())
                material = (obj.material_slots[slot].material
                            if slot < len(obj.material_slots) else None)
                label = material.name if material is not None else "default material"
                section = sections.setdefault(label, set())
                section.update(name for name, vertices in weighted.items() if vertices & used)
            return weighted

        bound.extend((armature.name, name) for name in _evaluated_mesh_data(obj, depsgraph, read))

    def parent_of(key):
        parent = bones_of[key].parent
        return (key[0], parent.name) if parent is not None else None

    def children_of(key):
        return [(key[0], child.name) for child in bones_of[key].children]

    return {"bones": core.skeleton_from_hierarchy(bound, parent_of, children_of,
                                                  lambda key: key in bones_of,
                                                  label=lambda key: key[1]),
            "sections": [(label, len(found)) for label, found in sections.items()]}


def _uv_values(layer, count):
    """Per-corner UVs of one UV layer. ``layer.uv`` (Blender 3.5+) or ``layer.data``
    (older): capability probing, as the attribute moved between versions."""
    import numpy as np

    values = np.empty(count * 2, dtype=np.float64)
    source = getattr(layer, "uv", None)
    if source is not None and hasattr(source, "foreach_get"):
        source.foreach_get("vector", values)
    else:
        layer.data.foreach_get("uv", values)
    return values.reshape(-1, 2)


def _lightmap(render, depsgraph, channel):
    """(mesh, per-corner UVs of layer *channel* or None, fan UV triangles) per static
    mesh. The FBX exporter writes the UV layers in order, so layer *channel* is
    the FBX UV set of that index."""
    found = []
    for obj in render:
        if _armature(obj) is not None:
            continue                                   # skinned: not lightmapped

        def read(mesh, name=obj.name):
            layers = mesh.uv_layers
            if channel >= len(layers):
                return (name, None, None)
            uvs = _uv_values(layers[channel], len(mesh.loops))
            counts, _corners = _polygons(mesh)
            import numpy as np

            triangles = np.asarray(core.fan_triangles(counts, list(range(len(uvs)))),
                                   dtype=np.int64).reshape(-1, 3)
            return (name, uvs.tolist(), uvs[triangles].tolist())

        found.append(_evaluated_mesh_data(obj, depsgraph, read))
    return found


def structure_facts(budget):
    """assess() keywords for [lod], [collision], [skeleton] and [lightmap].

    Nothing is read for a rule that is off. Blender has no LOD group the FBX
    exporter writes, so LODs come from object names (<name>_LOD0, _LOD1, ...);
    a collection is not exported as a node, so it does not make an LOD group.
    An API difference between Blender versions leaves the rule to the gate (an
    info note from assess()) instead of breaking the panel."""
    on = core.rules_on(budget) if budget is not None else {}
    facts = {}
    if not any(on.values()):
        return facts
    context = bpy.context
    depsgraph = context.evaluated_depsgraph_get()
    meshes = [obj for obj in _targets(context) if obj.type == "MESH"]
    render = [obj for obj in meshes if not core.is_collision_name(obj.name)]
    facts["render_names"] = sorted(obj.name for obj in render)
    if on.get("lod"):
        try:
            facts["lods"] = core.lods_by_name(
                (obj.name, _evaluated_mesh_data(obj, depsgraph,
                                                lambda mesh: _triangle_count(mesh)))
                for obj in render)
        except Exception:
            pass
    if on.get("collision"):
        try:
            # Selected, or next to a render mesh (same parent; at the top, the
            # same collection): an artist selects the mesh, not its hull.
            nearby = list(meshes)
            for obj in render:
                siblings = obj.parent.children if obj.parent is not None else [
                    item for collection in obj.users_collection
                    for item in collection.objects if item.parent is None]
                nearby.extend(item for item in siblings if item.type == "MESH")
            hulls = {obj.name: obj for obj in nearby if core.is_collision_name(obj.name)}
            facts["collision"] = [(name, *_evaluated_mesh_data(obj, depsgraph, _hull))
                                  for name, obj in sorted(hulls.items())]
            facts["skinned"] = any(_armature(obj) is not None for obj in render)
        except Exception:
            facts.pop("collision", None)
    if on.get("skeleton"):
        try:
            facts["skeleton"] = _skeleton(render, depsgraph)
        except Exception:
            pass
    if on.get("lightmap"):
        try:
            facts["lightmap"] = _lightmap(render, depsgraph, int(budget.lightmap_uv_channel))
        except Exception:
            pass
    return facts


def _triangle_count(mesh) -> int:
    count = len(mesh.polygons)
    return len(mesh.loop_triangles) or (len(mesh.loops) - 2 * count)


def _asset_folder():
    """The department folder the file lives in, for the fuzzy tag check."""
    path = bpy.data.filepath or ""
    if not path:
        return ""
    parent = os.path.basename(os.path.dirname(path))
    return parent


def run_preflight(rules_path=None, preset=None):
    triangles, vertices, materials, textures, degenerate = scene_facts()
    budget = None
    problem = ""
    try:
        budget = core.load_budget(rules_path, preset=preset)
    except Exception as exc:                          # missing rules / missing gate
        problem = str(exc)
    report = core.assess(asset=bpy.context.scene.name, triangles=triangles,
                         vertices=vertices, materials=materials,
                         textures=textures, degenerate=degenerate,
                         folder=_asset_folder(), budget=budget,
                         **structure_facts(budget))
    if problem:
        report.add("info", "no_rules", f"budget unavailable: {problem}")
    return report


# The sidebar redraws on every mouse move over it. Measuring there re-evaluated
# the whole selection each time; now it is measured when the scene changes.
_cache = {"report": None, "stale": True}


def _mark_stale(*_args) -> None:
    _cache["stale"] = True


def cached_report():
    if _cache["stale"] or _cache["report"] is None:
        _cache["report"] = run_preflight()
        _cache["stale"] = False
    return _cache["report"]


def _wrapped(layout, text, context, icon="NONE", alert=False):
    region = getattr(context, "region", None)
    width = getattr(region, "width", 300) or 300
    per_line = max(14, int(max(60.0, width - 60.0) / 7.0))
    for index, line in enumerate(textwrap.wrap(text, per_line) or [""]):
        row = layout.row()
        row.alert = alert
        row.label(text=line, icon=icon if index == 0 else "BLANK1")


class AGK_OT_preflight(bpy.types.Operator):
    """Check the selection against the studio budget"""
    bl_idname = "agk.preflight"
    bl_label = "Run pre-flight"
    bl_options = {"REGISTER"}

    def execute(self, context):
        if core is None:
            self.report({"ERROR"}, _IMPORT_PROBLEM)
            return {"CANCELLED"}
        _mark_stale()
        report = cached_report()
        for line in report.as_text().splitlines():
            self.report({"INFO"}, line)
        return {"FINISHED"}


class AGK_PT_preflight(bpy.types.Panel):
    bl_label = "Asset Gatekeeper"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Asset Gatekeeper"

    def draw(self, context):
        layout = self.layout
        if core is None:
            _wrapped(layout, _IMPORT_PROBLEM, context, icon="ERROR", alert=True)
            return
        report = cached_report()
        box = layout.box()
        _wrapped(box, report.headline(), context,
                 icon="CHECKMARK" if report.ok else "ERROR", alert=not report.ok)
        if report.triangles is not None:
            _wrapped(box, f"{report.triangles:,} tris   {report.materials or 0} materials",
                     context)
        if report.budget_summary:
            _wrapped(box, report.budget_summary, context)
        for finding in report.sorted_findings()[:6]:
            _wrapped(box, finding.message, context,
                     icon="ERROR" if finding.severity == "error" else "INFO",
                     alert=finding.severity == "error")
        layout.operator("agk.preflight", icon="PLAY")


CLASSES = (AGK_OT_preflight, AGK_PT_preflight)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    if _mark_stale not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_mark_stale)
    _mark_stale()


def unregister():
    if _mark_stale in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_mark_stale)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
