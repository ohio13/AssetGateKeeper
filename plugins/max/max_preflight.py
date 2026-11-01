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


def _selected_geometry():
    selection = list(rt.selection) if rt.selection.count else list(rt.objects)
    return [node for node in selection if rt.classOf(node) in
            (rt.Editable_Poly, rt.Editable_Mesh, rt.PolyMeshObject)]


def scene_facts():
    triangles = vertices = degenerate = 0
    materials = set()
    textures = []
    for node in _selected_geometry():
        triangles += int(rt.getPolygonCount(node)[0] or 0)
        vertices += int(rt.getPolygonCount(node)[1] or 0)
        material = node.material
        if material is not None and rt.classOf(material) in (rt.Multimaterial, rt.StandardMaterial):
            slots = material.materialList if rt.classOf(material) == rt.Multimaterial else [material]
            for slot in slots:
                if slot is None:
                    continue
                materials.add(str(slot.name))
                for map_slot in ("diffuseMap", "bumpMap", "specularMap"):
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
    return triangles, vertices, len(materials), textures, degenerate


def run_preflight(rules_path=None, preset=None):
    triangles, vertices, materials, textures, degenerate = scene_facts()
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
                         folder=os.path.basename(scene_file), budget=budget)
    if problem:
        report.add("info", "no_rules", "budget unavailable: " + problem)
    return report


def show():
    """Show the report. Uses a message box: no .ms UI to install, nothing to break."""
    report = run_preflight()
    rt.messageBox(report.as_text(), title="Asset Gatekeeper")
    return report


if __name__ == "__main__":
    show()
