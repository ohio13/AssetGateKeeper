"""DCC-agnostic pre-flight checks: the engine behind every in-app panel.

An artist should learn that a mesh is 4,000 triangles over budget while it is
still an editable scene, not after a submit is rejected. Every DCC adapter in
this folder does the same two things and nothing else:

1. read the numbers from the DCC that owns them (no export, no temp file);
2. ask :func:`assess` whether they pass, and print the answer in the viewport.

Keeping the decision here - pure, dependency-free, testable without Blender -
means the panel in Maya cannot disagree with the gate in CI. Adapters translate
numbers; this module owns the policy.

Fuzzy tags matter most here: an artist who sees "folder 'charcter' looks like
'character'" before export does not create a second folder with the same mistake.
"""

from __future__ import annotations

import dataclasses
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class Finding:
    severity: str          # error | warning | info
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


@dataclass
class PreflightReport:
    asset: str = ""
    ok: bool = True
    findings: list[Finding] = field(default_factory=list)
    triangles: Optional[int] = None
    vertices: Optional[int] = None
    materials: Optional[int] = None
    tag: Optional[str] = None
    tag_suggestion: Optional[str] = None
    budget_summary: str = ""

    def add(self, severity: str, code: str, message: str) -> None:
        self.findings.append(Finding(severity, code, message))
        if severity == "error":
            self.ok = False

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings,
                      key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.code))

    def headline(self) -> str:
        if self.ok and not self.findings:
            return "CLEAN - nothing to fix"
        if self.ok:
            return f"PASS with {len(self.warnings)} warning(s)"
        return f"BLOCKED - {len(self.errors)} error(s)"

    def as_dict(self) -> dict[str, Any]:
        return {"asset": self.asset, "ok": self.ok, "headline": self.headline(),
                "triangles": self.triangles, "vertices": self.vertices,
                "materials": self.materials, "tag": self.tag,
                "tag_suggestion": self.tag_suggestion,
                "budget": self.budget_summary,
                "findings": [f.as_dict() for f in self.sorted_findings()]}

    def as_text(self, width: int = 72) -> str:
        """Plain text: every DCC can render this, none can render HTML the same."""
        lines = [self.headline(), "-" * width]
        if self.triangles is not None:
            lines.append(f"triangles {self.triangles:,}"
                         + (f"   vertices {self.vertices:,}" if self.vertices else ""))
        if self.materials is not None:
            lines.append(f"materials {self.materials}")
        if self.budget_summary:
            lines.append(f"budget    {self.budget_summary}")
        if self.tag:
            lines.append(f"tag       {self.tag}")
        if self.tag_suggestion:
            lines.append(f"SUGGESTED {self.tag_suggestion}  (rename the folder)")
        if self.findings:
            lines.append("")
            for finding in self.sorted_findings():
                marker = {"error": "x", "warning": "!", "info": "-"}.get(
                    finding.severity, "-")
                lines.append(f"[{marker}] {finding.message}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Budget helpers
# --------------------------------------------------------------------------- #

def load_budget(rules_path: str | Path | None = None,
                preset: str | None = None, platform: str | None = None) -> Any:
    """Resolve a budget the same way the gate does, without a CLI.

    Imported lazily so a DCC panel works even when the gate is not installed:
    the panel then reports "the gate is not importable" instead of failing to
    load the add-on (which artists would experience as "the tool is broken").
    """
    import asset_gatekeeper as agk            # noqa: PLC0415 - top-level module

    document: dict = {}
    if rules_path:
        path = Path(rules_path)
        if not path.is_file():
            raise FileNotFoundError(f"no such rules file: {path}")
        document = agk.parse_rules(path)
    if platform:
        document = agk.apply_platform(document, platform)
    if preset:
        document = agk.apply_preset(document, preset)
    return agk.Budget.from_mapping(document)


def describe_budget(budget: Any) -> str:
    return (f"tris<={getattr(budget, 'max_triangles', 0):,}  "
            f"materials<={getattr(budget, 'max_material_slots', 0)}  "
            f"texture<={getattr(budget, 'max_texture_edge', 0)}px")


BUDGET_INTEGER_FIELDS = (("max_triangles", 0), ("max_material_slots", 0),
                         ("max_texture_edge", 1), ("max_bone_influences", 0))
BUDGET_FLOAT_FIELDS = ("degenerate_epsilon", "pivot_tolerance", "transform_epsilon",
                       "pbr_floor", "pbr_ceiling")


def budget_problem(budget: Any) -> str:
    """Why this budget cannot be trusted, or "" when it is usable.

    The panels occasionally build a budget themselves (from a panel field, a
    preset or a tag table) instead of going through ``agk.Budget``. A -1 or a nan
    that reached them used to be accepted quietly and then either failed every
    asset or disabled a rule, which reads as "the tool is broken". The gate
    refuses these; the early warning must say the same thing.
    """
    import math

    for name, minimum in BUDGET_INTEGER_FIELDS:
        if not hasattr(budget, name):
            continue
        value = getattr(budget, name)
        if isinstance(value, bool) or not isinstance(value, int):
            return f"budget {name} must be a whole number, got {value!r}"
        if value < minimum:
            return f"budget {name} must be at least {minimum}, got {value}"
    for name in BUDGET_FLOAT_FIELDS:
        if not hasattr(budget, name):
            continue
        value = getattr(budget, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"budget {name} must be a number, got {value!r}"
        if not math.isfinite(value) or value < 0:
            return f"budget {name} must be a finite, non-negative number, got {value}"
    return ""


# --------------------------------------------------------------------------- #
# The check
# --------------------------------------------------------------------------- #

def assess(asset: str = "untitled", triangles: Optional[int] = None,
           vertices: Optional[int] = None, materials: Optional[int] = None,
           textures: Iterable[tuple[str, int, int]] = (),
           degenerate: Optional[int] = None,
           non_manifold: Optional[int] = None,
           missing_references: Iterable[str] = (),
           folder: str = "", filename: str = "",
           tag_budgets: Optional[dict[str, Any]] = None,
           tag_mode: str = "suggest", tag_similarity: float = 0.75,
           budget: Any = None, asset_path: str | Path | None = None,
           mesh_names: Iterable[str] = (),
           lods: Optional[Iterable[Sequence]] = None,
           collision: Optional[Iterable[Sequence]] = None,
           render_names: Optional[Iterable[str]] = None,
           skinned: Optional[bool] = None,
           skeleton: Optional[dict] = None,
           lightmap: Optional[Iterable[Sequence]] = None,
           export_format: str = ".fbx") -> PreflightReport:
    """Judge one asset snapshot against the studio budget.

    Everything is optional: a DCC that cannot count degenerate faces simply passes
    ``None`` and the check is skipped - silently skipping is fine here because the
    gate still runs at submit time; the panel is an early warning, not the gate.

    The [lod], [collision], [skeleton] and [lightmap] rules (see
    :func:`check_structure` for the shapes) are judged by the gate's own code.
    ``None`` means "the adapter could not look": nothing is said while the rule
    is off, and an info note says the gate will check it while the rule is on.
    An empty list means "looked, found none", which a rule can fail.
    """
    report = PreflightReport(asset=asset, triangles=triangles, vertices=vertices,
                             materials=materials)
    budget = budget if budget is not None else load_budget()
    mesh_names = list(mesh_names)            # read twice: name rule, render meshes

    if budget is not None:
        problem = budget_problem(budget)
        if problem:
            # A budget the gate would refuse is not a budget: report it instead of
            # judging the mesh against nonsense.
            report.add("error", "budget", problem)
            budget = None

    if budget is not None:
        report.budget_summary = describe_budget(budget)

    # -- geometry budgets --
    if triangles is not None and budget is not None:
        limit = int(getattr(budget, "max_triangles", 0) or 0)
        if triangles <= 0:
            report.add("error", "empty",
                       "mesh has no triangles: an empty collection or a failed export")
        elif limit and triangles > limit:
            over = triangles - limit
            report.add("error", "tri_budget",
                       f"{triangles:,} triangles exceeds the {limit:,} budget "
                       f"(+{over:,}); decimate or move it to a tag with a bigger budget")

    if materials is not None and budget is not None:
        limit = int(getattr(budget, "max_material_slots", 0) or 0)
        if limit and materials > limit:
            report.add("error", "mat_budget",
                       f"{materials} material slots exceeds the {limit} slot budget "
                       f"(each slot is a draw call); merge or atlas")

    if degenerate:
        allowed = bool(getattr(budget, "allow_degenerate", False))
        if not allowed:
            report.add("error", "degenerate",
                       f"{degenerate} zero-area face(s): Nanite and ray tracing "
                       "compute garbage from these")

    # Mesh names: the gate's optional [mesh] name_prefixes rule, one rule for
    # every DCC (3ds Max used to skip it while Maya invented its own).
    if budget is not None:
        try:
            from asset_gatekeeper import mesh_name_problem     # the gate's own rule
        except ImportError:                                    # pragma: no cover
            mesh_name_problem = None
        prefixes = tuple(getattr(budget, "mesh_name_prefixes", ()) or ())
        for name in mesh_names:
            if mesh_name_problem is not None:
                problem = mesh_name_problem(str(name), budget)
            elif prefixes and not any(str(name).lower().startswith(p.lower()) for p in prefixes):
                problem = f"mesh name '{name}' does not start with {' or '.join(prefixes)}"
            else:
                problem = ""
            if problem:
                report.add("error", "mesh_name", problem)

    if non_manifold:
        report.add("warning", "non_manifold",
                   f"{non_manifold} non-manifold edge(s): fine for props, bad for "
                   "anything that gets simulated or lit with ambient occlusion")

    for reference in missing_references:
        report.add("error", "missing_ref",
                   f"missing reference '{reference}': the engine cannot load it")

    # -- [lod], [collision], [skeleton], [lightmap]: the gate's own rules --
    if budget is not None:
        if render_names is None:
            render_names = [name for name in mesh_names if not is_collision_name(name)]
        check_structure(report, budget, triangles=triangles, lods=lods,
                        collision=collision, render_names=render_names,
                        skinned=skinned, skeleton=skeleton, lightmap=lightmap,
                        export_format=export_format)

    # -- textures --
    edge_limit = int(getattr(budget, "max_texture_edge", 0) or 0) if budget else 0
    power_of_two = bool(getattr(budget, "require_power_of_two",
                               getattr(budget, "power_of_two", False))) if budget else False
    for name, width, height in textures:
        if edge_limit and max(width, height) > edge_limit:
            report.add("error", "tex_size",
                       f"texture '{name}' is {width}x{height}, over the "
                       f"{edge_limit}px budget")
        if power_of_two and (width & (width - 1) or height & (height - 1)):
            report.add("warning", "tex_pot",
                       f"texture '{name}' is {width}x{height}, not a power of two")

    # -- fuzzy tag check --
    if tag_budgets:
        tag, suggestion = diagnose_tag(folder or "", filename or asset,
                                       tag_budgets, tag_mode, tag_similarity)
        report.tag = tag
        report.tag_suggestion = suggestion
        if suggestion:
            severity = "error" if tag_mode == "strict" else "warning"
            report.add(severity, "tag_near_miss",
                       f"folder/tag '{suggestion[0]}' is not a known tag; closest "
                       f"match is '{suggestion[1]}' - rename it or the asset will be "
                       "judged by the wrong budget")

    return report


# --------------------------------------------------------------------------- #
# [lod], [collision], [skeleton], [lightmap]
# --------------------------------------------------------------------------- #

#: Unreal's collision prefixes, as the gate reads them (FBX_COLLISION_PREFIXES).
COLLISION_PREFIXES = ("ucx_", "ubx_", "usp_", "ucp_")
NOT_IN_PANEL = "not checked in the panel; the gate checks it on export"
RULE_LABELS = {"lod": "LOD levels ([lod])", "collision": "collision meshes ([collision])",
               "skeleton": "skeleton ([skeleton])", "lightmap": "lightmap UVs ([lightmap])"}
_LOD_OFF = {"lod_min_levels": 0, "lod_max_ratio": 0.0}
_COLLISION_OFF = {"collision_require": False, "collision_name_match": False,
                  "collision_convex": False, "collision_max_hulls": 0,
                  "collision_max_hull_vertices": 0}


def is_collision_name(name: str) -> bool:
    """UCX_/UBX_/USP_/UCP_ (any case, DCC namespace ignored): not a render mesh."""
    return str(name or "").rsplit(":", 1)[-1].lower().startswith(COLLISION_PREFIXES)


def rules_on(budget: Any) -> dict[str, bool]:
    """Which of the four structure tables have a rule switched on."""
    def on(*names):
        return any(bool(getattr(budget, name, False)) for name in names)

    overlap = getattr(budget, "lightmap_max_overlap", -1.0)
    return {"lod": on("lod_min_levels", "lod_max_ratio"),
            "collision": on(*_COLLISION_OFF),
            "skeleton": on("max_bones", "max_bones_per_section", "max_bone_depth",
                           "single_root_bone"),
            "lightmap": on("lightmap_require", "lightmap_inside_unit_square")
            or (isinstance(overlap, (int, float)) and overlap >= 0)}


def lods_by_name(meshes: Iterable[Sequence]) -> list[tuple[str, tuple[int, ...], str]]:
    """LOD chains from (object name, triangles) pairs named <base>_LOD<n>.

    The gate's own reading (``lod_chains_by_name``): case and namespaces ignored,
    a plain <base> stands in for <base>_LOD0, a chain stops at the first gap.
    """
    import asset_gatekeeper as agk            # noqa: PLC0415

    pairs = [(str(name), int(triangles)) for name, triangles in meshes]
    return [(base, levels, "names") for base, levels in agk.lod_chains_by_name(pairs)]


def fan_triangles(counts: Iterable[int], indices: Sequence) -> list[tuple]:
    """Polygons (corner counts + flat corner list) as fan triangles (c0, ck, ck+1).

    The gate triangulates an FBX polygon this way, so a hull or a lightmap built
    from the scene's polygons is tested on the same triangles as the export.
    """
    triangles, start = [], 0
    for count in counts:
        count = int(count)
        triangles.extend((indices[start], indices[start + k], indices[start + k + 1])
                         for k in range(1, count - 1))
        start += count
    return triangles


def skeleton_from_hierarchy(bound: Iterable, parent_of, children_of, is_joint,
                            label=str) -> list[tuple[str, Optional[str]]]:
    """The skeleton as the gate reads it from an FBX, as (bone, parent bone or None).

    A bone is a joint or a node the skin binds to (*bound*); the skeleton is
    every bone connected to a bound one: up to its root and down through
    unweighted bones (IK targets, twist helpers, tips). A plain group above the
    bones is not a bone. *parent_of* / *children_of* walk the host's hierarchy,
    *label* turns a host node into the name the export writes.
    """
    linked = list(dict.fromkeys(bound))
    bound_set = set(linked)

    def is_bone(node) -> bool:
        return node in bound_set or bool(is_joint(node))

    tops = []
    for bone in linked:
        node, seen = bone, {bone}
        while True:
            up = parent_of(node)
            if up is None or up in seen or not is_bone(up):
                break
            seen.add(up)
            node = up
        if node not in tops:
            tops.append(node)
    bones, stack = [], list(tops)
    found = set()
    while stack:
        node = stack.pop()
        if node in found:
            continue
        found.add(node)
        bones.append(node)
        stack.extend(child for child in (children_of(node) or ()) if is_bone(child))
    pairs = []
    for node in bones:
        up = parent_of(node)
        pairs.append((label(node), label(up) if up is not None and up in found else None))
    return pairs


def _gate_function(function, **replacements):
    """The gate's own *function*, reading *replacements* in place of its globals.

    The gate's [skeleton] and [lightmap] checks are written against a file path:
    ``Gatekeeper._check_skeleton`` calls ``skeleton_facts(path)``. The panel has
    the same facts from the live scene, so it runs that very function with
    ``skeleton_facts`` answering from the scene. Nothing in the gate module is
    changed (the copy has its own globals), and every threshold and every word
    of the verdict stays the gate's. A replaced name the gate no longer has is
    an error, so a renamed helper fails the tests instead of being bypassed.
    """
    missing = [name for name in replacements if name not in function.__globals__]
    if missing:
        raise RuntimeError("the gate has no " + ", ".join(missing))
    namespace = dict(function.__globals__)
    namespace.update(replacements)
    clone = types.FunctionType(function.__code__, namespace, function.__name__,
                               function.__defaults__, function.__closure__)
    clone.__kwdefaults__ = function.__kwdefaults__
    return clone


def _relaxed(budget: Any, changes: dict) -> Any:
    """The budget with some rules off (the ones the panel cannot judge)."""
    return dataclasses.replace(budget, **changes) if changes else budget


def check_structure(report: PreflightReport, budget: Any, *,
                    triangles: Optional[int] = None,
                    lods: Optional[Iterable[Sequence]] = None,
                    collision: Optional[Iterable[Sequence]] = None,
                    render_names: Optional[Iterable[str]] = None,
                    skinned: Optional[bool] = None,
                    skeleton: Optional[dict] = None,
                    lightmap: Optional[Iterable[Sequence]] = None,
                    export_format: str = ".fbx") -> None:
    """Add the [lod], [collision], [skeleton] and [lightmap] findings to *report*.

    What an adapter passes (``None`` = could not look; ``[]`` = looked, none):

    * ``lods``: ``(name, (LOD0 triangles, LOD1 triangles, ...), source)`` per
      chain, source ``"group"`` (an LOD group node) or ``"names"`` (from
      ``_LOD<n>`` names: use :func:`lods_by_name`).
    * ``collision``: ``(name, points, triangles)`` per UCX_/UBX_/USP_/UCP_ mesh
      (points as (x, y, z), triangles as vertex-index triples), or
      ``(name, vertex count)`` when the positions cannot be read - the hull is
      then counted but its convexity is left to the gate.
    * ``render_names``: the render meshes (LOD0 and meshes without LODs) a
      collision mesh must be named after; ``skinned``: a render mesh is skinned.
    * ``skeleton``: ``{"bones": [(bone, parent bone or None), ...],
      "sections": [(material, bones weighting it), ...] or None}``.
    * ``lightmap``: ``(mesh, uvs, uv triangles)`` per *static* mesh, ``uvs``
      the (u, v) of UV set ``[lightmap] uv_channel`` per face corner, None when
      the mesh has no such set, or True when it has one whose values were not
      read; ``uv triangles`` the (u, v) corners per triangle, or None when the
      adapter cannot build them (the overlap is then left to the gate).
    """
    on = rules_on(budget)
    if not any(on.values()):
        return                                     # every rule off: nothing new

    def note(rule: str, what: str = "", why: str = "") -> None:
        report.add("info", rule, f"{what or RULE_LABELS[rule]} "
                                 + (f"not checked in the panel ({why}); the gate checks it "
                                    "on export" if why else NOT_IN_PANEL))

    try:
        import asset_gatekeeper as agk            # noqa: PLC0415
    except ImportError:
        agk = None
    if agk is None or not isinstance(budget, agk.Budget):
        for rule in (rule for rule, active in on.items() if active):
            note(rule, why="the gate's budget is not loaded")
        return

    suffix = "." + str(export_format or "fbx").lstrip(".").lower()
    path = Path((Path(report.asset or "asset").stem or "asset") + suffix)

    def run(rule: str, check) -> None:
        try:
            problems, warnings = check()
        except Exception as exc:                  # a panel never breaks on a rule
            note(rule, why=f"{type(exc).__name__}: {exc}")
            return
        for message in problems:
            report.add("error", rule, message)
        for message in warnings:
            report.add("warning", rule, message)

    # [lod] and [collision]: lod_collision_problems() takes the structure itself.
    if (on["lod"] or on["collision"]) and triangles != 0:
        readable = suffix in agk.FBX_SUFFIXES or suffix in agk.GLTF_SUFFIXES
        chains = tuple((str(item[0]), tuple(int(count) for count in item[1]),
                        str(item[2]) if len(item) > 2 else "group") for item in (lods or ()))
        shapes, untested = [], []
        for item in (collision or ()):
            name = agk._bare_name(str(item[0]))
            points = item[1] if len(item) > 1 else None
            faces = item[2] if len(item) > 2 else None
            if points is not None and faces is not None and not isinstance(points, int) \
                    and agk.np is not None:
                shapes.append((name, *agk.hull_shape(points, faces)))
                continue
            count = points if isinstance(points, int) else len(points or ())
            # Convexity unknown here: the hull passes that test in the panel and
            # an info note says the gate will run it.
            shapes.append((name, int(count), 0.0, False))
            if name.upper().startswith("UCX_"):
                untested.append(name)
        structure = {"lod_groups": chains,
                     "render_names": tuple(sorted({agk._bare_name(str(name))
                                                   for name in (render_names or ())} - {""})),
                     "collision_shapes": tuple(shapes), "skinned": bool(skinned)}

        def lod_collision(changes):
            found = agk.lod_collision_problems(path, structure if readable else None,
                                               _relaxed(budget, changes))
            return found[0], found[1]

        if on["lod"]:
            if lods is None:
                note("lod")
            else:
                run("lod", lambda: lod_collision(_COLLISION_OFF))
        if on["collision"]:
            if collision is None:
                note("collision")
            else:
                run("collision", lambda: lod_collision(_LOD_OFF))
                if untested and budget.collision_convex:
                    note("collision", f"convexity of {agk._name_list(untested)}")

    # [skeleton]: Gatekeeper._check_skeleton, fed from the scene.
    if on["skeleton"]:
        bones = None if skeleton is None else skeleton.get("bones")
        if bones is None:
            note("skeleton")
        elif bones:
            parent = {str(bone): (None if up is None else str(up)) for bone, up in bones}
            children: dict = {}
            for bone, up in parent.items():
                if up is not None:
                    children.setdefault(up, []).append(bone)
            roots, depth = agk._bone_tree(set(parent), parent.get,
                                          lambda bone: children.get(bone, []))
            deepest = max(depth, key=lambda bone: (depth[bone], str(bone))) if depth else ""
            sections = skeleton.get("sections")
            facts = {"bones": len(parent), "roots": sorted(roots),
                     "depth": max(depth.values()) if depth else 0, "deepest": deepest,
                     "sections": [(str(label), int(count)) for label, count in (sections or ())],
                     "unread_sections": 0}
            changes = {}
            if sections is None and budget.max_bones_per_section:
                changes["max_bones_per_section"] = 0
                note("skeleton", "bones per material section ([skeleton] max_bones_per_section)")
            checked = _relaxed(budget, changes)
            if checked.skeleton_checked:
                def skeleton_check():
                    problems: list = []
                    warnings: list = []
                    check = _gate_function(agk.Gatekeeper._check_skeleton,
                                           skeleton_facts=lambda _path: facts)
                    check(agk.Gatekeeper(checked), path, problems, warnings, [], [])
                    return problems, warnings
                run("skeleton", skeleton_check)

    # [lightmap]: Gatekeeper._check_lightmap and lightmap_facts, fed from the scene.
    if on["lightmap"]:
        if lightmap is None:
            note("lightmap")
        elif agk.np is None:
            note("lightmap", why="numpy is not available in this Python")
        else:
            np = agk.np
            meshes, missing, unread, overlap_ready = [], [], [], True
            for item in lightmap:
                name, uvs = str(item[0]), item[1]
                corners = item[2] if len(item) > 2 else None
                if uvs is None:
                    missing.append(name)
                    continue
                if uvs is True:                    # the set is there; values not read
                    unread.append(name)
                    continue
                points = np.asarray(uvs, dtype=np.float64).reshape(-1, 2)
                if corners is None:
                    overlap_ready = False
                    corners = ()
                meshes.append((name, points,
                               np.asarray(corners, dtype=np.float64).reshape(-1, 3, 2)))
            changes = {}
            if unread and (budget.lightmap_inside_unit_square
                           or budget.lightmap_max_overlap >= 0):
                note("lightmap", f"lightmap UVs of {agk._name_list(unread)}")
            if budget.lightmap_max_overlap >= 0 and not overlap_ready:
                changes["lightmap_max_overlap"] = -1.0
                note("lightmap", "lightmap overlap ([lightmap] max_overlap)")
            checked = _relaxed(budget, changes)
            if checked.lightmap_checked:
                found = {"meshes": meshes, "missing": missing, "unread": ""}

                def lightmap_check():
                    problems: list = []
                    warnings: list = []
                    facts = _gate_function(agk.lightmap_facts,
                                           _lightmap_meshes=lambda _path, _channel: found)
                    check = _gate_function(agk.Gatekeeper._check_lightmap, lightmap_facts=facts)
                    check(agk.Gatekeeper(checked), path, problems, warnings, [], [])
                    return problems, warnings
                run("lightmap", lightmap_check)


def diagnose_tag(folder: str, filename: str, tag_budgets: dict[str, Any],
                 tag_mode: str = "suggest",
                 similarity: float = 0.75) -> tuple[Optional[str], Optional[tuple[str, str]]]:
    """Which tag applies, and whether the nearest folder name is a typo of one.

    Returns ``(tag, (given, closest) or None)``. Pure: no filesystem, no gate
    import, so every DCC adapter can use it and the tests can exhaust it.
    """
    import asset_gatekeeper as agk            # noqa: PLC0415

    # resolve_tag() wants a path; a synthetic one keeps the same code path as the
    # gate so the panel and the gate can never drift.
    synthetic = Path("/") / (folder or "untagged") / (filename or "asset.obj")
    tag, warning = agk.resolve_tag(synthetic, tag_budgets, Path("/"),
                                   similarity=similarity)
    if warning and "closest match" in warning:
        # The warning is the human string; recover the pair for structured use.
        given = warning.split("'")[1] if "'" in warning else (folder or "")
        closest = warning.split("'")[-2] if warning.count("'") >= 4 else ""
        return tag, (given, closest)
    return tag, None


def format_report(report: PreflightReport, width: int = 72) -> str:
    return report.as_text(width)
