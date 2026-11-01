"""Unreal Engine 5 pre-flight (Python, no plugin compile required).

Two ways in, both standard-shipped:

* **Editor Utility Widget / Python script** - call ``run_preflight()`` on the
  selected StaticMesh assets and print the report to the Output Log.
* **Content Browser right-click** - add the menu entry below from
  ``init_unreal.py`` (already in this folder) so the artist never leaves Unreal.

It measures the *asset*, not a packaged build: ``get_num_triangles()`` on the
StaticMesh LOD 0 is the same number the Statistics panel shows, and material
slots are counted from ``static_materials``, which is exactly what the budget
cares about (draw calls).
"""
import os
import sys

import unreal

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


def _selected_static_meshes():
    selected = unreal.EditorUtilityLibrary.get_selected_assets()
    return [asset for asset in selected
            if isinstance(asset, unreal.StaticMesh)]


def scene_facts(assets=None):
    assets = assets if assets is not None else _selected_static_meshes()
    triangles = vertices = 0
    materials = 0
    names = []
    for asset in assets:
        names.append(asset.get_name())
        triangles += int(asset.get_num_triangles(0) or 0)
        vertices += int(asset.get_num_vertices(0) or 0)
        slots = asset.get_editor_property("static_materials") or []
        materials += len(slots)
    return triangles, vertices, materials, [], 0, names


def run_preflight(rules_path=None, preset=None, assets=None):
    triangles, vertices, materials, textures, degenerate, names = scene_facts(assets)
    budget = None
    problem = ""
    try:
        budget = core.load_budget(rules_path, preset=preset)
    except Exception as exc:
        problem = str(exc)
    label = names[0] if len(names) == 1 else f"{len(names)} assets"
    folder = ""
    report = core.assess(asset=label, triangles=triangles, vertices=vertices,
                         materials=materials, textures=textures,
                         degenerate=degenerate, folder=folder, budget=budget)
    if problem:
        report.add("info", "no_rules", "budget unavailable: " + problem)
    return report


def run_and_log(rules_path=None, preset=None):
    """Print the report to the Output Log - the only reliable UI every version has."""
    report = run_preflight(rules_path, preset)
    for line in report.as_text().splitlines():
        unreal.log(line)
    if not report.ok:
        unreal.log_error(f"AssetGatekeeper: {report.headline()}")
    return report


def show(rules_path=None, preset=None):
    report = run_preflight(rules_path, preset)
    unreal.EditorDialog.show_message(
        "Asset Gatekeeper", report.as_text(),
        unreal.AppMsgType.OK if report.ok else unreal.AppMsgType.OK)
    return report


if __name__ == "__main__":
    run_and_log()
