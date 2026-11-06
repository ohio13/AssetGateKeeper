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
                         mesh_names=names)
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
