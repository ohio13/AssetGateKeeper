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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

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

    for field, minimum in BUDGET_INTEGER_FIELDS:
        if not hasattr(budget, field):
            continue
        value = getattr(budget, field)
        if isinstance(value, bool) or not isinstance(value, int):
            return f"budget {field} must be a whole number, got {value!r}"
        if value < minimum:
            return f"budget {field} must be at least {minimum}, got {value}"
    for field in BUDGET_FLOAT_FIELDS:
        if not hasattr(budget, field):
            continue
        value = getattr(budget, field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"budget {field} must be a number, got {value!r}"
        if not math.isfinite(value) or value < 0:
            return f"budget {field} must be a finite, non-negative number, got {value}"
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
           mesh_names: Iterable[str] = ()) -> PreflightReport:
    """Judge one asset snapshot against the studio budget.

    Everything is optional: a DCC that cannot count degenerate faces simply passes
    ``None`` and the check is skipped - silently skipping is fine here because the
    gate still runs at submit time; the panel is an early warning, not the gate.
    """
    report = PreflightReport(asset=asset, triangles=triangles, vertices=vertices,
                             materials=materials)
    budget = budget if budget is not None else load_budget()

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
