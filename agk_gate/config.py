"""AssetGatekeeper, part 2 of 7: Budget, rules.toml parsing, platform presets, tags and exceptions.

Split out of asset_gatekeeper.py on 11 Nov 2026 with no change in behaviour;
asset_gatekeeper.py still exposes every name. See agk_gate/__init__.py.
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Sequence

from agk_gate.common import (
    DEFAULT_TEXTURE_SUFFIXES, LOG, NAMING_PRESETS, RULES_FILENAME, tomllib,
)


# --- #
# Configuration
# --- #

@dataclass(frozen=True)
class Budget:
    """The hardcoded technical budget this gate enforces."""

    max_triangles: int = 50_000
    max_texture_edge: int = 2_048
    max_material_slots: int = 3
    require_power_of_two: bool = True
    enforce_texture_naming: bool = True
    # The map names real exports use (6 Nov: every one of 548 textures in the
    # Khronos glTF samples failed the old five-name list). Matching ignores case
    # and the separators inside a name, so _BaseColor also accepts _base_color
    # and _basecolor. A studio narrows it with [texture] suffixes.
    texture_suffixes: tuple[str, ...] = DEFAULT_TEXTURE_SUFFIXES
    # Deep data profiling: the bottlenecks that survive an art review.
    max_bone_influences: int = 4          # per-vertex weights; mobile/web GPUs cap at 4
    max_uv_channels: int = 4              # TEXCOORD_n sets; most engines stop at 4-8
    allow_degenerate: bool = False        # zero-area triangles: poison for Nanite / RT
    # A triangle is degenerate when it is flat - its area is at most
    # degenerate_ratio x (longest edge)^2 - or two corners are the same point.
    # That judges shape, not size: a fixed area cut-off called every triangle of
    # a real-scale 1 mm glTF prop "degenerate" (6 Nov corpus test).
    degenerate_ratio: float = 1e-6
    # What a degenerate triangle does: "block" (fail, the default), "warn" (pass
    # with a note) or "allow". allow_degenerate = true is the same as "allow".
    degenerate_mode: str = "block"
    # Optional: mesh names must start with one of these (["SM_", "SK_"]).
    # Off by default. The same rule runs in the gate (file name) and in every
    # DCC panel (object name), so Maya, Max, Blender and Unreal agree.
    mesh_name_prefixes: tuple[str, ...] = ()
    # [naming]: several conventions at once. Each entry is
    # (label, texture prefixes, texture suffixes, texture regex, mesh prefixes,
    # mesh regex); a name passes when it follows ANY entry. Empty = the plain
    # texture_suffixes / mesh_name_prefixes rules above.
    naming_conventions: tuple = ()
    degenerate_epsilon: float = 0.0       # optional extra: any area at or below this (units^2)

    # - filters - #
    # Globs that look like assets but are not: review renders, 4x paint sources,
    # hashed build caches. From [filters] exclude. Empty by default.
    exclude: tuple[str, ...] = ()

    # - reference & hierarchy auditing - #
    # On by default: a .gltf pointing at a texture nobody shipped is broken in
    # every engine, on every platform, with no ambiguity to argue about.
    check_references: bool = True
    # A reference that resolves outside the watch folder is a failure by
    # default. Studios with a shared texture library one level up can say so in
    # [mesh] and get a warning instead of a wall of red.
    allow_outside_references: bool = False

    # - PBR value sanitisation - #
    # Albedo is *not* a painting. Real-world base colour reflectance lives in a
    # narrow band; pure black absorbs everything and pure white blinds the
    # lighting model, which is why graders ask for it to be clamped.
    pbr_check: bool = True
    pbr_floor: float = 0.02               # 5/255: below this, albedo is "pure black"
    pbr_ceiling: float = 0.94             # 240/255: above this, albedo is "pure white"
    pbr_suffixes: tuple[str, ...] = ("_Albedo", "_Diffuse", "_BaseColor")

    # - geometric boundary profiling - #
    # Off by default, on purpose: plenty of legitimate exports are authored with
    # an offset pivot (a door hinged at its edge) or as a triangle soup. That is
    # a conversation with the artist, not a violation, so it is opt-in.
    audit_transforms: bool = False
    require_indexed: bool = False
    pivot_tolerance: float = 0.05         # fraction of the bbox diagonal
    transform_epsilon: float = 1e-4       # metres; below this a translation is "baked"

    # - memory (G2): what the asset costs in GPU memory on the target - #
    # 0 = not budgeted. Textures: width x height x bytes per pixel of the
    # compressed format, x 4/3 for mips. Meshes: vertices x vertex size +
    # indices. Estimates, calibrated against the engine's own numbers.
    max_texture_mb: float = 0.0
    max_mesh_mb: float = 0.0
    texture_format: str = "bc7"           # colour textures
    normal_format: str = "bc5"            # textures named _Normal/_nrm/_n
    # What the engine adds to every static mesh on import. Unreal's defaults:
    # a generated lightmap UV set, and a second index buffer for depth-only
    # passes. Calibrated on UE 5.3.2 (N2, 4 Nov 2026): without them the
    # estimate was 37-53% under Size Map on five meshes, with them within 6%.
    mesh_lightmap_uv: bool = True
    mesh_depth_index_buffer: bool = True

    # - texel density (G8): pixels per metre of surface; (0, 0) = not checked - #
    texel_density_min: float = 0.0
    texel_density_max: float = 0.0
    texel_texture: int = 1024             # assumed texture size when the mesh names none

    # - LODs and collision ([lod], [collision]); every one off by default - #
    # LOD levels: FBX LOD groups, glTF MSFT_lod, or _LOD0/_LOD1 names.
    lod_min_levels: int = 0               # at least this many levels, LOD0 included
    lod_max_ratio: float = 0.0            # each level <= this x the level above
    # Unreal collision (UCX_/UBX_/USP_/UCP_ meshes). A [collision] table in a
    # rules file turns name matching and the convexity test on (see
    # _read_structure); Budget() itself checks nothing, so no verdict changes.
    collision_require: bool = False       # a static mesh must carry collision
    collision_name_match: bool = False    # UCX_<RenderMesh>[_NN], or Unreal drops it
    collision_max_hulls: int = 0          # UCX_ hulls per asset
    collision_max_hull_vertices: int = 0  # vertices per UCX_ hull
    collision_convex: bool = False        # a UCX_ hull must be convex

    # - skeleton ([skeleton]): the bones the asset's skinned meshes use; 0 / False = off - #
    max_bones: int = 0                    # bones in the skeleton(s)
    max_bones_per_section: int = 0        # bones weighting one material section (mobile: 75)
    max_bone_depth: int = 0               # longest root-to-tip chain, the root counting 1
    single_root_bone: bool = False        # exactly one root bone (Unreal imports one)

    # - lightmap UVs ([lightmap]): an authored lightmap UV set on static meshes - #
    # Off by default. Unrelated to mesh_lightmap_uv, which only says whether the
    # memory estimate adds the set Unreal generates.
    lightmap_require: bool = False        # static meshes must have the set
    lightmap_uv_channel: int = 1          # which set: 1 = the second (Unreal's convention)
    lightmap_inside_unit_square: bool = False   # every lightmap UV within 0-1
    lightmap_max_overlap: float = -1.0    # percent of covered texels; below 0 = not checked
    lightmap_resolution: int = 256        # the grid the overlap is measured on

    # Which platform profile produced this budget (for the log line only).
    platform: str = ""

    def __post_init__(self) -> None:
        """Reject impossible budgets at construction, wherever they came from.

        A negative cap is not a tighter budget, it is a typo: ``--triangles -1``
        made every mesh in the repository fail, and the report blamed the assets.
        Rules files, presets, tag budgets and CLI overrides all land here, so
        this is the one place that has to check.
        """
        for field, minimum in (("max_triangles", 0), ("max_texture_edge", 1),
                               ("max_material_slots", 0), ("max_bone_influences", 0),
                               ("max_uv_channels", 1), ("max_bones", 0),
                               ("max_bones_per_section", 0), ("max_bone_depth", 0),
                               ("lightmap_uv_channel", 0), ("lightmap_resolution", 16)):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"budget {field} must be an integer, got {value!r}")
            if value < minimum:
                raise ConfigError(f"budget {field} must be at least {minimum}, got {value}")
        if self.lightmap_uv_channel > LIGHTMAP_MAX_CHANNEL:
            raise ConfigError(f"budget lightmap_uv_channel must be at most {LIGHTMAP_MAX_CHANNEL} "
                              f"(engines read 8 UV sets), got {self.lightmap_uv_channel}")
        if self.lightmap_resolution > LIGHTMAP_MAX_RESOLUTION:
            raise ConfigError(f"budget lightmap_resolution must be at most "
                              f"{LIGHTMAP_MAX_RESOLUTION}, got {self.lightmap_resolution}")
        overlap = self.lightmap_max_overlap
        if isinstance(overlap, bool) or not isinstance(overlap, (int, float)) \
                or not math.isfinite(overlap) or overlap > 100:
            raise ConfigError("budget lightmap_max_overlap must be a percentage from 0 to 100 "
                              f"(or below 0 for not checked), got {overlap!r}")
        if self.degenerate_epsilon < 0:
            raise ConfigError("budget degenerate_epsilon cannot be negative, got "
                              f"{self.degenerate_epsilon}")
        # Finite, non-negative floats. NaN is the dangerous one: every comparison
        # against it is False, so ``nan > 0`` silently disabled the degenerate-face
        # check instead of failing loudly.
        for field in ("degenerate_epsilon", "degenerate_ratio", "pivot_tolerance", "transform_epsilon"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"budget {field} must be a number, got {value!r}")
            if not math.isfinite(value):
                raise ConfigError(f"budget {field} must be a finite number, got {value}")
            if value < 0:
                raise ConfigError(f"budget {field} cannot be negative, got {value}")
        for field in ("pbr_floor", "pbr_ceiling"):
            value = getattr(self, field)
            if not 0.0 <= value <= 1.0:
                raise ConfigError(f"budget {field} must be between 0 and 1, got {value}")
        if self.pbr_floor > self.pbr_ceiling:
            raise ConfigError("budget pbr_floor must not exceed pbr_ceiling "
                              f"({self.pbr_floor} > {self.pbr_ceiling})")
        for field, where in (("lod_min_levels", "[lod].min_levels"),
                             ("collision_max_hulls", "[collision].max_hulls"),
                             ("collision_max_hull_vertices", "[collision].max_hull_vertices")):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigError(f"{where} must be a whole number, 0 (off) or more, got {value!r}")
        if 0 < self.collision_max_hull_vertices < 4:
            raise ConfigError("[collision].max_hull_vertices must be 0 (off) or at least 4: a "
                              "convex hull needs 4 corners, got "
                              f"{self.collision_max_hull_vertices}")
        ratio = self.lod_max_ratio
        if (isinstance(ratio, bool) or not isinstance(ratio, (int, float))
                or not math.isfinite(ratio) or not 0.0 <= ratio <= 1.0):
            raise ConfigError("[lod].max_ratio must be a fraction above 0 and at most 1, such as "
                              f"0.6 (each level at most 60% of the triangles of the one above), "
                              f"or 0 for off; got {ratio!r}")

        # Suffix lists. A blank entry is a typo that either matches every filename
        # ("") or none, and an empty texture list turns the naming rule into a
        # regex that matches everything - the check silently stops checking.
        texture_suffixes = tuple(str(item).strip() for item in self.texture_suffixes)
        pbr_suffixes = tuple(str(item).strip() for item in self.pbr_suffixes)
        for field, suffixes in (("texture_suffixes", texture_suffixes),
                                ("pbr_suffixes", pbr_suffixes)):
            if any(not item for item in suffixes):
                raise ConfigError(f"budget {field} contains an empty entry; remove it "
                                  f"instead of leaving a blank string")
        if not texture_suffixes:
            raise ConfigError("budget texture_suffixes is empty: no texture name could "
                              "ever satisfy the naming rule. List at least one suffix "
                              "or drop the [texture] table and use the defaults.")
        object.__setattr__(self, "texture_suffixes", texture_suffixes)
        object.__setattr__(self, "pbr_suffixes", pbr_suffixes)
        if self.degenerate_mode not in ("block", "warn", "allow"):
            raise ConfigError("budget degenerate must be \"block\", \"warn\" or \"allow\", got "
                              f"{self.degenerate_mode!r}")
        prefixes = tuple(str(item).strip() for item in (self.mesh_name_prefixes or ()))
        if any(not item for item in prefixes):
            raise ConfigError("budget mesh_name_prefixes contains an empty entry; remove it")
        object.__setattr__(self, "mesh_name_prefixes", prefixes)

    @property
    def skeleton_checked(self) -> bool:
        """Any [skeleton] rule is on (the skeleton is only read then)."""
        return bool(self.max_bones or self.max_bones_per_section or self.max_bone_depth
                    or self.single_root_bone)

    @property
    def lightmap_checked(self) -> bool:
        """Any [lightmap] rule is on (the lightmap UV set is only read then)."""
        return bool(self.lightmap_require or self.lightmap_inside_unit_square
                    or self.lightmap_max_overlap >= 0)

    @classmethod
    def from_mapping(cls, document: dict) -> "Budget":
        """Build a Budget from a parsed rules file, rejecting typos loudly.

        Keys that are absent fall back to the dataclass defaults, so a rules file
        only ever has to state what differs from the shipped policy.
        """
        if not isinstance(document, dict):
            raise ConfigError("the rules file must contain a table of settings")
        _reject_unknown(document, ("mesh", "texture", "server", "healer",
                                   "platforms", "platform", "tags", "tagging",
                                   "preset", "notify", "filters", "exceptions", "totals",
                                   "naming", "lod", "collision", "skeleton", "lightmap"),
                        "the rules file")
        filters = document.get("filters") or {}
        if not isinstance(filters, dict):
            raise ConfigError("[filters] must be a table")
        _reject_unknown(filters, ("exclude",), "[filters]")
        exclude = _read_strings(filters, "exclude", "[filters]") if filters else None

        mesh = document.get("mesh", {})
        texture = document.get("texture", {})
        if not isinstance(mesh, dict):
            raise ConfigError("[mesh] must be a table")
        if not isinstance(texture, dict):
            raise ConfigError("[texture] must be a table")
        _reject_unknown(mesh, ("max_triangles", "max_material_slots", "max_bone_influences",
                               "max_uv_channels", "allow_degenerate", "degenerate_epsilon", "degenerate_ratio",
                               "degenerate",
                               "name_prefixes",
                               "check_references", "allow_outside_references",
                               "audit_transforms", "require_indexed",
                               "pivot_tolerance", "transform_epsilon", "max_mb",
                               "texel_density", "texel_texture", "lightmap_uv",
                               "depth_index_buffer"), "[mesh]")
        _reject_unknown(texture, ("max_edge", "power_of_two", "naming", "suffixes",
                                  "pbr", "pbr_floor", "pbr_ceiling", "pbr_suffixes",
                                  "max_mb", "format", "normal_format"), "[texture]")
        memory = _read_memory(mesh, texture)
        structure = _read_structure(document)
        rigging = _read_skeleton_lightmap(document)

        max_triangles = _read_int(mesh, "max_triangles", "[mesh]", minimum=0)
        max_material_slots = _read_int(mesh, "max_material_slots", "[mesh]", minimum=0)
        max_bone_influences = _read_int(mesh, "max_bone_influences", "[mesh]", minimum=0)
        max_uv_channels = _read_int(mesh, "max_uv_channels", "[mesh]", minimum=1)
        allow_degenerate = _read_bool(mesh, "allow_degenerate", "[mesh]")
        degenerate_epsilon = _read_float(mesh, "degenerate_epsilon", "[mesh]")
        degenerate_ratio = _read_float(mesh, "degenerate_ratio", "[mesh]")
        degenerate_mode = _read_str(mesh, "degenerate", "[mesh]")
        mesh_name_prefixes = _read_strings(mesh, "name_prefixes", "[mesh]")
        max_texture_edge = _read_int(texture, "max_edge", "[texture]", minimum=1)
        require_power_of_two = _read_bool(texture, "power_of_two", "[texture]")
        enforce_texture_naming = _read_bool(texture, "naming", "[texture]")
        texture_suffixes = _read_strings(texture, "suffixes", "[texture]")
        naming_conventions = _read_naming(document.get("naming"), texture_suffixes,
                                          mesh_name_prefixes)

        # Reference / hierarchy auditing
        check_references = _read_bool(mesh, "check_references", "[mesh]")
        allow_outside_references = _read_bool(mesh, "allow_outside_references", "[mesh]")

        # PBR sanitisation
        pbr_check = _read_bool(texture, "pbr", "[texture]")
        pbr_floor = _read_float(texture, "pbr_floor", "[texture]")
        pbr_ceiling = _read_float(texture, "pbr_ceiling", "[texture]")
        pbr_suffixes = _read_strings(texture, "pbr_suffixes", "[texture]")

        # Geometric boundary profiling
        audit_transforms = _read_bool(mesh, "audit_transforms", "[mesh]")
        require_indexed = _read_bool(mesh, "require_indexed", "[mesh]")
        pivot_tolerance = _read_float(mesh, "pivot_tolerance", "[mesh]")
        transform_epsilon = _read_float(mesh, "transform_epsilon", "[mesh]")

        defaults = cls()
        return cls(
            max_triangles=defaults.max_triangles if max_triangles is None else max_triangles,
            max_texture_edge=defaults.max_texture_edge if max_texture_edge is None else max_texture_edge,
            max_material_slots=(
                defaults.max_material_slots if max_material_slots is None else max_material_slots
            ),
            require_power_of_two=(
                defaults.require_power_of_two if require_power_of_two is None else require_power_of_two
            ),
            enforce_texture_naming=(
                defaults.enforce_texture_naming if enforce_texture_naming is None else enforce_texture_naming
            ),
            texture_suffixes=defaults.texture_suffixes if texture_suffixes is None else texture_suffixes,
            max_bone_influences=(
                defaults.max_bone_influences if max_bone_influences is None else max_bone_influences
            ),
            max_uv_channels=defaults.max_uv_channels if max_uv_channels is None else max_uv_channels,
            allow_degenerate=defaults.allow_degenerate if allow_degenerate is None else allow_degenerate,
            degenerate_ratio=(
                defaults.degenerate_ratio if degenerate_ratio is None else degenerate_ratio),
            degenerate_mode=(defaults.degenerate_mode if degenerate_mode is None
                             else degenerate_mode.lower()),
            mesh_name_prefixes=(
                defaults.mesh_name_prefixes if mesh_name_prefixes is None else tuple(mesh_name_prefixes)),
            naming_conventions=naming_conventions,
            degenerate_epsilon=(
                defaults.degenerate_epsilon if degenerate_epsilon is None else degenerate_epsilon
            ),
            check_references=(
                defaults.check_references if check_references is None else check_references
            ),
            allow_outside_references=(
                defaults.allow_outside_references if allow_outside_references is None
                else allow_outside_references
            ),
            exclude=defaults.exclude if exclude is None else exclude,
            pbr_check=defaults.pbr_check if pbr_check is None else pbr_check,
            pbr_floor=defaults.pbr_floor if pbr_floor is None else pbr_floor,
            pbr_ceiling=defaults.pbr_ceiling if pbr_ceiling is None else pbr_ceiling,
            pbr_suffixes=defaults.pbr_suffixes if pbr_suffixes is None else pbr_suffixes,
            audit_transforms=(
                defaults.audit_transforms if audit_transforms is None else audit_transforms
            ),
            require_indexed=(
                defaults.require_indexed if require_indexed is None else require_indexed
            ),
            pivot_tolerance=(
                defaults.pivot_tolerance if pivot_tolerance is None else pivot_tolerance
            ),
            transform_epsilon=(
                defaults.transform_epsilon if transform_epsilon is None else transform_epsilon
            ),
            **memory,
            **structure,
            **rigging,
        )


# Bytes per pixel of the GPU texture formats a budget can name (before mips).
GPU_TEXTURE_FORMATS: dict[str, float] = {
    "bc1": 0.5, "bc3": 1.0, "bc4": 0.5, "bc5": 1.0, "bc6h": 1.0, "bc7": 1.0,
    "astc4x4": 1.0, "astc5x5": 0.64, "astc6x6": 0.444, "astc8x8": 0.25,
    "etc2": 0.5, "etc2a": 1.0, "rgba8": 4.0, "rgba16f": 8.0,
}
MIP_FACTOR = 4.0 / 3.0

#: [lightmap] limits: UV set indices engines read (Unreal: 8 sets, 0-7), the
#: finest overlap grid, and how far outside 0-1 a lightmap UV may sit (float noise
#: from an exporter, far below one texel of a 4096 lightmap).
LIGHTMAP_MAX_CHANNEL = 7
LIGHTMAP_MAX_RESOLUTION = 4096
LIGHTMAP_UV_TOLERANCE = 1e-4


def _read_memory(mesh: dict, texture: dict) -> dict:
    """The G2/G8 keys of [mesh]/[texture], only those present (the rest default)."""
    found: dict[str, object] = {}
    for table, key, field, where in ((texture, "max_mb", "max_texture_mb", "[texture]"),
                                     (mesh, "max_mb", "max_mesh_mb", "[mesh]")):
        value = _read_float(table, key, where)
        if value is not None:
            # not finite: nan compares False with every size, so the budget
            # silently checked nothing
            if not math.isfinite(value) or value < 0:
                raise ConfigError(f"{where}.{key} must be 0 (off) or more, got {value}")
            found[field] = value
    for key, field in (("format", "texture_format"), ("normal_format", "normal_format")):
        value = _read_str(texture, key, "[texture]")
        if value is not None:
            if value.lower() not in GPU_TEXTURE_FORMATS:
                raise ConfigError(f"[texture].{key} must be one of "
                                  f"{', '.join(GPU_TEXTURE_FORMATS)}, got {value!r}")
            found[field] = value.lower()
    if "texel_density" in mesh:
        value = mesh["texel_density"]
        if (not isinstance(value, (list, tuple)) or len(value) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0
                       or not math.isfinite(v) for v in value)
                or float(value[0]) > float(value[1]) or not value[1]):
            raise ConfigError("[mesh].texel_density must be [min, max] in pixels per metre, "
                              f"with min <= max, got {value!r}")
        found["texel_density_min"], found["texel_density_max"] = float(value[0]), float(value[1])
    texel_texture = _read_int(mesh, "texel_texture", "[mesh]", minimum=1)
    if texel_texture is not None:
        found["texel_texture"] = texel_texture
    for key, field in (("lightmap_uv", "mesh_lightmap_uv"),
                       ("depth_index_buffer", "mesh_depth_index_buffer")):
        value = _read_bool(mesh, key, "[mesh]")
        if value is not None:
            found[field] = value
    return found


LOD_KEYS = ("min_levels", "max_ratio")
COLLISION_KEYS = ("require", "name_must_match", "max_hulls", "max_hull_vertices", "convex")


def _read_structure(document: dict) -> dict:
    """[lod] and [collision], only the keys present (the rest default).

    A [collision] table switches on the two checks every Unreal team wants once
    it checks collision at all - names that match a render mesh, convex UCX_
    hulls - unless the table says ``false``. Budget() alone checks neither, so
    a rules file without the table judges exactly as before.
    """
    found: dict[str, object] = {}
    lod = document.get("lod")
    if lod is not None:
        if not isinstance(lod, dict):
            raise ConfigError("[lod] must be a table, such as [lod] min_levels = 3")
        _reject_unknown(lod, LOD_KEYS, "[lod] (known: " + ", ".join(LOD_KEYS) + ")")
        levels = _read_int(lod, "min_levels", "[lod]", minimum=0)
        if levels is not None:
            found["lod_min_levels"] = levels
        ratio = _read_float(lod, "max_ratio", "[lod]")
        if ratio is not None:
            found["lod_max_ratio"] = ratio          # range checked by Budget
    collision = document.get("collision")
    if collision is not None:
        if not isinstance(collision, dict):
            raise ConfigError("[collision] must be a table, such as [collision] require = true")
        _reject_unknown(collision, COLLISION_KEYS,
                        "[collision] (known: " + ", ".join(COLLISION_KEYS) + ")")
        for key, field, default in (("require", "collision_require", False),
                                    ("name_must_match", "collision_name_match", True),
                                    ("convex", "collision_convex", True)):
            value = _read_bool(collision, key, "[collision]")
            found[field] = default if value is None else value
        for key, field in (("max_hulls", "collision_max_hulls"),
                           ("max_hull_vertices", "collision_max_hull_vertices")):
            value = _read_int(collision, key, "[collision]", minimum=0)
            if value is not None:
                found[field] = value
    return found


def _read_skeleton_lightmap(document: dict) -> dict:
    """[skeleton] and [lightmap], only the keys present (the rest default to off).

        [skeleton]
        max_bones = 256              # 0 = off
        max_bones_per_section = 75   # 0 = off; Unreal's mobile cap
        max_depth = 0                # 0 = off
        single_root = true

        [lightmap]
        require = true               # static meshes need an authored lightmap UV set
        uv_channel = 1               # which set (0-7); 1 = the second, Unreal's convention
        inside_unit_square = true    # on by default once require or max_overlap is set
        max_overlap = 0.0            # percent of covered texels; absent = not checked
        resolution = 256             # the overlap grid, 16-4096
    """
    found: dict[str, object] = {}
    skeleton = document.get("skeleton", {})
    lightmap = document.get("lightmap", {})
    if not isinstance(skeleton, dict):
        raise ConfigError("[skeleton] must be a table")
    if not isinstance(lightmap, dict):
        raise ConfigError("[lightmap] must be a table")
    _reject_unknown(skeleton, ("max_bones", "max_bones_per_section", "max_depth", "single_root"),
                    "[skeleton]")
    _reject_unknown(lightmap, ("require", "uv_channel", "inside_unit_square", "max_overlap",
                               "resolution"), "[lightmap]")
    for key, field in (("max_bones", "max_bones"), ("max_bones_per_section", "max_bones_per_section"),
                       ("max_depth", "max_bone_depth")):
        value = _read_int(skeleton, key, "[skeleton]", minimum=0)
        if value is not None:
            found[field] = value
    single_root = _read_bool(skeleton, "single_root", "[skeleton]")
    if single_root is not None:
        found["single_root_bone"] = single_root

    require = _read_bool(lightmap, "require", "[lightmap]")
    if require is not None:
        found["lightmap_require"] = require
    channel = _read_int(lightmap, "uv_channel", "[lightmap]", minimum=0)
    if channel is not None:
        if channel > LIGHTMAP_MAX_CHANNEL:
            raise ConfigError(f"[lightmap].uv_channel must be 0-{LIGHTMAP_MAX_CHANNEL} (0 is the "
                              f"first UV set, 1 the second), got {channel}")
        found["lightmap_uv_channel"] = channel
    resolution = _read_int(lightmap, "resolution", "[lightmap]", minimum=16)
    if resolution is not None:
        if resolution > LIGHTMAP_MAX_RESOLUTION:
            raise ConfigError(f"[lightmap].resolution must be 16-{LIGHTMAP_MAX_RESOLUTION} "
                              f"texels, got {resolution}")
        found["lightmap_resolution"] = resolution
    overlap = _read_float(lightmap, "max_overlap", "[lightmap]")
    if overlap is not None:
        if not math.isfinite(overlap) or not 0.0 <= overlap <= 100.0:
            raise ConfigError("[lightmap].max_overlap must be a percentage from 0 to 100 "
                              "(0 = no overlap allowed); leave it out to skip the overlap "
                              f"check, got {overlap!r}")
        found["lightmap_max_overlap"] = overlap
    inside = _read_bool(lightmap, "inside_unit_square", "[lightmap]")
    if inside is None and (require or overlap is not None):
        inside = True               # a lightmap outside 0-1 is never right once you check
    if inside is not None:
        found["lightmap_inside_unit_square"] = inside
    return found


def is_normal_name(path: Path) -> bool:
    """Named as a normal map: these compress as BC5 / two-channel formats."""
    stem = path.stem.lower()
    return stem.endswith(("_normal", "_nrm", "_n")) or "_normal_" in stem


def texture_memory_bytes(width: int, height: int, format_name: str, mips: bool = True) -> int:
    return int(width * height * GPU_TEXTURE_FORMATS[format_name] * (MIP_FACTOR if mips else 1.0))


def mesh_vertex_size(uv_channels: int, skinned: bool, colours: bool = False) -> int:
    """Bytes per vertex, engine-style: float3 position, a packed tangent frame
    (8), half-float UVs (4 each), 8-bit colour, 4 bone indices + 4 weights."""
    return 12 + 8 + 4 * max(1, uv_channels) + (4 if colours else 0) + (8 if skinned else 0)


def mesh_memory_bytes(vertices: int, triangles: int, uv_channels: int, skinned: bool,
                      colours: bool = False, lightmap_uv: bool = False,
                      depth_index_buffer: bool = False) -> int:
    """Vertex and index buffers. ``lightmap_uv`` adds the UV set an engine
    generates for baked lighting; ``depth_index_buffer`` the second index
    buffer Unreal keeps for depth-only passes (both on in Unreal by default)."""
    index = 2 if vertices <= 65_535 else 4
    uv_sets = max(1, uv_channels) + (1 if lightmap_uv else 0)
    index_buffers = 2 if depth_index_buffer else 1
    return (vertices * mesh_vertex_size(uv_sets, skinned, colours)
            + triangles * 3 * index * index_buffers)


def _mb(value: int) -> str:
    if value < 1024 * 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


@dataclass(frozen=True)
class Verdict:
    """Result of inspecting one asset: zero reasons = passed."""

    path: Path
    problems: tuple[str, ...] = ()
    metric: str = ""
    # Structured equivalent of `metric` for machines: ("triangles", 4000), ...
    # Kept as a tuple so a Verdict stays immutable and hashable.
    stats: tuple[tuple[str, int], ...] = ()
    # Non-empty only when the asset was actively repaired and re-verified.
    heal_notes: tuple[str, ...] = ()
    # Non-fatal advisories: a fuzzy tag match, a guess a human should confirm.
    # Warnings never change `passed`.
    warnings: tuple[str, ...] = ()
    # The gate could not read the file at all - a locked handle, a permission the
    # account does not have, a reparse point gone stale. That is not the artist's
    # mesh being over budget, so it gets its own word: ERROR. It still fails the
    # run, because an asset nobody could inspect is an asset nobody can ship.
    unreadable: bool = False

    @property
    def passed(self) -> bool:
        return not self.problems and not self.unreadable

    @property
    def healed(self) -> bool:
        return bool(self.heal_notes)

    @property
    def status(self) -> str:
        """PASSED / HEALED / FAILED / ERROR - the word a dashboard branches on."""
        if self.unreadable:
            return "ERROR"
        if not self.passed:
            return "FAILED"
        return "HEALED" if self.healed else "PASSED"

    @property
    def reason(self) -> str:
        return "; ".join(self.problems)

    @property
    def tag(self) -> str:
        if self.passed:
            return f"PASSED WITH HEALING: {'; '.join(self.heal_notes)}" if self.healed else "PASSED"
        if self.unreadable:
            return f"ERROR: {self.reason}"
        return f"FAILED: {self.reason}"


class AssetError(Exception):
    """Raised when a file cannot be inspected at all (corrupt, unreadable, ...)."""


class ConfigError(Exception):
    """Raised when a rules file exists but cannot be used."""


# --- #
# Rules file: optional per-folder configuration (``rules.toml``)
# --- #

def _reject_unknown(table: dict, allowed: Sequence[str], where: str) -> None:
    """A silently ignored typo in a budget file is worse than a hard failure."""
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def _read_int(table: dict, key: str, where: str, minimum: int = 1) -> Optional[int]:
    if key not in table:
        return None
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key} must be an integer, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{where}.{key} must be at least {minimum}, got {value}")
    return value


def _read_bool(table: dict, key: str, where: str) -> Optional[bool]:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key} must be true or false, got {value!r}")
    return value


def _read_float(table: dict, key: str, where: str) -> Optional[float]:
    if key not in table:
        return None
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key} must be a number, got {value!r}")
    return float(value)


def _read_strings(table: dict, key: str, where: str) -> Optional[tuple[str, ...]]:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where}.{key} must be a list of strings, got {value!r}")
    return tuple(value)


def _read_naming(table, texture_suffixes, mesh_prefixes) -> tuple:
    """[naming]: presets plus one custom convention, as Budget.naming_conventions.

        [naming]
        preset = ["unreal", "substance"]      # any of NAMING_PRESETS
        texture_prefixes = ["TX_"]             # a custom convention, added to them
        texture_suffixes = ["_Diff", "_Nrm"]
        texture_pattern = "^TX_[A-Z][A-Za-z0-9]+_(Diff|Nrm)$"   # a regex, whole name
        mesh_prefixes = ["GEO_"]
        mesh_pattern = "^GEO_[a-z0-9_]+$"
    """
    if table is None:
        return ()
    if not isinstance(table, dict):
        raise ConfigError("[naming] must be a table")
    _reject_unknown(table, ("preset", "texture_prefixes", "texture_suffixes", "texture_pattern",
                            "mesh_prefixes", "mesh_pattern"), "[naming]")
    presets = table.get("preset", [])
    if isinstance(presets, str):
        presets = [presets]
    if not isinstance(presets, list) or not all(isinstance(item, str) for item in presets):
        raise ConfigError(f"[naming].preset must be a name or a list of names, got {presets!r}")
    conventions = []
    for name in presets:
        key = name.strip().lower()
        if key not in NAMING_PRESETS:
            raise ConfigError(f"[naming].preset {name!r} is not one of: "
                              + ", ".join(sorted(NAMING_PRESETS)))
        preset = NAMING_PRESETS[key]
        conventions.append((key, preset.get("texture_prefixes", ()), preset.get("texture_suffixes", ()),
                            "", preset.get("mesh_prefixes", ()), ""))
    custom = {key: _read_strings(table, key, "[naming]") or ()
              for key in ("texture_prefixes", "texture_suffixes", "mesh_prefixes")}
    patterns = {}
    for key in ("texture_pattern", "mesh_pattern"):
        pattern = _read_str(table, key, "[naming]") or ""
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(f"[naming].{key} is not a valid regular expression: {exc}") from exc
        patterns[key] = pattern
    if any(custom.values()) or any(patterns.values()):
        conventions.append(("custom", custom["texture_prefixes"], custom["texture_suffixes"],
                            patterns["texture_pattern"], custom["mesh_prefixes"],
                            patterns["mesh_pattern"]))
    if texture_suffixes or mesh_prefixes:
        # [texture] suffixes / [mesh] name_prefixes still count, as one more convention.
        conventions.append(("rules", (), tuple(texture_suffixes or ()), "",
                            tuple(mesh_prefixes or ()), ""))
    return tuple(conventions)


def _read_str(table: dict, key: str, where: str) -> Optional[str]:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key} must be a non-empty string, got {value!r}")
    return value.strip()


# The keys a platform profile may override. Deliberately a subset: a profile is
# a *compression* preset (what this platform can afford), not a place to switch
# auditing rules off per platform.
PLATFORM_KEYS = ("max_triangles", "max_material_slots", "max_texture_edge",
                 "max_bone_influences", "max_uv_channels")

# A second, narrower whitelist for *physical* strictness, expressed as nested
# tables so the flat namespace stays budget-only:
#
#     [platforms.mobile.texture]
#     pbr_floor = 0.05        # mobile screens are seen in daylight; be stricter
#
# Deliberately excludes every on/off switch (check_references, audit_transforms,
# require_indexed, pbr, naming). A profile may tighten a threshold; it may never
# make a check disappear - that would let one platform ship unaudited assets.
PLATFORM_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "mesh": ("pivot_tolerance", "transform_epsilon", "max_mb"),
    "texture": ("pbr_floor", "pbr_ceiling", "max_mb", "format", "normal_format"),
    # [platforms.mobile.skeleton] max_bones_per_section = 75: the bone limits
    # are what differ between targets.
    "skeleton": ("max_bones", "max_bones_per_section", "max_depth"),
    # [platforms.mobile.lod] min_levels = 4, [tags.ui.lod] min_levels = 1: how
    # many levels a target needs, and how much each must save, differ by target.
    "lod": ("min_levels", "max_ratio"),
    # The hull limits are numbers a target can tighten; require, name matching
    # and convexity are switches and stay project-wide.
    "collision": ("max_hulls", "max_hull_vertices"),
}


def apply_platform(document: dict, platform: str) -> dict:
    """Overlay ``[platforms.<name>]`` onto the base budget, returning a new document.

    The profile wins over the flat budget (a mobile build must be smaller than
    the project default) and loses to an explicit CLI flag, which is applied
    later in :func:`resolve_budget`.
    """
    if not platform:
        return document
    platforms = document.get("platforms", {})
    if not isinstance(platforms, dict):
        raise ConfigError("[platforms] must be a table of tables")
    if platform not in platforms:
        known = ", ".join(sorted(platforms)) or "none defined"
        raise ConfigError(f"unknown platform profile {platform!r} (known: {known})")

    profile = platforms[platform]
    if not isinstance(profile, dict):
        raise ConfigError(f"[platforms.{platform}] must be a table")
    return _overlay_profile(document, profile, f"[platforms.{platform}]",
                            keep_tag_tables=True)


# --- #
# Shipped presets and tag-based profiles
# --- #

# A preset is a known--good starting point that lives in code, so a project file
# cannot quietly degrade it.
#
# Two shapes live side by side on purpose:
#
#   "profile"   budget numbers only (PLATFORM_KEYS). This is what `--preset`
#               overlays, so the CLI keeps its one safety property: a preset may
#               tighten a limit, never switch a check off.
#   "document"  a full rules fragment - budget numbers *plus* texture policy and
#               [tags.character] / [tags.prop]. This is what the admin GUI writes
#               into rules.toml, where those keys are legal and visible in git.
#
# The numbers are defensible starting points for each machine, not law. A studio
# that knows its own frame budget should edit them - that is what the admin page
# exists for.

def _preset(label: str, notes: str, *, character: int, prop: int, texture: int,
            slots: int, power_of_two: bool, pbr: bool, uv: int, influences: int,
            naming: bool = False, legacy: bool = False) -> dict[str, object]:
    return {
        "label": label,
        "notes": notes,
        "legacy": legacy,
        "profile": {
            "max_triangles": character,
            "max_material_slots": slots,
            "max_texture_edge": texture,
            "max_uv_channels": uv,
            "max_bone_influences": influences,
        },
        "document": {
            "mesh": {
                "max_triangles": character,
                "max_material_slots": slots,
                "max_uv_channels": uv,
                "max_bone_influences": influences,
            },
            "texture": {
                "max_edge": texture,
                "power_of_two": power_of_two,
                "pbr": pbr,
                "naming": naming,
            },
            "tags": {
                "character": {"max_triangles": character},
                "prop": {"max_triangles": prop},
            },
        },
    }


PLATFORM_PRESETS: dict[str, dict[str, object]] = {
    "n64": _preset("Nintendo 64", "4 MB RDRAM, 4 KB TMEM, no programmable shading",
                    character=1_200, prop=400, texture=64, slots=2, power_of_two=True,
                    pbr=False, uv=1, influences=1),
    "ps1": _preset("Sony PlayStation", "1 MB VRAM, affine texture mapping",
                    character=1_100, prop=350, texture=256, slots=1, power_of_two=True,
                    pbr=False, uv=1, influences=1),
    "nds": _preset("Nintendo DS", "656 KB VRAM, 2,048 triangles per frame",
                    character=900, prop=300, texture=128, slots=2, power_of_two=True,
                    pbr=False, uv=1, influences=1),
    "gamecube": _preset("Nintendo GameCube", "24 MB 1T-SRAM, fixed-function T&L",
                         character=6_000, prop=2_000, texture=512, slots=4,
                         power_of_two=True, pbr=False, uv=2, influences=4),
    "xbox": _preset("Microsoft Xbox (2001)", "64 MB DDR, programmable vertex shaders",
                     character=9_000, prop=3_000, texture=1_024, slots=4,
                     power_of_two=True, pbr=False, uv=2, influences=4),
    "ps2": _preset("Sony PlayStation 2", "4 MB VRAM, 2 MB VU1 scratchpad",
                    character=5_000, prop=1_800, texture=256, slots=3, power_of_two=True,
                    pbr=False, uv=2, influences=4),
    "n3ds": _preset("Nintendo 3DS", "6 MB VRAM, stereoscopic: draw everything twice",
                     character=3_000, prop=1_200, texture=256, slots=2,
                     power_of_two=True, pbr=False, uv=2, influences=4),
    "wii": _preset("Nintendo Wii", "24 MB 1T-SRAM + 64 MB GDDR3, fixed-function era",
                    character=5_000, prop=2_000, texture=512, slots=3, power_of_two=True,
                    pbr=False, uv=2, influences=4),
    "ps3": _preset("Sony PlayStation 3", "256 MB RSX, specular/gloss workflow, no PBR maps",
                    character=20_000, prop=8_000, texture=1_024, slots=4,
                    power_of_two=False, pbr=False, uv=2, influences=8),
    # Added 11 Nov 2026 (the rules wizard borrowed ps3 / mobile / the defaults
    # for these). Starting points, as everything here: Switch from typical
    # third-party character budgets on a Tegra X1 with 4 GB shared memory; VR
    # from Meta's guidance for Quest 2/Pro of 750k-1M triangles and 80-200 draw
    # calls a frame, drawn for two eyes at 72-90 Hz; mid-range from the
    # PS4 / Xbox One generation.
    "switch": _preset("Nintendo Switch-class handheld",
                       "4 GB shared memory, Tegra X1, ASTC textures, docked 1080p / handheld 720p",
                       character=20_000, prop=5_000, texture=1_024, slots=4,
                       power_of_two=False, pbr=True, uv=2, influences=4),
    "vr": _preset("Standalone VR (Quest-class)",
                   "750k-1M triangles and 80-200 draw calls a frame for two eyes at 72-90 Hz",
                   character=15_000, prop=3_000, texture=2_048, slots=2,
                   power_of_two=True, pbr=True, uv=2, influences=4),
    "midrange": _preset("Mid-range PC / last-gen console",
                         "PS4 / Xbox One generation: 5-8 GB shared memory, PBR with mip chains",
                         character=50_000, prop=10_000, texture=2_048, slots=6,
                         power_of_two=False, pbr=True, uv=3, influences=8, naming=True),
    "pc": _preset("Modern PC", "8 GB+ VRAM, PBR metal/roughness with mip chains",
                   character=120_000, prop=30_000, texture=4_096, slots=8,
                   power_of_two=False, pbr=True, uv=4, influences=8, naming=True),
    # Legacy names, kept so existing scripts and rules files keep working.
    "retro": _preset("Generic retro (legacy alias)", "Use n64 / ps1 / nds instead",
                      character=1_500, prop=500, texture=256, slots=1, power_of_two=True,
                      pbr=False, uv=1, influences=1, legacy=True),
    "mobile": _preset("Generic mobile (legacy alias)", "Use n3ds or a phone profile",
                       character=15_000, prop=6_000, texture=1_024, slots=2,
                       power_of_two=True, pbr=True, uv=2, influences=4, legacy=True),
}


#: Flat budget profiles - what `--preset` overlays. Derived so there is one
#: catalogue, not two that can disagree.
PRESETS: dict[str, dict[str, object]] = {
    name: dict(entry["profile"])          # type: ignore[arg-type]
    for name, entry in PLATFORM_PRESETS.items()
}


def preset_names(include_legacy: bool = True) -> list[str]:
    """Preset keys, hardware targets first, legacy aliases last."""
    names = [name for name, entry in PLATFORM_PRESETS.items()
             if include_legacy or not entry["legacy"]]
    return sorted(names, key=lambda name: (bool(PLATFORM_PRESETS[name]["legacy"]), name))


def preset_document(name: str) -> dict[str, object]:
    """A full rules fragment for a platform target (deep copy, safe to mutate).

    This is what the admin GUI writes into rules.toml: budget numbers, texture
    policy and [tags.character]/[tags.prop], all as plain text in the repository.
    """
    key = (name or "").strip().lower()
    entry = PLATFORM_PRESETS.get(key)
    if entry is None:
        raise ConfigError(f"unknown preset '{name}' (known: {', '.join(preset_names())})")
    import copy

    return copy.deepcopy(entry["document"])  # type: ignore[return-value]


def preset_summary() -> list[dict[str, object]]:
    """What the CLI and the admin dropdown need to describe each target."""
    rows: list[dict[str, object]] = []
    for name in preset_names():
        entry = PLATFORM_PRESETS[name]
        document = entry["document"]  # type: ignore[index]
        texture = dict(document["texture"])  # type: ignore[index]
        tags = dict(document["tags"])  # type: ignore[index]
        rows.append({
            "name": name,
            "label": entry["label"],
            "notes": entry["notes"],
            "legacy": entry["legacy"],
            "character_triangles": tags["character"]["max_triangles"],
            "prop_triangles": tags["prop"]["max_triangles"],
            "max_texture_edge": texture["max_edge"],
            "max_material_slots": document["mesh"]["max_material_slots"],  # type: ignore[index]
            "power_of_two": texture["power_of_two"],
            "pbr": texture["pbr"],
        })
    return rows

TAG_MODES = ("auto", "suggest", "strict")
DEFAULT_TAG_SIMILARITY = 0.75


def apply_preset(document: dict, name: Optional[str]) -> dict:
    key = (name or "").strip().lower()
    if not key:
        return document
    profile = PRESETS.get(key)
    if profile is None:
        raise ConfigError(f"unknown preset '{name}' (known: {', '.join(sorted(PRESETS))})")
    return _overlay_profile(document, dict(profile), f"preset '{key}'",
                            keep_tag_tables=True)


def normalise_tag(value: object) -> str:
    """Tags are folder names written by humans: `Monsters_Boss` == `monsters boss`."""
    return re.sub(r"[\s_\-]+", " ", str(value or "").strip().lower()).strip()


def build_tag_budgets(document: dict) -> dict[str, Budget]:
    """Compile `[tags.<name>]` into ready-to-use budgets, keyed by normalised tag."""
    tags = document.get("tags") or {}
    if not isinstance(tags, dict):
        raise ConfigError("[tags] must be a table of tables")
    budgets: dict[str, Budget] = {}
    for name, table in tags.items():
        if not isinstance(table, dict):
            raise ConfigError(f"[tags.{name}] must be a table")
        budgets[normalise_tag(name)] = Budget.from_mapping(
            _overlay_profile(document, table, f"[tags.{name}]"))
    return budgets


def tag_candidates(path: Path, root: Optional[Path] = None) -> list[str]:
    """Names an asset could be tagged with, most specific first.

    The filename prefix (`Monsters_Boss_01.gltf` -> `Monsters`) catches the
    per-asset convention; the folder chain catches the drop-folder convention.
    """
    candidates: list[str] = []
    parts = [part for part in re.split(r"[ _\-]+", path.stem) if part]
    if parts:
        candidates.append(parts[0])
        candidates.append(path.stem)
    parent = path.parent
    for _ in range(4):
        if parent is None or (root is not None and parent == root):
            break
        candidates.append(parent.name)
        parent = parent.parent
    return [candidate for candidate in candidates if candidate]


def resolve_tag(path: Path, budgets: dict[str, Budget], root: Optional[Path] = None,
                similarity: float = DEFAULT_TAG_SIMILARITY) -> tuple[Optional[str], Optional[str]]:
    """Resolve an asset to a tag key. Returns (tag, warning).

    Exact matches win outright, including case and separator differences. A near
    miss is deliberately *not* dropped: falling back to the global budget on a
    typo is the failure this feature exists to prevent, so it resolves to the
    closest tag and says so.
    """
    if not budgets:
        return None, None
    normalised = {key: normalise_tag(key) for key in budgets}
    candidates = tag_candidates(path, root)

    for candidate in candidates:
        norm = normalise_tag(candidate)
        if norm in normalised.values():
            for key, value in normalised.items():
                if value == norm:
                    return key, None

    for candidate in candidates:
        norm = normalise_tag(candidate)
        if not norm:
            continue
        close = difflib.get_close_matches(norm, list(normalised.values()), n=1,
                                          cutoff=similarity)
        if close:
            for key, value in normalised.items():
                if value == close[0]:
                    return key, (f"tag '{candidate}' is not a known tag; "
                                 f"closest match is '{key}'")
    return None, None


def _overlay_profile(document: dict, profile: dict, where: str,
                     keep_tag_tables: bool = False) -> dict:
    """Merge one budget profile into a rules document, or refuse it loudly.

    Used by `[platforms.*]`, `[tags.*]` and the shipped presets - one merge
    implementation means one set of rules about what a profile may say, so a tag
    cannot do something a platform could not.
    """
    _reject_unknown(profile, (*PLATFORM_KEYS, *PLATFORM_SECTION_KEYS), where)

    # A *tag* profile must not inherit sibling tag tables, so they are dropped by
    # default. A platform or preset overlay is a different question - it refines
    # the base budget, and dropping [tags] there silently disabled every tag
    # budget in the project (found by tests/test_server.py: /api/rules showed an
    # empty tag list whenever [platforms.<name>] was in play).
    dropped = ["platforms", "platform", "preset"]
    if not keep_tag_tables:
        dropped += ["tags", "tagging"]
    merged = {key: value for key, value in document.items() if key not in dropped}
    merged["mesh"] = dict(document.get("mesh", {}) or {})
    merged["texture"] = dict(document.get("texture", {}) or {})
    for key, value in profile.items():
        if isinstance(value, dict):
            # A nested table is a strictness override, validated by the second
            # whitelist. Unknown sections or keys are refused loudly: silently
            # ignoring a typo here would mean shipping an unaudited platform.
            allowed = PLATFORM_SECTION_KEYS.get(key)
            if allowed is None:
                raise ConfigError(
                    f"{where}.{key} is not a known section "
                    f"(known: {', '.join(sorted(PLATFORM_SECTION_KEYS))})")
            _reject_unknown(value, allowed, f"{where}.{key}")
            base = merged.get(key) or {}
            if not isinstance(base, dict):
                raise ConfigError(f"[{key}] must be a table")
            merged[key] = {**base, **value}     # a copy: never edit the caller's document
        elif key == "max_texture_edge":
            merged["texture"]["max_edge"] = value
        else:
            merged["mesh"][key] = value
    return merged


def parse_rules(path: Path) -> dict:
    """Parse a rules file: TOML via tomllib/tomli, or plain JSON.

    Python 3.11+ reads TOML natively. On older interpreters we use ``tomli`` if it
    is installed, and otherwise accept the same structure written as JSON, so the
    tool stays runnable on 3.9/3.10 without extra wheels.
    """
    # utf-8-sig strips a leading BOM, which Notepad and PowerShell add by default
    # on Windows; tomllib would otherwise reject the file on its very first byte.
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        # Notepad's "ANSI" (Windows-1252) turns an accent in a comment into a
        # byte that is not UTF-8; the bare codec error did not say what to do.
        line = path.read_bytes()[:exc.start].count(b"\n") + 1
        raise ConfigError(f"{path.name} is not saved as UTF-8 (byte 0x{exc.object[exc.start]:02x} "
                          f"on line {line}): save it as UTF-8 (Notepad: Save As, Encoding "
                          "UTF-8)") from exc
    if path.suffix.lower() == ".json":
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ConfigError(f"{path.name}: {exc}") from exc

    if tomllib is not None:
        try:
            return tomllib.loads(text)
        except ValueError as exc:      # TOMLDecodeError subclasses ValueError
            raise ConfigError(f"{path.name}: {exc}") from exc

    try:
        import tomli  # type: ignore[import-not-found]  # Python 3.9 / 3.10
    except ModuleNotFoundError:
        if text.lstrip().startswith("{"):
            return json.loads(text)
        raise ConfigError(
            "reading TOML needs Python 3.11+ (tomllib) or `pip install tomli`; "
            "a JSON rules file is also accepted"
        ) from None
    return tomli.loads(text)


def load_rules(path: Optional[Path]) -> Optional[tuple[Budget, Path]]:
    """Load ``rules.toml`` if it exists. Missing file is not an error."""
    if path is None or not path.is_file():
        return None
    try:
        return Budget.from_mapping(parse_rules(path)), path
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    except (ValueError, OSError) as exc:  # malformed TOML/JSON, unreadable file
        raise ConfigError(f"{path}: {exc}") from exc


# --- #
# Expiring exceptions (G1): "this hero asset may use 80,000 triangles until
# alpha", without loosening the rule for everyone.
# --- #

# The Budget fields an exception may relax, and the type its "allow" must be.
EXCEPTION_RULES: dict[str, type] = {
    "max_triangles": int, "max_texture_edge": int, "max_material_slots": int,
    "max_bone_influences": int, "max_uv_channels": int,
    "require_power_of_two": bool, "enforce_texture_naming": bool,
    "allow_degenerate": bool, "check_references": bool, "pbr_check": bool,
    "max_texture_mb": float, "max_mesh_mb": float,
    "lod_min_levels": int, "collision_require": bool,
    "max_bones": int, "max_bones_per_section": int,
}
EXCEPTION_WARN_DAYS = 14


@dataclass(frozen=True)
class BudgetException:
    """One named, owned, dated relaxation of one rule for some paths."""

    path: str
    rule: str
    allow: object
    reason: str
    owner: str
    expires: date
    base: Optional[Path] = None      # what ``path`` is relative to (the rules file's folder)

    def matches(self, asset: Path) -> bool:
        """Case-insensitive glob against the path below ``base`` (``*`` crosses folders),
        or against the file name alone when the pattern has no folder in it."""
        pattern = self.path.replace("\\", "/").lower().removeprefix("./").lstrip("/")
        relative = asset.name
        if self.base is not None:
            try:
                relative = asset.resolve().relative_to(self.base.resolve()).as_posix()
            except (ValueError, OSError):
                relative = asset.name
        candidates = [relative.lower()]
        if "/" not in pattern:
            candidates.append(asset.name.lower())
        return any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates)

    def active(self, today: date) -> bool:
        return today <= self.expires

    def label(self) -> str:
        value = f"{self.allow:,}" if isinstance(self.allow, int) and not isinstance(self.allow, bool) \
            else str(self.allow).lower()
        return f"{self.rule} = {value} for {self.path}"


def _read_date(value: object, where: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise ConfigError(f"{where}.expires must be a date like 2026-12-01, got {value!r}")


def parse_exceptions(document: Optional[dict], base: Optional[Path] = None,
                     where: str = "[[exceptions]]") -> tuple[BudgetException, ...]:
    """Validate every exception loudly: one without a reason, an owner or an end
    date is how a temporary waiver becomes the real budget."""
    if not document:
        return ()
    entries = document.get("exceptions", [])
    if entries in (None, []):
        return ()
    if not isinstance(entries, list):
        raise ConfigError(f"{where} must be a list of tables ([[exceptions]] in TOML)")
    found = []
    for index, entry in enumerate(entries, 1):
        label = f"{where} #{index}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{label} must be a table")
        _reject_unknown(entry, ("path", "rule", "allow", "reason", "owner", "expires"), label)
        missing = [key for key in ("path", "rule", "allow", "reason", "owner", "expires")
                   if key not in entry or entry[key] in ("", None)]
        if missing:
            raise ConfigError(f"{label} needs {', '.join(missing)} (every exception says what, "
                              "why, who owns it and when it ends)")
        rule = str(entry["rule"]).strip()
        if rule not in EXCEPTION_RULES:
            raise ConfigError(f"{label}.rule must be one of {', '.join(sorted(EXCEPTION_RULES))}, "
                              f"got {rule!r}")
        kind = EXCEPTION_RULES[rule]
        allow = entry["allow"]
        if kind is int and (isinstance(allow, bool) or not isinstance(allow, int) or allow < 0):
            raise ConfigError(f"{label}.allow must be a whole number for {rule}, got {allow!r}")
        if kind is bool and not isinstance(allow, bool):
            raise ConfigError(f"{label}.allow must be true or false for {rule}, got {allow!r}")
        if kind is float:
            if isinstance(allow, bool) or not isinstance(allow, (int, float)) or allow < 0 \
                    or not math.isfinite(allow):
                raise ConfigError(f"{label}.allow must be a number of MB for {rule}, got {allow!r}")
            allow = float(allow)
        try:
            # The value must make a budget: max_texture_edge = 0 used to pass here
            # and then stop the run with a traceback on the first matching asset.
            replace(Budget(), **{rule: allow})
        except ConfigError as exc:
            raise ConfigError(f"{label}.allow = {allow!r} cannot be used for {rule}: "
                              f"{str(exc).replace('budget ', '', 1)}") from exc
        found.append(BudgetException(
            path=str(entry["path"]).strip(), rule=rule, allow=allow,
            reason=str(entry["reason"]).strip()[:300], owner=str(entry["owner"]).strip()[:80],
            expires=_read_date(entry["expires"], label), base=base))
    return tuple(found)


def resolve_exceptions(root: Path, args: argparse.Namespace,
                       remote_document: Optional[dict] = None) -> tuple[BudgetException, ...]:
    """Exceptions from the same document the budget came from. Paths in a rules
    file are relative to the folder holding it; remote ones to the watch folder."""
    if remote_document is not None:
        try:
            return parse_exceptions(remote_document, base=root)
        except ConfigError as exc:
            raise ConfigError(f"remote rules: {exc}") from exc
    rules_path = Path(args.rules).expanduser() if getattr(args, "rules", None) else root / RULES_FILENAME
    if not rules_path.is_file():
        return ()
    try:
        return parse_exceptions(parse_rules(rules_path), base=rules_path.parent.resolve())
    except ConfigError as exc:
        raise ConfigError(f"{rules_path}: {exc}") from exc


@dataclass(frozen=True)
class MemoryTotal:
    """A memory budget for everything under some paths: "Props/* <= 300 MB"."""

    path: str
    max_mb: float
    base: Optional[Path] = None

    def matches(self, asset: Path) -> bool:
        return BudgetException(self.path, "max_triangles", 0, "", "", date.max,
                               self.base).matches(asset)


def parse_totals(document: Optional[dict], base: Optional[Path] = None) -> tuple[MemoryTotal, ...]:
    if not document or document.get("totals") in (None, []):
        return ()
    entries = document["totals"]
    if not isinstance(entries, list):
        raise ConfigError("[[totals]] must be a list of tables")
    found = []
    for index, entry in enumerate(entries, 1):
        where = f"[[totals]] #{index}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a table")
        _reject_unknown(entry, ("path", "max_mb"), where)
        if not str(entry.get("path", "")).strip():
            raise ConfigError(f"{where} needs path")
        limit = _read_float(entry, "max_mb", where)
        if limit is None or not math.isfinite(limit) or limit <= 0:
            raise ConfigError(f"{where} needs max_mb above 0 (a number of MB)"
                              + (f", got {limit}" if limit is not None else ""))
        found.append(MemoryTotal(str(entry["path"]).strip(), limit, base))
    return tuple(found)


def resolve_totals(root: Path, args: argparse.Namespace,
                   remote_document: Optional[dict] = None) -> tuple[MemoryTotal, ...]:
    if remote_document is not None:
        return parse_totals(remote_document, base=root)
    rules_path = Path(args.rules).expanduser() if getattr(args, "rules", None) else root / RULES_FILENAME
    if not rules_path.is_file():
        return ()
    try:
        return parse_totals(parse_rules(rules_path), base=rules_path.parent.resolve())
    except ConfigError as exc:
        raise ConfigError(f"{rules_path}: {exc}") from exc


def check_totals(totals: Sequence[MemoryTotal], verdicts: Sequence[Verdict]) -> list[Verdict]:
    """One failing verdict per total that is over its budget."""
    failed = []
    for total in totals:
        members = [verdict for verdict in verdicts if total.matches(verdict.path)]
        used = sum(int(dict(verdict.stats).get("memory_kb", 0)) for verdict in members) * 1024
        limit = int(total.max_mb * 1024 * 1024)
        # An asset with no estimate (a mesh that could not be read or measured)
        # must not count as 0 MB: a total that silently leaves it out can pass a
        # folder that is over. Such a total fails, naming them.
        unmeasured = [verdict for verdict in members
                      if "memory_kb" not in dict(verdict.stats)]
        LOG.info("total   %s: ~%s of %g MB (%d asset%s%s)", total.path, _mb(used), total.max_mb,
                 len(members), "" if len(members) == 1 else "s",
                 f", {len(unmeasured)} not measured" if unmeasured else "")
        if unmeasured:
            names = ", ".join(verdict.path.name for verdict in unmeasured[:5])
            more = f" and {len(unmeasured) - 5} more" if len(unmeasured) > 5 else ""
            failed.append(Verdict(Path(f"[total] {total.path}"),
                                  (f"cannot check the {total.max_mb:g} MB budget for {total.path}: "
                                   f"{len(unmeasured)} asset(s) have no memory estimate "
                                   f"({names}{more}; its warning says why) - "
                                   "fix or re-export them, or narrow the path",),
                                  f"~{_mb(used)}+?", (("memory_kb", used // 1024),)))
            continue
        if used > limit:
            failed.append(Verdict(Path(f"[total] {total.path}"),
                                  (f"~{_mb(used)} estimated GPU memory across {len(members)} "
                                   f"assets exceeds the {total.max_mb:g} MB budget for "
                                   f"{total.path}",),
                                  f"~{_mb(used)}", (("memory_kb", used // 1024),)))
    return failed


def resolve_budget(root: Path, args: argparse.Namespace,
                   remote_document: Optional[dict] = None) -> tuple[Budget, Optional[Path], list[str]]:
    """Decide the effective budget.

    Precedence: explicit CLI flag > remote rules > ``rules.toml`` > shipped
    defaults. The local file keeps the artist's folder authoritative for everyday
    work; CI tightens a single value without editing a file artists own; and a
    studio endpoint overrides both when a project is under central control.

    ``remote_document`` is the already-fetched central profile (or None).  It is
    passed in rather than fetched here so this function stays pure: no sockets,
    no filesystem surprises, and the same code path in tests and in the field.
    """
    requested_platform = (getattr(args, "platform", None) or "").strip()

    requested_preset = (getattr(args, "preset", None) or "").strip()

    # A remote endpoint can return anything: a bare list, a string, an HTML error
    # page parsed as JSON. Indexing it produced an AttributeError traceback
    # instead of the usual exit 2 with the key named.
    if remote_document is not None and not isinstance(remote_document, dict):
        raise ConfigError("remote rules: expected a table of settings, got "
                          f"{type(remote_document).__name__}")

    if remote_document is not None:
        platform = requested_platform or str(remote_document.get("platform", "") or "")
        try:
            document = apply_preset(
                apply_platform(remote_document, platform),
                requested_preset or str(remote_document.get("preset", "") or ""))
            budget, source = Budget.from_mapping(document), None
        except ConfigError as exc:
            raise ConfigError(f"remote rules: {exc}") from exc
    else:
        rules_path = Path(args.rules).expanduser() if args.rules else root / RULES_FILENAME
        document = parse_rules(rules_path) if rules_path.is_file() else None
        # Start from an empty document when the project published no rules file,
        # so a shipped profile means the same thing with or without one. It used
        # to be dropped in that case: `asset-gatekeeper <folder> --preset retro`
        # quietly kept the 50k default, which is the first command in the manual
        # and the one a new studio tries first.
        base = document if document is not None else {}
        platform = requested_platform or str(base.get("platform", "") or "")
        preset = requested_preset or str(base.get("preset", "") or "")
        if platform and not (base.get("platforms") or {}):
            # "known: none defined" is true but useless: with no rules file the
            # shipped presets are what the caller almost certainly meant, and a
            # platform name that matches nothing must never pass silently.
            raise ConfigError(
                f"unknown platform profile {platform!r}: this project defines no "
                f"[platforms.*] tables. Use --preset {platform!r} for a shipped budget "
                f"({', '.join(preset_names(include_legacy=False))}).")
        try:
            budget = Budget.from_mapping(apply_preset(apply_platform(base, platform), preset))
            source = rules_path if document is not None else None
        except ConfigError as exc:
            raise ConfigError(f"{rules_path}: {exc}" if document is not None else str(exc)) from exc

    budget = replace(budget, platform=platform)

    overrides: dict[str, object] = {}
    if args.triangles is not None:
        overrides["max_triangles"] = args.triangles
    if args.max_texture is not None:
        overrides["max_texture_edge"] = args.max_texture
    if args.max_materials is not None:
        overrides["max_material_slots"] = args.max_materials
    if args.require_power_of_two is not None:
        overrides["require_power_of_two"] = args.require_power_of_two
    if args.enforce_texture_naming is not None:
        overrides["enforce_texture_naming"] = args.enforce_texture_naming
    if args.max_influences is not None:
        overrides["max_bone_influences"] = args.max_influences
    if args.max_uvs is not None:
        overrides["max_uv_channels"] = args.max_uvs
    if args.allow_degenerate is not None:
        overrides["allow_degenerate"] = args.allow_degenerate

    # Tri-state switches: None means "the config decides", True/False means the
    # command line was explicit. False must still be recorded, or --no-pbr-check
    # could not override a project that enabled the check.
    for attribute, field in (("check_references", "check_references"),
                             ("pbr_check", "pbr_check"),
                             ("audit_transforms", "audit_transforms"),
                             ("require_indexed", "require_indexed")):
        value = getattr(args, attribute, None)
        if value is not None:
            overrides[field] = value
    if getattr(args, "exclude", None):
        overrides["exclude"] = tuple(args.exclude)
    if getattr(args, "require_indexed", None):
        # Asking for an index audit without the transform audit would silently
        # skip the pivot checks that make the audit useful.
        overrides["audit_transforms"] = True

    if not overrides:
        return budget, source, {}
    # The dict, not its sorted keys: the caller passes this to Gatekeeper, which
    # applies it to the per-tag budgets with replace(**overrides). Returning the
    # key list turned every numeric flag into
    # "ValueError: dictionary update sequence element #0 has length 13".
    return replace(budget, **overrides), source, overrides  # type: ignore[arg-type]


# --- #
# `--init`: the five-question setup wizard
# --- #

PLATFORM_CHOICES = ("none", "mobile", "console", "pc")

RULES_TEMPLATE = """\
# rules.toml - created by `asset-gatekeeper --init` on {date}
#
# Precedence: explicit CLI flag > remote rules > [platforms.*] > this file > defaults.
{platform_line}
[mesh]
max_triangles = {max_triangles}
max_material_slots = {max_material_slots}
check_references = true

[texture]
max_edge = {max_edge}
power_of_two = true
naming = true
pbr = true

[healer]
# Off by default: the gate fails and locks. On, it renames PNGs missing a
# suffix, resizes non-power-of-two PNGs and strips zero-area .obj faces - each
# verified before adoption, the original kept in .agk-backups/ (undo with
# --undo-heal). To fix one asset instead: asset-gatekeeper <file> --heal
enabled = {auto_heal}

[platforms.mobile]
max_triangles = {mobile_triangles}
max_material_slots = 2
max_texture_edge = 1024

[platforms.console]
max_triangles = 250000
max_material_slots = 8
max_texture_edge = 4096

[platforms.pc]
max_triangles = 120000
max_material_slots = 6
max_texture_edge = 4096
"""


def _ask_number(ask: "object", question: str, default: int, low: int = 0) -> int:
    """Prompt until the answer is a number in range; Enter takes the default."""
    while True:
        try:
            raw = ask(f"{question} [{default}]: ")        # type: ignore[operator]
        except EOFError:                                   # piped input / CI
            return default
        text = (raw or "").strip().replace(",", "").replace("_", "")
        if not text:
            return default
        try:
            value = int(text)
        except ValueError:
            print(f"  '{text}' is not a number, try again")
            continue
        if value < low:
            print(f"  must be at least {low}")
            continue
        return value


def _ask_choice(ask: "object", question: str, default: str, choices: Sequence[str]) -> str:
    while True:
        try:
            raw = ask(f"{question} ({'/'.join(choices)}) [{default}]: ")  # type: ignore[operator]
        except EOFError:
            return default
        text = (raw or "").strip().lower()
        if not text:
            return default
        if text in choices:
            return text
        print(f"  pick one of: {', '.join(choices)}")


def _ask_bool(ask: "object", question: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    try:
        raw = ask(f"{question} [{suffix}]: ")              # type: ignore[operator]
    except EOFError:
        return default
    text = (raw or "").strip().lower()
    if not text:
        return default
    return text in ("y", "yes", "1", "true", "on")


def write_rules(target: Path, *, max_triangles: int = 50_000, max_material_slots: int = 3,
                max_edge: int = 2_048, platform: str = "none",
                auto_heal: bool = False, force: bool = False) -> Path:
    """Render and write a rules.toml. Refuses to clobber one unless forced."""
    rules_path = target / RULES_FILENAME
    if rules_path.exists() and not force:
        raise ConfigError(f"{rules_path} already exists (use --force to overwrite)")
    rules_path.write_text(RULES_TEMPLATE.format(
        date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        platform_line=(f'platform = "{platform}"' if platform != "none" else
                       "# platform = \"mobile\"   # uncomment to pick a default profile"),
        max_triangles=max_triangles,
        max_material_slots=max_material_slots,
        max_edge=max_edge,
        auto_heal="true" if auto_heal else "false",
        # A mobile-first project should not be born with a 50k default.
        mobile_triangles=min(max_triangles, 15_000),
    ), encoding="utf-8")
    return rules_path


def run_init(target: Optional[Path] = None, ask=input, out=None,
             assume_yes: bool = False, force: bool = False,
             max_triangles: Optional[int] = None, max_edge: Optional[int] = None,
             max_material_slots: Optional[int] = None,
             platform: Optional[str] = None) -> int:
    """Create a watched folder and its rules.toml, asking five questions.

    ``ask`` is injected so the wizard is testable without stdin, and any prompt
    hitting EOF falls back to its default - ``--init`` inside a CI script must
    not hang waiting for a human.
    """
    say = out or print
    say("AssetGatekeeper setup")
    say("")

    if target is None:
        try:
            raw = ask(f"Folder to watch [{Path.cwd() / 'assets'}]: ")
        except EOFError:
            raw = ""
        text = (raw or "").strip().strip('"')
        target = Path(text) if text else Path.cwd() / "assets"
    target = Path(target).expanduser()

    if assume_yes:
        values = {
            "max_triangles": max_triangles or 50_000,
            "max_material_slots": max_material_slots or 3,
            "max_edge": max_edge or 2_048,
            "platform": platform or "none",
            "auto_heal": False,
        }
    else:
        values = {
            "max_triangles": max_triangles if max_triangles is not None else _ask_number(
                ask, "Triangle budget per mesh", 50_000, low=1),
            "max_material_slots": (max_material_slots if max_material_slots is not None
                                   else _ask_number(ask, "Material slots per mesh", 3, low=1)),
            "max_edge": max_edge if max_edge is not None else _ask_number(
                ask, "Max texture edge (512/1024/2048/4096)", 2_048, low=16),
            "platform": platform or _ask_choice(
                ask, "Default platform profile", "none", PLATFORM_CHOICES),
            "auto_heal": _ask_bool(ask, "Enable auto-healing (off is safer)", False),
        }

    try:
        target.mkdir(parents=True, exist_ok=True)
        rules_path = write_rules(target, force=force, **values)
    except (OSError, ConfigError) as exc:
        say(f"error: {exc}")
        return 2

    say("")
    say(f"  folder   {target.resolve()}")
    say(f"  rules    {rules_path}")
    say(f"  budget   tris<={values['max_triangles']:,}  texture<={values['max_edge']}px  "
        f"materials<={values['max_material_slots']}")
    if values["platform"] != "none":
        say(f"  profile  {values['platform']}")
    say("")
    say("Next:")
    say(f"  asset-gatekeeper \"{target}\" --once          # one sweep")
    say(f"  asset-gatekeeper \"{target}\"                 # watch it live")
    return 0
