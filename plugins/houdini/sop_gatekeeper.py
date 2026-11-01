"""
AssetGatekeeper — Houdini Python SOP
====================================

Procedural graphs can quietly multiply geometry into the millions, and nothing
warns you until the output of a ``copy to points`` cook is 4M primitives. This
Python SOP sits in the stream and flags the node itself.

Usage (two options)
-------------------
1. **Python SOP** — create a SOP of type "Python", paste this file's contents into
   its Code field. It analyses the incoming geometry on every cook and posts a
   node warning badge when the budget is exceeded.
2. **Module import** — put the folder on HOUDINI_PATH and call
   ``sop_gatekeeper.run(node)`` from your own shelf script.

Environment
-----------
``ASSETGATEKEEPER_HOME``  folder containing ``asset_gatekeeper.py`` (for the rules
                          parser + Budget defaults)
``ASSETGATEKEEPER_RULES`` optional explicit ``rules.toml``

Design notes
------------
* The SOP **never raises** ``hou.Error``: a pipeline gate that breaks the cook is
  worse than no gate. It uses node warnings, which show the same badge in the
  network view without stopping downstream cooks.
* Areas are computed on a sample of primitives (``MAX_PRIMS_FOR_AREA_SCAN``) so a
  4M-prim cook does not freeze Houdini. Triangle counts use polygon vertex counts,
  which is exact and cheap.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Optional

try:
    import hou
except ImportError:  # pragma: no cover - imported outside Houdini (tests, docs)
    hou = None

ENV_HOME = "ASSETGATEKEEPER_HOME"
ENV_RULES = "ASSETGATEKEEPER_RULES"

MAX_PRIMS_FOR_AREA_SCAN = 50_000
UV_ATTRIBUTE_PATTERN = re.compile(r"^uv\d*$")

# Prim classes by name, because hou.primType gained members between versions and
# an AttributeError at cook time used to disable the whole check silently.
POLYGON_PRIM_TYPES = ("Polygon",)
PACKED_PRIM_TYPES = ("PackedGeometry", "PackedFragment", "PackedSequence")


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #

def load_budget(rules_path: Optional[str] = None):
    home = os.environ.get(ENV_HOME)
    if home and home not in sys.path:
        sys.path.insert(0, home)
    try:
        import asset_gatekeeper as engine
    except ImportError as exc:
        raise RuntimeError(
            "AssetGatekeeper engine not found. Set ASSETGATEKEEPER_HOME to the "
            f"folder containing asset_gatekeeper.py ({exc})"
        ) from exc

    path = rules_path or os.environ.get(ENV_RULES) or ""
    if path and os.path.isfile(path):
        from pathlib import Path

        return engine.Budget.from_mapping(engine.parse_rules(Path(path)))
    return engine.Budget()


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #

def uv_channel_count(geometry) -> int:
    """Houdini names extra UV sets ``uv``, ``uv2``, ``uv3``…; count them all."""
    channels = set()
    for owner in (geometry.pointAttribs(), geometry.vertexAttribs(),
                  geometry.primAttribs()):
        for attribute in owner:
            if UV_ATTRIBUTE_PATTERN.match(attribute.name()):
                channels.add(attribute.name())
    return len(channels)


def material_slots(geometry) -> int:
    """Unique ``shop_materialpath`` values, the SOP-level material assignment."""
    attribute = geometry.findPrimAttrib("shop_materialpath")
    if attribute is None:
        return 1 if len(geometry.prims()) else 0
    values = {prim.attribValue(attribute) for prim in geometry.prims()}
    return len(values)


def _type_name(prim) -> str:
    """'PackedGeometry' for prim.type() -> primType.PackedGeometry."""
    return str(prim.type()).rsplit(".", 1)[-1]


def _vertex_count(prim) -> int:
    """Houdini 19 renamed ``vertexCount()`` to ``numVertices()``.

    The SOP called the old name unconditionally, so on anything from 19 onwards
    every cook raised AttributeError, ``run()`` swallowed it as "check skipped",
    and the gate was decorative.
    """
    for attribute in ("numVertices", "vertexCount"):
        counter = getattr(prim, attribute, None)
        if callable(counter):
            return int(counter())
    return 0


def _is_closed(prim) -> bool:
    """Open polylines (a Line SOP, hair guides, curve tools) are prims of type
    Polygon too - only a *closed* one is a face. Counting ``vertices - 2`` on an
    open 1,000-point guide invented 998 triangles per strand: verified in
    Houdini 22, where 100 guides read as 99,800 triangles and 100 "degenerate"
    faces."""
    closed = getattr(prim, "isClosed", None)
    if not callable(closed):
        return True
    try:
        return bool(closed())
    except Exception:                                  # pragma: no cover - version
        return True


def _embedded_geometry(prim):
    """The geometry inside a packed prim, or None when it cannot be opened."""
    getter = getattr(prim, "getEmbeddedGeometry", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:                                  # pragma: no cover - version
        return None


def triangle_count(geometry, sample_cap: int = 2_000_000,
                   uncounted: Optional[list] = None) -> int:
    """Triangles after triangulation, opening packed primitives as it goes.

    * Polygons count ``vertices - 2``.
    * Packed prims are *expanded*: a packed prim reports one vertex, so a packed
      4,000,000-triangle asset used to count as zero and sail through the gate.
      A pack that cannot be expanded is recorded in ``uncounted`` rather than
      quietly counted as nothing.
    * Curves, volumes and particles are not polygons and contribute no triangles.
      Counting ``vertices - 2`` on a NURBS curve invented triangles out of thin
      air.
    """
    prims = geometry.prims()
    total = 0
    for index, prim in enumerate(prims):
        if index >= sample_cap:
            # Extrapolate from what we have rather than freezing the cook.
            average = total / index if index else 0
            total += int(average * (len(prims) - index))
            break
        kind = _type_name(prim)
        if kind in PACKED_PRIM_TYPES:
            embedded = _embedded_geometry(prim)
            if embedded is not None:
                total += triangle_count(embedded, sample_cap, uncounted)
            elif uncounted is not None:
                uncounted.append(prim)
            continue
        if kind in POLYGON_PRIM_TYPES and _is_closed(prim):
            total += max(0, _vertex_count(prim) - 2)
    return total


def degenerate_prim_count(geometry, epsilon: float = 1e-9) -> int:
    """Zero-area *polygons*. Packed prims are opened; curves are not polygons."""
    prims = geometry.prims()
    count = 0
    step = max(1, len(prims) // MAX_PRIMS_FOR_AREA_SCAN)
    for index, prim in enumerate(prims):
        if index % step:
            continue
        kind = _type_name(prim)
        if kind in PACKED_PRIM_TYPES:
            embedded = _embedded_geometry(prim)
            if embedded is not None:
                count += degenerate_prim_count(embedded, epsilon)
            continue
        if kind not in POLYGON_PRIM_TYPES or not _is_closed(prim):
            continue                      # an open curve is not a face, degenerate or not
        points = prim.points()
        if len(points) < 3:
            count += 1
            continue
        origin = points[0].position()
        area = 0.0
        for corner in range(1, len(points) - 1):
            edge_a = points[corner].position() - origin
            edge_b = points[corner + 1].position() - origin
            area += 0.5 * edge_a.cross(edge_b).length()
        if area <= epsilon:
            count += 1
    return count


def analyse(geometry, budget) -> list[str]:
    problems: list[str] = []

    uncounted: list = []
    triangles = triangle_count(geometry, uncounted=uncounted)
    if uncounted:
        # Counting an unreadable pack as zero is how a 4M-triangle pack passes.
        problems.append(
            f"{len(uncounted)} packed primitive(s) could not be expanded, so the "
            "triangle count is not trustworthy")
    if triangles == 0 and not uncounted:
        problems.append("geometry contains no polygons (empty stream)")
    elif triangles > budget.max_triangles:
        problems.append(f"{triangles:,} triangles exceeds the {budget.max_triangles:,} budget "
                        f"(+{triangles - budget.max_triangles:,})")

    slots = material_slots(geometry)
    if slots > budget.max_material_slots:
        problems.append(f"{slots} material slots exceeds the {budget.max_material_slots} "
                        f"slot budget (draw calls)")

    uvs = uv_channel_count(geometry)
    if uvs > budget.max_uv_channels:
        problems.append(f"{uvs} UV channels exceeds the {budget.max_uv_channels} channel limit")

    if not budget.allow_degenerate:
        degenerate = degenerate_prim_count(geometry, budget.degenerate_epsilon)
        if degenerate:
            problems.append(f"{degenerate:,} degenerate prims (zero area) - Nanite and ray "
                            f"tracing compute garbage from these")

    return problems


# --------------------------------------------------------------------------- #
# Flagging the node
# --------------------------------------------------------------------------- #

def flag(node, problems: list[str]) -> None:
    """Post the verdict on the node without breaking the cook."""
    if node is None:
        return
    if not problems:
        return

    message = "AssetGatekeeper: " + " | ".join(problems)

    # A *warning* badge (yellow) first: that is what the network view makes an
    # artist notice. addMessage only posts a blue info note, which is how the
    # SOP behaved in Houdini 22 - flagged, and easy to miss. Never addError:
    # a gate that breaks the cook is worse than no gate.
    for method in ("addWarning", "setWarning", "addMessage"):
        post = getattr(node, method, None)
        if callable(post):
            try:
                post(message)
                return
            except Exception:  # pragma: no cover - version dependent
                continue

    print(f"[AssetGatekeeper] {message}")  # last resort


def run(node=None, rules_path: Optional[str] = None) -> list[str]:
    """Analyse the node's cooked geometry and flag it. Returns the problems."""
    if hou is not None:
        node = node or hou.pwd()
    if node is None:
        return []

    budget = load_budget(rules_path)
    problems = analyse(node.geometry(), budget)
    flag(node, problems)

    if problems:
        print("[AssetGatekeeper] " + "; ".join(problems))
    else:
        print("[AssetGatekeeper] PASSED")
    return problems


# --- runs when the file is pasted into a Python SOP ------------------------ #
if hou is not None and __name__ == "__main__":
    try:
        run()
    except Exception as error:  # never break the cook
        print(f"[AssetGatekeeper] check skipped: {error}")
