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


def _element_count(geometry) -> int:
    total = 0
    for accessor in ("prims", "points"):
        getter = getattr(geometry, accessor, None)
        if callable(getter):
            try:
                total += len(getter())
            except Exception:                          # pragma: no cover
                continue
    return total


# --------------------------------------------------------------------------- #
# [lod], [collision], [skeleton], [lightmap]: the gate's rules via preflight_core
# --------------------------------------------------------------------------- #

COLLISION_PREFIXES = ("ucx_", "ubx_", "usp_", "ucp_")
#: Above this many prims the per-prim reads below (piece names, lightmap UVs)
#: are skipped and the rule is left to the gate, so a cook stays interactive.
MAX_PRIMS_FOR_STRUCTURE = 500_000


def _core():
    """plugins/preflight_core.py, or None (ASSETGATEKEEPER_HOME not on sys.path)."""
    try:
        from plugins import preflight_core as core
    except ImportError:
        try:
            import preflight_core as core              # type: ignore[no-redef]
        except ImportError:
            return None
    return core


def _is_collision(name: str) -> bool:
    return str(name or "").rsplit(":", 1)[-1].lower().startswith(COLLISION_PREFIXES)


def _name_attribute(geometry):
    """The ``name`` prim attribute: one piece per name, as Unreal's Houdini
    workflows and the FBX export of named pieces make one mesh per name. (A
    ``path`` hierarchy is not read.)"""
    finder = getattr(geometry, "findPrimAttrib", None)
    return finder("name") if callable(finder) else None


def _piece_names(attribute) -> list:
    """The distinct values of a string attribute, without a pass over the prims
    where this Houdini has ``Attrib.strings()`` (capability probe)."""
    strings = getattr(attribute, "strings", None)
    try:
        return [str(item) for item in strings()] if callable(strings) else []
    except Exception:                                  # pragma: no cover - version
        return []


def _prim_triangles(prim) -> int:
    kind = _type_name(prim)
    if kind in PACKED_PRIM_TYPES:
        embedded = _embedded_geometry(prim)
        return triangle_count(embedded) if embedded is not None else 0
    if kind in POLYGON_PRIM_TYPES and _is_closed(prim):
        return max(0, _vertex_count(prim) - 2)
    return 0


def pieces(geometry) -> dict:
    """{name: [prims]} by the ``name`` attribute; {} without one (or too many prims)."""
    attribute = _name_attribute(geometry)
    if attribute is None:
        return {}
    prims = geometry.prims()
    if len(prims) > MAX_PRIMS_FOR_STRUCTURE:
        return {}
    found: dict = {}
    for prim in prims:
        found.setdefault(str(prim.attribValue(attribute) or ""), []).append(prim)
    return found


def _point_attribute(geometry, name):
    for finder in ("findVertexAttrib", "findPointAttrib"):
        lookup = getattr(geometry, finder, None)
        attribute = lookup(name) if callable(lookup) else None
        if attribute is not None:
            return finder, attribute
    return None, None


def _hull(prims):
    """(points, fan triangles) of a collision piece's polygons."""
    index: dict = {}
    points, triangles = [], []
    for prim in prims:
        if _type_name(prim) not in POLYGON_PRIM_TYPES:
            continue
        corners = []
        for point in prim.points():
            key = point.number() if callable(getattr(point, "number", None)) else id(point)
            if key not in index:
                index[key] = len(points)
                position = point.position()
                points.append(tuple(float(axis) for axis in position))
            corners.append(index[key])
        triangles.extend((corners[0], corners[k], corners[k + 1])
                         for k in range(1, len(corners) - 1))
    return points, triangles


def _lightmap(geometry, split, channel, node_name):
    """(piece, per-corner UVs or None, fan UV triangles) per piece. UV set 0 is
    ``uv``, set n is ``uv<n+1>`` (Houdini's naming for extra sets)."""
    wanted = "uv" if channel == 0 else f"uv{channel + 1}"
    where, attribute = _point_attribute(geometry, wanted)
    groups = split or {node_name: list(geometry.prims())}
    found = []
    for name, prims in sorted(groups.items()):
        if _is_collision(name):
            continue
        if attribute is None:
            found.append((name, None, None))
            continue
        corners, triangles = [], []
        for prim in prims:
            if _type_name(prim) not in POLYGON_PRIM_TYPES or not _is_closed(prim):
                continue
            uvs = []
            for vertex in prim.vertices():
                owner = vertex if where == "findVertexAttrib" else vertex.point()
                value = owner.attribValue(attribute)
                uvs.append((float(value[0]), float(value[1])))
            corners.extend(uvs)
            triangles.extend([uvs[0], uvs[k], uvs[k + 1]] for k in range(1, len(uvs) - 1))
        found.append((name, corners, triangles))
    return found


def structure_facts(geometry, budget, split, node_name="geometry") -> dict:
    """assess() keywords for the four structure tables, read while a rule is on.

    The skeleton is not read from capture attributes (``boneCapture`` is a
    packed index/weight table whose region names live in detail info): a
    captured stream leaves [skeleton] to the gate, an uncaptured one has no
    skeleton to check."""
    core = _core()
    on = core.rules_on(budget) if core is not None else {}
    facts: dict = {}
    if not any(on.values()):
        return facts
    too_many = len(geometry.prims()) > MAX_PRIMS_FOR_STRUCTURE
    names = sorted(split) if split else [node_name]
    render = [name for name in names if not _is_collision(name)]
    facts["render_names"] = render
    _where, capture = _point_attribute(geometry, "boneCapture")
    facts["skinned"] = capture is not None
    if on.get("lod") and not too_many:
        facts["lods"] = core.lods_by_name(
            (name, sum(_prim_triangles(prim) for prim in split[name])) for name in render) \
            if split else []
    if on.get("collision") and not too_many:
        facts["collision"] = [(name, *_hull(split[name])) for name in names if _is_collision(name)]
    if on.get("skeleton") and capture is None:
        facts["skeleton"] = {"bones": []}
    if on.get("lightmap") and not too_many and capture is None:
        facts["lightmap"] = _lightmap(geometry, split, int(budget.lightmap_uv_channel), node_name)
    elif on.get("lightmap") and capture is not None:
        facts["lightmap"] = []          # a captured (skinned) stream is not lightmapped
    return facts


def structure_problems(geometry, budget, triangles, split, notes=None,
                       node_name="geometry") -> list:
    """The gate's [lod]/[collision]/[skeleton]/[lightmap] failures and warnings."""
    core = _core()
    if core is None:
        return []
    try:
        facts = structure_facts(geometry, budget, split, node_name)
        report = core.PreflightReport(asset=node_name)
        core.check_structure(report, budget, triangles=triangles, **facts)
    except Exception as error:          # never break the cook
        if notes is not None:
            notes.append(f"LOD/collision/skeleton/lightmap rules not checked here ({error})")
        return []
    if notes is not None:
        notes.extend(finding.message for finding in report.findings if finding.severity == "info")
    return [finding.message for finding in report.sorted_findings()
            if finding.severity in ("error", "warning")]


def analyse(geometry, budget, notes: Optional[list] = None,
            node_name: str = "geometry") -> list[str]:
    """Problems for the node badge; *notes* collects what was left to the gate."""
    problems: list[str] = []

    uncounted: list = []
    triangles = triangle_count(geometry, uncounted=uncounted)
    core = _core()
    attribute = _name_attribute(geometry)
    wanted = core is not None and any(core.rules_on(budget).values())
    split = {}
    if attribute is not None and (wanted or any(_is_collision(name)
                                                for name in _piece_names(attribute))):
        split = pieces(geometry)
        # UCX_/UBX_/USP_/UCP_ pieces are collision, never part of the gate's budget.
        triangles -= sum(_prim_triangles(prim) for name, prims in split.items()
                         if _is_collision(name) for prim in prims)
    if uncounted:
        # Counting an unreadable pack as zero is how a 4M-triangle pack passes.
        problems.append(
            f"{len(uncounted)} packed primitive(s) could not be expanded, so the "
            "triangle count is not trustworthy")
    if triangles == 0 and not uncounted:
        # Curves (hair guides), particles and volumes are legitimate streams with
        # no faces; warning on them taught artists to ignore the badge. Only a
        # stream with nothing in it at all is worth flagging.
        if _element_count(geometry) == 0:
            problems.append("geometry is empty (no points or primitives)")
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

    if wanted:
        problems.extend(structure_problems(geometry, budget, triangles, split, notes, node_name))
    elif core is None and notes is not None and (
            getattr(budget, "skeleton_checked", False) or getattr(budget, "lightmap_checked", False)
            or getattr(budget, "lod_min_levels", 0) or getattr(budget, "lod_max_ratio", 0)
            or getattr(budget, "collision_name_match", False)
            or getattr(budget, "collision_require", False)):
        notes.append("LOD/collision/skeleton/lightmap rules not checked here (plugins/"
                     "preflight_core.py not found); the gate checks them on export")
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
    notes: list[str] = []
    name = getattr(node, "name", None)
    problems = analyse(node.geometry(), budget, notes,
                       node_name=str(name()) if callable(name) else "geometry")
    flag(node, problems)
    for note in notes:
        print(f"[AssetGatekeeper] note: {note}")

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
