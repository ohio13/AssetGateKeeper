"""Blender pre-flight panel.

Install: Edit > Preferences > Add-ons > Install... > this file. It appears in the
3D Viewport sidebar (N panel) as "Asset Gatekeeper".

It reads the *scene*, not an export: polygon and material counts come from the
evaluated depsgraph, so a modifier stack, a subdivision surface or a geo-nodes
setup is measured as the artist will see it. Nothing is written to disk until the
artist exports, which is the point - the warning arrives before the export, not
after the submit.
"""
import os
import sys

import bpy

# Site-packages or the repository root: whichever holds the shared engine.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (_HERE,
                   os.path.dirname(_HERE),
                   os.path.dirname(os.path.dirname(_HERE))):
    if os.path.isdir(os.path.join(_candidate, "plugins")) or \
            os.path.isfile(os.path.join(_candidate, "asset_gatekeeper.py")):
        if _candidate not in sys.path:
            sys.path.insert(0, _candidate)
        break

try:
    from plugins import preflight_core as core
except ImportError:                                   # pragma: no cover - addon path
    import preflight_core as core                     # type: ignore[no-redef]


def scene_facts():
    """(triangles, vertices, materials, textures, degenerate) for selected objects."""
    triangles = vertices = materials = degenerate = 0
    textures = []
    seen = set()
    context = bpy.context
    depsgraph = context.evaluated_depsgraph_get()
    targets = context.selected_objects or [o for o in context.scene.objects
                                           if o.type == "MESH"]
    for obj in targets:
        if obj.type != "MESH":
            continue
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            triangles += len(mesh.loop_triangles) or sum(
                max(0, len(poly.vertices) - 2) for poly in mesh.polygons)
            vertices += len(mesh.vertices)
            for polygon in mesh.polygons:
                if polygon.area <= 1e-9:
                    degenerate += 1
        finally:
            evaluated.to_mesh_clear()
        materials += len([slot for slot in obj.material_slots if slot.material])
        for slot in obj.material_slots:
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
    return triangles, vertices, materials, textures, degenerate


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
                         folder=_asset_folder(), budget=budget)
    if problem:
        report.add("info", "no_rules", f"budget unavailable: {problem}")
    return report


class AGK_OT_preflight(bpy.types.Operator):
    """Check the selection against the studio budget"""
    bl_idname = "agk.preflight"
    bl_label = "Run pre-flight"
    bl_options = {"REGISTER"}

    def execute(self, context):
        report = run_preflight()
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
        report = run_preflight()
        box = layout.box()
        box.label(text=report.headline(),
                  icon="CHECKMARK" if report.ok else "ERROR")
        if report.triangles is not None:
            box.label(text=f"{report.triangles:,} tris   "
                           f"{report.materials or 0} materials")
        if report.budget_summary:
            box.label(text=report.budget_summary)
        for finding in report.sorted_findings()[:6]:
            row = box.row()
            row.alert = finding.severity == "error"
            row.label(text=finding.message[:64],
                      icon="ERROR" if finding.severity == "error" else "INFO")
        layout.operator("agk.preflight", icon="PLAY")


CLASSES = (AGK_OT_preflight, AGK_PT_preflight)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
