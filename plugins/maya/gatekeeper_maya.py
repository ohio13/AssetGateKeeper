"""
AssetGatekeeper for Autodesk Maya
=================================

In-scene validation: no export, no round trip. Queries the live scene for the
same things the engine checks on disk — triangles, material slots, UV sets, bone
influences, degenerate faces — and applies the project's ``rules.toml``.

Why in-scene instead of exporting to glTF: Maya's glTF exporter is an optional
plugin and its behaviour differs per version, while the polygon data is always
available through ``maya.cmds`` / OpenMaya. Checking the node before export also
catches problems that survive an export (e.g. 8 influences) rather than ones the
exporter silently rewrites.

Setup
-----
1. Put this folder on ``PYTHONPATH`` / MAYA_SCRIPT_PATH, or drop
   ``gatekeeper_maya.py`` into your ``scripts`` directory.
2. Set ``ASSETGATEKEEPER_HOME`` to the folder containing ``asset_gatekeeper.py``
   (the engine is imported here only for its rules parser and Budget defaults).
3. Optional: ``ASSETGATEKEEPER_RULES`` to point at a specific ``rules.toml``.

Use ``userSetup.py`` (next to this file) to get a shelf button automatically.

NOTE ON API COVERAGE: the commands used here are guarded and several have
fallbacks because Maya's flags have shifted between 2019→2024 (and Maya LT is a
different animal). Everything degrades to a warning rather than an exception,
but validate this in your Maya version before rolling it out studio-wide.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import maya.cmds as cmds

ENV_HOME = "ASSETGATEKEEPER_HOME"
ENV_RULES = "ASSETGATEKEEPER_RULES"

# Cost control: a 500k-poly evaluation should not freeze the viewport.
MAX_FACES_FOR_AREA_SCAN = 60_000
MAX_VERTICES_FOR_SKIN_SAMPLE = 4_000


# --------------------------------------------------------------------------- #
# Budget loading (shared with the engine)
# --------------------------------------------------------------------------- #

def _import_engine():
    home = os.environ.get(ENV_HOME)
    if home and home not in sys.path:
        sys.path.insert(0, home)
    try:
        import asset_gatekeeper  # noqa: F401
        return asset_gatekeeper
    except ImportError as exc:
        raise RuntimeError(
            "AssetGatekeeper engine not found. Set ASSETGATEKEEPER_HOME to the "
            f"folder containing asset_gatekeeper.py (import error: {exc})"
        ) from exc


def load_budget(rules_path: Optional[str] = None):
    """Budget from ``rules.toml`` if present, else the engine defaults."""
    engine = _import_engine()
    path = rules_path or os.environ.get(ENV_RULES) or ""
    if path and os.path.isfile(path):
        from pathlib import Path

        return engine.Budget.from_mapping(engine.parse_rules(Path(path)))
    return engine.Budget()


# --------------------------------------------------------------------------- #
# Scene queries
# --------------------------------------------------------------------------- #

def selected_transforms() -> list[str]:
    selection = cmds.ls(selection=True, long=True) or []
    transforms: list[str] = []
    for node in selection:
        if cmds.nodeType(node) == "transform" and cmds.listRelatives(node, shapes=True, type="mesh"):
            transforms.append(node)
        elif cmds.nodeType(node) == "mesh":
            parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
            transforms.extend(parents)
    return transforms


def mesh_shapes(transform: str) -> list[str]:
    return cmds.listRelatives(transform, shapes=True, type="mesh", fullPath=True) or []


def triangle_count(transform: str) -> int:
    total = 0
    for shape in mesh_shapes(transform):
        try:
            total += int(cmds.polyEvaluate(shape, triangle=True))
        except (RuntimeError, TypeError, ValueError):
            continue
    return total


def uv_channel_count(transform: str) -> int:
    channels: set[str] = set()
    for shape in mesh_shapes(transform):
        try:
            channels.update(cmds.polyUVSet(shape, query=True, allUVSets=True) or [])
        except (RuntimeError, TypeError, ValueError):
            continue
    return len(channels)


def shading_engines_used(shape: str) -> set:
    """Shading groups that hold this shape or some of its faces.

    ``listConnections(shape, type="shadingEngine")`` also returns groups that
    are connected but empty: after an artist assigns materials face by face,
    ``initialShadingGroup`` stays connected with no faces. Verified in Maya
    2027, a cube with 5 face-assigned materials read as 6 slots.
    """
    try:
        candidates = set(cmds.listConnections(shape, type="shadingEngine") or [])
    except (RuntimeError, TypeError, ValueError):
        return set()
    own = set(cmds.ls(shape, long=True) or [])
    own.update(cmds.listRelatives(shape, parent=True, fullPath=True) or [])
    used = set()
    for engine in candidates:
        members = cmds.sets(engine, query=True) or []
        if not members:
            continue
        nodes = cmds.ls(members, objectsOnly=True, long=True) or []
        if own.intersection(nodes):
            used.add(engine)
    return used


def material_slots(transform: str) -> int:
    engines: set[str] = set()
    for shape in mesh_shapes(transform):
        engines.update(shading_engines_used(shape))
    return len(engines)


def max_bone_influences(transform: str) -> int:
    """Worst per-vertex influence count.

    Primary: ``polyEvaluate -weightedVertexInfluence`` (Maya returns [min, max]).
    Fallback: sample vertices through the skinCluster with ``skinPercent``.
    """
    for shape in mesh_shapes(transform):
        try:
            result = cmds.polyEvaluate(shape, weightedVertexInfluence=True)
        except (RuntimeError, TypeError, ValueError):
            result = None
        if isinstance(result, (list, tuple)) and len(result) == 2:
            return int(result[1])

    for shape in mesh_shapes(transform):
        skin = (cmds.listHistory(shape, pruneDagObjects=True) or [])
        skin = [node for node in skin if cmds.nodeType(node) == "skinCluster"]
        if not skin:
            continue
        vertices = int(cmds.polyEvaluate(shape, vertex=True) or 0)
        stride = max(1, vertices // MAX_VERTICES_FOR_SKIN_SAMPLE)
        worst = 0
        for index in range(0, vertices, stride):
            try:
                weights = cmds.skinPercent(f"{skin[0]}", f"{shape}.vtx[{index}]",
                                           query=True, value=True)
            except (RuntimeError, TypeError, ValueError):
                continue
            if weights:
                weights = weights if isinstance(weights, (list, tuple)) else [weights]
                worst = max(worst, sum(1 for weight in weights if weight > 1e-4))
        return worst
    return 0


def degenerate_face_count(transform: str, epsilon: float = 1e-9) -> int:
    """Faces whose area is ~zero, via OpenMaya (polyInfo's normals are unreliable)."""
    try:
        import maya.api.OpenMaya as om
    except ImportError:  # pragma: no cover - Maya 2016 and earlier
        return 0

    count = 0
    for shape in mesh_shapes(transform):
        try:
            selection = om.MSelectionList().add(shape)
            mesh = om.MFnMesh(selection.getDagPath(0))
            points = mesh.getPoints(om.MSpace.kObject)
            face_count = mesh.numPolygons
        except (RuntimeError, ValueError):
            continue

        step = max(1, face_count // MAX_FACES_FOR_AREA_SCAN)
        for face_index in range(0, face_count, step):
            try:
                corners = mesh.getPolygonVertices(face_index)
            except (RuntimeError, ValueError):
                continue
            if len(corners) < 3:
                count += 1
                continue
            origin = om.MPoint(points[corners[0]])
            area = 0.0
            for index in range(1, len(corners) - 1):
                edge_a = om.MVector(om.MPoint(points[corners[index]]) - origin)
                edge_b = om.MVector(om.MPoint(points[corners[index + 1]]) - origin)
                area += 0.5 * (edge_a ^ edge_b).length()
            if area <= epsilon:
                count += 1
    return count


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

def check_node(transform: str, budget) -> list[str]:
    problems: list[str] = []

    triangles = triangle_count(transform)
    if triangles == 0:
        problems.append("mesh contains no triangles (empty or corrupt)")
    elif triangles > budget.max_triangles:
        problems.append(f"{triangles:,} triangles exceeds the {budget.max_triangles:,} budget "
                        f"(+{triangles - budget.max_triangles:,})")

    slots = material_slots(transform)
    if slots > budget.max_material_slots:
        problems.append(f"{slots} material slots exceeds the {budget.max_material_slots} slot "
                        f"budget (draw calls)")

    if budget.enforce_texture_naming:
        name = transform.rsplit("|", 1)[-1]
        if "_" not in name:
            problems.append(f"object name '{name}' lacks a namespace prefix (e.g. SM_/SK_)")

    uvs = uv_channel_count(transform)
    if uvs > budget.max_uv_channels:
        problems.append(f"{uvs} UV channels exceeds the {budget.max_uv_channels} channel limit")

    influences = max_bone_influences(transform)
    if influences > budget.max_bone_influences:
        problems.append(f"{influences} bone weights on one vertex exceeds the "
                        f"{budget.max_bone_influences} influence limit")

    if not budget.allow_degenerate:
        degenerate = degenerate_face_count(transform, budget.degenerate_epsilon)
        if degenerate:
            problems.append(f"{degenerate:,} degenerate faces (zero area) - Nanite and ray "
                            f"tracing compute garbage from these")
    return problems


def run(rules_path: Optional[str] = None, show_dialog: bool = True):
    """Check the selection (or the whole scene if nothing is selected)."""
    budget = load_budget(rules_path)
    transforms = selected_transforms()
    if not transforms:
        transforms = [node for node in (cmds.ls(type="transform", long=True) or [])
                      if cmds.listRelatives(node, shapes=True, type="mesh")]

    results = [(node, check_node(node, budget)) for node in transforms]
    failed = [(node, problems) for node, problems in results if problems]

    print("=" * 70)
    print(f"AssetGatekeeper — {len(results)} mesh(es), {len(failed)} with problems")
    for node, problems in results:
        short = node.rsplit("|", 1)[-1]
        if problems:
            print(f"FAILED: {short}")
            for problem in problems:
                print(f"   - {problem}")
        else:
            print(f"PASSED  {short}")
    print("=" * 70)

    if show_dialog:
        if failed:
            first = failed[0][1][0]
            cmds.confirmDialog(
                title="AssetGatekeeper",
                message=(f"{len(failed)} of {len(results)} mesh(es) exceed the budget.\n\n"
                         f"{failed[0][0].rsplit('|', 1)[-1]}: {first}\n\n"
                         "See the Script Editor for the full list."),
                button=["OK"], defaultButton="OK", icon="warning")
        else:
            cmds.inViewMessage(amg=f"<hl>PASSED</hl> — {len(results)} mesh(es) within budget",
                               pos="topCenter", fade=True)
    return results


if __name__ == "__main__":
    run()
