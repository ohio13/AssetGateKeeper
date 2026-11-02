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


def _mesh_transforms(selection):
    shapes = []
    for node in selection:
        if cmds.nodeType(node) == "mesh":
            shapes.append(node)
            continue
        shapes.extend(cmds.listRelatives(node, shapes=True, type="mesh") or [])
    return shapes


def scene_facts(selection=None):
    selection = selection or (cmds.ls(selection=True) or cmds.ls(type="mesh"))
    shapes = _mesh_transforms(selection)
    triangles = vertices = degenerate = 0
    materials = set()
    textures = []
    for shape in shapes:
        faces = cmds.polyEvaluate(shape, face=True) or 0
        triangles += cmds.polyEvaluate(shape, triangle=True) or faces * 2
        vertices += cmds.polyEvaluate(shape, vertex=True) or 0
        for engine in cmds.listConnections(shape, type="shadingEngine") or []:
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
                         budget=budget)
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
