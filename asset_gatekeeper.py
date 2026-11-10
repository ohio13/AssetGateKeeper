#!/usr/bin/env python3
"""
AssetGatekeeper
===============

A tiny, dependency-light "budget gate" that sits on an artist's export folder and
fails fast on assets that would blow a project's technical budget. Written to be
readable and hackable, not clever.

Rules enforced
-
    .obj / .gltf / .glb   triangles     <= 50,000
                          material slots <= 3          (draw-call proxy)
                          at least 1 triangle          (catches truncated writes)
    .png                  max edge      <= 2,048 px
                          both edges are a power of two
                          filename ends with _Albedo / _Normal / _Roughness

Usage
-
    # watch forever (Ctrl+C to stop)
    python asset_gatekeeper.py "D:\\Exports\\Incoming"

    # one-shot sweep (handy in CI or on a pre-existing drop folder)
    python asset_gatekeeper.py "D:\\Exports\\Incoming" --once --recursive

    # override the shipped budgets for a different project
    python asset_gatekeeper.py . --triangles 30000 --max-texture 1024 --max-materials 2

    # relax individual rules
    python asset_gatekeeper.py . --allow-npot --no-naming

    # use a shared rules file instead of <folder>/rules.toml
    python asset_gatekeeper.py . --rules studio/rules.toml

Configuration
-
If ``rules.toml`` sits in the watched folder it supplies the baseline budget;
CLI flags still win, so CI can tighten one value without editing a file the
artists own. Precedence: explicit CLI flag > rules.toml > built-in defaults.
See ``asset_gatekeeper.example.toml`` for the schema.

Exit codes (--once): 0 = every asset passed, 1 = at least one failed, 2 = bad setup.

Install
-
    python -m pip install watchdog trimesh pillow DracoPy

Notes for extending
-
* Every budget value lives in one dataclass (``Budget``); add a field, a matching
  ``_*_problems`` check, and an entry in ``Budget.from_mapping`` to make it
  configurable from ``rules.toml``.
* One asset, one log line: all violations for a file are collected and reported
  together, so an artist fixes everything in a single pass instead of playing
  whack-a-mole across five re-exports.
* Material slots are counted from the file itself (glTF material indices, OBJ
  ``usemtl`` names), not from trimesh's object graph, so primitives that share a
  material are not double-counted.
* Triangle counts are topology counts, not render cost. Instanced scenes count
  once per unique geometry; a draw-call budget would be a different gate.
"""

from __future__ import annotations

# Single source of truth for the version: pyproject.toml reads it from here
# (setuptools parses this literal statically), so it cannot drift.
__version__ = "0.1.0a1"   # shown as 0.1.0-alpha.1; internal 1.0-1.1.1 builds preceded it

import argparse
import array
import base64
import difflib
import fnmatch
import getpass
import hashlib
import json
import logging
import math
import os
import platform
import queue
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import importlib.util
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence
from urllib.parse import quote, unquote

# --- #
# Optional dependencies - fail with a readable message instead of a traceback.
# --- #

try:
    import trimesh
except ImportError:  # pragma: no cover - environment dependent
    trimesh = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover
    FileSystemEventHandler = object  # type: ignore[assignment]
    Observer = None  # type: ignore[assignment]

try:  # Python 3.11+ ships a TOML parser in the standard library
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.7 - 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        # DCC Pythons (Unreal 5.3: 3.9, Blender 4.0: 3.10, Maya 2022-24) have
        # neither, and a project rules.toml then could not be read in-editor.
        # tomli (the code tomllib came from) ships in agk_vendor/ for them.
        _engine_dir = os.path.dirname(os.path.abspath(__file__))
        if _engine_dir not in sys.path:
            sys.path.append(_engine_dir)
        try:
            from agk_vendor import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            tomllib = None  # type: ignore[assignment]

try:  # numpy arrives with trimesh; used only for vectorised geometry maths
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

RULES_FILENAME = "rules.toml"

# --- enterprise plumbing - #
# Everything below is standard library only: the tool still has to run from a
# bare interpreter on a studio workstation with no wheels of its own.
SERVER_ENV_URL = "ASSET_GATEKEEPER_SERVER_URL"
SERVER_ENV_TOKEN = "ASSET_GATEKEEPER_TOKEN"
SERVER_ENV_IDENTITY = "ASSET_GATEKEEPER_TELEMETRY_IDENTITY"
REMOTE_RULES_ENV = "ASSET_GATEKEEPER_REMOTE_RULES"
RULES_CACHE_FILENAME = ".rules.cache.json"

# A strict ceiling: a slow studio endpoint must never hold an artist's terminal.
HTTP_TIMEOUT = 2.0
# Bounded queue: an overnight 200k-file sweep must not grow memory without limit.
TELEMETRY_QUEUE_MAX = 1_000

# See ServerSettings.identity. The default is "host": the machine name keeps
# per-host dashboards working, while the OS account - "who was on that box" - is
# the part that needs an explicit opt-in.
IDENTITY_MODES = ("full", "host", "none")
DEFAULT_IDENTITY = "host"
PAYLOAD_SCHEMA = 1
USER_AGENT = "AssetGatekeeper/1.0 (+https://github.com/ohio13/asset-gatekeeper)"


LOG = logging.getLogger("assetgatekeeper")

MESH_SUFFIXES = frozenset({".obj", ".gltf", ".glb", ".fbx"})
GLTF_SUFFIXES = frozenset({".gltf", ".glb"})
# .tif/.webp/.dds are read by Pillow; .exr and .ktx2 by their headers. All six
# used to be skipped outright (KTX2 and WebP are standard glTF texture formats).
TEXTURE_SUFFIXES = frozenset({".png", ".tga", ".jpg", ".jpeg", ".tif", ".tiff", ".webp",
                              ".dds", ".exr", ".ktx2"})
HEADER_ONLY_TEXTURES = frozenset({".exr", ".ktx2"})
DEFAULT_TEXTURE_SUFFIXES: tuple[str, ...] = (
    "_Albedo", "_BaseColor", "_Diffuse", "_Color",
    "_Normal", "_Roughness", "_Metallic", "_Metalness",
    "_ORM", "_ARM", "_MetallicRoughness", "_OcclusionRoughnessMetallic",
    "_AO", "_AmbientOcclusion", "_Occlusion",
    "_Emissive", "_Emission", "_Height", "_Displacement",
    "_Opacity", "_Alpha", "_Mask", "_Specular", "_Gloss",
    "_D", "_N", "_R", "_M")

#: Ready-made naming conventions for [naming] preset. Each is complete on its
#: own: texture prefixes, texture suffixes and mesh prefixes. Choose several
#: (preset = ["unreal", "substance"]) and a name passes when it follows ANY
#: of them - studios mix exports from different tools.
NAMING_PRESETS: dict[str, dict[str, tuple[str, ...]]] = {
    "default": {"texture_suffixes": DEFAULT_TEXTURE_SUFFIXES},
    # Unreal's community style guide: T_ textures with one-letter map suffixes,
    # SM_ static and SK_ skeletal meshes.
    "unreal": {"texture_prefixes": ("T_",),
               "texture_suffixes": ("_D", "_N", "_R", "_M", "_A", "_O", "_B", "_E", "_S", "_H",
                                    "_ORM", "_MRA", "_ARM", "_RMA", "_Mask", "_BaseColor",
                                    "_Normal", "_Roughness", "_Metallic", "_Emissive"),
               "mesh_prefixes": ("SM_", "SK_")},
    # Unity's Standard / URP / HDRP map names.
    "unity": {"texture_suffixes": ("_Albedo", "_AlbedoTransparency", "_BaseMap", "_MetallicSmoothness",
                                   "_MaskMap", "_Smoothness", "_Normal", "_Occlusion", "_Emission",
                                   "_Height", "_DetailMask", "_Mask", "_Specular")},
    # The glTF material slots, as Khronos samples and Blender's exporter name them.
    "gltf": {"texture_suffixes": ("_baseColor", "_metallicRoughness", "_normal", "_occlusion",
                                  "_emissive", "_ORM", "_transmission", "_clearcoat", "_sheen")},
    # Adobe Substance 3D Painter's export presets.
    "substance": {"texture_suffixes": ("_BaseColor", "_Base_Color", "_Normal", "_Roughness",
                                       "_Metallic", "_Height", "_Emissive", "_AO", "_AmbientOcclusion",
                                       "_OcclusionRoughnessMetallic", "_Opacity", "_Mixed_AO",
                                       "_Specular", "_Glossiness")},
    # Godot's StandardMaterial3D map names.
    "godot": {"texture_suffixes": ("_albedo", "_normal", "_roughness", "_metallic", "_ao",
                                   "_emission", "_orm", "_height")},
}
#: The decoder format each texture extension must actually contain.
TEXTURE_FORMATS = {".png": "PNG", ".tga": "TGA", ".jpg": "JPEG", ".jpeg": "JPEG",
                   ".tif": "TIFF", ".tiff": "TIFF", ".webp": "WEBP", ".dds": "DDS"}
TRACKED_SUFFIXES = MESH_SUFFIXES | TEXTURE_SUFFIXES

# Partial writes and DCC scratch files that must never be inspected.
IGNORED_SUFFIXES = (
    ".tmp", ".temp", ".part", ".partial", ".crdownload", ".download",
    ".swp", ".lock", ".cache", ".blend1",
)
# ".agk-heal-" is the healer's staging folder: it lives inside the watch dir so the
# atomic rename is same-filesystem, and it must never be inspected as an asset.
# ".agk-backups" holds the originals every heal replaced.
IGNORED_PREFIXES = ("~$", ".~", ".git", ".agk-heal-", ".agk-backups")
# Folders whose *contents* are never assets. A name check on the file alone let a
# recursive watch inspect (and try to heal) the healer's own staged copies.
IGNORED_DIR_PREFIXES = (".agk-heal-", ".agk-backups")

GLB_MAGIC = b"glTF"
GLB_JSON_CHUNK = 0x4E4F534A  # 'JSON'


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
            if value < 0:
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
                       for v in value) or float(value[0]) > float(value[1]) or not value[1]):
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
    text = path.read_text(encoding="utf-8-sig")
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
            if isinstance(allow, bool) or not isinstance(allow, (int, float)) or allow < 0:
                raise ConfigError(f"{label}.allow must be a number of MB for {rule}, got {allow!r}")
            allow = float(allow)
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
        if limit is None or limit <= 0:
            raise ConfigError(f"{where} needs max_mb above 0")
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


# --- #
# Naming rules from the files a studio already has
# --- #

def _name_tokens(stem: str) -> list[str]:
    return [token for token in re.split(r"[_\-.]+", stem) if token]


def suggest_naming(root: Path, recursive: bool = True) -> str:
    """A ready-to-paste [naming] block learned from the names already in a folder.

    Scores every preset against the textures and meshes found, picks the ones
    that fit, adds the other map endings and prefixes the folder uses often,
    and says how many files the result would pass. Nothing is written."""
    files = [path for path in walk_files(root, recursive=recursive)
             if path.suffix.lower() in TRACKED_SUFFIXES]
    textures = sorted({path.stem for path in files if path.suffix.lower() in TEXTURE_SUFFIXES})
    meshes = sorted({path.stem for path in files if path.suffix.lower() in MESH_SUFFIXES})
    lines = [f"# Learned from {len(textures)} texture name(s) and {len(meshes)} mesh name(s) "
             f"in {root}"]
    if not textures and not meshes:
        return lines[0] + "\n# nothing to learn from: no textures or meshes here\n"

    def passing(budget: "Budget", names: list[str], kind: str) -> int:
        check = texture_name_problem if kind == "texture" else mesh_name_problem
        return sum(1 for name in names if not check(name, budget))

    def budget_for(document: dict) -> "Budget":
        return Budget.from_mapping({"naming": document})

    chosen: list[str] = []
    if textures:
        scores = sorted(((passing(budget_for({"preset": name}), textures, "texture"), name)
                         for name in NAMING_PRESETS), reverse=True)
        lines.append("# How many textures each preset already passes:")
        lines += [f"#   {name:<10} {count:>5} of {len(textures)}" for count, name in scores]
        covered: set[str] = set()
        for count, name in scores:          # greedy: add a preset while it helps
            budget = budget_for({"preset": [name]})
            gain = {stem for stem in textures if not texture_name_problem(stem, budget)} - covered
            if count and len(gain) >= max(2, len(textures) // 20):
                chosen.append(name)
                covered |= gain
    # Endings no chosen preset knows, used by two or more textures.
    budget = budget_for({"preset": chosen}) if chosen else None
    leftover = [stem for stem in textures if budget is None or texture_name_problem(stem, budget)]
    endings: dict[str, int] = {}
    spelling: dict[str, str] = {}
    for stem in leftover:
        tokens = _name_tokens(re.sub(r"[_\-]?\d+$", "", stem))
        # A map name is a word: not a number, a resolution (2k) or one letter.
        if len(tokens) >= 2 and re.fullmatch(r"[A-Za-z]{2,}", tokens[-1]):
            key = tokens[-1].lower()
            endings[key] = endings.get(key, 0) + 1
            spelling.setdefault(key, "_" + tokens[-1])
    extra = [spelling[key] for key, count in sorted(endings.items(), key=lambda item: -item[1])
             if count >= 2][:15]
    starts: dict[str, int] = {}
    for stem in meshes:
        tokens = _name_tokens(stem)
        if len(tokens) >= 2 and len(tokens[0]) <= 4:
            starts[tokens[0] + "_"] = starts.get(tokens[0] + "_", 0) + 1
    mesh_prefixes = [prefix for prefix, count in sorted(starts.items(), key=lambda item: -item[1])
                     if count >= max(2, len(meshes) // 5)]

    document: dict = {}
    block = ["[naming]"]
    if chosen:
        document["preset"] = chosen
        block.append("preset = [" + ", ".join(f'"{name}"' for name in chosen) + "]")
    if extra:
        document["texture_suffixes"] = extra
        block.append("texture_suffixes = [" + ", ".join(f'"{e}"' for e in extra)
                     + "]   # other endings used here")
    if mesh_prefixes:
        document["mesh_prefixes"] = mesh_prefixes
        block.append("mesh_prefixes = [" + ", ".join(f'"{p}"' for p in mesh_prefixes)
                     + "]   # most meshes here start with these")
    if len(block) == 1:
        lines.append("# No common convention found; [texture] suffixes or [naming] texture_pattern "
                     "can describe yours.")
        return "\n".join(lines) + "\n"
    result = budget_for(document)
    lines.append(f"# With this block: {passing(result, textures, 'texture')} of {len(textures)} "
                 f"textures and {passing(result, meshes, 'mesh')} of {len(meshes)} meshes pass the "
                 "naming rules.")
    lines.append("# Paste it into rules.toml (and edit freely):")
    return "\n".join(lines + [""] + block) + "\n"


# --- #
# Repository audit: the whole folder, not one asset
# --- #

def walk_files(root: Path, recursive: bool = True, follow_symlinks: bool = False,
               on_skip: Optional[Callable[[str], None]] = None) -> list[Path]:
    """Every readable file under ``root``, surviving entries the OS refuses.

    ``Path.rglob`` raises on the first entry it cannot traverse - a dangling
    symlink, a junction to a detached drive, ``WinError 1920`` on a reparse point
    -- and that exception used to abort an entire scan, which makes a project with
    one odd folder un-gateable. ``os.walk`` accepts an ``onerror`` hook, so a bad
    entry is reported and skipped while everything else is still checked.

    Symlinked folders are **not** followed by default: they re-introduce the same
    tree twice (double-counted verdicts) and a self-referential link is an
    infinite walk. ``--follow-symlinks`` opts in.
    """
    top = Path(root)
    found: list[Path] = []

    def _skip(message: str) -> None:
        LOG.warning("skipping unreadable entry: %s", message)
        if on_skip is not None:
            on_skip(message)

    def _walk_error(exc: OSError) -> None:
        _skip(str(getattr(exc, "filename", None) or exc))

    # A self-referential junction (``project/loop -> project``) makes
    # ``followlinks=True`` walk forever. Remember every real directory we have
    # already entered and prune repeats: a link back into the tree is then
    # walked once, as itself, and the scan still terminates.
    visited: set[str] = set()

    def _directory_key(directory: str) -> str:
        try:
            return os.path.normcase(os.path.realpath(directory))
        except OSError:                       # pragma: no cover - defensive
            return os.path.normcase(os.path.abspath(directory))

    visited.add(_directory_key(str(top)))

    try:
        for directory, dirnames, filenames in os.walk(top, onerror=_walk_error,
                                                      followlinks=follow_symlinks):
            # The healer's staging and backup folders are never part of the
            # project: backups would read as orphaned textures in an audit.
            dirnames[:] = [name for name in dirnames
                           if not name.startswith(IGNORED_DIR_PREFIXES)]
            if follow_symlinks:
                keep = []
                for name in dirnames:
                    child = os.path.join(directory, name)
                    key = _directory_key(child)
                    if key in visited:
                        LOG.warning("not descending into %s again (symlink loop)", child)
                        _skip(child)
                        continue
                    visited.add(key)
                    keep.append(name)
                dirnames[:] = keep          # os.walk honours in-place pruning
            for name in filenames:
                candidate = Path(directory) / name
                try:
                    if candidate.is_file():
                        found.append(candidate)
                except OSError:
                    _skip(str(candidate))
            if not recursive:
                break
    except OSError as exc:                    # the root itself is unreadable
        _skip(str(getattr(exc, "filename", None) or exc))
    return found


def run_audit(root: Path) -> int:
    """Report broken references and orphaned textures across a repository.

    Exit 0 when the repository is clean, 1 when anything is wrong - a folder that
    looks shut but is missing its textures is exactly the failure this catches.
    """
    assets = sorted(p for p in walk_files(root)
                    if p.suffix.lower() in MESH_SUFFIXES)
    broken_total = 0
    for asset in assets:
        broken = missing_references(asset)
        if not broken:
            continue
        broken_total += len(broken)
        print(f"BROKEN  {asset.relative_to(root)}")
        for written, target in broken:
            print(f"          -> {written}   (expected {target})")

    orphans = orphaned_files(root)
    for orphan in orphans:
        try:
            relative = orphan.relative_to(root)
        except ValueError:       # pragma: no cover - outside the root
            relative = orphan
        print(f"ORPHAN  {relative}")

    referenced = sum(len(resolve_references(asset)) for asset in assets)
    print()
    print(f"{len(assets):,} mesh file(s), {referenced:,} reference(s), "
          f"{broken_total:,} broken, {len(orphans):,} orphaned texture(s)")
    return 1 if (broken_total or orphans) else 0


# --- #
# Enterprise: central rules endpoint, offline cache, server settings
# --- #

def _utc_now() -> str:
    """ISO-8601 UTC with a real offset, so logs from two studios can be merged."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _http_body(url: str, timeout: float = HTTP_TIMEOUT, token: str = "",
               accept: str = "application/json") -> str:
    """GET a URL and return its body as text. Raises on anything abnormal.

    Kept dumb on purpose: the caller decides what a failure means (a cached
    budget, versus nothing at all).
    """
    request = urllib.request.Request(url, headers={"Accept": accept,
                                                   "User-Agent": USER_AGENT})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        charset = "utf-8"
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                charset = headers.get_content_charset() or "utf-8"
            except AttributeError:  # plain dict in a test double
                charset = "utf-8"
        return response.read().decode(charset, errors="replace")


@dataclass(frozen=True)
class ServerSettings:
    """Where the platform lives: telemetry sink, central budget, shared token."""

    telemetry_url: str = ""
    remote_rules_url: str = ""
    token: str = ""
    # How much identifying detail rides along with each verdict. Every payload
    # used to carry the machine name, the OS account and an absolute path -
    # "who worked on what, from which box" - to an endpoint the artist never
    # chose. That is a studio decision, so it is explicit and conservative:
    #   "full" - hostname, username and an absolute path
    #   "host" - hostname and an absolute path, no OS account (DEFAULT)
    #   "none" - no host or user, and the path relative to the watch folder
    identity: str = DEFAULT_IDENTITY

    @property
    def telemetry_enabled(self) -> bool:
        return bool(self.telemetry_url)

    @classmethod
    def from_mapping(cls, table: dict, where: str = "[server]") -> "ServerSettings":
        if not isinstance(table, dict):
            raise ConfigError(f"{where} must be a table")
        _reject_unknown(table, ("url", "token", "remote_rules_url", "identity"), where)
        identity = _read_str(table, "identity", where)
        if identity is not None and identity not in IDENTITY_MODES:
            raise ConfigError(f"{where}.identity must be one of "
                              f"{', '.join(IDENTITY_MODES)}, got {identity!r}")
        return cls(
            telemetry_url=_read_str(table, "url", where) or "",
            remote_rules_url=_read_str(table, "remote_rules_url", where) or "",
            token=_read_str(table, "token", where) or "",
            identity=DEFAULT_IDENTITY if identity is None else identity,
        )


def _loads_with(module, body: str, origin: str) -> dict:
    """Parse, turning a syntax error into a readable refusal, not a traceback."""
    try:
        return module.loads(body)
    except Exception as exc:                       # json.JSONDecodeError, TOMLDecodeError
        raise ConfigError(f"{origin}: {exc}") from exc


def parse_rules_text(text: str, origin: str = "<remote>") -> dict:
    """Parse a rules payload: JSON first (every Python), TOML second (3.11+).

    Accepting JSON first means a studio can serve the same object from any web
    stack, and older interpreters still work without ``tomli``.
    """
    body = text.lstrip("\ufeff").strip()
    if not body:
        raise ConfigError(f"{origin}: empty rules payload")
    try:
        if body.startswith("{"):
            document = _loads_with(json, body, origin)
        elif tomllib is not None:
            document = _loads_with(tomllib, body, origin)
        else:
            # 3.9/3.10: tomli is a declared dependency now, so a plain pip install
            # can read the same rules file. Missing it is still a clear refusal.
            try:
                import tomli
            except ImportError as exc:
                raise ConfigError(
                    f"{origin}: TOML needs Python 3.11+ or 'pip install tomli'"
                ) from exc
            document = _loads_with(tomli, body, origin)
    except ConfigError:
        raise
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{origin}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"{origin}: expected a JSON/TOML table at the top level")
    return document


def read_rules_cache(cache_path: Path) -> Optional[dict]:
    """Return the last good remote profile, or None if there is nothing usable."""
    try:
        wrapper = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        LOG.debug("rules cache unreadable (%s): %s", cache_path, exc)
        return None
    rules = wrapper.get("rules") if isinstance(wrapper, dict) else None
    return rules if isinstance(rules, dict) else None


def write_rules_cache(cache_path: Path, url: str, document: dict) -> None:
    """Persist the remote profile atomically: a half-written cache is worse than none."""
    payload = {"source": url, "fetched_at": _utc_now(), "rules": document}
    staging = cache_path.with_name(cache_path.name + ".tmp")
    try:
        staging.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(staging, cache_path)
    except OSError as exc:  # read-only watch folder: keep running, just say so
        LOG.debug("could not write rules cache %s: %s", cache_path, exc)


def fetch_remote_rules(url: str, cache_path: Path, timeout: float = HTTP_TIMEOUT,
                       token: str = "") -> Optional[dict]:
    """GET the central budget, falling back to the last good cached copy.

    Raises ``ConfigError`` only when the server cannot supply a usable budget
    (unreachable, or serving an invalid one) *and* there is no cached profile.

    The failure contract is the whole point of this function: a studio's network
    being down must degrade to "yesterday's budget", not to a traceback that
    stops the artists working. The broad ``except`` is deliberate.
    """
    problem = ""
    try:
        text = _http_body(url, timeout=timeout, token=token,
                          accept="application/json, application/toml")
    except Exception as exc:  # network, DNS, TLS, HTTP status
        problem = f"unreachable ({type(exc).__name__}: {exc})"
        failure: BaseException = exc
    else:
        try:
            document = parse_rules_text(text, url)
            Budget.from_mapping(document)  # validate before caching: a broken payload
            write_rules_cache(cache_path, url, document)  # must not poison the good cache
            LOG.debug("remote rules %s: %d bytes accepted", url, len(text))
            return document
        except Exception as exc:  # malformed payload, not a table, impossible values
            # The server answered; what it served is wrong. Saying "unreachable"
            # here sent people to debug the network instead of the rules.
            problem = f"invalid ({exc})"
            failure = exc
    LOG.debug("remote rules fetch failed: %s", problem)
    cached = read_rules_cache(cache_path)
    if cached is not None:
        if problem.startswith("invalid"):
            LOG.warning("Central rules at %s are %s; using the last good cached profile (%s)",
                        url, problem, cache_path.name)
        else:
            LOG.warning("Central server offline; using cached rules profile (%s)", cache_path.name)
        return cached
    # Fail closed. Falling back to the local 50k default would pass assets the
    # central budget rejects, which is the worst possible answer from a gate.
    # Keep the "remote rules unreachable" wording for the network case: pipelines
    # and the test suite match on it.
    raise ConfigError(
        f"remote rules {problem.split(' ', 1)[0]} ({url}: {problem.split(' ', 1)[1][1:-1]}) "
        f"and no cached profile at {cache_path.name}: refusing to validate against "
        f"local defaults"
    ) from failure


@dataclass(frozen=True)
class HealSettings:
    """Opt-in healing policy.

    ``enabled`` is False unless a human says otherwise - via ``--auto-heal`` or
    ``[healer] enabled = true``. Nothing in this tool ever repairs an asset as a
    side effect of failing it; automated mesh decimation and resampling destroy
    artistic intent, so the default answer is "no".
    """

    enabled: bool = False
    textures: bool = True       # NPOT + missing-suffix renames
    meshes: bool = True         # zero-area faces, .obj only
    # resize keeps UVs exact; pad keeps every pixel but shifts UVs (opt-in)
    texture_mode: str = "resize"
    # JPEG is re-saved (quality 95) when healed, which loses a little each time.
    jpg: bool = False

    @classmethod
    def from_mapping(cls, table: dict, where: str = "[healer]") -> "HealSettings":
        if not isinstance(table, dict):
            raise ConfigError(f"{where} must be a table")
        _reject_unknown(table, ("enabled", "textures", "meshes", "texture_mode", "jpg"), where)
        mode = _read_str(table, "texture_mode", where)
        if mode is not None and mode not in ("pad", "resize"):
            raise ConfigError(f"{where}.texture_mode must be 'pad' or 'resize', got {mode!r}")
        enabled = _read_bool(table, "enabled", where)
        textures = _read_bool(table, "textures", where)
        meshes = _read_bool(table, "meshes", where)
        jpg = _read_bool(table, "jpg", where)
        defaults = cls()
        return cls(
            enabled=defaults.enabled if enabled is None else enabled,
            textures=defaults.textures if textures is None else textures,
            meshes=defaults.meshes if meshes is None else meshes,
            texture_mode=defaults.texture_mode if mode is None else mode,
            jpg=defaults.jpg if jpg is None else jpg,
        )


def resolve_healer(root: Path, args: argparse.Namespace,
                   remote_document: Optional[dict] = None) -> HealSettings:
    """Merge healing policy: explicit CLI flag > remote rules > ``[healer]`` > off.

    The remote document matters here as much as it does for the budget: a
    producer who ticks "Enable auto-heal" in the budget planner expects the
    artists' clients to pick it up, and the planner's output arrives as remote
    rules. ``--no-auto-heal`` exists so CI can still pin healing off when a
    project (or a producer) turns it on.
    """
    from_source = HealSettings()
    if remote_document is not None:
        if not isinstance(remote_document, dict):
            raise ConfigError("remote rules: expected a table of settings, got "
                              f"{type(remote_document).__name__}")
        try:
            from_source = HealSettings.from_mapping(remote_document.get("healer", {}))
        except ConfigError as exc:
            raise ConfigError(f"remote rules: {exc}") from exc
    else:
        rules_path = Path(args.rules).expanduser() if getattr(args, "rules", None) else root / RULES_FILENAME
        if rules_path.is_file():
            try:
                from_source = HealSettings.from_mapping(parse_rules(rules_path).get("healer", {}))
            except (ConfigError, ValueError, OSError) as exc:
                raise ConfigError(f"{rules_path}: {exc}") from exc
    forced = getattr(args, "auto_heal", None)
    if forced is None:
        return from_source
    return replace(from_source, enabled=bool(forced))


def healer_module():
    """The companion ``healer.py``, loaded by path (once per process)."""
    cached = sys.modules.get("agk_healer")
    if cached is not None and getattr(cached, "__file__", None) == str(
            Path(__file__).with_name("healer.py")):
        return cached
    module_path = Path(__file__).with_name("healer.py")
    if not module_path.is_file():
        raise ConfigError(f"healing is enabled but {module_path.name} is missing "
                          f"(expected next to asset_gatekeeper.py)")
    spec = importlib.util.spec_from_file_location("agk_healer", module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ConfigError(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    # Register before executing: @dataclass resolves its own module through
    # sys.modules, and an unregistered module makes it raise AttributeError.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_healer(budget: Budget, settings: HealSettings, root: Optional[Path] = None,
                **options):
    """Import the companion ``healer.py`` next to this file - only when asked.

    Loaded by path rather than by import name so the tool still works when it is
    copied alone into a studio's tools folder next to its healer.
    """
    module = healer_module()
    try:
        return module.Healer(budget, textures=settings.textures, meshes=settings.meshes,
                             texture_mode=settings.texture_mode, root=root,
                             resolve_references=resolve_references, jpg=settings.jpg,
                             **options)
    except module.HealError as exc:
        raise ConfigError(f"healer cannot start: {exc}") from exc


def describe_auto_heal(watch_dir: Path, settings: HealSettings, args: argparse.Namespace,
                       remote_document: Optional[dict]) -> None:
    """Say, before the first file, exactly what folder-wide healing may change.

    Healing rewrites artists' files; whoever turned it on (a flag, rules.toml, a
    producer in the budget planner) is named, so nobody finds out from a diff.
    """
    if getattr(args, "auto_heal", None):
        source = "--auto-heal"
    elif remote_document is not None:
        source = "the remote rules (AssetHealth budget planner)"
    else:
        source = f"[healer] in {RULES_FILENAME}"
    changes = []
    if settings.textures:
        kinds = "PNG/TGA" + ("/JPG" if settings.jpg else "")
        changes.append(f"rename {kinds} textures missing a suffix (and repoint the "
                       ".mtl/.gltf naming them)")
        changes.append(f"{settings.texture_mode} non-power-of-two {kinds} textures"
                       + (" - pad shifts UVs" if settings.texture_mode == "pad" else ""))
    if settings.meshes:
        changes.append("strip zero-area faces from .obj")
    LOG.warning("healing ON for every failing asset in %s (turned on by %s): may %s. "
                "Each change is verified first; originals kept in %s "
                "(undo one: asset-gatekeeper <file> --undo-heal).",
                watch_dir, source, "; ".join(changes) or "change nothing",
                watch_dir / ".agk-backups")


def run_heal_history(target: Path, undo: bool) -> int:
    """``--list-heals`` and ``--undo-heal``. Exit 0 done, 1 nothing to do, 2 refused."""
    try:
        module = healer_module()
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2
    if undo:
        if target.is_dir():
            LOG.error("--undo-heal takes the healed file (or its old name), not a folder; "
                      "--list-heals shows what was healed there")
            return 2
        try:
            record = module.undo_heal(target)
        except module.HealError as exc:
            LOG.error("not undone: %s", exc)
            return 2 if "changed after" in str(exc) or "missing" in str(exc) else 1
        LOG.info("undone  %s: restored %s from %s", record.manifest["healed"],
                 ", ".join(record.manifest["files"]), record.folder)
        return 0
    records = module.find_heals(target)
    if target.is_dir():
        records = [record for record in records if module.is_inside(record.asset, target)]
    else:
        records = [record for record in records
                   if module._same_file(record.healed, target)
                   or module._same_file(record.asset, target)]
    if not records:
        print(f"no heals recorded under {target}")
        return 1
    for record in records:
        state = "undone" if record.undone else "kept  "
        print(f"{record.manifest.get('healed_at', '?')[:19].replace('T', ' ')}  {state}  {record.manifest['asset']}"
              f"{' -> ' + record.manifest['healed'] if record.manifest['healed'] != record.manifest['asset'] else ''}"
              f"  {'; '.join(record.manifest.get('modifications', []))}")
    return 0


def resolve_tagging(root: Path, args: argparse.Namespace,
                    remote_document: Optional[dict] = None
                    ) -> tuple[dict[str, Budget], str, float]:
    """Compile the per-tag budgets and the matching policy for this run.

    Reads the same document the budget came from, so `[tags.*]` inherits the
    project's platform/preset baseline instead of being resolved in a vacuum.
    """
    if remote_document is not None:
        document = remote_document
    else:
        rules_path = Path(args.rules).expanduser() if getattr(args, "rules", None) else root / RULES_FILENAME
        document = parse_rules(rules_path) if rules_path.is_file() else {}

    try:
        platform = ((getattr(args, "platform", None) or "").strip()
                    or str(document.get("platform", "") or ""))
        preset = ((getattr(args, "preset", None) or "").strip()
                  or str(document.get("preset", "") or ""))
        document = apply_preset(apply_platform(document, platform), preset)

        tagging = document.get("tagging") or {}
        _reject_unknown(tagging, ("mode", "min_similarity"), "[tagging]")
        mode = ((getattr(args, "tag_mode", None) or "").strip()
                or _read_str(tagging, "mode", "[tagging]") or "suggest")
        if mode not in TAG_MODES:
            raise ConfigError(f"[tagging].mode must be one of {', '.join(TAG_MODES)}, got {mode!r}")
        similarity = _read_float(tagging, "min_similarity", "[tagging]")
        budgets = build_tag_budgets(document)
    except ConfigError as exc:
        raise ConfigError(str(exc)) from exc
    return budgets, mode, DEFAULT_TAG_SIMILARITY if similarity is None else similarity


def _budget_summary(budget: Budget) -> dict[str, object]:
    """One budget, flattened into the handful of numbers a UI can show.

    ``max_vertices`` is carried through as None: the gate enforces triangles,
    not vertices, and inventing a number here would be a lie an artist acts on.
    """
    return {
        "max_triangles": int(budget.max_triangles),
        "max_vertices": int(getattr(budget, "max_vertices", 0) or 0) or None,
        "max_texture_edge": int(budget.max_texture_edge),
        "max_material_slots": int(budget.max_material_slots),
        "max_bone_influences": int(budget.max_bone_influences),
        "max_uv_channels": int(budget.max_uv_channels),
        "power_of_two": bool(budget.require_power_of_two),
        "texture_naming": bool(budget.enforce_texture_naming),
        "allow_degenerate": bool(budget.allow_degenerate),
        "check_references": bool(budget.check_references),
        "mesh_name_prefixes": list(budget.mesh_name_prefixes),
        "naming_conventions": [list(c[:1]) + [list(c[1]), list(c[2]), c[3], list(c[4]), c[5]]
                               for c in budget.naming_conventions],
    }


def describe_rules(root: Path, rules_path: Optional[str | Path] = None,
                   platform: str = "", preset: str = "",
                   project_id: Optional[int] = None,
                   project_name: Optional[str] = None) -> dict[str, object]:
    """A read-only snapshot of the configuration the gate would enforce here.

    The web inspectors show this, so it deliberately runs the *real* resolvers
    instead of re-reading the TOML: one code path, one truth, no second parser
    to drift. Nothing is written and no socket is opened, so it is safe to serve
    to any artist who can open the dashboard.

    The shape is stable and documented in the servers' ``/api/rules`` routes;
    ``max_vertices`` is None because the gate has no vertex budget to report.
    """
    argv = [str(root)]
    if rules_path:
        argv += ["--rules", str(rules_path)]
    if platform:
        argv += ["--platform", platform]
    if preset:
        argv += ["--preset", preset]
    args = build_parser().parse_args(argv)

    budget, source, _overrides = resolve_budget(root, args)
    tag_budgets, tag_mode, similarity = resolve_tagging(root, args)

    return {
        "read_only": True,
        "generated_at": _utc_now(),
        "root": str(root),
        "source": str(source) if source else "built-in defaults",
        "rules_file_found": bool(source),
        "project_id": project_id,
        "project_name": project_name,
        "target_platform": budget.platform or "default",
        "preset": str(getattr(budget, "preset", "") or "") or "none",
        "tag_mode": tag_mode,
        "tag_similarity": round(float(similarity), 3),
        "vertex_budget_enforced": False,
        "global_budget": _budget_summary(budget),
        "tag_budgets": [
            dict(tag=name, **_budget_summary(tag_budgets[name]))
            for name in sorted(tag_budgets)
        ],
    }


def resolve_server(root: Path, args: argparse.Namespace) -> ServerSettings:
    """Merge server settings: CLI flag > environment > ``[server]`` > nothing.

    Kept separate from :func:`resolve_budget` because the two answer different
    questions - "what is allowed" versus "where does it come from, where does it
    report" - and a studio may centralise one without the other.
    """
    from_file = ServerSettings()
    rules_path = Path(args.rules).expanduser() if getattr(args, "rules", None) else root / RULES_FILENAME
    if rules_path.is_file():
        try:
            from_file = ServerSettings.from_mapping(parse_rules(rules_path).get("server", {}))
        except (ConfigError, ValueError, OSError) as exc:
            raise ConfigError(f"{rules_path}: {exc}") from exc
    return replace(
        from_file,
        telemetry_url=(getattr(args, "telemetry_url", None)
                       or os.environ.get(SERVER_ENV_URL) or from_file.telemetry_url),
        remote_rules_url=(getattr(args, "remote_rules", None)
                          or os.environ.get(REMOTE_RULES_ENV) or from_file.remote_rules_url),
        token=os.environ.get(SERVER_ENV_TOKEN) or from_file.token,
        identity=(getattr(args, "telemetry_identity", None)
                  or os.environ.get(SERVER_ENV_IDENTITY) or from_file.identity),
    )


# --- #
# Analyzers - one concern each, no logging, no watching.
# --- #

def _describe_unexpected(exc: BaseException) -> str:
    """``TypeName: message``, or just ``TypeName`` when the message is empty.

    An empty message once surfaced as ``could not read mesh (AssertionError: )``,
    which tells an artist nothing. The type alone at least reads as a word."""
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _mesh_read_error(exc: BaseException) -> str:
    """The FAILED reason for a mesh trimesh could not load, with the fix if there is one."""
    if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") == "charset_normalizer":
        return ("could not read mesh: it is not UTF-8 text, and reading it needs "
                "charset-normalizer (python -m pip install charset-normalizer)")
    if isinstance(exc, (IndexError, AssertionError)):
        return ("could not read mesh: it is damaged (an index points past the end of its "
                f"data - {_describe_unexpected(exc)}); re-export it")
    return f"could not read mesh ({_describe_unexpected(exc)})"


def obj_colour_source(path: Path) -> str:
    """How an .obj carries colour without a texture: 'polypaint', 'vertex colours' or ''.

    ZBrush writes Polypaint as ``#MRGB`` comment lines, which Houdini and Blender
    read and Maya, 3ds Max and three.js skip, so a sculpt looked blue in one and
    grey in the next (9 Nov 2026). Other exporters put r g b on each ``v`` line.
    Read in chunks: a sculpt can be 90 MB.
    """
    try:
        with open(path, "rb") as handle:
            carry = b"\n"
            first_vertex_checked = False
            while True:
                chunk = handle.read(16 * 1024 * 1024)
                if not chunk:
                    return ""
                data = carry + chunk
                if b"\n#MRGB " in data:
                    return "polypaint"
                if not first_vertex_checked:
                    start = data.find(b"\nv ")
                    end = data.find(b"\n", start + 1) if start >= 0 else -1
                    if end > 0:
                        first_vertex_checked = True
                        if len(data[start + 1:end].split()) >= 7:
                            return "vertex colours"
                carry = data[-8:]
    except OSError:
        return ""


def load_trimesh_scene(source, **kwargs) -> object:
    """trimesh's scene loader, without the call trimesh marks as a compatibility
    wrapper: ``trimesh.load(force='scene')`` (9 Nov 2026 audit, B7)."""
    loader = getattr(trimesh, "load_scene", None)
    if loader is not None:
        return loader(source, **kwargs)
    return trimesh.load(source, force="scene", **kwargs)    # trimesh before load_scene


@lru_cache(maxsize=8)
def _load_scene_cached(path_string: str, modified_ns: int) -> object:
    """One load per (path, mtime). The mtime keeps the cache honest on re-export."""
    return load_trimesh_scene(path_string, process=False)


def load_scene(path: Path) -> object:
    """Load a mesh once and reuse it across the triangle/material/geometry checks."""
    if trimesh is None:
        raise AssetError("trimesh is not installed; mesh data cannot be analysed")
    try:
        return _load_scene_cached(str(path), path.stat().st_mtime_ns)
    except Exception as exc:
        raise AssetError(_mesh_read_error(exc)) from exc


def _geometries(scene: object) -> list:
    if trimesh is not None and isinstance(scene, trimesh.Scene):
        return list(scene.geometry.values())
    return [scene]



# --- #
# FBX (binary, 7.1+): a small read-only parser, standard library + numpy only
# --- #
#
# Why not an SDK: Autodesk's FBX SDK is a native install per platform and per
# Python version, which is exactly what a gate that has to run on every artist
# machine and every CI agent cannot depend on. The binary format is a plain tree
# of records with typed (optionally zlib-compressed) properties; the handful of
# facts the gate needs - polygon indices, vertex positions, UV layers, material
# and skin connections, texture paths - are all directly in it.
#
# ASCII FBX (7.1+) is read into the same record tree as binary, so every
# measurement below is shared. What is refused rather than guessed: files older
# than 7.1 (FBX 2011), whose geometry layout is different; they get a message
# that says how to re-export.
#
# Hostile input is bounded: record offsets are checked against the file size,
# nesting depth is capped, and every array is decompressed with an explicit
# maximum output size, so a 1 KB file cannot ask for gigabytes.

FBX_SUFFIXES = frozenset({".fbx"})
_FBX_MAGIC = b"Kaydara FBX Binary  \x00"
FBX_MAX_DEPTH = 64
FBX_MAX_ARRAY_BYTES = 512 * 1024 * 1024      # one array
FBX_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # all arrays in one file
# 'c' is a byte array some exporters write where others write 'b' (assimp's
# boxWithCompressedCTypeArray.FBX); it used to fail as "unknown property type 99".
_FBX_ARRAY_TYPES = {ord("f"): ("f", 4), ord("d"): ("d", 8), ord("l"): ("q", 8),
                    ord("i"): ("i", 4), ord("b"): ("b", 1), ord("c"): ("b", 1)}


class _FbxNode:
    __slots__ = ("name", "props", "children")

    def __init__(self, name: str, props: list, children: list) -> None:
        self.name = name
        self.props = props
        self.children = children

    def child(self, name: str) -> "Optional[_FbxNode]":
        for node in self.children:
            if node.name == name:
                return node
        return None

    def all(self, name: str) -> "list[_FbxNode]":
        return [node for node in self.children if node.name == name]


def _fbx_array(raw: bytes, code: str):
    if np is not None:
        return np.frombuffer(raw, dtype="<" + {"f": "f4", "d": "f8", "q": "i8",
                                               "i": "i4", "b": "i1"}[code])
    values = array.array(code)
    values.frombytes(raw)
    if sys.byteorder != "little":                     # pragma: no cover
        values.byteswap()
    return values


def parse_fbx_binary(data: bytes) -> "list[_FbxNode]":
    """The top-level records of a binary FBX file. Raises AssetError when malformed."""
    import zlib

    if not data.startswith(_FBX_MAGIC):
        if _looks_like_ascii_fbx(data):
            return parse_fbx_ascii(data)
        raise AssetError("not an FBX file (missing the binary FBX header)")
    if len(data) < 27:
        raise AssetError("truncated FBX header")
    version = struct.unpack_from("<I", data, 23)[0]
    if version < 7100:
        raise AssetError(f"FBX version {version} is too old to read (7.1 / FBX 2011 or "
                         f"newer is needed); re-export with a current exporter")
    wide = version >= 7500
    header = struct.Struct("<QQQB" if wide else "<IIIB")
    size = len(data)
    budget = [FBX_MAX_TOTAL_BYTES]

    def read_property(pos: int):
        if pos >= size:
            raise AssetError("truncated FBX property")
        kind = data[pos]
        pos += 1
        try:
            if kind == ord("Y"):
                return struct.unpack_from("<h", data, pos)[0], pos + 2
            if kind == ord("C"):
                return bool(data[pos]), pos + 1
            if kind == ord("I"):
                return struct.unpack_from("<i", data, pos)[0], pos + 4
            if kind == ord("F"):
                return struct.unpack_from("<f", data, pos)[0], pos + 4
            if kind == ord("D"):
                return struct.unpack_from("<d", data, pos)[0], pos + 8
            if kind == ord("L"):
                return struct.unpack_from("<q", data, pos)[0], pos + 8
            if kind in (ord("S"), ord("R")):
                length = struct.unpack_from("<I", data, pos)[0]
                pos += 4
                if pos + length > size:
                    raise AssetError("truncated FBX string")
                raw = data[pos:pos + length]
                return (raw.decode("utf-8", "replace") if kind == ord("S") else raw), pos + length
            if kind in _FBX_ARRAY_TYPES:
                code, width = _FBX_ARRAY_TYPES[kind]
                count, encoding, stored = struct.unpack_from("<III", data, pos)
                pos += 12
                expected = count * width
                if expected > FBX_MAX_ARRAY_BYTES or expected > budget[0]:
                    raise AssetError(f"FBX array of {count:,} values is too large to inspect safely")
                if pos + stored > size:
                    raise AssetError("truncated FBX array")
                raw = data[pos:pos + stored]
                if encoding == 1:
                    inflater = zlib.decompressobj()
                    raw = inflater.decompress(raw, expected)
                elif encoding != 0:
                    raise AssetError(f"unknown FBX array encoding {encoding}")
                if len(raw) != expected:
                    raise AssetError("corrupt FBX array (wrong decoded length)")
                budget[0] -= expected
                return _fbx_array(raw, code), pos + stored
        except struct.error as exc:
            raise AssetError(f"truncated FBX property ({exc})") from exc
        except zlib.error as exc:
            raise AssetError(f"corrupt compressed FBX array ({exc})") from exc
        raise AssetError(f"corrupt FBX (unknown property type {kind!r})")

    def read_node(pos: int, depth: int):
        if depth > FBX_MAX_DEPTH:
            raise AssetError("FBX nesting is too deep to be a real export")
        if pos + header.size > size:
            raise AssetError("truncated FBX record")
        end, count, _length, name_length = header.unpack_from(data, pos)
        if end == 0:
            return None, pos + header.size              # end-of-list sentinel
        if end <= pos or end > size:
            raise AssetError("corrupt FBX (record points outside the file)")
        pos += header.size
        name = data[pos:pos + name_length].decode("ascii", "replace")
        pos += name_length
        props = []
        for _ in range(count):
            value, pos = read_property(pos)
            props.append(value)
        children = []
        while pos < end:
            node, pos = read_node(pos, depth + 1)
            if node is None:
                break
            children.append(node)
        return _FbxNode(name, props, children), end

    nodes: list[_FbxNode] = []
    pos = 27
    while pos + header.size <= size:
        node, pos = read_node(pos, 0)
        if node is None:
            break
        nodes.append(node)
    return nodes


def _looks_like_ascii_fbx(data: bytes) -> bool:
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith(b";") or head.startswith(b"FBXHeaderExtension")


# One token of ASCII FBX: a key ("Vertices:"), a string, a number, an array
# length ("*24"), a brace, a comma, a newline, or a bare word (T, Y, W, ...).
_FBX_ASCII_TOKEN = re.compile(
    rb"""(?P<key>[A-Za-z_][A-Za-z0-9_|]*)[ \t]*:(?!:)"""
    # No backslash escapes in ASCII FBX (a quote is written &quot;): Maya writes
    # paths such as "D:\\" whose final backslash once swallowed the closing quote.
    rb"""|(?P<string>"[^"]*")"""
    rb"""|(?P<count>\*[0-9]+)"""
    rb"""|(?P<number>[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)"""
    rb"""|(?P<open>\{)|(?P<close>\})|(?P<comma>,)|(?P<newline>\n)"""
    rb"""|(?P<comment>;[^\n]*)|(?P<space>[ \t\r]+)"""
    rb"""|(?P<word>[A-Za-z_][A-Za-z0-9_]*)""")


def parse_fbx_ascii(data: bytes) -> "list[_FbxNode]":
    """The top-level records of an ASCII FBX file, as the binary parser returns them.

    Arrays (``Vertices: *24 { a: ... }``) become the record's first property, as
    in binary; their numbers are parsed in one pass (a 240k-triangle file has
    millions of them). Same bounds as binary: depth, per-array and total size.
    """
    import base64

    if np is None:                                       # pragma: no cover
        raise AssetError("numpy is not installed; FBX cannot be analysed")
    size = len(data)
    budget = [FBX_MAX_TOTAL_BYTES]
    position = [0]

    def token():
        while position[0] < size:
            match = _FBX_ASCII_TOKEN.match(data, position[0])
            if match is None:
                snippet = data[position[0]:position[0] + 20]
                raise AssetError(f"corrupt ASCII FBX near byte {position[0]:,} ({snippet!r})")
            position[0] = match.end()
            kind = match.lastgroup
            if kind in ("space", "comment"):
                continue
            return kind, match.group(kind)
        return "end", b""

    def peek():
        saved = position[0]
        result = token()
        position[0] = saved
        return result

    def value(kind, raw):
        if kind == "string":
            return raw[1:-1].decode("utf-8", "replace").replace("&quot;", '"')
        if kind == "number":
            text = raw.decode("ascii")
            if any(mark in text for mark in ".eE"):
                return float(text)
            return int(text)
        return raw.decode("ascii", "replace")          # bare word: T, Y, W, ...

    def read_array(count: int):
        # "{" "a:" numbers "}" - the numbers are read in one go.
        kind, _ = token()
        if kind != "open":
            raise AssetError("corrupt ASCII FBX array (missing '{')")
        kind, raw = token()
        while kind == "newline":
            kind, raw = token()
        if kind == "close":
            return np.zeros(0)
        if kind != "key" or raw.split(b":")[0].strip() != b"a":
            raise AssetError("corrupt ASCII FBX array (missing 'a:')")
        end = data.find(b"}", position[0])
        if end < 0:
            raise AssetError("truncated ASCII FBX array")
        text = data[position[0]:end]
        position[0] = end + 1
        width = 8
        if count * width > FBX_MAX_ARRAY_BYTES or count * width > budget[0]:
            raise AssetError(f"FBX array of {count:,} values is too large to inspect safely")
        budget[0] -= count * width
        integral = not any(mark in text for mark in (b".", b"e", b"E"))
        try:
            values = np.array(text.replace(b"\n", b"").replace(b"\r", b"").split(b","),
                              dtype=np.int64 if integral else np.float64) \
                if text.strip() else np.zeros(0, dtype=np.int64)
        except ValueError as exc:
            raise AssetError(f"corrupt ASCII FBX array ({exc})") from exc
        if values.size != count:
            raise AssetError(f"corrupt ASCII FBX array ({values.size:,} values, header says {count:,})")
        return values

    def read_node(name: str, depth: int) -> _FbxNode:
        if depth > FBX_MAX_DEPTH:
            raise AssetError("FBX nesting is too deep to be a real export")
        props: list = []
        children: list = []
        expect_value = True
        while True:
            kind, raw = peek()
            if kind == "count" and expect_value:
                token()
                props.append(read_array(int(raw[1:])))
                break
            if kind in ("string", "number", "word") and expect_value:
                token()
                props.append(value(kind, raw))
                expect_value = False
                continue
            if kind == "comma":
                token()
                expect_value = True
                while peek()[0] == "newline":           # a list may continue on the next line
                    token()
                continue
            break
        if peek()[0] == "open":
            token()
            while True:
                kind, raw = token()
                if kind == "close":
                    break
                if kind == "end":
                    raise AssetError("truncated ASCII FBX (unclosed '{')")
                if kind in ("newline", "comma"):
                    continue
                if kind != "key":
                    raise AssetError(f"corrupt ASCII FBX (unexpected {raw[:20]!r})")
                children.append(read_node(raw.split(b":")[0].strip().decode("ascii"), depth + 1))
        if name == "Content" and props and all(isinstance(item, str) for item in props):
            # Embedded media: base64 text in ASCII, raw bytes in binary.
            joined = "".join(props)
            try:
                props = [base64.b64decode(joined)] if joined else [b""]
            except ValueError:
                props = [joined.encode("utf-8", "replace")]
        return _FbxNode(name, props, children)

    nodes: list = []
    while True:
        kind, raw = token()
        if kind == "end":
            break
        if kind in ("newline", "comma"):
            continue
        if kind != "key":
            raise AssetError(f"corrupt ASCII FBX (unexpected {raw[:20]!r} at top level)")
        nodes.append(read_node(raw.split(b":")[0].strip().decode("ascii"), 0))

    header = next((node for node in nodes if node.name == "FBXHeaderExtension"), None)
    version_node = header.child("FBXVersion") if header is not None else None
    version = version_node.props[0] if version_node is not None and version_node.props else None
    if not isinstance(version, int):
        raise AssetError("ASCII FBX without a version (FBXHeaderExtension/FBXVersion); "
                         "re-export it")
    if version < 7100:
        raise AssetError(f"FBX version {version} is too old to read (7.1 / FBX 2011 or "
                         f"newer is needed); re-export with a current exporter")
    return nodes


@dataclass(frozen=True)
class FbxSummary:
    """The facts the gate reads from an FBX file, computed once per (path, mtime)."""

    triangles: int
    material_slots: int
    uv_channels: int
    max_influences: int
    degenerate: int
    #: (as written, candidate paths in preference order) per external texture
    references: tuple
    #: UCX_/UBX_/USP_/UCP_ meshes: Unreal's collision convention, not rendered
    collision_meshes: int = 0
    #: meshes below LOD0 in an LOD group: not part of the LOD0 budget
    lower_lod_meshes: int = 0
    #: (name, width, height) of each texture packed inside the FBX
    embedded_textures: tuple = ()
    #: LOD chains: (name, (LOD0 triangles, LOD1 triangles, ...), "group" | "names")
    lod_groups: tuple = ()
    #: names of the LOD0 render meshes, which a collision mesh must be named after
    render_names: tuple = ()
    #: (name, distinct vertices, concavity or None, flat) per collision mesh
    collision_shapes: tuple = ()
    #: a rendered mesh is skinned (it uses a physics asset, not UCX_ collision)
    skinned: bool = False


def _fbx_id(node: _FbxNode) -> Optional[int]:
    return node.props[0] if node.props and isinstance(node.props[0], int) else None


def _fbx_class(node: _FbxNode) -> str:
    return node.props[2] if len(node.props) > 2 and isinstance(node.props[2], str) else ""


def _fbx_string(node: Optional[_FbxNode]) -> str:
    if node is None or not node.props or not isinstance(node.props[0], str):
        return ""
    return node.props[0]


#: Unreal's collision-mesh prefixes (UCX convex, UBX box, USP sphere, UCP capsule).
#: Engines import these as collision, never as render geometry.
FBX_COLLISION_PREFIXES = ("ucx_", "ubx_", "usp_", "ucp_")


def _fbx_object_name(node: _FbxNode) -> str:
    """The object's own name: binary writes 'Name\\x00\\x01Class', ASCII 'Class::Name'."""
    raw = node.props[1] if len(node.props) > 1 and isinstance(node.props[1], str) else ""
    if "\x00\x01" in raw:
        return raw.split("\x00\x01", 1)[0]
    if "::" in raw:
        return raw.split("::", 1)[1]
    return raw


def _fbx_polygons(indices):
    """(corner vertex indices, polygon start offsets, polygon sizes) from PolygonVertexIndex.

    FBX ends each polygon by storing its last index as ``-(index + 1)``.
    """
    raw = np.asarray(indices, dtype=np.int64)
    if raw.size == 0:
        return raw, raw, raw
    ends = np.flatnonzero(raw < 0)
    if ends.size == 0 or ends[-1] != raw.size - 1:
        raise AssetError("corrupt FBX polygon list (last polygon is not terminated)")
    corners = np.where(raw < 0, -raw - 1, raw)
    starts = np.concatenate(([0], ends[:-1] + 1))
    return corners, starts, ends - starts + 1


@lru_cache(maxsize=8)
def _read_fbx_cached(path_string: str, modified_ns: int, epsilon: float,
                     ratio: float = 1e-6) -> FbxSummary:
    if np is None:                                       # pragma: no cover
        raise AssetError("numpy is not installed; FBX cannot be analysed")
    try:
        data = Path(path_string).read_bytes()
    except OSError as exc:
        raise AssetError(f"could not read FBX ({exc})") from exc
    top = {node.name: node for node in parse_fbx_binary(data)}
    objects = top.get("Objects")
    if objects is None:
        return FbxSummary(0, 0, 0, 0, 0, ())

    geometries: dict = {}
    models: set = set()
    model_names: dict = {}
    lod_groups: list = []
    materials: set = set()
    textures: dict = {}
    videos: dict = {}
    skins: set = set()
    clusters: dict = {}
    for node in objects.children:
        node_id = _fbx_id(node)
        kind = _fbx_class(node)
        if node.name == "Geometry" and kind == "Mesh":
            geometries[node_id] = node
        elif node.name == "Model":
            models.add(node_id)
            model_names[node_id] = _fbx_object_name(node)
            if kind == "LodGroup":
                lod_groups.append(node_id)
        elif node.name == "Material":
            materials.add(node_id)
        elif node.name == "Texture":
            textures[node_id] = node
        elif node.name == "Video":
            videos[node_id] = node
        elif node.name == "Deformer" and kind == "Skin":
            skins.add(node_id)
        elif node.name == "Deformer" and kind == "Cluster":
            clusters[node_id] = node

    # Connections: "OO" child -> parent object, "OP" child -> parent property.
    parents: dict = {}
    children_of: dict = {}
    ordered_children: dict = {}
    connections = top.get("Connections")
    for link in (connections.all("C") if connections else []):
        if len(link.props) < 3:
            continue
        child, parent = link.props[1], link.props[2]
        parents.setdefault(child, set()).add(parent)
        children_of.setdefault(parent, set()).add(child)
        ordered_children.setdefault(parent, []).append(child)

    # What an engine imports as LOD0 render geometry: not collision meshes, and
    # not the LOD1+ children of an LOD group (exporters write them in LOD order).
    def descendants(start) -> set:
        found, stack = set(), [start]
        while stack:
            for item in children_of.get(stack.pop(), ()):
                if item not in found:
                    found.add(item)
                    stack.append(item)
        return found

    collision_models = {model for model, name in model_names.items()
                        if name.lower().startswith(FBX_COLLISION_PREFIXES)}
    lower_lod: set = set()
    group_levels: dict = {}
    for group in lod_groups:
        # Unreal parents the UCX_ hull to the LOD group too; it is not a level.
        levels = [item for item in ordered_children.get(group, [])
                  if item in models and item not in collision_models]
        group_levels[group] = levels
        for level in levels[1:]:
            lower_lod.add(level)
            lower_lod |= descendants(level)
    collision_meshes = lower_lod_meshes = 0
    rendered: dict = {}
    for geometry_id, geometry in geometries.items():
        owners = parents.get(geometry_id, set()) & models
        if owners and owners <= collision_models:
            collision_meshes += 1
        elif owners and owners <= lower_lod:
            lower_lod_meshes += 1
        else:
            rendered[geometry_id] = geometry

    triangles = degenerate = uv_channels = max_influences = 0
    triangles_of: dict = {}
    for geometry_id, geometry in rendered.items():
        index_node = geometry.child("PolygonVertexIndex")
        vertex_node = geometry.child("Vertices")
        if index_node is None or not index_node.props:
            continue
        corners, starts, sizes = _fbx_polygons(index_node.props[0])
        fan = np.clip(sizes - 2, 0, None)
        count = int(fan.sum())
        triangles_of[geometry_id] = count
        triangles += count
        uv_channels = max(uv_channels, len(geometry.all("LayerElementUV")))
        if count and vertex_node is not None and vertex_node.props:
            positions = np.asarray(vertex_node.props[0], dtype=np.float64)
            if positions.size % 3:
                raise AssetError("corrupt FBX vertex array (not a multiple of 3)")
            positions = positions.reshape(-1, 3)
            if corners.size and int(corners.max()) >= len(positions):
                raise AssetError("corrupt FBX polygon list (index past the last vertex)")
            # Fan triangulation, as engines do on import: (c0, ck, ck+1).
            owner = np.repeat(np.arange(len(sizes)), fan)
            first = np.repeat(np.cumsum(fan) - fan, fan)
            step = np.arange(count) - first + 1
            a = corners[starts[owner]]
            b = corners[starts[owner] + step]
            c = corners[starts[owner] + step + 1]
            degenerate += int(np.count_nonzero(degenerate_mask(
                positions[a], positions[b], positions[c], ratio, epsilon)))
        # Skin: Skin deformer -> this geometry; clusters -> that skin.
        for skin_id in children_of.get(geometry_id, ()):
            if skin_id not in skins:
                continue
            per_vertex: dict = {}
            for cluster_id in children_of.get(skin_id, ()):
                cluster = clusters.get(cluster_id)
                if cluster is None:
                    continue
                indexes, weights = cluster.child("Indexes"), cluster.child("Weights")
                if indexes is None or weights is None:
                    continue
                for index, weight in zip(np.asarray(indexes.props[0]).tolist(),
                                         np.asarray(weights.props[0]).tolist()):
                    if weight > 1e-4:
                        per_vertex[index] = per_vertex.get(index, 0) + 1
            if per_vertex:
                max_influences = max(max_influences, max(per_vertex.values()))

    # Materials that a mesh-bearing model actually uses.
    mesh_models = {parent for geometry_id in rendered
                   for parent in parents.get(geometry_id, ()) if parent in models}
    used = {material for material in materials
            if parents.get(material, set()) & mesh_models}
    slots = len(used) if used else (1 if triangles else 0)
    lods, render_names, shapes, skinned = _fbx_structure(
        geometries, models, model_names, parents, children_of, descendants, collision_models,
        group_levels, rendered, mesh_models, triangles_of, skins)

    references = []
    packed: dict = {}
    for texture_id, texture in textures.items():
        relative = _fbx_string(texture.child("RelativeFilename"))
        absolute = _fbx_string(texture.child("FileName"))
        embedded = False
        for video_id in children_of.get(texture_id, ()):
            video = videos.get(video_id)
            content = video.child("Content") if video is not None else None
            if content is not None and content.props and isinstance(content.props[0], bytes) \
                    and len(content.props[0]) > 0:
                embedded = True
                size = image_size_from_bytes(content.props[0])
                if size is not None and video_id not in packed:
                    label = (relative or absolute).replace("\\", "/").rsplit("/", 1)[-1] \
                        or f"texture {len(packed) + 1}"
                    packed[video_id] = (label, size[0], size[1])
        if embedded or not (relative or absolute):
            continue
        written = relative or absolute
        references.append((written, tuple(item for item in (relative, absolute) if item)))
    return FbxSummary(triangles, slots, uv_channels, max_influences, degenerate,
                      tuple(references), collision_meshes, lower_lod_meshes,
                      tuple(packed.values()), lods, render_names, shapes, skinned)


def _fbx_structure(geometries, models, model_names, parents, children_of, descendants,
                   collision_models, group_levels, rendered, mesh_models, triangles_of, skins):
    """LOD chains, render-mesh names, collision shapes and skinning of one FBX.

    LOD chains come from LOD groups (what Unreal and Unity import); without one,
    from _LOD0/_LOD1 model names (Unity builds an LOD group from those).
    """
    def geometry_triangles(geometry_id) -> int:
        if geometry_id not in triangles_of:
            index_node = geometries[geometry_id].child("PolygonVertexIndex")
            count = 0
            if index_node is not None and index_node.props:
                _corners, _starts, sizes = _fbx_polygons(index_node.props[0])
                count = int(np.clip(sizes - 2, 0, None).sum())
            triangles_of[geometry_id] = count
        return triangles_of[geometry_id]

    owned: dict = {}
    for geometry_id in geometries:
        for owner in parents.get(geometry_id, set()) & models:
            owned.setdefault(owner, []).append(geometry_id)

    def subtree_triangles(model) -> int:
        members = ({model} | (descendants(model) & models)) - collision_models
        found = {item for member in members for item in owned.get(member, ())}
        return sum(geometry_triangles(item) for item in found)

    lods = [(_bare_name(model_names.get(group, "")), tuple(subtree_triangles(level) for level in levels),
             "group") for group, levels in group_levels.items() if levels]
    if not lods:
        named = [(model_names.get(model, ""), sum(geometry_triangles(item) for item in owned[model]))
                 for model in owned if model not in collision_models]
        lods = [(base, levels, "names") for base, levels in lod_chains_by_name(named)]
    render_names = tuple(sorted({_bare_name(model_names.get(model, "")) for model in mesh_models}
                                - {""}))
    shapes = []
    for model in sorted(collision_models, key=lambda item: model_names.get(item, "")):
        points, triangles, offset = [], [], 0
        for geometry_id in owned.get(model, ()):
            geometry = geometries[geometry_id]
            vertex_node, index_node = geometry.child("Vertices"), geometry.child("PolygonVertexIndex")
            if vertex_node is None or not vertex_node.props:
                continue
            positions = np.asarray(vertex_node.props[0], dtype=np.float64)
            if positions.size % 3:
                raise AssetError("corrupt FBX vertex array (not a multiple of 3)")
            positions = positions.reshape(-1, 3)
            if index_node is not None and index_node.props:
                corners, starts, sizes = _fbx_polygons(index_node.props[0])
                fan = np.clip(sizes - 2, 0, None)
                owner = np.repeat(np.arange(len(sizes)), fan)
                first = np.repeat(np.cumsum(fan) - fan, fan)
                step = np.arange(int(fan.sum())) - first + 1
                triangles.append(np.stack([corners[starts[owner]], corners[starts[owner] + step],
                                           corners[starts[owner] + step + 1]], axis=1) + offset)
            points.append(positions)
            offset += len(positions)
        if not points:
            continue                          # a UCX_ null with no mesh: nothing to import
        shapes.append((_bare_name(model_names.get(model, "")),
                       *hull_shape(np.concatenate(points),
                                   np.concatenate(triangles) if triangles else np.zeros((0, 3), int))))
    skinned = any(item in skins for geometry_id in rendered
                  for item in children_of.get(geometry_id, ()))
    return tuple(lods), render_names, tuple(shapes), skinned


def read_fbx(path: Path, epsilon: float = 0.0, ratio: float = 1e-6) -> FbxSummary:
    try:
        modified = path.stat().st_mtime_ns
    except OSError as exc:
        raise AssetError(f"could not read FBX ({exc})") from exc
    try:
        return _read_fbx_cached(str(path), modified, float(epsilon), float(ratio))
    except AssetError:
        raise
    except (ValueError, IndexError, TypeError, MemoryError) as exc:
        raise AssetError(f"could not read FBX ({type(exc).__name__}: {exc})") from exc


def _fbx_reference_target(path: Path, candidates: tuple) -> Path:
    """Where an engine will look for an FBX texture: relative path, absolute path,
    then the bare filename next to the FBX (Unreal and Unity both fall back to it)."""
    base = path.parent
    options = []
    for candidate in candidates:
        normalised = candidate.replace("\\", "/")
        options.append(base / normalised)
        options.append(base / normalised.rsplit("/", 1)[-1])
    for option in options:
        try:
            if option.is_file():
                return option
        except OSError:
            continue
    return options[0] if options else base


def _count_triangles_trimesh(path: Path) -> int:
    """Triangle count via trimesh. Handles single meshes and multi-mesh scenes."""
    total = 0
    for geometry in _geometries(load_scene(path)):
        faces = getattr(geometry, "faces", None)
        if faces is not None:
            total += len(faces)
    return total


def read_gltf_document(path: Path) -> dict:
    """Return the glTF JSON document for a .gltf (text) or .glb (binary) file."""
    try:
        if path.suffix.lower() == ".gltf":
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        else:
            document = _glb_json(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Corrupt text used to escape as an uncaught UnicodeDecodeError (6 Nov
        # fuzzing) and stop a folder scan; it is a damaged file like any other.
        raise AssetError(f"damaged glTF: its JSON is not valid ({type(exc).__name__}); "
                         "re-export it") from exc
    if not isinstance(document, dict):
        raise AssetError("damaged glTF: its JSON is not an object; re-export it")
    asset = document.get("asset")
    version = str(asset.get("version", "")) if isinstance(asset, dict) else ""
    # glTF 1.0 kept meshes/accessors as name-keyed objects, not lists.
    if version.startswith("1") or isinstance(document.get("meshes"), dict):
        raise AssetError("glTF 1.0 file: this format was replaced by glTF 2.0 in 2017; "
                         "re-export it as glTF 2.0")
    return document


def _glb_json(blob: bytes) -> dict:
    if blob[:4] != GLB_MAGIC:
        raise AssetError("not a GLB container (bad magic header)")
    offset = 12  # magic(4) + version(4) + total length(4)
    while offset + 8 <= len(blob):
        chunk_length, chunk_type = struct.unpack_from("<II", blob, offset)
        offset += 8
        if chunk_type == GLB_JSON_CHUNK:
            return json.loads(blob[offset:offset + chunk_length].decode("utf-8"))
        offset += chunk_length
    raise AssetError("GLB contains no JSON chunk")


#: glTF primitive modes: 4 TRIANGLES, 5 TRIANGLE_STRIP, 6 TRIANGLE_FAN. 0-3 are
#: points and lines, which an engine does not import as mesh triangles.
_GLTF_TRIANGLE_MODES = (4, 5, 6)


def _primitive_triangles(document: dict, primitive: dict) -> int:
    accessors = document.get("accessors", [])
    mode = primitive.get("mode", 4)
    if mode not in _GLTF_TRIANGLE_MODES:
        return 0
    index_accessor = primitive.get("indices")
    if index_accessor is None:
        index_accessor = (primitive.get("attributes") or {}).get("POSITION")
    if not isinstance(index_accessor, int) or not 0 <= index_accessor < len(accessors):
        return 0
    count = int(accessors[index_accessor].get("count", 0) or 0)
    if mode == 4:
        return count // 3
    return max(count - 2, 0)        # a strip or a fan: one triangle per index after two


def gltf_triangle_counts(document: dict) -> tuple[int, int]:
    """(unique, drawn) triangles of a glTF document, read from its accessors.

    *unique* counts each mesh once. *drawn* counts a mesh once per node that
    places it - what the scene renders, and what Unreal Engine 5.3 imports: its
    glTF importer bakes repeated parts into the static mesh (CesiumMilkTruck:
    2,856 unique, 3,624 drawn, and Unreal's two meshes hold 2,088 + 1,536 =
    3,624; measured 6 Nov). The gate budgets the drawn count. Both come straight from the
    accessor counts, so they need no decoding: Draco- or meshopt-compressed
    files count the same as plain ones, and strips and fans are not dropped (a
    fan-only mesh used to read as 0 triangles).
    """
    meshes = document.get("meshes", [])
    per_mesh = [sum(_primitive_triangles(document, primitive)
                    for primitive in (mesh.get("primitives") or []))
                for mesh in meshes]
    nodes = document.get("nodes", [])
    scenes = document.get("scenes", [])
    scene_index = document.get("scene", 0)
    if scenes:
        scene = scenes[scene_index] if isinstance(scene_index, int) and 0 <= scene_index < len(scenes) else scenes[0]
        roots = list(scene.get("nodes", []))
    else:
        roots = []
    if not roots:
        total = sum(per_mesh)
        return total, total

    used: set[int] = set()
    drawn = 0
    stack = [(root, frozenset()) for root in roots]
    while stack:
        node_index, seen = stack.pop()
        if not isinstance(node_index, int) or not 0 <= node_index < len(nodes) or node_index in seen:
            continue                     # malformed or cyclic graph: skip, never loop
        node = nodes[node_index]
        mesh_index = node.get("mesh")
        if isinstance(mesh_index, int) and 0 <= mesh_index < len(per_mesh):
            used.add(mesh_index)
            copies = 1
            instancing = (node.get("extensions") or {}).get("EXT_mesh_gpu_instancing")
            if isinstance(instancing, dict):
                attributes = instancing.get("attributes") or {}
                accessor = next(iter(attributes.values()), None)
                accessors = document.get("accessors", [])
                if isinstance(accessor, int) and 0 <= accessor < len(accessors):
                    copies = max(int(accessors[accessor].get("count", 1) or 1), 1)
            drawn += per_mesh[mesh_index] * copies
        for child in node.get("children", []):
            stack.append((child, seen | {node_index}))
    return sum(per_mesh[index] for index in used), drawn


def _count_triangles_from_document(document: dict) -> int:
    """Drawn triangles of a glTF document (see gltf_triangle_counts)."""
    return gltf_triangle_counts(document)[1]


def count_triangles(path: Path) -> int:
    """Triangle count for .obj / .gltf / .glb and .fbx.

    glTF is counted from its accessors (one counter, no decoding); OBJ through
    trimesh; FBX through the built-in reader."""
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        return read_fbx(path).triangles
    if suffix in GLTF_SUFFIXES:
        document = read_gltf_document(path)
        try:
            return _count_triangles_from_document(document)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AssetError(f"damaged glTF ({_describe_unexpected(exc)}); re-export it") from exc
    if trimesh is not None:
        try:
            return _count_triangles_trimesh(path)
        except AssetError:
            raise
        except Exception as exc:  # trimesh raises a wide variety of errors
            raise AssetError(_mesh_read_error(exc)) from exc
    raise AssetError("trimesh is not installed; .obj cannot be analysed")


def _count_material_slots_trimesh(path: Path) -> int:
    """Fallback: distinct material objects across a loaded scene."""
    if trimesh is None:
        raise AssetError("trimesh is not installed; material slots cannot be counted")
    loaded = load_trimesh_scene(str(path), process=False)
    geometries = loaded.geometry.values() if isinstance(loaded, trimesh.Scene) else [loaded]

    unused_default = 0
    named: set[object] = set()
    for geometry in geometries:
        material = getattr(getattr(geometry, "visual", None), "material", None)
        if material is None:
            unused_default += 1
            continue
        key = getattr(material, "name", None) or id(material)
        named.add(key)
    return len(named) + unused_default


def count_material_slots(path: Path) -> int:
    """Number of distinct materials referenced by a mesh (approx. draw calls).

    Counted from the source data rather than from the loaded object graph, so two
    primitives that point at the same glTF material index count as one slot.
    """
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        return read_fbx(path).material_slots

    if suffix in GLTF_SUFFIXES:
        try:
            document = read_gltf_document(path)
        except (AssetError, ValueError, OSError, struct.error):
            return _count_material_slots_trimesh(path)

        used_indices: set[int] = set()
        has_default_material = False
        for mesh in document.get("meshes", []):
            for primitive in mesh.get("primitives", []):
                material_index = primitive.get("material")
                if material_index is None:
                    has_default_material = True
                else:
                    used_indices.add(material_index)
        return len(used_indices) + (1 if has_default_material else 0)

    if suffix == ".obj":
        names: set[str] = set()
        has_default_material = False
        current: Optional[str] = None
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.lstrip()
                if stripped.startswith("usemtl"):
                    current = stripped[6:].strip() or None
                    if current is not None:
                        names.add(current)
                elif stripped.startswith("f ") or stripped.startswith("f\t"):
                    if current is None:
                        has_default_material = True
        return len(names) + (1 if has_default_material else 0)

    return _count_material_slots_trimesh(path)


# --- #
# Deep data profiling: skin weights, UV sets, degenerate geometry
# --- #

_COMPONENT_TYPES = {
    5120: ("b", 1),   # byte
    5121: ("B", 1),   # unsigned byte
    5122: ("h", 2),   # short
    5123: ("H", 2),   # unsigned short
    5125: ("I", 4),   # unsigned int
    5126: ("f", 4),   # float
}
_TYPE_COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def _glb_binary_chunk(path: Path) -> bytes:
    """The BIN chunk of a .glb, where embedded accessor data lives."""
    blob = path.read_bytes()
    offset = 12  # magic(4) + version(4) + length(4)
    while offset + 8 <= len(blob):
        chunk_length, chunk_type = struct.unpack_from("<II", blob, offset)
        offset += 8
        if chunk_type == 0x004E4942:  # 'BIN'
            return blob[offset:offset + chunk_length]
        offset += chunk_length
    raise AssetError("GLB contains no binary chunk")


def buffer_bytes(path: Path, document: dict, index: int) -> bytes:
    """Resolve a glTF buffer: GLB chunk, base64 data URI, or sibling file."""
    buffers = document.get("buffers", [])
    if index >= len(buffers):
        raise AssetError(f"glTF references missing buffer {index}")
    uri = buffers[index].get("uri")
    if uri is None:
        return _glb_binary_chunk(path)
    if uri.startswith("data:"):
        _, _, payload = uri.partition("base64,")
        try:
            return base64.b64decode(payload)
        except ValueError as exc:
            raise AssetError(f"glTF data URI is not valid base64 ({exc})") from exc
    return (path.parent / unquote(uri)).read_bytes()


def read_accessor(path: Path, document: dict, index: int) -> list[tuple]:
    """Decode one glTF accessor into a list of tuples. Standard library only.

    Honours ``byteStride`` so interleaved buffers (the common Unity/UE export
    layout) decode correctly instead of producing garbage weights.
    """
    accessors = document.get("accessors", [])
    if index >= len(accessors):
        raise AssetError(f"glTF references missing accessor {index}")
    accessor = accessors[index]

    component_type = accessor.get("componentType")
    if component_type not in _COMPONENT_TYPES:
        raise AssetError(f"unsupported glTF componentType {component_type}")
    code, size = _COMPONENT_TYPES[component_type]

    components = _TYPE_COMPONENTS.get(accessor.get("type", "SCALAR"))
    if components is None:
        raise AssetError(f"unsupported glTF accessor type {accessor.get('type')}")
    count = int(accessor.get("count", 0))

    view_index = accessor.get("bufferView")
    if view_index is None:  # valid: accessor with no data (all zeros)
        return [tuple([0] * components) for _ in range(count)]

    view = document.get("bufferViews", [])[view_index]
    raw = buffer_bytes(path, document, int(view.get("buffer", 0)))
    start = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    tight = components * size
    stride = int(view.get("byteStride") or 0) or tight

    if start + stride * (count - 1) + tight > len(raw):
        raise AssetError("glTF accessor runs past the end of its buffer")

    def unpack(base: int) -> tuple:
        values = array.array(code)
        values.frombytes(raw[base:base + tight])
        return tuple(values)

    if stride == tight:
        return [unpack(start + i * tight) for i in range(count)]
    return [unpack(start + i * stride) for i in range(count)]


def _texture_edge_of(geometry) -> int:
    """The base-colour texture's size (sqrt of its area) a mesh's material names, or 0."""
    material = getattr(getattr(geometry, "visual", None), "material", None)
    for attribute in ("baseColorTexture", "image"):
        image = getattr(material, attribute, None)
        size = getattr(image, "size", None)
        # trimesh gives a material-less mesh a tiny placeholder image; anything
        # under 16 px is that, not an artist's texture.
        if size and len(size) == 2 and min(size) >= 16:
            return int(round(math.sqrt(size[0] * size[1])))
    return 0


def mesh_facts(path: Path, assumed_texture: int = 1024) -> dict:
    """Vertex count, vertex colours and texel density, from the placed scene.

    Texel density is pixels per metre of surface, area-weighted over every mesh
    with UVs: sqrt(UV area x texture pixels / world area). The texture is the
    one the material names when it can be read, else ``assumed_texture``.
    Units are taken as metres (glTF's unit; OBJ has none).
    """
    import numpy as np
    scene = load_scene(path)
    meshes = scene.dump(concatenate=False) if hasattr(scene, "dump") else [scene]
    vertices = 0
    colours = False
    weighted = world_total = 0.0
    with_uv = without_uv = 0
    textures = set()
    for mesh in meshes:
        points = getattr(mesh, "vertices", None)
        faces = getattr(mesh, "faces", None)
        if points is None or faces is None or not len(faces):
            continue
        vertices += len(points)
        visual = getattr(mesh, "visual", None)
        kind = getattr(visual, "kind", None)
        colours = colours or kind == "vertex"
        uv = getattr(visual, "uv", None)
        if uv is None or len(uv) != len(points):
            without_uv += 1
            continue
        with_uv += 1
        uv = np.asarray(uv, dtype=np.float64)[:, :2]
        tri = uv[np.asarray(faces)]
        uv_area = 0.5 * np.abs((tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1])
                               - (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1])).sum()
        world_area = float(mesh.area)
        edge = _texture_edge_of(mesh) or assumed_texture
        textures.add(edge if _texture_edge_of(mesh) else 0)
        weighted += float(uv_area) * edge * edge
        world_total += world_area
    density = math.sqrt(weighted / world_total) if world_total > 0 and weighted > 0 else None
    return {"vertices": int(vertices), "colours": colours, "texel_density": density,
            "with_uv": with_uv, "without_uv": without_uv,
            "assumed_texture": 0 in textures}


def count_uv_channels(path: Path) -> int:
    """Number of distinct texture-coordinate sets (TEXCOORD_n / OBJ vt / FBX UV layers)."""
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        return read_fbx(path).uv_channels

    if suffix in GLTF_SUFFIXES:
        try:
            document = read_gltf_document(path)
        except (AssetError, ValueError, OSError, struct.error):
            document = None
        if document is not None:
            channels = set()
            for mesh in document.get("meshes", []):
                for primitive in mesh.get("primitives", []):
                    for attribute in primitive.get("attributes", {}):
                        if attribute.startswith("TEXCOORD_"):
                            channels.add(attribute)
            return len(channels)

    if suffix == ".obj":
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.lstrip().startswith("vt "):
                    return 1  # the OBJ format carries a single UV set
        return 0

    for geometry in _geometries(load_scene(path)):
        if getattr(getattr(geometry, "visual", None), "uv", None) is not None:
            return 1
    return 0


def max_bone_influences(path: Path, epsilon: float = 1e-4) -> int:
    """Worst per-vertex bone-weight count across every JOINTS_n/WEIGHTS_n set.

    glTF encodes four influences per set, so a vertex bound to six bones carries a
    second set (JOINTS_1/WEIGHTS_1). We sum non-zero weights across all sets -
    which is exactly what a mobile GPU cannot afford above four.
    """
    if path.suffix.lower() in FBX_SUFFIXES:
        return read_fbx(path).max_influences
    if path.suffix.lower() not in GLTF_SUFFIXES:
        return 0  # .obj has no skin data

    document = read_gltf_document(path)
    worst = 0
    for mesh in document.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            attributes = primitive.get("attributes", {})
            sets = sorted(
                int(name.rsplit("_", 1)[1])
                for name in attributes
                if name.startswith("WEIGHTS_") and name.rsplit("_", 1)[1].isdigit()
            )
            if not sets:
                continue

            per_vertex: Optional[list[int]] = None
            for set_index in sets:
                weights = read_accessor(path, document, attributes[f"WEIGHTS_{set_index}"])
                counts = [sum(1 for w in row if abs(w) > epsilon) for row in weights]
                if per_vertex is None:
                    per_vertex = counts
                else:
                    per_vertex = [a + b for a, b in zip(per_vertex, counts)]

            if per_vertex:
                worst = max(worst, max(per_vertex))
    return worst


#: glTF extensions that compress vertex data trimesh cannot read without help.
_GLTF_DRACO = "KHR_draco_mesh_compression"
_GLTF_MESHOPT = ("EXT_meshopt_compression", "KHR_meshopt_compression")


def _dracopy_available() -> bool:
    # trimesh decodes Draco through DracoPy; only its presence matters here.
    return importlib.util.find_spec("DracoPy") is not None


def gltf_uses_draco(path: Path) -> bool:
    if path.suffix.lower() not in GLTF_SUFFIXES:
        return False
    try:
        return _GLTF_DRACO in set(read_gltf_document(path).get("extensionsUsed") or [])
    except AssetError:
        return False


def gltf_undecodable_compression(path: Path) -> str:
    """A warning when a glTF's vertex data is compressed in a way this machine
    cannot decode, else ''. Geometry checks are then skipped rather than run on
    placeholder zeros: without DracoPy every Draco vertex reads as (0, 0, 0) and
    every triangle looked degenerate (duck.glb: 4,212 of 4,212)."""
    if path.suffix.lower() not in GLTF_SUFFIXES:
        return ""
    try:
        document = read_gltf_document(path)
    except AssetError:
        return ""
    used = set(document.get("extensionsUsed") or [])
    required = set(document.get("extensionsRequired") or [])
    if _GLTF_DRACO in used and not _dracopy_available():
        return ("compressed with Draco and DracoPy is not installed, so the shape checks "
                "(degenerate triangles, bone weights) were skipped - "
                "python -m pip install DracoPy")
    if required & set(_GLTF_MESHOPT):
        return ("compressed with meshopt, which this version cannot decode, so the shape "
                "checks (degenerate triangles, bone weights) were skipped; counts are exact")
    return ""


# --- #
# Skeleton and lightmap UVs ([skeleton], [lightmap]): read only when a rule is on
# --- #

#: FBX Model classes that are skeleton joints (Maya, Max and Blender write LimbNode).
FBX_BONE_CLASSES = frozenset({"LimbNode", "Limb", "Root"})
#: A weight at or below this does not bind a vertex to a bone (as max_bone_influences).
BONE_WEIGHT_EPSILON = 1e-4
#: The overlap grid is sampled in chunks of this many texel centres; above
#: LIGHTMAP_MAX_SAMPLES centres in one mesh the grid is halved until it fits.
LIGHTMAP_CHUNK = 1 << 20
LIGHTMAP_MAX_SAMPLES = 1 << 23


def _name_list(names: Sequence[str], shown: int = 3) -> str:
    """'a', 'b', 'c' and 2 more: names in a one-line verdict."""
    quoted = ", ".join(f"'{name}'" for name in list(names)[:shown])
    return quoted + (f" and {len(names) - shown} more" if len(names) > shown else "")


def _ordinal(number: int) -> str:
    words = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth",
             7: "seventh", 8: "eighth"}
    return words.get(number, f"{number}th")


def _percent(value: float) -> str:
    """12, 3.4, 0.05: enough digits to tell a small overlap from none."""
    if value == 0:
        return "0"
    if value >= 10:
        return f"{value:.0f}"
    return f"{value:.1f}" if value >= 1 else f"{value:.2f}"


@lru_cache(maxsize=1)
def _fbx_tree_cached(path_string: str, modified_ns: int) -> dict:
    return {node.name: node for node in parse_fbx_binary(Path(path_string).read_bytes())}


def _fbx_tree(path: Path) -> dict:
    """The parsed FBX, shared by the skeleton and lightmap readers of one check."""
    try:
        return _fbx_tree_cached(str(path), path.stat().st_mtime_ns)
    except AssetError:
        raise
    except OSError as exc:
        raise AssetError(f"could not read FBX ({exc})") from exc
    except (ValueError, IndexError, TypeError, MemoryError) as exc:
        raise AssetError(f"could not read FBX ({type(exc).__name__}: {exc})") from exc


def _fbx_graph(top: dict) -> dict:
    """Objects and connections of a parsed FBX, and its render geometry.

    The render geometry is chosen as in _read_fbx_cached: no UCX_/UBX_/USP_/UCP_
    collision meshes and no LOD1+ levels of an LOD group.
    """
    graph: dict = {"geometries": {}, "models": {}, "classes": {}, "names": {}, "materials": {},
                   "skins": set(), "clusters": {}, "parents": {}, "children": {}, "rendered": []}
    objects = top.get("Objects")
    if objects is None:
        return graph
    lod_groups = []
    for node in objects.children:
        node_id, kind = _fbx_id(node), _fbx_class(node)
        if node.name == "Geometry" and kind == "Mesh":
            graph["geometries"][node_id] = node
        elif node.name == "Model":
            graph["models"][node_id] = node
            graph["classes"][node_id] = kind
            graph["names"][node_id] = _fbx_object_name(node)
            if kind == "LodGroup":
                lod_groups.append(node_id)
        elif node.name == "Material":
            graph["materials"][node_id] = node
        elif node.name == "Deformer" and kind == "Skin":
            graph["skins"].add(node_id)
        elif node.name == "Deformer" and kind == "Cluster":
            graph["clusters"][node_id] = node
    parents, children = graph["parents"], graph["children"]
    connections = top.get("Connections")
    for link in (connections.all("C") if connections else []):
        if len(link.props) < 3:
            continue
        child, parent = link.props[1], link.props[2]
        if parent not in parents.setdefault(child, []):
            parents[child].append(parent)
        if child not in children.setdefault(parent, []):
            children[parent].append(child)
    models = graph["models"]
    collision = {model for model, name in graph["names"].items()
                 if name.lower().startswith(FBX_COLLISION_PREFIXES)}
    lower_lod: set = set()
    for group in lod_groups:
        levels = [item for item in children.get(group, []) if item in models and item not in collision]
        stack = list(levels[1:])
        while stack:
            item = stack.pop()
            if item not in lower_lod:
                lower_lod.add(item)
                stack.extend(children.get(item, ()))
    for geometry_id in graph["geometries"]:
        owners = {parent for parent in parents.get(geometry_id, ()) if parent in models}
        if owners and (owners <= collision or owners <= lower_lod):
            continue
        graph["rendered"].append(geometry_id)
    return graph


def _fbx_owner(graph: dict, geometry_id) -> object:
    return next((parent for parent in graph["parents"].get(geometry_id, ())
                 if parent in graph["models"]), None)


def _fbx_mesh_label(graph: dict, geometry_id) -> str:
    owner = _fbx_owner(graph, geometry_id)
    name = graph["names"].get(owner, "") if owner is not None else ""
    return name or _fbx_object_name(graph["geometries"][geometry_id]) or "mesh"


def _fbx_skin_binds(graph: dict, geometry_id) -> list:
    """(cluster node, bone model id) for every skin cluster deforming this geometry."""
    binds = []
    for skin_id in graph["children"].get(geometry_id, ()):
        if skin_id not in graph["skins"]:
            continue
        for cluster_id in graph["children"].get(skin_id, ()):
            cluster = graph["clusters"].get(cluster_id)
            bone = next((item for item in graph["children"].get(cluster_id, ())
                         if item in graph["models"]), None)
            if cluster is not None and bone is not None:
                binds.append((cluster, bone))
    return binds


def _fbx_polygon_materials(geometry, polygons: int):
    """Material slot index of each polygon (LayerElementMaterial; 0 when absent)."""
    per_polygon = np.zeros(polygons, dtype=np.int64)
    layer = geometry.child("LayerElementMaterial")
    values = layer.child("Materials") if layer is not None else None
    if values is not None and values.props:
        raw = np.asarray(values.props[0], dtype=np.int64).reshape(-1)
        mapping = _fbx_string(layer.child("MappingInformationType"))
        if mapping == "ByPolygon" and raw.size == polygons:
            per_polygon = raw.copy()
        elif raw.size:
            per_polygon[:] = raw[0]
    return np.clip(per_polygon, 0, None)


def _fbx_layer_values(layer, values_name: str, index_name: str, width: int, corners):
    """Per-corner values of one FBX layer element (a UV set), or None.

    ByPolygonVertex or ByControlPoint mapping, Direct or IndexToDirect reference,
    as engine/fbx_preview reads the first UV set.
    """
    values_node = layer.child(values_name)
    if values_node is None or not values_node.props:
        return None
    values = np.asarray(values_node.props[0], dtype=np.float64).reshape(-1)
    if values.size == 0 or values.size % width:
        return None
    values = values.reshape(-1, width)
    mapping = _fbx_string(layer.child("MappingInformationType"))
    reference = _fbx_string(layer.child("ReferenceInformationType"))
    index_node = layer.child(index_name)
    if mapping == "ByPolygonVertex":
        lookup = np.arange(len(corners))
    elif mapping in ("ByControlPoint", "ByVertice", "ByVertex"):
        lookup = corners
    else:
        return None
    if reference == "IndexToDirect" and index_node is not None and index_node.props:
        indices = np.asarray(index_node.props[0], dtype=np.int64).reshape(-1)
        if lookup.size and int(lookup.max()) >= len(indices):
            return None
        lookup = indices[lookup]
    if lookup.size and (int(lookup.max()) >= len(values) or int(lookup.min()) < 0):
        return None
    return values[lookup]


def _fbx_fan(starts, sizes):
    """Fan triangulation as engines import it: corner indices (n, 3) of (c0, ck, ck+1)."""
    fan = np.clip(sizes - 2, 0, None)
    count = int(fan.sum())
    owner = np.repeat(np.arange(len(sizes)), fan)
    first = np.repeat(np.cumsum(fan) - fan, fan)
    step = np.arange(count) - first + 1
    return np.stack([starts[owner], starts[owner] + step, starts[owner] + step + 1], axis=1), owner


_NUMPY_COMPONENTS = {5120: "<i1", 5121: "<u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}
_NORMALISED_SCALE = {5120: 127.0, 5121: 255.0, 5122: 32767.0, 5123: 65535.0, 5125: 4294967295.0}


def _accessor_numpy(path: Path, document: dict, index, buffers: dict):
    """One glTF accessor as a float64 (count, components) array (read_accessor, in numpy).

    ``buffers`` caches each buffer's bytes for the caller. Normalised integers
    (KHR_mesh_quantization UVs and weights) are scaled to 0-1.
    """
    accessors = document.get("accessors", [])
    if not isinstance(index, int) or not 0 <= index < len(accessors):
        raise AssetError(f"glTF references missing accessor {index}")
    accessor = accessors[index]
    component = accessor.get("componentType")
    dtype = _NUMPY_COMPONENTS.get(component)
    width = _TYPE_COMPONENTS.get(accessor.get("type", "SCALAR"))
    if dtype is None or width is None:
        raise AssetError(f"unsupported glTF accessor {index} ({component}, {accessor.get('type')})")
    count = int(accessor.get("count", 0) or 0)
    view_index = accessor.get("bufferView")
    if view_index is None or count == 0:
        return np.zeros((count, width))
    views = document.get("bufferViews", [])
    if not isinstance(view_index, int) or not 0 <= view_index < len(views):
        raise AssetError(f"glTF references missing bufferView {view_index}")
    view = views[view_index]
    buffer_index = int(view.get("buffer", 0))
    if buffer_index not in buffers:
        buffers[buffer_index] = buffer_bytes(path, document, buffer_index)
    raw = buffers[buffer_index]
    item = np.dtype(dtype).itemsize
    start = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    stride = int(view.get("byteStride") or 0) or item * width
    if start < 0 or start + stride * (count - 1) + item * width > len(raw):
        raise AssetError("glTF accessor runs past the end of its buffer")
    values = np.ndarray((count, width), dtype=dtype, buffer=raw, offset=start,
                        strides=(stride, item)).astype(np.float64)
    if accessor.get("normalized") and component in _NORMALISED_SCALE:
        values = np.maximum(values / _NORMALISED_SCALE[component], -1.0)
    return values


def _gltf_triangles(path: Path, document: dict, primitive: dict, buffers: dict):
    """Vertex indices (n, 3) of a triangle primitive (lists, strips and fans), or None."""
    mode = primitive.get("mode", 4)
    if mode not in _GLTF_TRIANGLE_MODES:
        return None
    if primitive.get("indices") is not None:
        indices = _accessor_numpy(path, document, primitive["indices"], buffers)[:, 0]
        indices = indices.astype(np.int64)
    else:
        position = (primitive.get("attributes") or {}).get("POSITION")
        accessors = document.get("accessors", [])
        if not isinstance(position, int) or not 0 <= position < len(accessors):
            return None
        indices = np.arange(int(accessors[position].get("count", 0) or 0), dtype=np.int64)
    if mode == 4:
        return indices[:len(indices) // 3 * 3].reshape(-1, 3)
    if len(indices) < 3:
        return np.zeros((0, 3), dtype=np.int64)
    first = indices[:-2] if mode == 5 else np.full(len(indices) - 2, indices[0])
    return np.stack([first, indices[1:-1], indices[2:]], axis=1)


def _gltf_draco(primitive: dict) -> bool:
    """This primitive's vertex data is in a Draco blob, not in its accessors."""
    extensions = primitive.get("extensions") if isinstance(primitive, dict) else None
    return isinstance(extensions, dict) and _GLTF_DRACO in extensions


def _bone_tree(bones: set, parent_of, children_of) -> tuple:
    """(roots, depth per bone): roots are bones whose parent is not a bone."""
    roots = []
    for bone in sorted(bones, key=str):
        up = parent_of(bone)
        if up is None or up not in bones:
            roots.append(bone)
    depth: dict = {}
    stack = [(root, 1) for root in roots]
    while stack:
        bone, level = stack.pop()
        if bone in depth:
            continue
        depth[bone] = level
        stack.extend((child, level + 1) for child in children_of(bone)
                     if child in bones and child not in depth)
    return roots, depth


def _fbx_skeleton(path: Path) -> Optional[dict]:
    graph = _fbx_graph(_fbx_tree(path))
    models, children, names = graph["models"], graph["children"], graph["names"]
    skinned = [(geometry_id, binds) for geometry_id in graph["rendered"]
               for binds in [_fbx_skin_binds(graph, geometry_id)] if binds]
    if not skinned:
        return None

    def parent_of(model):
        return next((item for item in graph["parents"].get(model, ()) if item in models), None)

    def child_models(model):
        return [item for item in children.get(model, ()) if item in models]

    # A bone is a joint node (LimbNode) or any node a skin cluster binds to. The
    # skeleton is every bone connected to a bound one: up to its root, and down
    # through unweighted bones (IK targets, twist helpers, tips) below that root.
    linked = {bone for _geometry, binds in skinned for _cluster, bone in binds}

    def is_bone(model) -> bool:
        return model in linked or graph["classes"].get(model) in FBX_BONE_CLASSES

    tops = set()
    for bone in linked:
        node, seen = bone, {bone}
        while True:
            up = parent_of(node)
            if up is None or up in seen or not is_bone(up):
                break
            seen.add(up)
            node = up
        tops.add(node)
    bones: set = set()
    stack = list(tops)
    while stack:
        model = stack.pop()
        if model in bones:
            continue
        bones.add(model)
        stack.extend(item for item in child_models(model) if is_bone(item))
    roots, depth = _bone_tree(bones, parent_of, child_models)

    # Bones per material section: the clusters with weight on a vertex of a
    # polygon of that material. Sections of several meshes that share a
    # material are one section, as Unreal builds them.
    sections: dict = {}
    for geometry_id, binds in skinned:
        geometry = graph["geometries"][geometry_id]
        index_node = geometry.child("PolygonVertexIndex")
        if index_node is None or not index_node.props:
            continue
        corners, starts, sizes = _fbx_polygons(index_node.props[0])
        if not corners.size:
            continue
        vertex_count = int(corners.max()) + 1
        per_polygon = _fbx_polygon_materials(geometry, len(sizes))
        per_corner = np.repeat(per_polygon, sizes)
        owner = _fbx_owner(graph, geometry_id)
        slots = [item for item in children.get(owner, []) if item in graph["materials"]]
        weighted = []
        for cluster, bone in binds:
            indexes, weights = cluster.child("Indexes"), cluster.child("Weights")
            if indexes is None or weights is None or not indexes.props or not weights.props:
                continue
            index = np.asarray(indexes.props[0], dtype=np.int64).reshape(-1)
            weight = np.asarray(weights.props[0], dtype=np.float64).reshape(-1)
            size = min(index.size, weight.size)
            index = index[:size][weight[:size] > BONE_WEIGHT_EPSILON]
            weighted.append((bone, index[(index >= 0) & (index < vertex_count)]))
        for slot in np.unique(per_polygon).tolist():
            used = np.zeros(vertex_count, dtype=bool)
            used[corners[per_corner == slot]] = True
            if slot < len(slots):
                key = slots[slot]
                label = _fbx_object_name(graph["materials"][key]) or f"material {slot}"
            elif not slots:
                key, label = "default", "default material"
            else:
                key, label = (owner, slot), f"material {slot}"
            section = sections.setdefault(key, (label, set()))[1]
            section.update(bone for bone, index in weighted if index.size and used[index].any())
    deepest = max(depth, key=lambda bone: (depth[bone], str(names.get(bone)))) if depth else None
    return {"bones": len(bones), "roots": sorted(names.get(root, "") or "bone" for root in roots),
            "depth": max(depth.values()) if depth else 0,
            "deepest": names.get(deepest, "") if deepest is not None else "",
            "sections": [(label, len(found)) for label, found in sections.values()],
            "unread_sections": 0}


def _gltf_skeleton(path: Path) -> Optional[dict]:
    document = read_gltf_document(path)
    nodes = document.get("nodes") or []
    skins = document.get("skins") or []
    meshes = document.get("meshes") or []
    uses = []
    for node in nodes:
        mesh, skin = node.get("mesh"), node.get("skin")
        if isinstance(mesh, int) and isinstance(skin, int) and 0 <= mesh < len(meshes) \
                and 0 <= skin < len(skins):
            uses.append((mesh, skin))
    if not uses:
        return None
    joints_of = {}
    for skin in {skin for _mesh, skin in uses}:
        # Kept whole: JOINTS_n values index this list. Invalid entries are not bones.
        joints_of[skin] = [item if isinstance(item, int) and 0 <= item < len(nodes) else None
                           for item in (skins[skin].get("joints") or [])]
    bones = {item for joints in joints_of.values() for item in joints if item is not None}
    parent: dict = {}
    for index, node in enumerate(nodes):
        for child in node.get("children") or []:
            if isinstance(child, int):
                parent.setdefault(child, index)

    def child_nodes(index):
        return [item for item in (nodes[index].get("children") or []) if isinstance(item, int)]

    roots, depth = _bone_tree(bones, parent.get, child_nodes)
    materials = document.get("materials") or []
    sections: dict = {}
    unread = 0
    buffers: dict = {}
    # meshopt-compressed buffers hold no plain weights to read
    compressed = bool(gltf_undecodable_compression(path))
    for mesh, skin in uses:
        joints = joints_of[skin]
        for primitive in meshes[mesh].get("primitives") or []:
            attributes = primitive.get("attributes") or {}
            material = primitive.get("material")
            if isinstance(material, int) and 0 <= material < len(materials):
                label = materials[material].get("name") or f"material {material}"
            else:
                material, label = "default", "default material"
            section = sections.setdefault(material, (label, set()))[1]
            if compressed or _gltf_draco(primitive):
                unread += 1
                continue
            for name in attributes:
                if not (name.startswith("JOINTS_") and name[7:].isdigit()):
                    continue
                weights_name = "WEIGHTS_" + name[7:]
                if weights_name not in attributes:
                    continue
                local = _accessor_numpy(path, document, attributes[name], buffers)
                weight = _accessor_numpy(path, document, attributes[weights_name], buffers)
                size = min(len(local), len(weight))
                used = np.unique(local[:size][weight[:size] > BONE_WEIGHT_EPSILON]).astype(np.int64)
                section.update(joints[item] for item in used.tolist()
                               if 0 <= item < len(joints) and joints[item] is not None)
    names = {index: (nodes[index].get("name") or f"node {index}") for index in bones}
    deepest = max(depth, key=lambda bone: (depth[bone], str(names.get(bone)))) if depth else None
    return {"bones": len(bones), "roots": sorted(names[root] for root in roots),
            "depth": max(depth.values()) if depth else 0,
            "deepest": names.get(deepest, "") if deepest is not None else "",
            "sections": [(label, len(found)) for label, found in sections.values()],
            "unread_sections": unread}


def skeleton_facts(path: Path) -> Optional[dict]:
    """The skeleton the asset's skinned meshes use, or None when nothing is skinned.

    {"bones", "roots" (names), "depth" (longest root-to-tip chain, root = 1),
    "deepest" (its last bone), "sections" [(material, bones weighting it)],
    "unread_sections" (compressed primitives whose weights were not read)}.
    FBX: LimbNode joints and every node a skin cluster binds, connected to the
    bound ones. glTF: the joints of every skin a mesh node uses. OBJ: no skins.
    """
    if np is None:                                         # pragma: no cover
        raise AssetError("numpy is not installed; the skeleton cannot be read")
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        return _fbx_skeleton(path)
    if suffix in GLTF_SUFFIXES:
        return _gltf_skeleton(path)
    return None


def lightmap_coverage(triangles, resolution: int = 256) -> tuple[int, int, int]:
    """(texels covered, texels covered more than once, grid used) of UV triangles.

    ``triangles`` is an (n, 3, 2) array of UVs, rasterised into a resolution x
    resolution grid over 0-1. A texel belongs to a triangle when its centre is
    inside it. A centre exactly on an edge goes to one side only (the top-left
    rule GPUs use), and the edge test is computed from the same end of the edge
    for both triangles that share it, so it gives exactly opposite answers: the
    triangles of one island, and islands that only touch, never overlap.

    Cost: one test per texel centre inside each triangle's bounding box, in
    chunks of LIGHTMAP_CHUNK. A clean layout tests about two centres per covered
    texel whatever the triangle count (100,000 triangles at 256: ~0.05 s). A layout
    whose triangles each cover the square many times over would test far more,
    so above LIGHTMAP_MAX_SAMPLES the grid is halved until it fits (down to 4).
    """
    tri = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 2)
    tri = tri[np.all(np.isfinite(tri), axis=(1, 2))]
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    twice_area = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    keep = twice_area != 0                  # zero-area triangles cover nothing
    a, b, c, twice_area = a[keep], b[keep], c[keep], twice_area[keep]
    mirrored = (twice_area < 0)[:, None]    # one winding for all: mirrored islands too
    b, c = np.where(mirrored, c, b), np.where(mirrored, b, c)
    res = max(4, int(resolution))
    while True:
        low = np.minimum(np.minimum(a, b), c) * res
        high = np.maximum(np.maximum(a, b), c) * res
        x0 = np.clip(np.ceil(low[:, 0] - 0.5), 0, res - 1).astype(np.int64)
        y0 = np.clip(np.ceil(low[:, 1] - 0.5), 0, res - 1).astype(np.int64)
        x1 = np.clip(np.floor(high[:, 0] - 0.5), -1, res - 1).astype(np.int64)
        y1 = np.clip(np.floor(high[:, 1] - 0.5), -1, res - 1).astype(np.int64)
        width = np.clip(x1 - x0 + 1, 0, None)
        samples = width * np.clip(y1 - y0 + 1, 0, None)
        total = int(samples.sum())
        if total <= LIGHTMAP_MAX_SAMPLES or res <= 4:
            break
        res = max(4, res // 2)
    if total == 0:
        return 0, 0, res
    pa, pb, pc = a * res, b * res, c * res
    edges = []
    for p, q in ((pa, pb), (pb, pc), (pc, pa)):
        dx, dy = q[:, 0] - p[:, 0], q[:, 1] - p[:, 1]
        # Counter-clockwise, interior on the left: a centre on a left or top edge
        # is inside. The neighbour sees the edge reversed, so not its left or top.
        own = (dy < 0) | ((dy == 0) & (dx < 0))
        # Measure from the lexicographically smaller end: the neighbour's value
        # is then the exact negative of this one, bit for bit.
        swap = (p[:, 0] > q[:, 0]) | ((p[:, 0] == q[:, 0]) & (p[:, 1] > q[:, 1]))
        origin = np.where(swap[:, None], q, p)
        edges.append((origin[:, 0].copy(), origin[:, 1].copy(), dx, dy, own))
    counts = np.zeros(res * res, dtype=np.int64)
    ends = np.cumsum(samples)
    begins = ends - samples
    for start in range(0, total, LIGHTMAP_CHUNK):
        k = np.arange(start, min(start + LIGHTMAP_CHUNK, total), dtype=np.int64)
        t = np.searchsorted(ends, k, side="right")
        iy, ix = np.divmod(k - begins[t], width[t])
        ix += x0[t]
        iy += y0[t]
        px, py = ix + 0.5, iy + 0.5
        inside = np.ones(len(k), dtype=bool)
        for ox, oy, dx, dy, own in edges:
            value = dx[t] * (py - oy[t]) - dy[t] * (px - ox[t])
            inside &= (value > 0) | ((value == 0) & own[t])
        counts += np.bincount(iy[inside] * res + ix[inside], minlength=res * res)
    return int(np.count_nonzero(counts)), int(np.count_nonzero(counts > 1)), res


def _lightmap_meshes(path: Path, channel: int) -> dict:
    """The static meshes of an asset and their lightmap UV set.

    {"meshes": [(name, uv points (n, 2), uv triangles (m, 3, 2))], "missing":
    [names of static meshes without the set], "unread": why some were not read}.
    Skinned meshes are left out: a skeletal mesh is not lightmapped.
    """
    found: dict = {"meshes": [], "missing": [], "unread": ""}
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        graph = _fbx_graph(_fbx_tree(path))
        for geometry_id in graph["rendered"]:
            if _fbx_skin_binds(graph, geometry_id):
                continue
            geometry = graph["geometries"][geometry_id]
            index_node = geometry.child("PolygonVertexIndex")
            if index_node is None or not index_node.props:
                continue
            corners, starts, sizes = _fbx_polygons(index_node.props[0])
            if not int(np.clip(sizes - 2, 0, None).sum()):
                continue
            name = _fbx_mesh_label(graph, geometry_id)
            # A UV layer element carries its set index; files without one are
            # read in order, as the sets appear.
            layers = geometry.all("LayerElementUV")
            indexed = [item for item in layers if item.props and isinstance(item.props[0], int)]
            if indexed:
                layer = next((item for item in indexed if item.props[0] == channel), None)
            else:
                layer = layers[channel] if len(layers) > channel else None
            if layer is None:
                found["missing"].append(name)
                continue
            uv = _fbx_layer_values(layer, "UV", "UVIndex", 2, corners)
            if uv is None:
                found["unread"] = (f"'{name}' has a UV set {channel} this version cannot read "
                                   "(unusual mapping)")
                continue
            triangles, _owner = _fbx_fan(starts, sizes)
            found["meshes"].append((name, uv, uv[triangles]))
        return found
    if suffix in GLTF_SUFFIXES:
        document = read_gltf_document(path)
        undecodable = gltf_undecodable_compression(path)
        if undecodable:
            found["unread"] = "the vertex data is compressed in a way this machine cannot decode"
            return found
        buffers: dict = {}
        attribute = f"TEXCOORD_{channel}"
        for index, mesh in enumerate(document.get("meshes") or []):
            name = mesh.get("name") or f"mesh {index}"
            points, triangles, missing = [], [], False
            for primitive in mesh.get("primitives") or []:
                attributes = primitive.get("attributes") or {}
                if any(key.startswith("JOINTS_") for key in attributes):
                    continue                              # skinned: not lightmapped
                if primitive.get("mode", 4) not in _GLTF_TRIANGLE_MODES:
                    continue
                if attribute not in attributes:
                    missing = True
                    continue
                if _gltf_draco(primitive):
                    found["unread"] = f"'{name}' is Draco-compressed"
                    continue
                uv = _accessor_numpy(path, document, attributes[attribute], buffers)[:, :2]
                corners = _gltf_triangles(path, document, primitive, buffers)
                if corners is None or not len(corners):
                    continue
                if int(corners.max()) >= len(uv) or int(corners.min()) < 0:
                    raise AssetError(f"damaged glTF: '{name}' indexes past its {attribute}; "
                                     "re-export it")
                points.append(uv[np.unique(corners)])
                triangles.append(uv[corners])
            if missing:
                found["missing"].append(name)
            if triangles:
                found["meshes"].append((name, np.concatenate(points), np.concatenate(triangles)))
        return found
    if suffix == ".obj":
        if channel > 0:
            found["obj_single_set"] = True
            return found
        scene = load_scene(path)
        items = scene.geometry.items() if hasattr(scene, "geometry") else [(path.stem, scene)]
        for name, geometry in items:
            faces = getattr(geometry, "faces", None)
            if faces is None or not len(faces):
                continue
            uv = getattr(getattr(geometry, "visual", None), "uv", None)
            if uv is None or len(uv) != len(geometry.vertices):
                found["missing"].append(str(name))
                continue
            uv = np.asarray(uv, dtype=np.float64)[:, :2]
            faces = np.asarray(faces, dtype=np.int64)
            found["meshes"].append((str(name), uv[np.unique(faces)], uv[faces]))
    return found


def lightmap_facts(path: Path, channel: int = 1, resolution: int = 256,
                   overlap: bool = True) -> dict:
    """What the [lightmap] rules need about an asset's lightmap UV set.

    {"static": static meshes found, "missing": [names without the set],
    "obj_single_set": an OBJ asked for a set past its one, "uvs": lightmap UVs
    read, "outside": how many lie outside 0-1, "worst": (u, v) farthest outside,
    "overlap": (percent, texels overlapped, texels covered, mesh, grid) of the
    worst mesh or None, "collapsed": [meshes whose set covers no texel],
    "unread": why part of the asset was not read, or ""}.
    """
    if np is None:                                         # pragma: no cover
        raise AssetError("numpy is not installed; lightmap UVs cannot be read")
    found = _lightmap_meshes(path, channel)
    facts: dict = {"static": len(found["meshes"]) + len(found["missing"]),
                   "missing": found["missing"], "obj_single_set": found.get("obj_single_set", False),
                   "uvs": 0, "outside": 0, "worst": None, "overlap": None, "collapsed": [],
                   "unread": found["unread"]}
    farthest = 0.0
    for name, points, triangles in found["meshes"]:
        points = points[np.all(np.isfinite(points), axis=1)]
        beyond = np.max(np.stack([-points[:, 0], points[:, 0] - 1.0,
                                  -points[:, 1], points[:, 1] - 1.0], axis=1), axis=1) \
            if len(points) else np.zeros(0)
        outside = beyond > LIGHTMAP_UV_TOLERANCE
        facts["uvs"] += len(points)
        facts["outside"] += int(np.count_nonzero(outside))
        if outside.any():
            worst = int(np.argmax(beyond))
            if beyond[worst] > farthest:
                farthest = float(beyond[worst])
                facts["worst"] = (float(points[worst, 0]), float(points[worst, 1]))
        if overlap and len(triangles):
            covered, overlapped, grid = lightmap_coverage(triangles, resolution)
            if not covered:
                facts["collapsed"].append(name)
                continue
            percent = 100.0 * overlapped / covered
            if facts["overlap"] is None or percent > facts["overlap"][0]:
                facts["overlap"] = (percent, overlapped, covered, name, grid)
    return facts


#: Corners closer than this (in file units) are the same point, whatever the scale.
_COINCIDENT_EDGE_SQUARED = 1e-14


def degenerate_mask(a, b, c, ratio: float = 1e-6, epsilon: float = 0.0):
    """Which triangles (corner arrays a, b, c of shape (n, 3)) are degenerate.

    Flat (area <= ratio x longest edge squared), collapsed (all corners within
    1e-7 units), or - when epsilon is set - no larger than epsilon in area.
    Shape, not size: a well-made 0.1 mm triangle on a real-scale prop is fine.
    """
    ab, bc, ca = b - a, c - b, a - c
    areas = 0.5 * np.linalg.norm(np.cross(ab, -ca), axis=1)
    longest = np.max(np.stack([(ab * ab).sum(axis=1), (bc * bc).sum(axis=1),
                               (ca * ca).sum(axis=1)]), axis=0)
    mask = (areas <= ratio * longest) | (longest <= _COINCIDENT_EDGE_SQUARED)
    if epsilon > 0:
        mask |= areas <= epsilon
    return mask


def count_degenerate_triangles(path: Path, epsilon: float = 0.0, ratio: float = 1e-6) -> int:
    """Triangles that are flat or collapsed (see degenerate_mask)."""
    if path.suffix.lower() in FBX_SUFFIXES:
        return read_fbx(path, epsilon, ratio).degenerate
    if np is None:  # pragma: no cover - numpy ships with trimesh
        LOG.debug("numpy is unavailable; skipping degenerate geometry check")
        return 0

    total = 0
    for geometry in _geometries(load_scene(path)):
        faces = getattr(geometry, "faces", None)
        vertices = getattr(geometry, "vertices", None)
        if faces is None or vertices is None or len(faces) == 0:
            continue
        vertices = np.asarray(vertices, dtype=np.float64)
        faces = np.asarray(faces)
        if faces.size and (int(faces.max()) >= len(vertices) or int(faces.min()) < 0):
            raise AssetError("could not read mesh: it is damaged (a triangle points past the "
                             f"last of its {len(vertices):,} vertices); re-export it")
        corners = vertices[faces]
        total += int(np.count_nonzero(degenerate_mask(corners[:, 0], corners[:, 1],
                                                      corners[:, 2], ratio, epsilon)))
    return total


def image_size_from_bytes(data: bytes) -> Optional[tuple[int, int]]:
    """(width, height) of an image held in memory, or None if unrecognised.

    Pillow reads PNG/JPEG/TGA/WebP/DDS headers; KTX2 (the GPU format glTF
    uses) is read from its fixed header so no extra library is needed."""
    if data[:12] == b"\xabKTX 20\xbb\r\n\x1a\n" and len(data) >= 28:
        width, height = struct.unpack_from("<II", data, 20)
        return int(width), int(max(height, 1))
    if Image is None:
        return None
    try:
        import io
        with Image.open(io.BytesIO(data)) as image:
            return int(image.size[0]), int(image.size[1])
    except Exception:
        return None


def embedded_texture_sizes(path: Path) -> list[tuple[str, int, int]]:
    """(label, width, height) of every texture packed inside a mesh file.

    glTF: images stored in the .glb binary chunk or as data: URIs. FBX: embedded
    media. External textures are not listed - they are checked as files of
    their own. Packed ones used to be skipped entirely, so a .glb carrying
    4096x4096 maps passed a 2048 budget (6 Nov corpus test)."""
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        return [tuple(item) for item in read_fbx(path).embedded_textures]
    if suffix not in GLTF_SUFFIXES:
        return []
    document = read_gltf_document(path)
    images = document.get("images") or []
    if not images:
        return []
    views = document.get("bufferViews") or []
    binary: Optional[bytes] = None
    found: list[tuple[str, int, int]] = []
    for number, image in enumerate(images, start=1):
        if not isinstance(image, dict):
            continue
        label = str(image.get("name") or "") or f"image {number} of {len(images)}"
        data: Optional[bytes] = None
        uri = image.get("uri")
        if isinstance(uri, str) and uri.startswith("data:") and "," in uri:
            try:
                data = base64.b64decode(uri.split(",", 1)[1])
            except ValueError:
                data = None
        elif isinstance(image.get("bufferView"), int) and suffix == ".glb":
            view_index = image["bufferView"]
            if 0 <= view_index < len(views):
                if binary is None:
                    try:
                        binary = _glb_binary_chunk(path)
                    except (AssetError, OSError, struct.error):
                        binary = b""
                view = views[view_index]
                start = int(view.get("byteOffset", 0) or 0)
                data = binary[start:start + int(view.get("byteLength", 0) or 0)]
        if not data:
            continue
        size = image_size_from_bytes(data)
        if size is not None:
            found.append((label, size[0], size[1]))
    return found


def _exr_size(data: bytes) -> Optional[tuple[int, int]]:
    """Width and height from an OpenEXR header's dataWindow attribute."""
    if data[:4] != b"\x76\x2f\x31\x01":
        return None
    position = 8
    while position < len(data):
        end = data.find(b"\0", position)
        if end <= position:
            return None
        name = data[position:end]
        type_end = data.find(b"\0", end + 1)
        if type_end < 0 or type_end + 5 > len(data):
            return None
        size = struct.unpack_from("<i", data, type_end + 1)[0]
        value = type_end + 5
        if name == b"dataWindow" and size == 16 and value + 16 <= len(data):
            x_min, y_min, x_max, y_max = struct.unpack_from("<4i", data, value)
            return x_max - x_min + 1, y_max - y_min + 1
        position = value + max(size, 0)
    return None


def measure_texture(path: Path) -> tuple[int, int]:
    """A texture's resolution, after proving the whole file decodes."""
    suffix = path.suffix.lower()
    if suffix in HEADER_ONLY_TEXTURES:
        try:
            with path.open("rb") as handle:
                head = handle.read(1 << 16)
        except OSError as exc:
            raise AssetError(f"could not read image ({exc})") from exc
        size = _exr_size(head) if suffix == ".exr" else image_size_from_bytes(head[:64])
        if size is None or min(size) <= 0:
            raise AssetError(f"could not read the {suffix} header (damaged or not really {suffix})")
        return size
    if Image is None:
        raise AssetError("Pillow is not installed; textures cannot be analysed")
    try:
        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format
            # Decode the pixels too. The header alone said a half-written PNG/JPG/TGA
            # was a perfectly good 1024x1024 texture; decoding is what notices the
            # missing second half ("image file is truncated").
            image.load()
    except Image.DecompressionBombError as exc:  # pragma: no cover - extreme input
        raise AssetError(f"image dimensions are unsafe to inspect ({exc})") from exc
    except OSError as exc:
        raise AssetError(f"could not read image ({exc})") from exc

    suffix = path.suffix.lower()
    expected = TEXTURE_FORMATS.get(suffix, "PNG")
    if image_format != expected:
        raise AssetError(f"file has a {suffix or '.png'} extension but is actually {image_format}")
    return width, height


def is_power_of_two(value: int) -> bool:
    """True for 1, 2, 4, ... 1024, 2048, 4096."""
    return value > 0 and value & (value - 1) == 0


def _suffix_token(suffix: str) -> str:
    """``_BaseColor`` -> a pattern that also accepts base_color, base-color, basecolor."""
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", suffix.strip("_- ."))
    if not words:
        words = [suffix.strip("_- .")]
    return r"[_\-.]?".join(re.escape(word) for word in words)


def build_naming_pattern(suffixes: Sequence[str]) -> Optional[re.Pattern[str]]:
    """Case-insensitive ``_Albedo``-style suffix matcher.

    Separators inside a suffix are optional (``_BaseColor`` matches
    ``_base_color``), the suffix itself may follow ``_`` ``-`` or ``.``, and a
    trailing index is tolerated (``_Albedo_02``) because exported atlases and
    variant sets legitimately number their maps.
    """
    tokens = [_suffix_token(suffix) for suffix in suffixes if suffix.strip("_- .")]
    if not tokens:
        return None
    return re.compile(rf"[_\-.](?:{'|'.join(tokens)})(?:[_\-]?\d+)?$", re.IGNORECASE)


def _starts_with_any(name: str, prefixes: Sequence[str]) -> bool:
    return not prefixes or any(name.lower().startswith(prefix.lower()) for prefix in prefixes)


def _full_match(pattern: str, name: str) -> bool:
    return not pattern or re.fullmatch(pattern, name) is not None


def mesh_name_problem(name: str, rules) -> str:
    """The FAILED reason when a mesh name breaks the naming rules, else ''.

    ``rules`` is a Budget (its [naming] conventions, or mesh_name_prefixes) or a
    plain list of prefixes. One rule for every caller: the gate passes the file
    name, the DCC panels pass the object name. Case-insensitive prefixes."""
    conventions = tuple(getattr(rules, "naming_conventions", ()) or ())
    if conventions:
        relevant = [c for c in conventions if c[4] or c[5]]
        if not relevant or any(_starts_with_any(name, c[4]) and _full_match(c[5], name)
                               for c in relevant):
            return ""
        wanted = sorted({prefix for c in relevant for prefix in c[4]})
        shown = " or ".join(wanted) if wanted else "the naming pattern"
        return (f"mesh name '{name}' does not follow the naming rules (start with {shown}"
                f"; conventions: {', '.join(c[0] for c in relevant)})")
    prefixes = getattr(rules, "mesh_name_prefixes", rules) or ()
    if not prefixes:
        return ""
    if _starts_with_any(name, prefixes):
        return ""
    return f"mesh name '{name}' does not start with {' or '.join(prefixes)}"


def texture_name_problem(stem: str, budget) -> str:
    """The FAILED reason when a texture name breaks the naming rules, else ''."""
    if not getattr(budget, "enforce_texture_naming", True):
        return ""
    conventions = tuple(getattr(budget, "naming_conventions", ()) or ())
    relevant = [c for c in conventions if c[1] or c[2] or c[3]]
    if relevant:
        for label, prefixes, suffixes, pattern, _mesh_prefixes, _mesh_pattern in relevant:
            suffix_pattern = build_naming_pattern(suffixes) if suffixes else None
            if (_starts_with_any(stem, prefixes)
                    and (suffix_pattern is None or suffix_pattern.search(stem))
                    and _full_match(pattern, stem)):
                return ""
        return ("name follows none of the naming conventions ("
                + "; ".join(_describe_convention(c) for c in relevant) + ")")
    pattern = build_naming_pattern(budget.texture_suffixes)
    if pattern is not None and not pattern.search(stem):
        return f"name does not end with a standard suffix ({describe_suffixes(budget.texture_suffixes)})"
    return ""


def _describe_convention(convention) -> str:
    label, prefixes, suffixes, pattern = convention[:4]
    parts = []
    if prefixes:
        parts.append("starts " + "/".join(prefixes))
    if suffixes:
        parts.append("ends " + describe_suffixes(suffixes, shown=4))
    if pattern:
        parts.append(f"matches {pattern}")
    return f"{label}: " + ", ".join(parts)


def describe_suffixes(suffixes: Sequence[str], shown: int = 6) -> str:
    """The suffix list for a FAILED line: the first few, then how many more."""
    listed = ", ".join(suffixes[:shown])
    return listed + (f" and {len(suffixes) - shown} more" if len(suffixes) > shown else "")


# --- #
# The gate itself
# --- #

# --- #
# Reference & hierarchy auditing
# --- #

# What counts as a texture on disk when hunting orphans. Wider than the gated
# suffixes on purpose: an orphaned .tga is still an orphan.
TEXTURE_DISK_SUFFIXES = (".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".exr", ".webp")

# Material lines in an .mtl that point at a file (map_Kd foo.png, bump foo.png ...)
_MTL_MAP_KEYS = ("map_kd", "map_ka", "map_ks", "map_bump", "bump", "map_ns", "map_d",
                 "map_pr", "norm", "refl")


def _is_external_uri(uri: object) -> bool:
    """True for a path we must resolve on disk (not a data URI, not a URL)."""
    if not isinstance(uri, str) or not uri.strip():
        return False
    lowered = uri.lower()
    return not lowered.startswith(("data:", "http://", "https://", "blob:", "//"))


def _gltf_external_uris(document: dict) -> list[str]:
    """Buffer and image URIs - the two places glTF stores a sibling file."""
    uris: list[str] = []
    for section in ("buffers", "images"):
        for entry in document.get(section, []) or []:
            uri = entry.get("uri") if isinstance(entry, dict) else None
            if _is_external_uri(uri):
                uris.append(unquote(str(uri).split("?")[0]))
    return uris


def _mtl_texture_uris(mtl_path: Path) -> list[str]:
    """Texture files named inside a .mtl. Missing .mtl files are reported by the caller."""
    uris: list[str] = []
    try:
        text = mtl_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return uris
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        if parts[0].lower() in _MTL_MAP_KEYS:
            # `map_Kd -s 1 1 1 foo.png`: the filename is the last token.
            uris.append(parts[-1].strip('"'))
    return uris


def collect_references(path: Path) -> list[str]:
    """Every external file this asset points at, exactly as written in the file.

    glTF: ``buffers[].uri`` and ``images[].uri``. OBJ: ``mtllib`` plus the maps
    referenced *by* those .mtl files (one level, which is how OBJ works).
    """
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        try:
            return [written for written, _ in read_fbx(path).references]
        except AssetError as exc:
            LOG.debug("cannot read references from %s: %s", path.name, exc)
            return []
    if suffix in (".gltf", ".glb"):
        try:
            return _gltf_external_uris(read_gltf_document(path))
        except Exception as exc:
            LOG.debug("cannot read references from %s: %s", path.name, exc)
            return []
    if suffix != ".obj":
        return []

    references: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return references
    for line in text.splitlines():
        if not line.lower().startswith("mtllib "):
            continue
        for name in line.split()[1:]:
            name = name.strip('"')
            references.append(name)
            material_path = (path.parent / name)
            if material_path.is_file():
                references.extend(_mtl_texture_uris(material_path))
    return references


def resolve_references(path: Path) -> list[tuple[str, Path]]:
    """[(as written, resolved absolute path)] for every external reference."""
    base = path.parent
    if path.suffix.lower() in FBX_SUFFIXES:
        try:
            summary = read_fbx(path)
        except AssetError:
            return []
        return [(written, _fbx_reference_target(path, candidates))
                for written, candidates in summary.references]
    resolved = []
    for written in collect_references(path):
        # Normalise Windows-style separators coming out of a DCC.
        normalised = written.replace("\\", "/")
        resolved.append((written, (base / normalised)))
    return resolved


def missing_references(path: Path) -> list[tuple[str, Path]]:
    """References that point at a file which is not there."""
    return [(written, target) for written, target in resolve_references(path)
            if not target.is_file()]


def escaping_references(path: Path, root: Optional[Path]) -> list[tuple[str, Path]]:
    """References that resolve outside the project folder.

    ``../../shared/tex.png`` or a hardcoded ``C:/Users/.../wood.png`` is either a
    mistaken export path or an attempt to read a file the project never shipped.
    Either way the build machine will not have it, and everything that follows a
    reference on trust - the dashboard bridge, the workstation importer - walks
    out of the repository on the asset's say-so.

    ``root is None`` (single-file mode) means there is no boundary to escape, so
    nothing is reported.
    """
    if root is None:
        return []
    try:
        base = Path(root).resolve()
    except OSError:                       # pragma: no cover - defensive
        return []
    escaping: list[tuple[str, Path]] = []
    for written, target in resolve_references(path):
        try:
            resolved = Path(target).resolve()
        except OSError:
            escaping.append((written, target))
            continue
        if resolved != base and base not in resolved.parents:
            escaping.append((written, target))
    return escaping


def orphaned_files(root: Path) -> list[Path]:
    """Texture files in the repository that no mesh references.

    Repository-level, not per-asset: an orphan is only visible when you compare
    every asset against every texture on disk.
    """
    root = Path(root)
    on_disk = walk_files(root)
    referenced: set[Path] = set()
    for candidate in on_disk:
        if candidate.suffix.lower() not in MESH_SUFFIXES:
            continue
        for _written, target in resolve_references(candidate):
            try:
                referenced.add(target.resolve())
            except OSError:      # pragma: no cover - unreadable path
                continue
    orphans = []
    for candidate in on_disk:
        if candidate.suffix.lower() not in TEXTURE_DISK_SUFFIXES:
            continue
        try:
            if candidate.resolve() not in referenced:
                orphans.append(candidate)
        except OSError:          # pragma: no cover
            continue
    return sorted(orphans)


# --- #
# PBR value sanitisation
# --- #

def _luma(pixel: tuple[int, int, int]) -> float:
    """Rec. 709 luminance on the 0..1 scale the shader will see."""
    return (0.2126 * pixel[0] + 0.7152 * pixel[1] + 0.0722 * pixel[2]) / 255.0


def pbr_violation(path: Path, budget: Budget) -> Optional[str]:
    """Is this albedo inside the range a physically based shader can use?

    Sampled at 64x64: a 4k texture costs the same as a 512 to inspect, and an
    albedo that is uniformly black or white is uniform at any resolution.
    """
    if Image is None:            # pragma: no cover - Pillow is optional
        return None
    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((64, 64), Image.BILINEAR)
            # Pillow 12+ renamed it (getdata goes in Pillow 14, 2027-10).
            flatten = getattr(image, "get_flattened_data", None) or image.getdata
            pixels = list(flatten())
    except Exception as exc:     # unreadable: the texture check already fails it
        LOG.debug("pbr: cannot sample %s: %s", path.name, exc)
        return None
    if not pixels:
        return None

    luminances = [_luma(pixel) for pixel in pixels]
    mean = sum(luminances) / len(luminances)
    if mean < budget.pbr_floor:
        return (f"albedo mean luminance {mean:.3f} is below the PBR floor "
                f"{budget.pbr_floor:g} (near-black absorbs all light)")
    if mean > budget.pbr_ceiling:
        return (f"albedo mean luminance {mean:.3f} is above the PBR ceiling "
                f"{budget.pbr_ceiling:g} (near-white blinds the lighting model)")

    # A texture can average mid-grey and still be two clipped extremes.
    extreme = sum(1 for value in luminances if value <= 0.01 or value >= 0.99)
    if extreme / len(luminances) > 0.9:
        return (f"{extreme * 100 // len(luminances)}% of albedo pixels are clipped to "
                f"black or white (mean {mean:.3f}); the surface has no usable variance")
    return None


# --- #
# Geometric boundary profiling
# --- #

def _node_translation(node: dict) -> tuple[float, float, float]:
    """Translation from a TRS node or from a 4x4 matrix, in metres."""
    translation = node.get("translation")
    if isinstance(translation, list) and len(translation) == 3:
        try:
            return (float(translation[0]), float(translation[1]), float(translation[2]))
        except (TypeError, ValueError):
            return (0.0, 0.0, 0.0)
    matrix = node.get("matrix")
    if isinstance(matrix, list) and len(matrix) == 16:
        try:
            return (float(matrix[12]), float(matrix[13]), float(matrix[14]))
        except (TypeError, ValueError):
            return (0.0, 0.0, 0.0)
    return (0.0, 0.0, 0.0)


def transform_audit(path: Path, budget: Budget) -> list[str]:
    """Unbaked node transforms, floating/off-centre pivots, unindexed geometry.

    Only called when ``budget.audit_transforms`` (or ``require_indexed``) is on:
    an offset pivot is often authored on purpose, so this is a review prompt,
    not a hard violation.
    """
    problems: list[str] = []
    suffix = path.suffix.lower()

    if suffix in (".gltf", ".glb"):
        try:
            document = read_gltf_document(path)
        except Exception:
            return problems

        for node in document.get("nodes", []) or []:
            if not isinstance(node, dict):
                continue
            x, y, z = _node_translation(node)
            distance = (x * x + y * y + z * z) ** 0.5
            if distance > budget.transform_epsilon:
                name = node.get("name") or f"node {node.get('mesh', '?')}"
                problems.append(
                    f"'{name}' has an unbaked translation ({x:.3f}, {y:.3f}, {z:.3f}); "
                    f"the pivot is floating, so engine placement will double the offset")
            rotation = node.get("rotation")
            if isinstance(rotation, list) and len(rotation) == 4:
                try:
                    w = float(rotation[3])
                except (TypeError, ValueError):
                    w = 1.0
                if abs(abs(w) - 1.0) > 1e-3:
                    name = node.get("name") or f"node {node.get('mesh', '?')}"
                    problems.append(f"'{name}' carries an unbaked rotation; bake it into the mesh")

        if budget.audit_transforms:
            accessors = document.get("accessors", []) or []
            for mesh in document.get("meshes", []) or []:
                for primitive in mesh.get("primitives", []) or []:
                    position = (primitive.get("attributes") or {}).get("POSITION")
                    if not isinstance(position, int) or position >= len(accessors):
                        continue
                    accessor = accessors[position]
                    low, high = accessor.get("min"), accessor.get("max")
                    if not (isinstance(low, list) and isinstance(high, list) and len(low) == 3):
                        continue
                    center = [(low[i] + high[i]) / 2.0 for i in range(3)]
                    size = [(high[i] - low[i]) for i in range(3)]
                    diagonal = (size[0] ** 2 + size[1] ** 2 + size[2] ** 2) ** 0.5 or 1.0
                    offset = (center[0] ** 2 + center[1] ** 2 + center[2] ** 2) ** 0.5
                    if offset / diagonal > budget.pivot_tolerance:
                        problems.append(
                            f"geometry is centred {offset:.3f}m from its own origin "
                            f"({offset / diagonal * 100:.0f}% of its size); the pivot is uncentred")

        if budget.require_indexed:
            for mesh in document.get("meshes", []) or []:
                for primitive in mesh.get("primitives", []) or []:
                    if "indices" not in primitive:
                        name = mesh.get("name") or "unnamed mesh"
                        problems.append(f"'{name}' has unindexed geometry (no index buffer)")
        return problems

    if suffix != ".obj":
        return problems

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return problems
    vertices = 0
    triangles = 0
    low = [float("inf")] * 3
    high = [float("-inf")] * 3
    for line in text.splitlines():
        if line.startswith("v "):
            parts = line.split()
            if len(parts) >= 4:
                vertices += 1
                for axis in range(3):
                    try:
                        value = float(parts[axis + 1])
                    except ValueError:
                        continue
                    low[axis] = min(low[axis], value)
                    high[axis] = max(high[axis], value)
        elif line.startswith("f "):
            triangles += max(0, len(line.split()) - 3)

    if triangles and vertices:
        if budget.audit_transforms and low[0] != float("inf"):
            center = [(low[i] + high[i]) / 2.0 for i in range(3)]
            size = [(high[i] - low[i]) for i in range(3)]
            diagonal = (size[0] ** 2 + size[1] ** 2 + size[2] ** 2) ** 0.5 or 1.0
            offset = (center[0] ** 2 + center[1] ** 2 + center[2] ** 2) ** 0.5
            if offset / diagonal > budget.pivot_tolerance:
                problems.append(
                    f"geometry is centred {offset:.3f}m from its own origin "
                    f"({offset / diagonal * 100:.0f}% of its size); the pivot is uncentred")
        if budget.require_indexed:
            # An OBJ is indexed by definition; the useful question is whether the
            # exporter welded its vertices or emitted a triangle soup (3 per tri).
            ratio = vertices / (3.0 * triangles)
            if ratio > 0.95:
                problems.append(
                    f"unindexed geometry: {vertices:,} vertices for {triangles:,} triangles "
                    f"(a welded mesh needs about a third of that)")
    return problems


# --- #
# LOD levels and collision ([lod], [collision]): what Unreal and Unity import
# --- #

#: Unity's LOD naming (Rock_LOD0, Rock_LOD1, ...): case-insensitive, "_LOD<n>" at the end.
LOD_NAME = re.compile(r"^(?P<base>.+?)_LOD(?P<level>\d+)$", re.IGNORECASE)
#: A UCX_ hull with a vertex further than this fraction of its bounding-box
#: diagonal in front of one of its own faces is not convex. 1%: a dent of 1.7 cm
#: in a 1 m crate. Smaller dents (a slightly bent quad) change nothing in play.
HULL_CONCAVITY_TOLERANCE = 0.01
#: A hull thinner than this fraction of its diagonal is flat: no volume to build.
HULL_FLAT_TOLERANCE = 1e-4
#: The convexity test compares every vertex with every face; above this many
#: comparisons (a hull far too big to be a hull) it is not run.
HULL_TEST_LIMIT = 20_000_000


def _bare_name(name: str) -> str:
    """An object name without its DCC namespace ("props:Rock" -> "Rock"), as Unreal reads it."""
    return str(name or "").rsplit(":", 1)[-1]


def lod_chains_by_name(items) -> list[tuple[str, tuple[int, ...]]]:
    """LOD chains from (object name, triangles) pairs named <base>_LOD<n>.

    A chain is LOD0, LOD1, ... up to the first missing number; an object named
    just <base> stands in for a missing _LOD0. Only chains of two levels or more
    are returned: a lone Rock_LOD0 is a mesh without LODs.
    """
    plain: dict[str, int] = {}
    chains: dict[str, dict[int, int]] = {}
    labels: dict[str, str] = {}
    for name, triangles in items:
        name = _bare_name(name)
        match = LOD_NAME.match(name)
        if match is None:
            plain[name.lower()] = plain.get(name.lower(), 0) + int(triangles)
            continue
        key = match.group("base").lower()
        labels.setdefault(key, match.group("base"))
        level = int(match.group("level"))
        levels = chains.setdefault(key, {})
        levels[level] = levels.get(level, 0) + int(triangles)
    found = []
    for key, levels in chains.items():
        if 0 not in levels and key in plain:
            levels[0] = plain[key]
        counts = []
        while len(counts) in levels:
            counts.append(levels[len(counts)])
        if len(counts) >= 2:
            found.append((labels[key], tuple(counts)))
    return sorted(found)


def hull_shape(points, triangles) -> tuple[int, Optional[float], bool]:
    """(distinct vertices, concavity, flat) of one collision mesh.

    Concavity is how far the worst vertex lies in front of one of the mesh's own
    faces, as a fraction of its bounding-box diagonal: 0 for a convex hull (every
    vertex on or behind every face plane). The test does not care which way a
    face is wound: it asks whether a face plane has vertices on both sides. None
    when it could not be run (no faces, or too big to compare).
    """
    if np is None:                                       # pragma: no cover
        return len(points), None, False
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if not len(points):
        return 0, None, True
    unique, inverse = np.unique(points, axis=0, return_inverse=True)
    inverse = np.asarray(inverse).reshape(-1)
    count = len(unique)
    diagonal = float(np.linalg.norm(unique.max(axis=0) - unique.min(axis=0)))
    if count < 4 or diagonal <= 0:
        return count, None, True
    centred = unique - unique.mean(axis=0)
    thinnest = np.linalg.svd(centred, full_matrices=False)[2][-1]
    spread = centred @ thinnest
    if float(spread.max() - spread.min()) <= HULL_FLAT_TOLERANCE * diagonal:
        return count, None, True
    triangles = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    if not len(triangles) or int(triangles.max()) >= len(points) or int(triangles.min()) < 0:
        return count, None, False
    corners = unique[inverse[triangles]]
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    keep = lengths > 1e-12 * diagonal * diagonal
    if not keep.any() or count * int(keep.sum()) > HULL_TEST_LIMIT:
        return count, None, False
    normals = normals[keep] / lengths[keep, None]
    offsets = np.einsum("ij,ij->i", corners[keep, 0], normals)
    worst = 0.0
    block = max(1, 1_000_000 // count)
    for start in range(0, len(normals), block):
        distances = unique @ normals[start:start + block].T - offsets[start:start + block]
        both_sides = np.minimum(distances.max(axis=0), -distances.min(axis=0))
        worst = max(worst, float(both_sides.max()))
    return count, round(max(worst, 0.0) / diagonal, 6), False


def _gltf_points_and_triangles(path: Path, document: dict, mesh: dict):
    """POSITION points and triangle indices of one glTF mesh; triangles None if unreadable."""
    accessors = document.get("accessors") or []
    points: list = []
    triangles: Optional[list] = []
    for primitive in mesh.get("primitives") or []:
        position = (primitive.get("attributes") or {}).get("POSITION")
        if not isinstance(position, int) or not 0 <= position < len(accessors):
            continue
        accessor = accessors[position]
        compressed = (accessor.get("bufferView") is None
                      or "KHR_draco_mesh_compression" in (primitive.get("extensions") or {}))
        if compressed:
            # No readable positions: count them, but they cannot be tested.
            points.extend([(float(len(points) + i), 0.0, 0.0)
                           for i in range(int(accessor.get("count", 0) or 0))])
            triangles = None
            continue
        offset = len(points)
        points.extend(read_accessor(path, document, position))
        mode = primitive.get("mode", 4)
        if triangles is None or mode != 4:
            triangles = None
            continue
        index = primitive.get("indices")
        flat = ([item[0] for item in read_accessor(path, document, index)]
                if isinstance(index, int) else list(range(len(points) - offset)))
        triangles.extend((offset + flat[i], offset + flat[i + 1], offset + flat[i + 2])
                         for i in range(0, len(flat) - 2, 3))
    return points, triangles


def gltf_structure(path: Path, document: dict) -> dict:
    """LOD chains, render-mesh names, collision shapes and skinning of one glTF.

    LOD chains come from the MSFT_lod node extension (its ``ids`` are the
    coarser levels, which sit outside the scene); without it, from node names
    ending _LOD0, _LOD1 ... Collision meshes are nodes (or, for an unnamed node,
    meshes) named UCX_/UBX_/USP_/UCP_, as Unreal's glTF import reads them.
    """
    nodes = document.get("nodes") or []
    meshes = document.get("meshes") or []
    per_mesh = [sum(_primitive_triangles(document, primitive)
                    for primitive in (mesh.get("primitives") or []))
                if isinstance(mesh, dict) else 0 for mesh in meshes]

    def node(index):
        return nodes[index] if isinstance(index, int) and 0 <= index < len(nodes) \
            and isinstance(nodes[index], dict) else None

    def mesh_of(index):
        item = node(index)
        mesh = item.get("mesh") if item else None
        return mesh if isinstance(mesh, int) and 0 <= mesh < len(meshes) else None

    def name_of(index):
        item = node(index) or {}
        mesh = mesh_of(index)
        mesh_name = meshes[mesh].get("name", "") if mesh is not None and isinstance(meshes[mesh], dict) else ""
        return _bare_name(item.get("name") or mesh_name or "")

    def walk(roots):
        order, seen, stack = [], set(), list(reversed(list(roots)))
        while stack:
            index = stack.pop()
            if node(index) is None or index in seen:
                continue                             # malformed or cyclic: never loop
            seen.add(index)
            order.append(index)
            stack.extend(reversed([child for child in node(index).get("children", []) or []]))
        return order

    scenes = document.get("scenes") or []
    scene_index = document.get("scene", 0)
    if scenes:
        scene = scenes[scene_index] if isinstance(scene_index, int) and 0 <= scene_index < len(scenes) \
            else scenes[0]
        reachable = walk((scene or {}).get("nodes", []) if isinstance(scene, dict) else [])
    else:
        reachable = walk(range(len(nodes)))

    def is_collision(index):
        return name_of(index).lower().startswith(FBX_COLLISION_PREFIXES)

    def subtree_triangles(index):
        return sum(per_mesh[mesh_of(item)] for item in walk([index])
                   if mesh_of(item) is not None and not is_collision(item))

    lods = []
    for index in reachable:
        extension = ((node(index) or {}).get("extensions") or {}).get("MSFT_lod")
        ids = extension.get("ids") if isinstance(extension, dict) else None
        if isinstance(ids, list) and ids:
            levels = [index] + [item for item in ids if node(item) is not None]
            lods.append((name_of(index) or f"node {index}",
                         tuple(subtree_triangles(item) for item in levels), "MSFT_lod"))
    if not lods:
        named = [(name_of(index), per_mesh[mesh_of(index)]) for index in reachable
                 if mesh_of(index) is not None and not is_collision(index)]
        lods = [(base, levels, "names") for base, levels in lod_chains_by_name(named)]

    render_names, shapes, skinned = set(), [], False
    for index in reachable:
        mesh = mesh_of(index)
        if "skin" in (node(index) or {}):
            skinned = True
        if mesh is None:
            continue
        if any("JOINTS_0" in (primitive.get("attributes") or {})
               for primitive in (meshes[mesh].get("primitives") or [])):
            skinned = True
        if is_collision(index):
            points, triangles = _gltf_points_and_triangles(path, document, meshes[mesh])
            if triangles is None:
                shapes.append((name_of(index), len(points), None, False))
            else:
                shapes.append((name_of(index), *hull_shape(points, triangles)))
            continue
        render_names.update(name for name in (_bare_name((node(index) or {}).get("name", "")),
                                               _bare_name(meshes[mesh].get("name", ""))) if name)
    return {"lod_groups": tuple(lods), "render_names": tuple(sorted(render_names)),
            "collision_shapes": tuple(shapes), "skinned": skinned}


def asset_structure(path: Path) -> Optional[dict]:
    """LOD chains and collision of a mesh file, or None for a format with neither (.obj)."""
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        summary = read_fbx(path)
        return {"lod_groups": summary.lod_groups, "render_names": summary.render_names,
                "collision_shapes": summary.collision_shapes, "skinned": summary.skinned}
    if suffix in GLTF_SUFFIXES:
        try:
            return gltf_structure(path, read_gltf_document(path))
        except AssetError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OSError) as exc:
            raise AssetError(f"damaged glTF ({_describe_unexpected(exc)}); re-export it") from exc
    return None


def collision_target(name: str, render_names: Sequence[str]) -> bool:
    """Whether Unreal attaches collision mesh *name* to one of *render_names*.

    Unreal's FBX import reads UCX_<RenderMeshName> and UCX_<RenderMeshName>_<suffix>
    (its documentation: UCX_Tree_01_00 and UCX_Tree_01_01 both belong to Tree_01).
    Names compare exactly, case included, as Unreal compares them.
    """
    _prefix, _sep, rest = _bare_name(name).partition("_")
    candidates = {rest}
    if "_" in rest:
        candidates.add(rest.rsplit("_", 1)[0])
    candidates.discard("")
    return bool(candidates & set(render_names))


def _share(fraction: float) -> str:
    """12%, 3.4%: a fraction (0-1) as a percentage, for the LOD and collision lines."""
    return f"{fraction:.0%}" if fraction >= 0.1 else f"{fraction:.1%}"


def lod_collision_problems(path: Path, structure: Optional[dict], budget: "Budget",
                           ) -> tuple[list[str], list[str], list[str], list[tuple[str, int]]]:
    """(problems, warnings, metrics, stats) of the [lod] and [collision] rules."""
    problems: list[str] = []
    warnings: list[str] = []
    metrics: list[str] = []
    stats: list[tuple[str, int]] = []
    suffix = path.suffix.lower()
    lod_on = budget.lod_min_levels > 0 or budget.lod_max_ratio > 0
    collision_on = (budget.collision_require or budget.collision_name_match
                    or budget.collision_convex or budget.collision_max_hulls > 0
                    or budget.collision_max_hull_vertices > 0)

    if lod_on:
        chains = tuple((structure or {}).get("lod_groups", ()))
        levels = min((len(chain[1]) for chain in chains), default=1)
        shortest = min(chains, key=lambda chain: len(chain[1]), default=None)
        stats.append(("lod_levels", levels))
        if shortest is not None:
            stats.extend((f"lod{level}_triangles", count) for level, count in enumerate(shortest[1]))
        metrics.append(f"{levels} LODs" if levels > 1 else "no LODs")
        if any(chain[2] == "names" for chain in chains) and suffix in FBX_SUFFIXES:
            warnings.append("LODs found by name only (_LOD1, ...): Unity builds an LOD group from "
                            "these, but Unreal reads an FBX's LODs only from an LOD group")
        need = budget.lod_min_levels
        if need and levels < need:
            missing = f"LOD{levels}" + (f"-LOD{need - 1}" if need - 1 > levels else "")
            if not chains:
                how = {".obj": ".obj cannot carry LODs: export FBX with an LOD group"}.get(
                    suffix, "export an LOD group" if suffix in FBX_SUFFIXES
                    else "add them as MSFT_lod levels or as nodes named <mesh>_LOD1, ...")
                problems.append(f"no LODs (LOD0 only); at least {need} levels are required: "
                                f"add {missing} ({how})")
            else:
                problems.append(f"{levels} LOD levels in '{shortest[0]}'; at least {need} are "
                                f"required: add {missing}")
        limit = budget.lod_max_ratio
        if limit:
            for name, counts, _source in chains:
                for level in range(1, len(counts)):
                    above, here = counts[level - 1], counts[level]
                    if here == 0:
                        problems.append(f"LOD{level} of '{name}' has no triangles: re-export it "
                                        "or remove the level")
                    elif above and here / above > limit:
                        problems.append(
                            f"LOD{level} of '{name}' keeps {here / above:.2f} of LOD{level - 1}'s "
                            f"triangles ({here:,} of {above:,}); at most {limit:g} is allowed: "
                            f"reduce it to {int(limit * above):,} triangles or fewer")

    if collision_on:
        if structure is None:
            if budget.collision_require:
                warnings.append(f"collision not checked: {suffix or 'this format'} carries no "
                                "collision Unreal reads; export FBX for UCX_ collision")
            return problems, warnings, metrics, stats
        shapes = tuple(structure.get("collision_shapes", ()))
        render_names = tuple(structure.get("render_names", ()))
        hulls = [shape for shape in shapes if shape[0].upper().startswith("UCX_")]
        stats.append(("collision_shapes", len(shapes)))
        if hulls:
            stats.append(("collision_hulls", len(hulls)))
            stats.append(("max_hull_vertices", max(shape[1] for shape in hulls)))
        metrics.append(f"{len(shapes)} collision")
        if budget.collision_require and not shapes and not structure.get("skinned"):
            example = render_names[0] if render_names else path.stem
            problems.append(f"no collision: a static mesh needs a UCX_ (or UBX_/USP_/UCP_) mesh "
                            f"named after its render mesh, such as UCX_{example}_00 (a skinned mesh "
                            "is exempt: it uses a physics asset)")
        if budget.collision_name_match and render_names:
            orphans = [shape[0] for shape in shapes if not collision_target(shape[0], render_names)]
            if orphans:
                prefix = orphans[0].split("_", 1)[0].upper()
                problems.append(
                    f"collision mesh{'es' if len(orphans) > 1 else ''} {_name_list(orphans)} "
                    f"match{'' if len(orphans) > 1 else 'es'} no render mesh, so Unreal ignores "
                    f"{'them' if len(orphans) > 1 else 'it'} on import: name "
                    f"{'each' if len(orphans) > 1 else 'it'} {prefix}_<mesh> or {prefix}_<mesh>_00 "
                    f"after {'one of ' if len(render_names) > 1 else ''}{_name_list(render_names)} "
                    "(case matters)")
        if budget.collision_max_hulls and len(hulls) > budget.collision_max_hulls:
            problems.append(f"{len(hulls)} convex hulls (UCX_) exceed the {budget.collision_max_hulls} "
                            f"hull budget: merge or remove {len(hulls) - budget.collision_max_hulls}")
        cap = budget.collision_max_hull_vertices
        if cap:
            heavy = [shape for shape in hulls if shape[1] > cap]
            if heavy:
                named = ", ".join(f"'{shape[0]}' ({shape[1]:,} vertices)" for shape in heavy[:3])
                if len(heavy) > 3:
                    named += f" and {len(heavy) - 3} more"
                problems.append(f"collision hull{'s' if len(heavy) > 1 else ''} {named} "
                                f"{'are' if len(heavy) > 1 else 'is'} over the {cap}-vertex hull "
                                f"limit: simplify {'them' if len(heavy) > 1 else 'it'}")
        if budget.collision_convex:
            flat = [shape[0] for shape in hulls if shape[3]]
            dented = [shape for shape in hulls if not shape[3] and shape[2] is not None
                      and shape[2] > HULL_CONCAVITY_TOLERANCE]
            untested = [shape[0] for shape in hulls if not shape[3] and shape[2] is None]
            if flat:
                problems.append(f"collision hull{'s' if len(flat) > 1 else ''} {_name_list(flat)} "
                                f"{'are' if len(flat) > 1 else 'is'} flat (no volume): Unreal "
                                "cannot build a convex hull from a plane; give it thickness")
            for name, _vertices, concavity, _flat in dented:
                problems.append(f"collision hull '{name}' is not convex (a dent {_share(concavity)} "
                                "of its size): Unreal replaces it with its convex hull on import, "
                                "filling the dent; split it into convex pieces "
                                f"({name.split('_', 1)[0]}_<mesh>_00, _01, ...)")
            if untested:
                warnings.append(f"collision hull{'s' if len(untested) > 1 else ''} "
                                f"{_name_list(untested)} not tested for convexity (compressed, "
                                "not triangles, or too large to compare)")
    return problems, warnings, metrics, stats


class Gatekeeper:
    """Applies the budget rules and returns a Verdict. No I/O side effects.

    The single exception is healing, and it is opt-in: pass a ``Healer`` and a
    failing asset is repaired *only if* the repaired copy then verifies as
    passing. With ``healer=None`` (the default) this class never writes a byte.
    """

    def __init__(self, budget: Budget, healer: Optional[object] = None,
                 overrides: Optional[dict] = None,
                 tag_budgets: Optional[dict[str, Budget]] = None,
                 tag_mode: str = "suggest",
                 tag_similarity: float = DEFAULT_TAG_SIMILARITY,
                 tag_root: Optional[Path] = None,
                 root: Optional[Path] = None,
                 exceptions: Sequence["BudgetException"] = (),
                 today: Optional[date] = None) -> None:
        self.budget = budget
        self.exceptions = tuple(exceptions)
        self.today = today
        # file -> the path [[exceptions]] see it under (the P4 trigger's temp copy
        # stands for a depot file: --exception-path)
        self.exception_paths: dict = {}
        # Measure mesh memory even without a per-asset budget: [[totals]] add it up.
        self.want_memory = False
        self.healer = healer
        self.tag_budgets = tag_budgets or {}
        self.tag_mode = tag_mode if tag_mode in TAG_MODES else "suggest"
        # CLI flags, re-applied on top of any tag budget: the documented order
        # is flags last, and a tag must not silently outrank an explicit flag.
        self.overrides = dict(overrides or {})
        self.tag_similarity = tag_similarity
        self.tag_root = tag_root
        # The project boundary, for the reference-escape check. Separate from
        # tag_root only so a caller can point tagging at a category root while
        # the boundary stays the watch folder.
        self.root = root if root is not None else tag_root
        self._naming = build_naming_pattern(budget.texture_suffixes) if budget.enforce_texture_naming else None

    def _exceptions_for(self, path: Path, budget: Budget) -> tuple[Budget, list[str], list[str]]:
        """Apply the active exceptions for this path; describe the expired ones."""
        if not self.exceptions:
            return budget, [], []
        today = self.today or date.today()
        notes: list[str] = []
        expired: list[str] = []
        changes: dict[str, object] = {}
        seen_as = self.exception_paths.get(path, path)
        for exception in self.exceptions:
            if not exception.matches(seen_as):
                continue
            if exception.active(today):
                changes[exception.rule] = exception.allow
                left = (exception.expires - today).days
                soon = (f" - ends in {left} day{'s' if left != 1 else ''}"
                        if left <= EXCEPTION_WARN_DAYS else "")
                notes.append(f"exception: {exception.label()} until {exception.expires.isoformat()} "
                             f"({exception.owner}: {exception.reason}){soon}")
            else:
                expired.append(f"the exception {exception.label()} ({exception.owner}) expired on "
                               f"{exception.expires.isoformat()}")
        if changes:
            budget = replace(budget, **changes)
        return budget, notes, expired

    def check(self, path: Path) -> Verdict:
        budget, tag, warning = self._budget_for(path)
        budget, exception_notes, expired = self._exceptions_for(path, budget)
        previous = self.budget
        if budget is not previous:
            # One asset is inspected at a time (single worker thread), so a
            # scoped swap is safe and avoids threading a budget through every
            # analyzer signature.
            self.budget = budget
            self._naming = (build_naming_pattern(budget.texture_suffixes)
                            if budget.enforce_texture_naming else None)
        try:
            try:
                verdict = self._inspect(path)
            except (KeyboardInterrupt, SystemExit, MemoryError):
                raise
            except Exception as exc:
                # One damaged file must never stop a folder scan, a watch, a
                # trigger or a report: it becomes an ERROR verdict like any
                # other unreadable file, and the run carries on (6 Nov corpus:
                # an out-of-range glTF index used to abort --once outright).
                LOG.debug("unexpected error inspecting %s", path, exc_info=True)
                verdict = Verdict(path, (f"could not be checked ({_describe_unexpected(exc)})",),
                                  unreadable=True)
            if verdict.problems and self.healer is not None:
                verdict = self._heal(path, verdict)
        finally:
            self.budget = previous
            self._naming = (build_naming_pattern(previous.texture_suffixes)
                            if previous.enforce_texture_naming else None)

        notes = list(verdict.warnings) + exception_notes
        if expired and verdict.problems:
            # Only when it fails: an asset that now fits the normal budget does
            # not need its old waiver, expired or not.
            verdict = Verdict(verdict.path, verdict.problems + tuple(expired), verdict.metric,
                              verdict.stats, verdict.heal_notes, verdict.warnings,
                              verdict.unreadable)
        if warning:
            if self.tag_mode == "strict":
                # Strict mode turns a near miss into a failure, with the fix in
                # the message: that is the whole point of not guessing politely.
                problems = (warning + " (strict tag mode: rename the folder to match)",) + verdict.problems
                return Verdict(verdict.path, problems, verdict.metric, verdict.stats,
                               verdict.heal_notes, tuple(notes), verdict.unreadable)
            notes.append(warning)
        if notes:
            # unreadable rides along: an ERROR with a note attached is still an
            # ERROR, not a FAILED budget.
            return Verdict(verdict.path, verdict.problems, verdict.metric, verdict.stats,
                           verdict.heal_notes, tuple(notes), verdict.unreadable)
        return verdict

    def _budget_for(self, path: Path) -> tuple[Budget, Optional[str], Optional[str]]:
        if not self.tag_budgets:
            return self.budget, None, None
        tag, warning = resolve_tag(path, self.tag_budgets, self.tag_root, self.tag_similarity)
        if tag is None:
            return self.budget, None, None
        if self.tag_mode == "strict" and warning:
            # Do not apply a guess in strict mode; let the caller fail it.
            return self.budget, tag, warning
        # CLI flags win over a tag budget (documented order: flags last).
        if self.overrides:
            try:
                return replace(self.tag_budgets[tag], **self.overrides), tag, warning
            except TypeError:
                return self.tag_budgets[tag], tag, warning
        return self.tag_budgets[tag], tag, warning

    def _heal(self, path: Path, verdict: Verdict) -> Verdict:
        """Attempt a repair, then re-inspect with the same budget.

        ``_inspect`` (not ``check``) is the checker on purpose: the healer's
        output must be judged by the rules, never by the healer itself.
        """
        try:
            result = self.healer.heal(path, self._inspect)  # type: ignore[attr-defined]
        except Exception as exc:  # healing must never turn a FAILED into a crash
            LOG.warning("healing %s failed: %s", path.name, exc)
            return verdict
        if result is None:
            return verdict          # nothing safe to do; the original is untouched
        if getattr(result, "dry_run", False):
            # Verified in staging, nothing written: still FAILED on disk, and
            # the notes say what a real heal would do.
            return Verdict(verdict.path, verdict.problems, verdict.metric, verdict.stats,
                           (), verdict.warnings + tuple(
                               f"--heal would: {note}" for note in result.modifications))
        healed = self._inspect(result.path)
        if healed.problems:         # belt and braces: never report a lie
            LOG.warning("healing %s did not satisfy the budget: %s",
                        result.path.name, healed.reason)
            return verdict
        return Verdict(result.path, (), healed.metric, healed.stats, result.modifications)

    def _inspect(self, path: Path) -> Verdict:
        suffix = path.suffix.lower()
        if suffix in MESH_SUFFIXES:
            return self._check_mesh(path)
        if suffix in TEXTURE_SUFFIXES:
            return self._check_texture(path)
        return Verdict(path)

    # - meshes - #

    def _check_mesh(self, path: Path) -> Verdict:
        try:
            triangles = count_triangles(path)
        except AssetError as exc:
            return Verdict(path, (str(exc),))

        problems: list[str] = []
        warnings: list[str] = []
        metrics = [f"{triangles:,} tris"]
        stats: list[tuple[str, int]] = [("triangles", triangles)]
        if path.suffix.lower() in GLTF_SUFFIXES:
            # Repeated parts (one wheel placed twice) count every time - Unreal
            # bakes them into the mesh - and the unique count is kept for the
            # dashboard and --json.
            try:
                unique = gltf_triangle_counts(read_gltf_document(path))[0]
            except Exception:
                unique = triangles
            if unique != triangles:
                stats.append(("unique_triangles", unique))
        naming = mesh_name_problem(path.stem, self.budget)
        if naming:
            problems.append(naming)

        if triangles == 0:
            # A zero-triangle mesh is never a legitimate export: it means a
            # truncated write, an empty collection or a broken DCC round-trip.
            problems.append("mesh contains no triangles (empty or corrupt export)")
        else:
            limit = self.budget.max_triangles
            if triangles > limit:
                problems.append(
                    f"{triangles:,} triangles exceeds the {limit:,} budget (+{triangles - limit:,})"
                )

        try:
            slots = count_material_slots(path)
        except AssetError as exc:
            problems.append(str(exc))
        else:
            metrics.append(f"{slots} mat")
            stats.append(("material_slots", slots))
            limit = self.budget.max_material_slots
            if slots > limit:
                problems.append(
                    f"{slots:,} material slots exceeds the {limit:,} slot budget (draw calls)"
                )

        undecodable = gltf_undecodable_compression(path)
        if undecodable:
            warnings.append(undecodable)
        if gltf_uses_draco(path):
            # Measured 6 Nov: Unreal Engine 5.3's glTF importer created nothing
            # from a Draco-compressed .glb (duck.glb) and reported no error.
            warnings.append("compressed with Draco: Unreal Engine 5.3 imports nothing from such a "
                            "file; re-export it without Draco compression for Unreal")
        if path.suffix.lower() == ".obj":
            colour = obj_colour_source(path)
            if colour:
                what = "ZBrush Polypaint" if colour == "polypaint" else "vertex colours"
                warnings.append(
                    f"its colour is {what} (one colour per vertex), not a texture: Maya, 3ds Max "
                    "and most engines show it grey, and reducing the mesh loses it. Bake it into "
                    f"a texture with: asset-gatekeeper \"{path.name}\" --bake-polypaint")

        try:
            influences = 0 if undecodable else max_bone_influences(path)
        except AssetError as exc:
            problems.append(str(exc))
        else:
            if influences:
                metrics.append(f"{influences} inf")
                stats.append(("max_bone_influences", influences))
                limit = self.budget.max_bone_influences
                if influences > limit:
                    problems.append(
                        f"{influences} bone weights on one vertex exceeds the {limit} influence limit"
                    )

        try:
            uv_channels = count_uv_channels(path)
        except AssetError as exc:
            problems.append(str(exc))
        else:
            if uv_channels:
                metrics.append(f"{uv_channels} uv")
                stats.append(("uv_channels", uv_channels))
                limit = self.budget.max_uv_channels
                if uv_channels > limit:
                    problems.append(f"{uv_channels} UV channels exceeds the {limit} channel limit")

        # [skeleton] and [lightmap]: off by default, read only when a rule is on.
        if self.budget.skeleton_checked:
            self._check_skeleton(path, problems, warnings, metrics, stats)
        if self.budget.lightmap_checked:
            self._check_lightmap(path, problems, warnings, metrics, stats)

        if (not self.budget.allow_degenerate and self.budget.degenerate_mode != "allow"
                and not undecodable):
            try:
                degenerate = count_degenerate_triangles(path, self.budget.degenerate_epsilon,
                                                        self.budget.degenerate_ratio)
            except AssetError as exc:
                problems.append(str(exc))
            else:
                stats.append(("degenerate_triangles", degenerate))
                if degenerate:
                    message = (f"{degenerate:,} degenerate triangles (flat or zero-area) - Nanite "
                               f"and ray tracing compute garbage from these")
                    (warnings if self.budget.degenerate_mode == "warn" else problems).append(message)

        try:
            packed = embedded_texture_sizes(path)
        except AssetError:
            packed = []
        if packed:
            stats.append(("embedded_textures", len(packed)))
            edge = self.budget.max_texture_edge
            too_big = [(label, width, height) for label, width, height in packed
                       if max(width, height) > edge]
            not_pot = [(label, width, height) for label, width, height in packed
                       if self.budget.require_power_of_two
                       and not (is_power_of_two(width) and is_power_of_two(height))]
            # Up to three are named one per line; more become one line, so a
            # file with 100 packed textures (NodePerformanceTest.glb) is not a
            # hundred-line verdict.
            for found, wording in ((too_big, f"exceeds the {edge}x{edge} maximum resolution"),
                                   (not_pot, "is not a power of two")):
                if len(found) <= 3:
                    problems.extend(f"embedded texture '{label}' {width}x{height} {wording}"
                                    for label, width, height in found)
                else:
                    named = ", ".join(f"'{label}' {width}x{height}" for label, width, height in found[:3])
                    verb = wording.replace("exceeds", "exceed").replace("is not a power", "are not powers")
                    problems.append(f"{len(found)} embedded textures {verb} ({named} and "
                                    f"{len(found) - 3} more)")

        # Memory (G2) and texel density (G8): measured when budgeted, or when a
        # [[totals]] line needs the number.
        want_memory = self.budget.max_mesh_mb > 0 or self.want_memory
        want_texel = self.budget.texel_density_max > 0
        if want_memory or want_texel:
            try:
                if path.suffix.lower() in FBX_SUFFIXES:
                    facts = engine_module("fbx_preview").measure(path, self.budget.texel_texture)
                else:
                    facts = mesh_facts(path, self.budget.texel_texture)
            except Exception as exc:          # a measurement must never break the verdict
                warnings.append(f"mesh memory / texel density not measured ({exc})")
            else:
                known = dict(stats)
                if want_memory:
                    size = mesh_memory_bytes(facts["vertices"], triangles,
                                             int(known.get("uv_channels", 0)),
                                             int(known.get("max_bone_influences", 0)) > 0,
                                             facts["colours"],
                                             self.budget.mesh_lightmap_uv,
                                             self.budget.mesh_depth_index_buffer)
                    stats.append(("memory_kb", size // 1024))
                    stats.append(("vertices", facts["vertices"]))
                    if self.budget.max_mesh_mb:
                        metrics.append(f"~{_mb(size)}")
                        if size > self.budget.max_mesh_mb * 1024 * 1024:
                            problems.append(
                                f"~{_mb(size)} estimated GPU memory exceeds the "
                                f"{self.budget.max_mesh_mb:g} MB mesh budget "
                                f"({facts['vertices']:,} vertices)")
                if want_texel:
                    density = facts["texel_density"]
                    if density is None:
                        warnings.append("no UVs: texel density not checked")
                    else:
                        stats.append(("texel_density", int(round(density))))
                        metrics.append(f"{density:,.0f} px/m")
                        assumed = (f" (assuming a {self.budget.texel_texture}px texture)"
                                   if facts["assumed_texture"] else "")
                        low, high = self.budget.texel_density_min, self.budget.texel_density_max
                        if density < low:
                            problems.append(f"texel density {density:,.0f} px/m is below the "
                                            f"{low:,.0f} px/m minimum{assumed}")
                        elif density > high:
                            problems.append(f"texel density {density:,.0f} px/m is above the "
                                            f"{high:,.0f} px/m maximum{assumed}")
                        if facts["without_uv"]:
                            warnings.append(f"{facts['without_uv']} mesh(es) without UVs are "
                                            "not in the texel density")

        # Broken references are unconditional when enabled: a .gltf pointing at a
        # texture nobody shipped fails to load in every engine, on every platform.
        if self.budget.check_references:
            # One line per missing file: an FBX that names ALPHA.png from two
            # materials used to report it twice (Head_69.fbx, 6 Nov).
            seen_written: set = set()
            broken = [item for item in missing_references(path)
                      if not (item[0] in seen_written or seen_written.add(item[0]))]
            broken_names = {written for written, _target in broken}
            for written, _target in broken:
                problems.append(
                    f"missing reference '{written}' (broken link: the engine cannot load it)")
            if broken:
                stats.append(("broken_references", len(broken)))
            # A reference that leaves the watch folder is a build-machine failure
            # waiting to happen, and a path-traversal hazard for anything that
            # follows it on trust. Studios with a shared library opt out in rules.
            escaped = [item for item in escaping_references(path, self.root)
                       if item[0] not in broken_names]
            if escaped:
                if self.budget.allow_outside_references:
                    warnings.extend(f"reference '{written}' points outside the project "
                                    "folder ([mesh] allow_outside_references is on)"
                                    for written, _target in escaped)
                else:
                    problems.extend(
                        f"reference '{written}' points outside the project folder "
                        "(the build machine will not have it; set "
                        "allow_outside_references in [mesh] if it is a shared library)"
                        for written, _target in escaped)
                stats.append(("outside_references", len(escaped)))

        # Transform/pivot/index audits are opt-in (see Budget.audit_transforms).
        if self.budget.audit_transforms or self.budget.require_indexed:
            try:
                problems.extend(transform_audit(path, self.budget))
            except Exception as exc:      # an audit must never break the verdict
                LOG.debug("transform audit failed for %s: %s", path.name, exc)

        # LODs and collision ([lod], [collision]): opt-in, every key off by default.
        budget = self.budget
        if triangles and (budget.lod_min_levels or budget.lod_max_ratio or budget.collision_require
                          or budget.collision_name_match or budget.collision_convex
                          or budget.collision_max_hulls or budget.collision_max_hull_vertices):
            try:
                found = lod_collision_problems(path, asset_structure(path), budget)
            except AssetError as exc:
                problems.append(f"LODs / collision not read ({exc})")
            else:
                problems.extend(found[0])
                warnings.extend(found[1])
                metrics.extend(found[2])
                stats.extend(found[3])

        return Verdict(path, tuple(problems), " / ".join(metrics), tuple(stats),
                       warnings=tuple(warnings))

    def _check_skeleton(self, path: Path, problems: list, warnings: list, metrics: list,
                        stats: list) -> None:
        """[skeleton]: bones, bones per material section, roots and chain depth."""
        budget = self.budget
        try:
            skeleton = skeleton_facts(path)
        except AssetError as exc:
            problems.append(str(exc))
            return
        except Exception as exc:              # a skeleton read must never break the verdict
            warnings.append(f"skeleton not checked ({_describe_unexpected(exc)})")
            return
        if not skeleton or not skeleton["bones"]:
            return                            # nothing skinned: the rules do not apply
        bones, roots, depth = skeleton["bones"], skeleton["roots"], skeleton["depth"]
        metrics.append(f"{bones} bones")
        stats.extend((("bones", bones), ("root_bones", len(roots)), ("bone_depth", depth)))
        if budget.max_bones and bones > budget.max_bones:
            problems.append(f"{bones:,} bones in the skeleton exceeds the {budget.max_bones:,} "
                            f"bone budget (+{bones - budget.max_bones:,}): remove helper or "
                            "unused bones before export, or split the character")
        sections = skeleton["sections"]
        if skeleton["unread_sections"]:
            sections = []      # not counted, rather than counted as 0 bones
            warnings.append("bones per material section not counted: the skin weights are "
                            "compressed, and this check reads plain vertex data only")
        if sections:
            stats.append(("max_bones_per_section", max(count for _label, count in sections)))
        limit = budget.max_bones_per_section
        over = sorted(((count, label) for label, count in sections if limit and count > limit),
                      reverse=True)
        if over:
            count, label = over[0]
            where = (f"material section '{label}' is skinned to {count} bones" if len(over) == 1
                     else f"{len(over)} material sections are skinned to more than {limit} bones "
                          f"(worst '{label}': {count})")
            problems.append(f"{where}, over the {limit} bones-per-section limit: the engine "
                            "splits such a section into extra draw calls (Unreal on mobile "
                            "allows 75); weight it to fewer bones or give some of its faces "
                            "another material")
        if budget.single_root_bone and len(roots) != 1:
            problems.append(f"skeleton has {len(roots)} root bones ({_name_list(roots)}): Unreal "
                            "imports one root only (\"Multiple roots are found in the bone "
                            "hierarchy\"); parent every bone under a single root bone")
        if budget.max_bone_depth and depth > budget.max_bone_depth:
            problems.append(f"bone chain is {depth} bones deep (root to "
                            f"'{skeleton['deepest']}'), over the {budget.max_bone_depth} depth "
                            "limit: shorten the chain or remove in-between bones")

    def _check_lightmap(self, path: Path, problems: list, warnings: list, metrics: list,
                        stats: list) -> None:
        """[lightmap]: an authored lightmap UV set on static meshes, inside 0-1, no overlap."""
        budget = self.budget
        channel = budget.lightmap_uv_channel
        wanted_overlap = budget.lightmap_max_overlap >= 0
        try:
            facts = lightmap_facts(path, channel, budget.lightmap_resolution, wanted_overlap)
        except AssetError as exc:
            problems.append(str(exc))
            return
        except Exception as exc:              # a lightmap read must never break the verdict
            warnings.append(f"lightmap UVs not checked ({_describe_unexpected(exc)})")
            return
        label = f"UV channel {channel}, the {_ordinal(channel + 1)} set"
        if facts["unread"]:
            warnings.append(f"lightmap UVs not fully checked: {facts['unread']}")
        if budget.lightmap_require:
            if facts["obj_single_set"]:
                problems.append(f"no lightmap UV set: an OBJ file holds one UV set, so {label} "
                                "cannot exist; export FBX or glTF with a lightmap UV set, or set "
                                "[lightmap] uv_channel = 0 if the first set is the lightmap")
                stats.append(("lightmap_missing", 1))
            elif facts["missing"]:
                missing = facts["missing"]
                which = (f"static mesh '{missing[0]}'" if len(missing) == 1
                         else f"{len(missing)} static meshes ({_name_list(missing)})")
                problems.append(f"no lightmap UV set ({label}) on {which}: add a UV set unwrapped "
                                "for lightmaps (no overlaps, inside 0-1), or switch off "
                                "[lightmap] require to let the engine generate one")
                stats.append(("lightmap_missing", len(missing)))
        if budget.lightmap_inside_unit_square and facts["uvs"]:
            stats.append(("lightmap_uvs_outside", facts["outside"]))
            if facts["outside"]:
                u, v = facts["worst"]
                problems.append(f"{facts['outside']:,} of {facts['uvs']:,} lightmap UVs ({label}) "
                                f"are outside 0-1 (worst at u {u:.3f}, v {v:.3f}): a lightmap does "
                                "not tile, so those texels get another surface's light; move "
                                "every lightmap island inside the 0-1 square")
        if wanted_overlap:
            collapsed = facts["collapsed"]
            if collapsed:
                which = (f"'{collapsed[0]}'" if len(collapsed) == 1
                         else f"{len(collapsed)} meshes ({_name_list(collapsed)})")
                problems.append(f"lightmap UVs ({label}) of {which} cover no texel of a "
                                f"{budget.lightmap_resolution}x{budget.lightmap_resolution} grid "
                                "(collapsed or far too small): unwrap the lightmap UVs again")
            if facts["overlap"] is not None:
                percent, overlapped, covered, name, grid = facts["overlap"]
                stats.extend((("lightmap_overlap_texels", overlapped), ("lightmap_texels", covered)))
                metrics.append(f"lightmap {_percent(percent)}% overlap")
                if grid < budget.lightmap_resolution:
                    warnings.append(f"lightmap overlap measured on a {grid}x{grid} grid, not "
                                    f"{budget.lightmap_resolution}x{budget.lightmap_resolution}: "
                                    "its triangles cover the square many times over")
                if overlapped and percent > budget.lightmap_max_overlap:
                    problems.append(f"lightmap UVs ({label}) overlap on {_percent(percent)}% of "
                                    f"their texels in '{name}' ({overlapped:,} of {covered:,} on a "
                                    f"{grid}x{grid} grid), over the "
                                    f"{budget.lightmap_max_overlap:g}% limit: overlapping islands "
                                    "share baked light and bleed into each other; repack the "
                                    "lightmap UVs so no islands overlap")

    # - textures - #

    def _check_texture(self, path: Path) -> Verdict:
        try:
            width, height = measure_texture(path)
        except AssetError as exc:
            return Verdict(path, (str(exc),))

        problems: list[str] = []

        limit = self.budget.max_texture_edge
        if max(width, height) > limit:
            problems.append(f"{width}x{height} exceeds the {limit}x{limit} maximum resolution")

        if self.budget.require_power_of_two and not (is_power_of_two(width) and is_power_of_two(height)):
            problems.append(f"{width}x{height} is not a power of two")

        # GPU memory in the target's compressed format, mips included (G2).
        texture_format = (self.budget.normal_format if is_normal_name(path)
                          else self.budget.texture_format)
        memory = texture_memory_bytes(width, height, texture_format)
        memory_metric = ""
        if self.budget.max_texture_mb:
            memory_metric = f" ~{_mb(memory)} {texture_format.upper()}"
            if memory > self.budget.max_texture_mb * 1024 * 1024:
                problems.append(f"~{_mb(memory)} as {texture_format.upper()} with mips exceeds the "
                                f"{self.budget.max_texture_mb:g} MB texture budget")

        if self._naming is not None:
            naming_problem = texture_name_problem(path.stem, self.budget)
            if naming_problem:
                problems.append(naming_problem)

        # PBR sanitisation applies to albedo/diffuse only: a normal map is
        # allowed to be flat violet-blue, and a roughness map is allowed to be
        # pure white, because neither is a reflectance colour.
        if self.budget.pbr_check and self._is_pbr(path):
            violation = pbr_violation(path, self.budget)
            if violation:
                problems.append(violation)

        return Verdict(path, tuple(problems), f"{width}x{height}{memory_metric}",
                       (("width", width), ("height", height), ("memory_kb", memory // 1024)))

    def _is_pbr(self, path: Path) -> bool:
        if path.suffix.lower() in HEADER_ONLY_TEXTURES:
            return False              # sizes come from the header; pixels are not decoded
        stem = path.stem.lower()
        return any(stem.endswith(suffix.lower()) for suffix in self.budget.pbr_suffixes)


# --- #
# Reporting
# --- #

def _safe_username() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no passwd entry / locked-down container
        return ""


def build_telemetry_payload(verdict: Verdict, root: Optional[Path] = None,
                            identity: str = DEFAULT_IDENTITY) -> dict:
    """One flat JSON object per asset: the studio's record of what was gated.

    Deliberately flat - a data warehouse can ingest it without a nested schema,
    and the plugin contract (``--json``) stays a superset of these keys.

    ``identity`` decides how much of the artist's machine travels with it; see
    ``ServerSettings.identity``. The keys are always present, because the ingest
    contract is a fixed schema - what changes is whether they carry a value.
    """
    detail = identity if identity in IDENTITY_MODES else DEFAULT_IDENTITY
    path = verdict.path
    relative = path.name
    if root is not None:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:  # single-file mode: outside the watch folder
            pass
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = -1
    absolute = (str((root / path).resolve())
                if root is not None and not Path(path).is_absolute() else str(path))
    full = detail == "full"
    # "host" keeps the machine name: a dashboard groups by workstation, and a
    # fleet of "unknown" buckets is worse than useless. What it drops is the OS
    # account, which is the identifying half.
    include_host = detail in ("full", "host")
    return {
        "schema": PAYLOAD_SCHEMA,
        "timestamp": _utc_now(),
        "hostname": platform.node() if include_host else "",
        "username": _safe_username() if full else "",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "name": path.name,
        # Absolute by default: the dashboard hands this straight to the
        # workstation when a reviewer clicks "Send to Workstation", so a bare
        # relative name is not enough. The relative form is kept for display and
        # grouping, and identity="none" sends only that.
        "path": absolute if detail in ("full", "host") else relative,
        "relative_path": relative,
        "size_bytes": size_bytes,
        "passed": verdict.passed,
        # PASSED / HEALED / FAILED / ERROR: a healed asset passed, but the studio
        # still wants to know it was rewritten, and what was done to it.
        "status": verdict.status,
        "metric": verdict.metric,
        "stats": dict(verdict.stats),
        "problems": list(verdict.problems),
        "modifications": list(verdict.heal_notes),
        "warnings": list(verdict.warnings),
    }


class Telemetry:
    """Fire-and-forget POST of every verdict to the studio endpoint.

    Three rules, in priority order:

    1. **Never raise.** A dead endpoint is a debug line, not a crash - the artist
       still gets their PASSED/FAILED verdict either way.
    2. **Never block the gate.** ``submit()`` only enqueues; one daemon thread
       owns the sockets. A 2 s timeout can therefore delay the *network*, never
       the inspection.
    3. **Never grow without bound.** The queue is capped; past that, payloads are
       dropped and counted rather than slowing the local run.
    """

    def __init__(self, url: str, root: Optional[Path] = None, token: str = "",
                 timeout: float = HTTP_TIMEOUT, queue_max: int = TELEMETRY_QUEUE_MAX,
                 identity: str = DEFAULT_IDENTITY) -> None:
        self.url = url
        self.root = root
        self.token = token
        self.identity = identity if identity in IDENTITY_MODES else DEFAULT_IDENTITY
        self.timeout = float(timeout)
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self._warned = False
        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=queue_max)
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        if not url.lower().startswith("https://"):
            # Asset names and paths are shipping in clear text; say it once.
            LOG.warning("telemetry endpoint is not https - payloads are sent in clear text")

    # - producer side - #

    def submit(self, verdict: Verdict, root: Optional[Path] = None) -> None:
        """Enqueue one verdict. Never blocks, never raises."""
        if self._closed:
            return
        try:
            payload = build_telemetry_payload(verdict, self.root if root is None else root,
                                              identity=self.identity)
        except Exception as exc:  # a malformed Verdict must not stop the line
            self.dropped += 1
            LOG.debug("telemetry: could not build payload: %s", exc)
            return
        self._ensure_worker()
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self.dropped += 1
            LOG.debug("telemetry: queue full, dropped %s", verdict.path.name)

    def _ensure_worker(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="agk-telemetry", daemon=True)
            self._thread.start()

    # - worker side - #

    def _run(self) -> None:
        while True:
            payload = self._queue.get()
            if payload is None:  # shutdown sentinel
                return
            self._post(payload)

    def _post(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                response.read()  # drain, so the socket is released promptly
            self.sent += 1
        except urllib.error.HTTPError as exc:
            self.failed += 1
            LOG.debug("telemetry: POST rejected (%s %s)", exc.code, exc.reason)
            hint = {401: "check the token (ASSET_GATEKEEPER_TOKEN)",
                    403: "if the dashboard is reached by name, start it with "
                         "--allow-host <that name>; otherwise check the token"}.get(
                exc.code, "the dashboard refused the payload")
            self._warn_once(f"refused by {self.url} ({exc.code} {exc.reason}) - {hint}")
        except Exception as exc:
            # Quiet by contract for repeats: the artist's terminal is not a network
            # monitor. But one line per run is owed - a dashboard that silently
            # stops filling is how a studio loses a month of verdicts.
            self.failed += 1
            LOG.debug("telemetry: POST failed (%s): %s", type(exc).__name__, exc)
            self._warn_once(f"cannot reach {self.url} ({type(exc).__name__})")

    def _warn_once(self, problem: str) -> None:
        if self._warned:
            return
        self._warned = True
        LOG.warning("telemetry %s; verdicts are not being recorded on the dashboard "
                    "(repeats are only shown with -v)", problem)

    # - lifecycle - #

    def close(self, timeout: Optional[float] = None) -> None:
        """Flush what is already queued, bounded by one request timeout."""
        self._closed = True
        if self._thread is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(self.timeout if timeout is None else timeout)
        LOG.debug("telemetry: %d sent, %d failed, %d dropped", self.sent, self.failed, self.dropped)
        if self.failed or self.dropped:
            LOG.warning("telemetry: %d verdict(s) not delivered to %s (%d sent)",
                        self.failed + self.dropped, self.url, self.sent)


class Reporter:
    """Formats verdicts as one terminal line each, with optional ANSI colour."""

    GREEN = "\033[32m"
    RED = "\033[31m"
    DIM = "\033[2m"
    RESET = "\033[0m"

    def __init__(self, color: bool = True, root: Optional[Path] = None,
                 json_mode: bool = False, telemetry: Optional[Telemetry] = None,
                 notifier: Optional[object] = None) -> None:
        self.color = color and self._ansi_supported()
        self.root = root
        self.json_mode = json_mode
        self.telemetry = telemetry
        self.notifier = notifier
        self.passed = 0
        self.failed = 0
        # Kept only for --report (JUnit / SARIF): a watch that runs for weeks
        # must not hold every verdict it ever printed.
        self.keep_verdicts = False
        self.verdicts: list[Verdict] = []
        self._lock = threading.Lock()

    @staticmethod
    def _ansi_supported() -> bool:
        if not sys.stdout.isatty():
            return False
        if os.name == "nt":  # enable VT processing on modern Windows terminals
            try:
                import ctypes

                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            except Exception:
                return False
        return True

    def _paint(self, text: str, code: str) -> str:
        return f"{code}{text}{self.RESET}" if self.color else text

    def display_path(self, path: Path) -> str:
        if self.root is not None:
            try:
                return str(path.relative_to(self.root))
            except ValueError:
                pass
        return str(path)

    def report(self, verdict: Verdict) -> None:
        if self.keep_verdicts:
            with self._lock:
                self.verdicts.append(verdict)
        # Telemetry mirrors the local verdict; it never influences it.
        if self.telemetry is not None:
            self.telemetry.submit(verdict)
        # Same contract for desktop toasts: queued, rate-limited, non-blocking.
        if self.notifier is not None:
            self.notifier.notify_verdict(verdict)
        if self.json_mode:
            # Machine-readable contract for the DCC plugins: one object per file.
            with self._lock:
                self.passed += int(verdict.passed)
                self.failed += int(not verdict.passed)
                print(json.dumps({
                    "file": str(verdict.path),
                    "name": verdict.path.name,
                    "passed": verdict.passed,
                    "status": verdict.status,
                    "metric": verdict.metric,
                    "problems": list(verdict.problems),
                    "modifications": list(verdict.heal_notes),
                    "warnings": list(verdict.warnings),
                    # Numbers behind the metric: triangles, memory_kb, texel density...
                    "stats": {name: value for name, value in verdict.stats},
                }), flush=True)
            return

        with self._lock:
            if verdict.passed:
                self.passed += 1
                # A healed asset passed, but the artist must be told it was rewritten.
                tag = self._paint(verdict.tag if verdict.healed else "PASSED", self.GREEN)
            else:
                self.failed += 1
                tag = self._paint(verdict.tag, self.RED)

            detail = f" {self._paint(verdict.metric, self.DIM)}" if verdict.metric else ""
            LOG.info("%s  %s%s", tag, self.display_path(verdict.path), detail)

            # Advisories sit under the verdict, dimmed: they inform, they never
            # change the exit code, and they must not look like failures.
            for warning in verdict.warnings:
                LOG.info("       %s", self._paint(f"note: {warning}", self.DIM))

    def summary(self, skipped: Optional[dict] = None) -> None:
        total = self.passed + self.failed
        if self.json_mode:
            document = {"total": total, "passed": self.passed, "failed": self.failed}
            if skipped is not None:
                document["skipped"] = skipped
            print(json.dumps({"summary": document}), flush=True)
            return
        LOG.info("%d asset(s): %d passed, %d failed", total, self.passed, self.failed)
        if skipped and skipped.get("total"):
            LOG.info("%s", describe_skipped(skipped))

    def close(self, timeout: Optional[float] = None) -> None:
        """Flush pending telemetry and toasts. Bounded, and a no-op when off."""
        if self.telemetry is not None:
            self.telemetry.close(timeout)
        if self.notifier is not None:
            self.notifier.close(timeout)


# --- #
# Watching
# --- #

#: Files that belong to the tool or the system, never mentioned as "skipped".
QUIET_NAMES = frozenset({"rules.toml", "rules.example.toml", "asset_gatekeeper.toml",
                         "thumbs.db", "desktop.ini", ".ds_store"})
#: A word of advice for the unsupported types people most often drop in.
SKIPPED_HINTS = {
    ".jfif": "a JPEG: rename it to .jpg to check it",
    ".jpe": "a JPEG: rename it to .jpg to check it",
    ".bmp": "engines want PNG or TGA",
    ".gif": "engines want PNG or TGA",
    ".psd": "a Photoshop file: export a PNG or TGA",
    ".kra": "a Krita file: export a PNG",
    ".blend": "a Blender scene: export FBX, glTF or OBJ",
    ".ztl": "a ZBrush tool: export an OBJ",
    ".zpr": "a ZBrush project: export an OBJ",
    ".ma": "a Maya scene: export FBX", ".mb": "a Maya scene: export FBX",
    ".max": "a 3ds Max scene: export FBX",
    ".spp": "a Substance Painter project: export the textures",
    ".stl": "a print mesh: export OBJ or glTF for a game",
    ".dae": "Collada: export FBX or glTF",
    ".usd": "USD is not read yet", ".usda": "USD is not read yet", ".usdz": "USD is not read yet",
}


def skipped_files(files: Iterable[Path], exclude: Sequence[str] = ()) -> dict:
    """What a run walked past and why: {"total", "unsupported": {".jfif": 6},
    "excluded": n}. Scratch files, hidden files and the tool's own (rules.toml,
    .agk folders) are not counted - only what a person put there and might
    expect to be checked. Reference images saved as .jfif used to be ignored
    without a word, so a folder looked checked when half of it was not."""
    unsupported: dict[str, int] = {}
    excluded = 0
    for path in files:
        name = path.name
        if (name.startswith(IGNORED_PREFIXES) or name.startswith(".")
                or name.lower() in QUIET_NAMES or name.lower().endswith(IGNORED_SUFFIXES)
                or any(part.startswith(IGNORED_DIR_PREFIXES) or part == ".agk"
                       for part in path.parts[:-1])):
            continue
        suffix = path.suffix.lower()
        if suffix not in TRACKED_SUFFIXES:
            key = suffix or "(no extension)"
            unsupported[key] = unsupported.get(key, 0) + 1
        elif not is_candidate(path, exclude):
            excluded += 1
    ordered = dict(sorted(unsupported.items(), key=lambda item: (-item[1], item[0])))
    return {"total": sum(ordered.values()) + excluded, "unsupported": ordered,
            "excluded": excluded}


def describe_skipped(skipped: dict, limit: int = 5) -> str:
    """6 file(s) skipped: unsupported type .jfif x6 (a JPEG: rename it to .jpg to check it)"""
    parts = []
    kinds = list(skipped.get("unsupported", {}).items())
    for suffix, count in kinds[:limit]:
        hint = SKIPPED_HINTS.get(suffix)
        parts.append(f"{suffix} x{count}" + (f" ({hint})" if hint else ""))
    if len(kinds) > limit:
        parts.append(f"{len(kinds) - limit} more type(s)")
    pieces = []
    if parts:
        pieces.append("unsupported type " + ", ".join(parts))
    if skipped.get("excluded"):
        pieces.append(f"{skipped['excluded']} by [filters] exclude / --exclude")
    return f"{skipped.get('total', 0)} file(s) skipped: " + "; ".join(pieces)


def is_candidate(path: Path, exclude: Sequence[str] = ()) -> bool:
    """Cheap pre-filter: right extension, not a hidden/scratch/excluded file.

    `exclude` holds glob patterns from `[filters] exclude` / `--exclude`. Real
    asset folders keep review renders, 4x paint sources and hashed build caches
    next to shipped textures; without this the gate reports them as budget
    violations and the real findings get lost in the noise.
    """
    name = path.name
    if name.startswith(IGNORED_PREFIXES):
        return False
    if any(part.startswith(IGNORED_DIR_PREFIXES) for part in path.parts[:-1]):
        return False
    if name.lower().endswith(IGNORED_SUFFIXES):
        return False
    if path.suffix.lower() not in TRACKED_SUFFIXES:
        return False
    if exclude:
        for pattern in exclude:
            # fnmatchcase, not fnmatch: fnmatch normalises case on Windows, which
            # would make identical rules behave differently on an artist's box and
            # on the Linux CI runner.
            if fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(str(path), pattern):
                return False
    return True


def wait_until_stable(path: Path, quiet: float = 0.35, timeout: float = 30.0,
                      settled_age: float = 5.0) -> bool:
    """Block until the file stops growing and can be opened.

    Exporters (Blender, Substance, Unreal, Maya) fire a create/modify event the
    instant the handle opens, so inspecting immediately means reading a truncated
    file. Poll size until it has been unchanged for ``quiet`` seconds.

    A file nothing has touched for ``settled_age`` seconds is not being written
    right now, so it is taken as stable immediately. Without that, ``--once`` on
    a finished repository paid 0.35 s per asset - a 4,000-asset project waited
    23 minutes before reporting a single verdict.
    """
    try:
        if time.time() - path.stat().st_mtime >= settled_age:
            return True
    except OSError:
        return False  # deleted or moved away before we looked

    deadline = time.monotonic() + timeout
    last_size: Optional[int] = None
    stable_since = 0.0

    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            return False  # deleted or moved away while we waited

        # A zero-byte file settles quickly and is a failure worth reporting, but
        # give exporters that open a handle before writing a little extra grace.
        required_quiet = quiet if size else max(quiet, 1.5)

        now = time.monotonic()
        if size == last_size:
            if now - stable_since >= required_quiet:
                try:
                    with path.open("rb") as handle:
                        handle.read(1)
                    return True
                except OSError:
                    pass  # still locked by the exporter
        else:
            last_size = size
            stable_since = now
        time.sleep(0.1)

    LOG.debug("timed out waiting for %s to settle", path.name)
    return False


class AssetEventHandler(FileSystemEventHandler):  # type: ignore[misc]
    """Feeds candidate paths into the work queue; never analyses on the event thread."""

    def __init__(self, work_queue: "queue.Queue[Optional[Path]]") -> None:
        self._queue = work_queue

    def _submit(self, raw_path: str) -> None:
        path = Path(raw_path)
        if is_candidate(path, getattr(self, "exclude", ())):
            self._queue.put(path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._submit(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._submit(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._submit(event.dest_path)


class WatchWorker(threading.Thread):
    """Single worker thread: debounces duplicates, inspects, reports."""

    def __init__(
        self,
        work_queue: "queue.Queue[Optional[Path]]",
        gatekeeper: Gatekeeper,
        reporter: Reporter,
    ) -> None:
        super().__init__(name="assetgatekeeper-worker", daemon=True)
        self._queue = work_queue
        self._gatekeeper = gatekeeper
        self._reporter = reporter
        self._last_seen: dict[Path, tuple[int, int]] = {}

    def run(self) -> None:
        while True:
            path = self._queue.get()
            try:
                if path is None:  # shutdown sentinel
                    return
                self._process(path)
            except Exception:  # never let one bad asset kill the watcher
                LOG.exception("unexpected error while processing %s", path)
            finally:
                self._queue.task_done()

    def _process(self, path: Path) -> None:
        if not is_candidate(path, getattr(self, "exclude", ())) or not path.exists():
            return
        if not wait_until_stable(path):
            LOG.debug("skipping %s (vanished or never settled)", path.name)
            return

        stat = path.stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        if self._last_seen.get(path) == fingerprint:
            return  # duplicate event from the same write (created + modified)
        self._last_seen[path] = fingerprint

        self._reporter.report(self._gatekeeper.check(path))


# --- #
# Entry points
# --- #

def describe_budget(budget: Budget) -> str:
    """One--line summary of the active rules, printed at startup."""
    return (
        f"tris<={budget.max_triangles:,}  "
        f"texture<={budget.max_texture_edge}px  "
        f"pot={'on' if budget.require_power_of_two else 'off'}  "
        f"materials<={budget.max_material_slots}  "
        f"influences<={budget.max_bone_influences}  "
        f"uvs<={budget.max_uv_channels}  "
        f"degenerate={'allowed' if budget.allow_degenerate or budget.degenerate_mode == 'allow' else ('warned' if budget.degenerate_mode == 'warn' else 'blocked')}  "
        f"naming={'on' if budget.enforce_texture_naming else 'off'}  "
        f"refs={'checked' if budget.check_references else 'ignored'}  "
        f"pbr={'on' if budget.pbr_check else 'off'}  "
        f"audit={'on' if budget.audit_transforms else 'off'}"
        + (f"  texture<={budget.max_texture_mb:g}MB({budget.texture_format})"
           if budget.max_texture_mb else "")
        + (f"  mesh<={budget.max_mesh_mb:g}MB" if budget.max_mesh_mb else "")
        + (f"  names={','.join(budget.mesh_name_prefixes)}" if budget.mesh_name_prefixes else "")
        + (f"  conventions={','.join(c[0] for c in budget.naming_conventions)}"
           if budget.naming_conventions else "")
        + (f"  texel={budget.texel_density_min:g}-{budget.texel_density_max:g}px/m"
           if budget.texel_density_max else "")
        + (f"  lod>={budget.lod_min_levels}" if budget.lod_min_levels else "")
        + (f"  lod_ratio<={budget.lod_max_ratio:g}" if budget.lod_max_ratio else "")
        + ("  collision=" + ",".join(name for name, on in (
            ("required", budget.collision_require), ("names", budget.collision_name_match),
            ("convex", budget.collision_convex),
            (f"hulls<={budget.collision_max_hulls}", budget.collision_max_hulls),
            (f"hull_verts<={budget.collision_max_hull_vertices}",
             budget.collision_max_hull_vertices)) if on)
           if (budget.collision_require or budget.collision_name_match or budget.collision_convex
               or budget.collision_max_hulls or budget.collision_max_hull_vertices) else "")
        + (f"  bones<={budget.max_bones}" if budget.max_bones else "")
        + (f"  bones/section<={budget.max_bones_per_section}"
           if budget.max_bones_per_section else "")
        + (f"  bone-depth<={budget.max_bone_depth}" if budget.max_bone_depth else "")
        + ("  single-root" if budget.single_root_bone else "")
        + (f"  lightmap=uv{budget.lightmap_uv_channel}"
           + ("(required)" if budget.lightmap_require else "")
           + ("(0-1)" if budget.lightmap_inside_unit_square else "")
           + (f"(overlap<={budget.lightmap_max_overlap:g}%)"
              if budget.lightmap_max_overlap >= 0 else "")
           if budget.lightmap_checked else "")
        + (f"  platform={budget.platform}" if budget.platform else "")
    )


# --- #
# Incremental runs (G4): a verdict cache, and checks in parallel
# --- #

CACHE_FILENAME = ".agk-cache.json"
CACHE_FORMAT = 1
CACHE_DIR_ENV = "AGK_CACHE_DIR"


def default_cache_path(root: Path) -> Path:
    """Where a folder's verdict cache lives when --cache does not say.

    Never inside the folder being checked: a cache file committed with the
    assets could carry a "passed" for any file whose hash it names, and a CI
    run on a fresh checkout would believe it. One file per folder, in the
    user's cache directory (``%LOCALAPPDATA%\\AssetGatekeeper\\cache`` on
    Windows, ``~/.cache/asset-gatekeeper`` elsewhere, or ``$AGK_CACHE_DIR``).
    """
    base = os.environ.get(CACHE_DIR_ENV)
    if not base:
        if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
            base = str(Path(os.environ["LOCALAPPDATA"]) / "AssetGatekeeper" / "cache")
        else:
            base = str(Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
                       / "asset-gatekeeper")
    try:
        resolved = str(root.resolve())
    except OSError:
        resolved = str(root.absolute())
    key = hashlib.sha256(resolved.lower().encode("utf-8")).hexdigest()[:20]
    return Path(base) / f"{root.name or 'root'}-{key}.json"


def cache_inside(root: Path, cache: Path) -> bool:
    """Is the cache file somewhere the assets' authors can write it (the tree)?"""
    try:
        cache.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _file_state(path: Path) -> Optional[list]:
    """[size, mtime_ns] of a file, or None when it is not there."""
    try:
        info = path.stat()
    except OSError:
        return None
    if not os.path.isfile(path):
        return None
    return [info.st_size, info.st_mtime_ns]


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def dependency_paths(path: Path) -> list[Path]:
    """Every file a verdict on ``path`` can depend on, besides the file itself.

    The .mtl and maps of an .obj, a .gltf's buffers and images, and for an FBX
    every place an engine would look for each texture (so a texture that turns
    up in the fallback location invalidates the cached "missing reference").
    """
    if path.suffix.lower() in FBX_SUFFIXES:
        try:
            references = read_fbx(path).references
        except AssetError:
            return []
        found: list[Path] = []
        for _written, candidates in references:
            for candidate in candidates:
                normalised = candidate.replace("\\", "/")
                found.append(path.parent / normalised)
                found.append(path.parent / normalised.rsplit("/", 1)[-1])
        return found
    return [target for _written, target in resolve_references(path)]


def _verdict_to_json(verdict: Verdict, root: Path) -> dict:
    return {"problems": list(verdict.problems), "metric": verdict.metric,
            "stats": [list(item) for item in verdict.stats],
            "warnings": list(verdict.warnings)}


def _verdict_from_json(path: Path, data: dict) -> Verdict:
    return Verdict(path, tuple(data.get("problems", ())), data.get("metric", ""),
                   tuple(tuple(item) for item in data.get("stats", ())), (),
                   tuple(data.get("warnings", ())))


def rules_fingerprint(gatekeeper: "Gatekeeper") -> str:
    """Everything outside the file that decides its verdict, as one hash.

    The rules (budget, tag budgets, CLI overrides, tag mode, exceptions), the
    gate's own code and version, and the mesh library's version. An exception
    within its warning window says "ends in N days", so the fingerprint also
    carries those day counts: the cache turns over on those days, not otherwise.
    """
    today = gatekeeper.today or date.today()
    exceptions = []
    for item in gatekeeper.exceptions:
        left = (item.expires - today).days
        exceptions.append((repr(item), min(max(left, -1), EXCEPTION_WARN_DAYS + 1)))
    try:
        code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        code = ""
    try:
        mesh_library = getattr(importlib.import_module("trimesh"), "__version__", "")
    except Exception:
        mesh_library = "none"
    parts = [__version__, code, mesh_library, repr(gatekeeper.budget),
             repr(sorted((name, repr(budget)) for name, budget in gatekeeper.tag_budgets.items())),
             repr(sorted(gatekeeper.overrides.items())), gatekeeper.tag_mode,
             repr(gatekeeper.tag_similarity), repr(exceptions), repr(gatekeeper.want_memory),
             str(gatekeeper.root), str(gatekeeper.tag_root)]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


class VerdictCache:
    """``.agk-cache.json``: reuse the verdict of a file that has not changed.

    An entry is reused when the rules fingerprint matches and the file - and
    every file its verdict depends on (``dependency_paths``) - has the same size
    and modification time as last run. A file whose time changed but whose bytes
    did not (a fresh checkout) is hashed, found identical and reused. Verdicts
    that are unreadable or healed are never stored; with healing on, a stored
    failure is re-checked so the healer gets its chance.
    """

    def __init__(self, path: Path, root: Path, fingerprint: str,
                 reuse_failures: bool = True) -> None:
        self.path = path
        self.root = root
        self.fingerprint = fingerprint
        self.reuse_failures = reuse_failures
        self.entries: dict[str, dict] = {}
        self.seen: set[str] = set()
        self.hits = 0
        self.misses = 0
        self.dirty = False
        self.reset_reason = ""
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self.reset_reason = f"unreadable ({exc.__class__.__name__}), starting fresh"
            self.dirty = True
            return
        if not isinstance(document, dict) or document.get("format") != CACHE_FORMAT:
            self.reset_reason = "from another version, starting fresh"
            self.dirty = True
            return
        if document.get("fingerprint") != fingerprint:
            self.reset_reason = "rules or gate changed, re-checking everything"
            self.dirty = True
            return
        entries = document.get("entries")
        self.entries = entries if isinstance(entries, dict) else {}

    def _key(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def lookup(self, path: Path) -> Optional[Verdict]:
        key = self._key(path)
        self.seen.add(key)
        entry = self.entries.get(key)
        state = _file_state(path)
        if not isinstance(entry, dict) or state is None:
            self.misses += 1
            return None
        verdict_data = entry.get("verdict") or {}
        if verdict_data.get("problems") and not self.reuse_failures:
            self.misses += 1
            return None
        if entry.get("state") != state:
            try:
                same_bytes = entry.get("sha256") == _sha256_of(path)
            except OSError:
                same_bytes = False
            if not same_bytes:
                self.misses += 1
                return None
            entry["state"] = state          # touched, not changed
            self.dirty = True
        for name, recorded in (entry.get("deps") or {}).items():
            if _file_state(Path(name) if os.path.isabs(name) else self.root / name) != recorded:
                self.misses += 1
                return None
        self.hits += 1
        return _verdict_from_json(path, verdict_data)

    def store(self, path: Path, verdict: Verdict) -> None:
        key = self._key(path)
        self.seen.add(key)
        if verdict.unreadable or verdict.healed or verdict.path != path:
            self.entries.pop(key, None)
            self.dirty = True
            return
        state = _file_state(path)
        if state is None:
            return
        try:
            sha = _sha256_of(path)
            deps = {}
            for dependency in dependency_paths(path):
                deps[self._key(dependency)] = _file_state(dependency)
        except (OSError, AssetError):
            return
        self.entries[key] = {"state": state, "sha256": sha, "deps": deps,
                             "verdict": _verdict_to_json(verdict, self.root)}
        self.dirty = True

    def save(self) -> None:
        stale = set(self.entries) - self.seen
        for key in stale:
            del self.entries[key]
        if not (self.dirty or stale):
            return
        document = {"format": CACHE_FORMAT, "tool": f"asset-gatekeeper {__version__}",
                    "fingerprint": self.fingerprint, "entries": self.entries}
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            LOG.warning("could not write the cache %s: %s", self.path, exc)
            try:
                temporary.unlink()
            except OSError:
                pass


_POOL_GATEKEEPER: Optional["Gatekeeper"] = None


def _pool_start(gatekeeper: "Gatekeeper") -> None:
    global _POOL_GATEKEEPER
    _POOL_GATEKEEPER = gatekeeper


def _pool_check(path: Path) -> Verdict:
    assert _POOL_GATEKEEPER is not None
    try:
        return _POOL_GATEKEEPER.check(path)
    except Exception as exc:    # one bad file must not take down the whole run
        return Verdict(path, (f"could not be checked ({exc.__class__.__name__}: {exc})",),
                       unreadable=True)


def engine_module(name: str):
    """engine/<name>.py from a downloaded copy, or agk_suite.engine when installed."""
    import importlib
    try:
        return importlib.import_module(f"agk_suite.engine.{name}")
    except ImportError:
        engine = Path(__file__).resolve().parent / "engine"
        if str(engine) not in sys.path:
            sys.path.insert(0, str(engine))
        return importlib.import_module(name)


def polypaint_module():
    """engine/polypaint.py from a downloaded copy, or agk_suite.engine when installed."""
    return engine_module("polypaint")


def bake_defaults(budget: "Budget") -> tuple[int, int]:
    """(triangles, texture edge) a baked asset should aim for under ``budget``."""
    triangles = max(100, min(budget.max_triangles, 30_000))
    edge = 16
    while edge * 2 <= min(budget.max_texture_edge, 2048):
        edge *= 2
    return triangles, edge


def run_bake_polypaint(source: Path, budget: "Budget", args: argparse.Namespace) -> int:
    """--bake-polypaint: make a game asset from a Polypaint .obj, then gate it."""
    if not source.is_file() or source.suffix.lower() != ".obj":
        LOG.error("--bake-polypaint takes one .obj file with ZBrush Polypaint or vertex colours")
        return 2
    module = polypaint_module()
    preset = getattr(args, "bake_preset", None) or ""
    triangles, edge = bake_defaults(budget)
    if preset:
        # A preset sets everything, within the folder's rules: n64 under a
        # 1,200-triangle, 64 px rules.toml is exactly that.
        try:
            settings = module.resolve_settings(
                preset, args.bake_triangles or 0, args.bake_texture or 0,
                getattr(args, "bake_normal", None), getattr(args, "bake_colours", None),
                max_triangles=budget.max_triangles, max_texture=budget.max_texture_edge,
                face_texture=getattr(args, "bake_face", None),
                max_materials=budget.max_material_slots)
        except module.PolypaintError as exc:
            LOG.error("%s", exc)
            return 2
    else:
        # The defaults follow the folder's rules, so a strict rules.toml makes a
        # tiny asset; say so, with the flag that overrides it.
        if not args.bake_texture and edge < 512:
            LOG.warning("the texture budget here is %d px, so the baked texture will be %dx%d; "
                        "use --bake-texture 2048 for more detail, or --bake-preset n64 for a "
                        "retro asset (16 colours, no normal map)", edge, edge, edge)
        if not args.bake_triangles and triangles < 5000:
            LOG.warning("the triangle budget here is %s, so the baked mesh will have %s triangles; "
                        "use --bake-triangles 30000 for more", f"{triangles:,}", f"{triangles:,}")
        normal = getattr(args, "bake_normal", None)
        settings = module.resolve_settings(
            "", args.bake_triangles or triangles, args.bake_texture or edge,
            True if normal is None else normal, getattr(args, "bake_colours", None) or 0,
            face_texture=getattr(args, "bake_face", None),
            max_materials=budget.max_material_slots)
    if settings.get("face_texture_note"):
        LOG.warning("%s; allow 2 with max_material_slots = 2 in rules.toml", settings["face_texture_note"])
    edge = settings["texture"]
    LOG.info("bake    %s -> %s triangles, %dx%d BaseColor%s%s%s%s", source.name,
             f"{settings['triangles']:,}", edge, edge,
             " + Normal" if settings["normal_map"] else "",
             f" in {settings['colours']} colours" if settings["colours"] else "",
             " + a face texture" if settings.get("face_texture") else "",
             f" (preset {preset})" if preset else "")
    try:
        result = module.bake(source, source.parent, triangles=settings["triangles"],
                             texture=edge, normal_map=settings["normal_map"],
                             colours=settings["colours"], preset=preset,
                             face_texture=settings.get("face_texture", False),
                             progress=lambda name, i, n: LOG.info("bake    %d/%d %s", i + 1, n, name))
    except module.PolypaintError as exc:
        LOG.error("%s", exc)
        return 2
    made = [Path(result["mesh"]), Path(result["texture"])]
    for key in ("normal", "face_texture", "face_normal"):
        if result.get(key):
            made.append(Path(result[key]))
    LOG.info("bake    wrote %s in %s s (%s)", ", ".join(path.name for path in made),
             result["seconds"], result["note"])
    # A texture an earlier bake wrote that this one did not (a normal map before
    # an N64 bake, say) is not part of the new .glb; say so rather than leave it
    # looking current. Nothing is deleted.
    face_base, face_normal = module.face_texture_names(source)
    for name in (module.normal_map_name(source), face_base, face_normal):
        old = source.parent / name
        if old.is_file() and old not in made:
            LOG.warning("bake    %s is from an earlier bake and is not used by %s; "
                        "delete it if you no longer need it", name, Path(result["mesh"]).name)
    gatekeeper = Gatekeeper(budget)
    reporter = Reporter(color=not getattr(args, "no_color", False), root=source.parent,
                        json_mode=bool(getattr(args, "json", False)))
    failed = 0
    for path in made:
        verdict = gatekeeper.check(path)
        reporter.report(verdict)
        failed += 0 if verdict.passed else 1
    return 1 if failed else 0


def resolve_jobs(requested: Optional[int]) -> int:
    if requested is None:
        return 1
    if requested == 0:
        return max(1, os.cpu_count() or 1)
    return requested


#: Peak memory of one check, as a multiple of the file's size. Measured on a
#: 90 MB ZBrush OBJ (1.08 M triangles): 748 MB, so about 8.3x; rounded up.
MEMORY_PER_FILE_BYTE = 9
#: What a worker needs before it opens anything (Python, numpy, trimesh).
MEMORY_PER_WORKER = 256 * 1024 * 1024


def available_memory() -> Optional[int]:
    """Bytes of memory free for new work, or None when this system will not say."""
    try:
        if os.name == "nt":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                            ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                            ("total_virtual", ctypes.c_ulonglong),
                            ("avail_virtual", ctypes.c_ulonglong),
                            ("avail_extended", ctypes.c_ulonglong)]
            status = _Status()
            status.length = ctypes.sizeof(_Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return int(status.avail_phys)
            return None
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            for line in meminfo.read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        page = int(os.sysconf("SC_PAGE_SIZE"))
        try:
            return int(os.sysconf("SC_AVPHYS_PAGES")) * page    # other Unix
        except (OSError, ValueError):
            # macOS has no free-page count here (found by CI, 9 Nov 2026): assume
            # half the installed memory is free rather than giving up.
            return int(os.sysconf("SC_PHYS_PAGES")) * page // 2
    except (OSError, ValueError, AttributeError, ImportError):
        return None


def memory_safe_jobs(jobs: int, paths: Sequence[Path],
                     free: Optional[int] = None) -> int:
    """Lower ``jobs`` so N workers on the largest files fit in free memory.

    --jobs 0 on a 16-core PC used to start 16 workers whatever the files were;
    sixteen 90 MB sculpts at about 750 MB each is 12 GB (9 Nov 2026 audit, B8).
    Assumes each worker may get one of the largest files, and leaves 30 % free.
    """
    if jobs <= 1 or not paths:
        return max(1, jobs)
    free = available_memory() if free is None else free
    if not free:
        return jobs
    sizes = []
    for path in paths:
        try:
            sizes.append(path.stat().st_size)
        except OSError:
            continue
    largest = sorted(sizes, reverse=True)[:jobs]
    need_each = MEMORY_PER_WORKER + MEMORY_PER_FILE_BYTE * (max(largest) if largest else 0)
    allowed = max(1, int(free * 0.7) // need_each)
    if allowed < jobs:
        LOG.warning("checking %d files at a time, not %d: the largest is %.0f MB and needs "
                    "about %.0f MB each, with %.0f MB free", allowed, jobs,
                    (max(largest) if largest else 0) / 1048576, need_each / 1048576,
                    free / 1048576)
    return min(jobs, allowed)


def run_once(root: Path, gatekeeper: Gatekeeper, reporter: Reporter,
             recursive: bool, exclude: Sequence[str] = (),
             follow_symlinks: bool = False, allow_empty: bool = False,
             totals: Sequence["MemoryTotal"] = (), cache: Optional[VerdictCache] = None,
             jobs: int = 1) -> int:
    unreadable: list[str] = []
    walked = walk_files(root, recursive=recursive, follow_symlinks=follow_symlinks,
                        on_skip=unreadable.append)
    candidates = sorted(p for p in walked if is_candidate(p, exclude))
    skipped = skipped_files(walked, exclude)
    del walked

    # An entry the walker could not even list used to disappear without a word:
    # a locked 90,000-triangle mesh reported "no tracked assets found" and exit 0,
    # so CI stayed green on a repository nobody had actually gated.
    for message in unreadable:
        reporter.report(Verdict(Path(message), (f"cannot be read ({message})",),
                                unreadable=True))

    if not candidates and not unreadable:
        found = f" ({describe_skipped(skipped)})" if skipped["total"] else ""
        if allow_empty:
            LOG.info("no tracked assets found in %s%s (--allow-empty)", root, found)
            return 0
        LOG.error("no tracked assets found in %s%s - check the path, or pass "
                  "--allow-empty if this folder is meant to be empty", root, found)
        return 1

    checked: list[Verdict] = []
    ready: list[tuple[Path, Optional[Verdict]]] = []
    for path in candidates:
        if wait_until_stable(path):
            ready.append((path, cache.lookup(path) if cache is not None else None))
        else:
            LOG.warning("%s is still being written by another process; skipped", path.name)

    pending = [path for path, cached in ready if cached is None]
    futures: dict[Path, object] = {}
    pool = None
    if jobs > 1 and len(pending) > 1:
        jobs = memory_safe_jobs(min(jobs, len(pending)), pending)
    if jobs > 1 and len(pending) > 1:
        try:
            from concurrent.futures import ProcessPoolExecutor
            pool = ProcessPoolExecutor(max_workers=min(jobs, len(pending)),
                                       initializer=_pool_start, initargs=(gatekeeper,))
            futures = {path: pool.submit(_pool_check, path) for path in pending}
        except Exception as exc:   # no processes on this machine: one at a time
            LOG.warning("--jobs %d unavailable (%s); checking one file at a time", jobs, exc)
            if pool is not None:
                pool.shutdown(cancel_futures=True)
            pool, futures = None, {}
    try:
        # Reported in path order whatever finishes first, so two runs - and
        # --jobs 1 against --jobs 8 - print and report the same thing.
        for path, cached in ready:
            if cached is not None:
                verdict = cached
            elif path in futures:
                try:
                    verdict = futures[path].result()  # type: ignore[attr-defined]
                except Exception as exc:          # a worker died (out of memory)
                    verdict = Verdict(path, (f"could not be checked ({exc.__class__.__name__})",),
                                      unreadable=True)
            else:
                verdict = gatekeeper.check(path)
            if cached is None and cache is not None:
                cache.store(path, verdict)
            checked.append(verdict)
            reporter.report(verdict)
    finally:
        if pool is not None:
            pool.shutdown()
        if cache is not None:
            cache.save()
    if cache is not None:
        LOG.info("cache   %d reused, %d checked (%s)", cache.hits, cache.misses, cache.path.name)

    over = check_totals(totals, checked) if totals else []
    for verdict in over:
        # Printed and counted, and in the JUnit/SARIF report, but never sent as
        # telemetry: a folder total is not an asset on the dashboard.
        LOG.error("FAILED: %s  %s", verdict.reason, str(verdict.path).replace("\\", "/"))
        if reporter.keep_verdicts:
            reporter.verdicts.append(verdict)
    reporter.summary(skipped)
    return 1 if (reporter.failed or over) else 0


def run_watch(root: Path, gatekeeper: Gatekeeper, reporter: Reporter,
              recursive: bool, exclude: Sequence[str] = ()) -> int:
    if Observer is None:
        LOG.error("watchdog is not installed: python -m pip install watchdog")
        return 2

    work_queue: "queue.Queue[Optional[Path]]" = queue.Queue()
    worker = WatchWorker(work_queue, gatekeeper, reporter)
    worker.exclude = tuple(exclude)
    worker.start()

    handler = AssetEventHandler(work_queue)
    handler.exclude = tuple(exclude)
    observer = Observer()
    observer.schedule(handler, str(root), recursive=recursive)
    observer.start()

    LOG.info("watching %s - Ctrl+C to stop", root)

    try:
        while observer.is_alive():
            time.sleep(0.25)
    except KeyboardInterrupt:
        LOG.info("stopping")
    finally:
        observer.stop()
        observer.join(timeout=5)
        work_queue.put(None)
        worker.join(timeout=5)
        reporter.summary()
    return 0


def port_in_use_hint(port: int) -> str:
    """Platform-appropriate commands for finding and freeing a busy TCP port.

    "Address already in use" is the single most common first-run failure: the
    artist's own previous server is still alive, or another tool owns 8080. The
    message has to say what to type next, not just what went wrong.
    """
    if os.name == "nt":
        find_it = f"netstat -ano | findstr :{port}"
        stop_it = "taskkill /PID <pid> /F"
    else:
        find_it = f"lsof -i :{port}    (or: fuser -k {port}/tcp)"
        stop_it = "kill <pid>"
    # Suggest a port that is neither busy nor another server's default: telling
    # someone to move AssetHealth onto the workstation's 8100 is not a fix.
    spare = next(candidate for candidate in range(port + 1, port + 10)
                 if candidate not in (8080, 8100))
    return (f"hint: port {port} is already in use by another process.\n"
            f"      who has it:  {find_it}\n"
            f"      stop it:     {stop_it}\n"
            f"      or just move: --port {spare}")


def is_addr_in_use(exc: BaseException) -> bool:
    """Was this bind failure "somebody else has the port", or something worse?

    Worth separating because the *only* safe reaction to a busy port is to move
    to the next one. A missing interface (EADDRNOTAVAIL), a privilege problem on
    a low port, or a malformed host must stay a hard error - walking forward
    through ten ports would just bury the real message.

    Windows reports a port held by another process as either WSAEADDRINUSE
    (10048) or, maddeningly, WSAEACCES (10013, "access permissions"). Both are
    treated as "try the next port", and a range excluded by Hyper-V simply
    exhausts the retry budget and reports the original error.
    """
    import errno

    numbers = {getattr(exc, "errno", None), getattr(exc, "winerror", None)}
    busy = {errno.EADDRINUSE, 98, 48, 10048, 10013}
    if numbers & busy:
        return True
    text = str(exc).lower()
    return "in use" in text or "address already" in text or "access permissions" in text


def port_is_busy(host: str, port: int) -> bool:
    """Is somebody using this port right now?

    Two probes, because either one alone lies somewhere:

    * ``connect()`` - authoritative on Windows, where ``SO_REUSEADDR`` lets a
      second socket bind a port another process is already *listening* on. A
      bind test alone would call that port free and two servers would then fight
      over the same port, silently.
    * a bind with no ``SO_REUSEADDR`` - catches a listener that is not accepting
      connections (still occupied) and is the only signal on POSIX for a socket
      that is bound but not listening.
    """
    import socket

    address = ("127.0.0.1" if host in ("", "0.0.0.0", "::") else host, int(port))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        if probe.connect_ex(address) == 0:
            return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(address)
        except OSError:
            return True
    return False


def next_free_port(host: str, start: int, reserved: tuple[int, ...] = (),
                   tries: int = 20) -> Optional[int]:
    """First port at or after ``start`` that nobody is using.

    A *pre-check*, for callers that must know the port before starting anything
    (a launcher writing a URL file, the ``--port-auto`` loop). It is inherently
    racy - another process can take the port between this call and the bind - so
    callers still have to handle a bind failure.
    """
    for candidate in range(start, start + tries):
        if candidate in reserved:
            continue
        if port_is_busy(host, candidate):
            continue
        return candidate
    return None


def bind_with_fallback(build, host: str, port: int, auto: bool = False,
                       tries: int = 10, reserved: tuple[int, ...] = (),
                       report=None):
    """Build a server on ``port``, optionally walking forward when it is busy.

    Returns ``(server, port)``. Re-raises the original ``OSError`` when ``auto``
    is off, when the failure is not "port busy", or when the budget is spent -
    a caller that asked for the default port and did not ask for fallback must
    still get an honest failure.

    ``report(old_port, new_port, exc)`` is called once per hop so the CLI can
    say what happened; silence here would be the dangerous part, because a
    client configured with ``--telemetry-url http://host:8080/...`` would keep
    posting to whatever now owns 8080.
    """
    import errno

    attempt = int(port)
    last: Optional[BaseException] = None
    hops = 0
    while True:
        # Pre-check, because on Windows a bind onto an occupied port does not
        # necessarily raise - and a second server silently sharing a port is
        # worse than an error message.
        if auto and port_is_busy(host, attempt):
            last = OSError(errno.EADDRINUSE, f"port {attempt} is in use")
            if hops >= tries:
                raise last
            spare = next_free_port(host, attempt + 1, reserved)
            if spare is None:
                raise last
            hops += 1
            if report is not None:
                report(attempt, spare, last)
            attempt = spare
            continue
        try:
            return build(host, attempt), attempt
        except OSError as exc:
            last = exc
            if not auto or not is_addr_in_use(exc) or hops >= tries:
                raise
            spare = next_free_port(host, attempt + 1, reserved)
            if spare is None:
                raise last from None
            hops += 1
            if report is not None:
                report(attempt, spare, exc)
            attempt = spare





def run_doctor(target: Optional[Path], args: argparse.Namespace,
               out: Optional[object] = None) -> int:
    """Answer "can this machine run the gate?" in one command.

    Every check that fails comes with the fix, because a setup tool that only
    reports failure is just a slower error message. Ports are only a warning:
    something else using 8080 does not stop you gating a folder.
    """
    say = out or print
    problems = 0

    def line(status: str, label: str, detail: str = "", hint: str = "") -> None:
        nonlocal problems
        # Lowercase on purpose: the test harness greps "^  FAIL", and a doctor
        # line must never be mistaken for a failing assertion.
        mark = {"ok": "ok  ", "warn": "warn", "fail": "fail"}[status]
        if status == "fail":
            problems += 1
        say(f"  {mark}  {label}" + (f"  [{detail}]" if detail else ""))
        if hint and status != "ok":
            say(f"        -> {hint}")

    say(f"AssetGatekeeper {__version__} - environment check")
    say("")

    version = sys.version_info
    line("ok" if version >= (3, 9) else "fail", "python",
         f"{version.major}.{version.minor}.{version.micro}",
         "the gate needs Python 3.9+; install it, then re-create the venv")

    for name, module in (("trimesh", trimesh), ("pillow", Image)):
        if module is None:
            line("fail", name, "not importable",
                 f"python -m pip install {name if name != 'pillow' else 'pillow'}")
        else:
            found = getattr(module, "__version__", "?")
            line("ok", name, f"version {found}")

    try:
        import charset_normalizer  # presence check only
        line("ok", "charset-normalizer",
             f"{getattr(charset_normalizer, '__version__', '?')} - non-UTF-8 .obj/.mtl")
    except Exception:
        line("warn", "charset-normalizer", "not installed",
             "python -m pip install charset-normalizer (an .obj/.mtl that is not UTF-8 "
             "fails as unreadable without it)")

    try:
        import watchdog  # presence check only
        line("ok", "watchdog", f"{getattr(watchdog, '__version__', '?')} - native events")
    except Exception:
        line("warn", "watchdog", "not installed",
             "optional: python -m pip install watchdog (polling fallback is in use)")

    say("")
    root = target or Path(".")
    if not root.exists():
        line("fail", "target folder", str(root), "check the path, or run without a folder")
        root = None
    elif not root.is_dir():
        line("ok", "target folder", f"{root} (single file)")
    else:
        readable = os.access(str(root), os.R_OK)
        line("ok" if readable else "fail", "target folder", str(root),
             "the gate cannot read this folder - check the share permissions")

    say("")
    try:
        budget, source, _overrides = resolve_budget(root or Path("."), args)
        if source is None:
            line("warn", "rules.toml", "not found, using built-in defaults",
                 "write one with: asset-gatekeeper <folder> --init")
        else:
            line("ok", "rules.toml", str(source))
        line("ok", "budget", f"{budget.max_triangles:,} tris / "
                             f"{budget.max_texture_edge}px / "
                             f"{budget.max_material_slots} slots"
                             + (f" / platform {budget.platform}" if budget.platform else ""))
        tags, mode, _sim = resolve_tagging(root or Path("."), args)
        if tags:
            line("ok", "tag budgets", f"{len(tags)}: {', '.join(sorted(tags))} (mode {mode})")
        else:
            line("warn", "tag budgets", f"none (mode {mode})",
                 "optional: [tags.<name>] gives a folder its own limits")
    except ConfigError as exc:
        line("fail", "rules.toml", str(exc),
             "fix the file, or compare it with asset_gatekeeper.example.toml")

    say("")
    for port, who in ((8080, "AssetHealth"), (8100, "Art Workstation")):
        if not port_is_busy("127.0.0.1", port):
            line("ok", f"port {port}", "free")
        else:
            line("warn", f"port {port}", f"in use (would block {who})",
                 port_in_use_hint(port).splitlines()[0].replace("hint: ", ""))

    say("")
    if problems:
        say(f"{problems} problem(s) found - fix the 'fail' lines above.")
    else:
        say("No blocking problems. Next:")
        if root is not None and root.is_dir():
            say(f'  asset-gatekeeper "{root}" --recursive --once')
        else:
            say('  asset-gatekeeper "<folder>" --recursive --once')
        say("  asset-health --port 8080        (dashboard)")
        say("  art-workstation --port 8100     (3D review screen)")
    return 1 if problems else 0


RULES_HEADER = """\
# rules.toml - AssetGatekeeper
#
# Plain text on purpose: this file is reviewed in pull requests, diffed in git
# and read by CI. Edit it here, or through the admin page (`asset-health` ->
# /admin), which writes it atomically and records who changed what.
#
# Precedence: CLI flag > central/remote rules > [platforms.<name>] > this file
# > shipped defaults. Unknown keys are a hard error, deliberately.
"""


def _toml_value(value: object) -> str:
    """One TOML value. Only the types a rules file can legally contain."""
    import json as _json

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ConfigError(f"cannot write {value!r} to TOML")
        return repr(value)
    if isinstance(value, str):
        return _json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ConfigError(f"cannot write {type(value).__name__} to TOML")


def _emit_table(lines: list[str], path: str, table: dict) -> None:
    if lines:
        lines.append("")
    lines.append(f"[{path}]")
    nested: list[tuple[str, dict]] = []
    for key, value in table.items():
        if isinstance(value, dict):
            nested.append((key, value))
        else:
            lines.append(f"{key} = {_toml_value(value)}")
    for key, value in nested:
        _emit_table(lines, f"{path}.{key}", value)


def to_toml(document: dict, header: str = RULES_HEADER) -> str:
    """Serialise a rules document as canonical, diff-friendly TOML.

    Canonical matters more than pretty: the admin page rewrites this file on
    every apply, so a stable key order and stable spacing are what keep the git
    history readable. Because the file is regenerated, hand-written comments are
    not preserved - the header says so, and the audit table plus `git log` are
    the record of who changed what.

    ``parse_rules`` of the output round-trips to the same document (tested).
    """
    lines: list[str] = []
    if header:
        lines.extend(header.rstrip("\n").splitlines())
        lines.append("")
    scalars = [(k, v) for k, v in document.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in document.items() if isinstance(v, dict)]
    for key, value in scalars:
        lines.append(f"{key} = {_toml_value(value)}")
    for key, table in tables:
        _emit_table(lines, key, table)
    return "\n".join(lines).rstrip("\n") + "\n"


def _non_negative_int(text: str) -> int:
    """argparse type for a count that may legitimately be 0 (e.g. materials).

    ``--triangles -1`` used to be accepted and then failed every mesh in the
    repository, which reads as an art problem rather than a typo.
    """
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return value


def _positive_int(text: str) -> int:
    """argparse type for a size or a rate, which has no meaningful zero."""
    value = _non_negative_int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more, got 0")
    return value


# --- #
# CI reports (G3): one entry per asset, in the formats build servers display
# --- #

REPORT_FORMATS = ("junit", "sarif", "github")


def _report_target(text: str) -> tuple[str, str]:
    kind, sep, path = str(text).partition(":")
    kind = kind.strip().lower()
    if not sep or kind not in REPORT_FORMATS or not path.strip():
        raise argparse.ArgumentTypeError(
            f"use junit:<file>, sarif:<file> or github:<file>, got {text!r}")
    return kind, path.strip()


def _relative_name(path: Path, root: Optional[Path]) -> str:
    if str(path).startswith("[total] "):       # a [[totals]] line, not a file
        return str(path).replace("\\", "/")
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return path.name


def junit_report(verdicts: Sequence[Verdict], root: Optional[Path] = None) -> str:
    """JUnit XML: one test case per asset. Jenkins, TeamCity, Horde, GitLab and
    Azure DevOps all show these per test, so a failing asset is a red line with
    its path, not a word in a build log."""
    from xml.sax.saxutils import escape, quoteattr
    failures = sum(1 for verdict in verdicts if not verdict.passed and not verdict.unreadable)
    errors = sum(1 for verdict in verdicts if verdict.unreadable)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<testsuites name="AssetGatekeeper" tests="{len(verdicts)}" failures="{failures}" '
             f'errors="{errors}">',
             f'  <testsuite name="asset budgets" tests="{len(verdicts)}" failures="{failures}" '
             f'errors="{errors}" skipped="0">']
    for verdict in verdicts:
        name = _relative_name(verdict.path, root)
        folder = name.rsplit("/", 1)[0] if "/" in name else "."
        lines.append(f"    <testcase classname={quoteattr(folder)} name={quoteattr(name)}>")
        body = "\n".join(verdict.problems)
        if verdict.unreadable:
            lines.append(f"      <error message={quoteattr(verdict.reason)}>{escape(body)}</error>")
        elif not verdict.passed:
            lines.append(f"      <failure message={quoteattr(verdict.reason)} "
                         f"type=\"budget\">{escape(body)}</failure>")
        notes = list(verdict.heal_notes) + list(verdict.warnings)
        if verdict.metric or notes:
            out = "\n".join(([verdict.metric] if verdict.metric else []) + notes)
            lines.append(f"      <system-out>{escape(out)}</system-out>")
        lines.append("    </testcase>")
    lines += ["  </testsuite>", "</testsuites>", ""]
    return "\n".join(lines)


def sarif_report(verdicts: Sequence[Verdict], root: Optional[Path] = None) -> str:
    """SARIF 2.1.0: GitHub code scanning shows each failing asset as an annotation
    on the file in the pull request."""
    results = []
    for verdict in verdicts:
        if verdict.passed:
            continue
        name = _relative_name(verdict.path, root)
        # A URI, not a path: spaces, '#' and non-ASCII are percent-encoded. A folder
        # total is not a file; it is reported against the rules file that set it.
        target = RULES_FILENAME if name.startswith("[total] ") else name
        location = {"physicalLocation": {"artifactLocation": {
            "uri": quote(target.replace("\\", "/"), safe="/"), "uriBaseId": "%SRCROOT%"}}}
        for problem in verdict.problems or ("could not be checked",):
            results.append({
                "ruleId": "agk/unreadable" if verdict.unreadable else "agk/budget",
                "level": "error",
                "message": {"text": problem},
                "locations": [location]})
    document = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "AssetGatekeeper", "version": __version__,
                "informationUri": "https://github.com/ohio13/AssetGateKeeper",
                "rules": [
                    {"id": "agk/budget", "name": "AssetBudget",
                     "shortDescription": {"text": "Asset exceeds the project's budget"}},
                    {"id": "agk/unreadable", "name": "AssetUnreadable",
                     "shortDescription": {"text": "Asset could not be read"}}]}},
            "results": results}]}
    return json.dumps(document, indent=2) + "\n"


def _github_escape(text: str, property_value: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_value:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


def github_report(verdicts: Sequence[Verdict], root: Optional[Path] = None) -> str:
    """GitHub Actions annotation commands (``::error file=...::...``).

    Printed by any workflow step (``cat`` the file), each failing asset is marked
    on the pull request's Files tab - free, in private repositories too, where
    SARIF upload needs paid code scanning."""
    lines = []
    cwd = Path.cwd().resolve()
    for verdict in verdicts:
        if verdict.passed:
            continue
        name = _relative_name(verdict.path, root)
        if not name.startswith("[total] "):
            try:
                name = verdict.path.resolve().relative_to(cwd).as_posix()
            except (ValueError, OSError):
                pass
        title = "AssetGatekeeper: could not read" if verdict.unreadable else "AssetGatekeeper"
        for problem in verdict.problems or ("could not be checked",):
            lines.append(f"::error file={_github_escape(name, True)},"
                         f"title={_github_escape(title, True)}::{_github_escape(problem)}")
    return "\n".join(lines) + ("\n" if lines else "")


def write_reports(targets: Sequence[tuple[str, str]], verdicts: Sequence[Verdict],
                  root: Optional[Path]) -> None:
    writers = {"junit": junit_report, "sarif": sarif_report, "github": github_report}
    for kind, destination in targets:
        text = writers[kind](verdicts, root)
        path = Path(destination).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        LOG.info("report  %s written to %s (%d assets)", kind, path, len(verdicts))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asset_gatekeeper",
        description="Watch a folder of art exports and enforce asset budgets.",
        epilog=(
            "common first runs:\n"
            "  asset-gatekeeper <folder> --recursive --once     one scan, then exit\n"
            "  asset-gatekeeper <folder> --init                 write a rules.toml\n"
            "  asset-gatekeeper <folder> --wizard               ...from questions about your project\n"
            "  asset-gatekeeper --doctor                        check this machine\n"
            "  asset-gatekeeper <folder> --recursive            watch and toast\n"
            "\n"
            "exit codes: 0 all passed, 1 at least one failed, 2 bad setup.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("folder", nargs="?", default=None,
                        help="folder to watch (default: current directory)")
    parser.add_argument("--triangles", type=_non_negative_int, default=None,
                        help="max triangles per mesh (overrides rules.toml; default 50,000)")
    parser.add_argument("--max-texture", type=_positive_int, default=None, metavar="EDGE",
                        help="max texture width/height in pixels (overrides rules.toml; default 2,048)")
    parser.add_argument("--max-materials", type=_non_negative_int, default=None, metavar="N",
                        help="max material slots per mesh (overrides rules.toml; default 3)")
    parser.add_argument("--max-influences", type=_non_negative_int, default=None, metavar="N",
                        help="max bone weights per vertex (overrides rules.toml; default 4)")
    parser.add_argument("--max-uvs", type=_positive_int, default=None, metavar="N",
                        help="max UV channels per mesh (overrides rules.toml; default 4)")
    parser.add_argument("--allow-degenerate", action="store_true", default=None,
                        help="do not fail meshes containing zero-area triangles")
    parser.add_argument("--allow-npot", action="store_false", dest="require_power_of_two", default=None,
                        help="accept textures whose edges are not a power of two")
    parser.add_argument("--no-naming", action="store_false", dest="enforce_texture_naming", default=None,
                        help="accept texture names without a standard suffix")
    parser.add_argument("--rules", metavar="PATH",
                        help=f"rules file to use instead of <folder>/{RULES_FILENAME}")
    parser.add_argument("--preset", metavar="NAME", default=None,
                        help=f"apply a shipped preset ({', '.join(sorted(PRESETS))}); "
                             "beats rules.toml but not an explicit numeric flag")
    parser.add_argument("--tag-mode", choices=TAG_MODES, default=None, dest="tag_mode",
                        help="how a near-miss asset tag is handled: auto (accept), "
                             "suggest (accept + note, default), strict (fail with the fix)")
    parser.add_argument("--notify", action="store_true", dest="notify", default=None,
                        help="native desktop toasts on failure/heal (default: on while watching)")
    parser.add_argument("--no-notify", action="store_false", dest="notify", default=None,
                        help="never raise desktop toasts")
    parser.add_argument("--notify-max", type=_positive_int, default=None, metavar="N",
                        help="maximum toasts per minute (default 6)")
    parser.add_argument("--exclude", action="append", metavar="GLOB", default=None,
                        help="ignore files matching this glob, e.g. 'preview_*' "
                             "(repeatable; same as [filters] exclude)")
    parser.add_argument("--platform", metavar="NAME", default=None,
                        help="apply the [platforms.<NAME>] compression profile from rules.toml "
                             "(e.g. mobile, console, pc)")
    parser.add_argument("--no-reference-check", action="store_false", dest="check_references",
                        default=None, help="do not fail assets with missing texture/buffer links")
    parser.add_argument("--no-pbr-check", action="store_false", dest="pbr_check", default=None,
                        help="do not clamp albedo luminance")
    parser.add_argument("--audit-transforms", action="store_true", dest="audit_transforms",
                        default=None,
                        help="flag unbaked node transforms, uncentred pivots and unindexed geometry")
    parser.add_argument("--require-indexed", action="store_true", dest="require_indexed",
                        default=None, help="also fail triangle-soup geometry (implies the audit)")
    parser.add_argument("--doctor", action="store_true",
                        help="check this machine can run the gate (deps, rules, ports), then exit")
    parser.add_argument("--bake-polypaint", action="store_true", dest="bake_polypaint",
                        help="turn an .obj with ZBrush Polypaint (or vertex colours) into a game "
                             "asset beside it: a reduced .glb with UVs, a baked _BaseColor "
                             "texture and a _Normal map, sized to the folder's rules, then "
                             "check them. The source "
                             "is never changed. Needs: pip install \"asset-gatekeeper[bake]\"")
    parser.add_argument("--bake-triangles", type=int, default=None, metavar="N",
                        help="with --bake-polypaint: triangles to reduce to (default: the "
                             "budget, at most 30,000)")
    parser.add_argument("--bake-texture", type=int, default=None, metavar="PX",
                        help="with --bake-polypaint: texture size, a power of two (default: "
                             "the texture budget, at most 2048)")
    parser.add_argument("--bake-preset", default=None, metavar="NAME", choices=("n64",),
                        help="with --bake-polypaint: n64 = Nintendo 64 style: about 1,200 "
                             "triangles, a 64 px texture in 16 colours, no normal map "
                             "(never more than the folder's rules allow)")
    parser.add_argument("--bake-no-normal", action="store_false", dest="bake_normal",
                        default=None,
                        help="with --bake-polypaint: do not bake a normal map (one is made "
                             "by default, except with --bake-preset n64)")
    parser.add_argument("--bake-face-texture", action="store_true", dest="bake_face", default=None,
                        help="with --bake-polypaint: give the face (the sculpt's densest colour "
                             "detail) a texture of its own, the same size, as a second material "
                             "(on with --bake-preset n64)")
    parser.add_argument("--bake-no-face-texture", action="store_false", dest="bake_face",
                        help="with --bake-polypaint: one texture for the whole model")
    parser.add_argument("--bake-colours", type=int, default=None, metavar="N",
                        help="with --bake-polypaint: reduce the BaseColor to N colours "
                             "(16 or 256 like the N64's palette textures; 0 keeps them all)")
    parser.add_argument("--suggest-naming", action="store_true", dest="suggest_naming",
                        help="read the names in <folder> and print a [naming] block for "
                             "rules.toml that fits them, then exit (nothing is written)")
    parser.add_argument("--list-presets", action="store_true", dest="list_presets",
                        help="print the shipped platform targets and their budgets, then exit")
    parser.add_argument("--init", action="store_true",
                        help="interactive setup: create the watch folder and its rules.toml")
    parser.add_argument("--yes", action="store_true",
                        help="with --init or --wizard: accept every default instead of prompting")
    parser.add_argument("--force", action="store_true",
                        help="with --init: overwrite an existing rules.toml (with --wizard: replace "
                             "it without asking; a copy is still kept)")
    parser.add_argument("--wizard", action="store_true",
                        help="ask six plain-English questions (engines, art tools, platforms, "
                             "kinds of asset, style, broken geometry; several answers each) and "
                             "write one commented rules.toml for all of them. Asks before "
                             "replacing a rules.toml and keeps a copy. With --yes: every "
                             "default, no questions")
    parser.add_argument("--wizard-answers", metavar="FILE", default=None, dest="wizard_answers",
                        help="the --wizard answers from a JSON file instead of a terminal "
                             "(for scripts), e.g. {\"engines\": [\"unreal\", \"unity\"], "
                             "\"platforms\": [\"pc\", \"switch\"]}")
    parser.add_argument("--audit", action="store_true",
                        help="audit the whole folder for broken references and orphaned "
                             "textures, then exit")
    parser.add_argument("--auto-heal", action="store_true", dest="auto_heal", default=None,
                        help="repair failing assets in place (NPOT textures, missing texture "
                             "suffixes, zero-area .obj faces) - off unless this or "
                             "[healer] enabled = true is set")
    parser.add_argument("--no-auto-heal", action="store_false", dest="auto_heal",
                        help="force healing off, even if rules.toml enables it (use in CI)")
    parser.add_argument("--heal", action="store_true",
                        help="repair this one file now, even with healing off: verified "
                             "before adoption, the original kept in .agk-backups/")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="with --heal: say what would change and write nothing")
    parser.add_argument("--undo-heal", action="store_true", dest="undo_heal",
                        help="put back what the last heal of this file replaced (refuses "
                             "if the healed file was edited since)")
    parser.add_argument("--list-heals", action="store_true", dest="list_heals",
                        help="list the heals recorded in this folder, newest first")
    parser.add_argument("--remote-rules", metavar="URL", default=None,
                        help="fetch the budget from a central endpoint; cached locally for "
                             f"offline use (or set {REMOTE_RULES_ENV})")
    parser.add_argument("--telemetry-url", metavar="URL", default=None,
                        help="POST every verdict to this endpoint (or set "
                             f"{SERVER_ENV_URL})")
    parser.add_argument("--telemetry-identity", choices=IDENTITY_MODES, default=None,
                        metavar="MODE",
                        help="what identifying detail telemetry carries (or set "
                             f"{SERVER_ENV_IDENTITY}): full (host, user, absolute path), "
                             "host (host and absolute path, the default), none (relative path)")
    parser.add_argument("--version", action="version",
                        version=f"asset-gatekeeper {__version__}",
                        help="print the version and exit (pipelines log this)")
    parser.add_argument("--once", action="store_true", help="scan existing files and exit")
    parser.add_argument("--allow-empty", action="store_true", dest="allow_empty",
                        help="with --once: exit 0 when the folder holds no tracked assets "
                             "(without it, an empty folder fails the run: a wrong path "
                             "is the usual reason)")
    parser.add_argument("--recursive", action="store_true", help="include subfolders")
    parser.add_argument("--cache", metavar="FILE", default=None,
                        help="with --once: where the verdict cache lives, outside the folder "
                             "(default: one file per folder in the user's cache directory, "
                             f"or ${CACHE_DIR_ENV}); unchanged files reuse their verdict")
    parser.add_argument("--no-cache", action="store_true", dest="no_cache",
                        help="with --once: check every file and leave the cache alone")
    parser.add_argument("--jobs", type=_non_negative_int, default=None, metavar="N",
                        help="with --once: check N files at a time in separate processes "
                             "(0 = one per CPU; default 1; healing always runs one at a time)")
    parser.add_argument("--follow-symlinks", action="store_true",
                        help="descend into symlinked/junctioned folders. Off by default: "
                             "a link re-counts the same tree and a self-link never ends. "
                             "Unreadable entries are always skipped, never fatal")
    parser.add_argument("--report", action="append", type=_report_target, default=[],
                        metavar="FORMAT:FILE",
                        help="with --once or a single file: also write junit:gate.xml (Jenkins, "
                             "TeamCity, Horde) or sarif:gate.sarif (GitHub code scanning); "
                             "repeatable")
    parser.add_argument("--exception-path", metavar="PATH", default=None,
                        help="with one file: the path [[exceptions]] match it as, relative to "
                             "the rules file's folder (the Perforce trigger passes the depot "
                             "path; its temp copy is laid out differently)")
    parser.add_argument("--json", action="store_true",
                        help="one JSON object per file on stdout (used by the DCC plugins)")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        # Keep non-ASCII asset paths from killing cp1252/cp437 consoles. stderr
        # matters too: --json mode routes logs there, and a CJK filename would
        # otherwise raise UnicodeEncodeError mid-run.
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    # In JSON mode stdout is a data channel: logs must not pollute it.
    stream = sys.stderr if args.json else sys.stdout
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=stream)

    if getattr(args, "list_presets", False):
        rows = preset_summary()
        print(f"{'preset':<10}{'platform':<26}{'char tris':>10}{'prop tris':>11}"
              f"{'texture':>9}{'slots':>7}{'POT':>5}{'PBR':>5}")
        for row in rows:
            print(f"{row['name']:<10}{str(row['label'])[:25]:<26}{row['character_triangles']:>10,}"
                  f"{row['prop_triangles']:>11,}{row['max_texture_edge']:>9}"
                  f"{row['max_material_slots']:>7}{'yes' if row['power_of_two'] else 'no':>5}"
                  f"{'yes' if row['pbr'] else 'no':>5}")
        print("\nlegacy aliases (retro, mobile) are kept so old scripts keep working.")
        print("apply one: asset-gatekeeper <folder> --preset n64")
        return 0

    if getattr(args, "suggest_naming", False):
        if not args.folder:
            print("error: --suggest-naming needs a folder", file=sys.stderr)
            return 2
        target = Path(args.folder).expanduser()
        if not target.is_dir():
            print(f"error: not a folder: {target}", file=sys.stderr)
            return 2
        print(suggest_naming(target, recursive=True), end="")
        return 0

    # The doctor runs before the dependency check on purpose: reporting a missing
    # trimesh is exactly its job.
    if getattr(args, "doctor", False):
        target = Path(args.folder).expanduser() if args.folder else None
        return run_doctor(target, args)

    # The wizard runs before anything touches the folder: it may be creating it.
    if getattr(args, "wizard", False) or getattr(args, "wizard_answers", None):
        target = Path(args.folder).expanduser() if args.folder else None
        answers = Path(args.wizard_answers).expanduser() if args.wizard_answers else None
        return engine_module("rules_wizard").run_wizard(
            target, answers_file=answers, force=args.force, assume_yes=args.yes)
    if getattr(args, "init", False):
        target = Path(args.folder).expanduser() if args.folder else None
        return run_init(target, ask=input, assume_yes=args.yes, force=args.force,
                        max_triangles=None, max_edge=None, max_material_slots=None)

    root = Path(args.folder or ".").expanduser().resolve()
    # Undo and the heal list read backups only: no budget, no trimesh. Before the
    # existence check: undoing a rename names a file that no longer exists.
    if getattr(args, "undo_heal", False) or getattr(args, "list_heals", False):
        return run_heal_history(root, undo=args.undo_heal)
    if not root.exists():
        LOG.error("not found: %s", root)
        return 2
    # Single-file mode lets a DCC plugin gate one temporary export directly.
    watch_dir = root if root.is_dir() else root.parent

    if getattr(args, "dry_run", False) and not getattr(args, "heal", False):
        LOG.error("--dry-run goes with --heal")
        return 2
    if getattr(args, "heal", False):
        if not root.is_file():
            LOG.error("--heal repairs one file: pass the file, not a folder "
                      "(folder-wide healing is --auto-heal)")
            return 2
        if getattr(args, "auto_heal", None) is False:
            LOG.error("--heal and --no-auto-heal contradict each other")
            return 2
        # Heal against the project, not the file's folder: the project's rules
        # judge the result, and the meshes that name a texture get repointed.
        try:
            project = healer_module().project_root_for(root.parent)
        except ConfigError as exc:
            LOG.error("%s", exc)
            return 2
        if project is not None:
            heal_project = project
            if not args.rules and not (root.parent / RULES_FILENAME).is_file() \
                    and (project / RULES_FILENAME).is_file():
                args.rules = str(project / RULES_FILENAME)
        else:
            heal_project = root.parent

    # Folder audit is a report, not a gate: it needs no budget and no deps
    # beyond the parser, so it runs before the dependency check on purpose.
    if getattr(args, "audit", False):
        if root.is_file():
            LOG.error("--audit needs a folder, not a single file")
            return 2
        return run_audit(watch_dir)

    if trimesh is None or Image is None:
        missing = [name for name, module in (("trimesh", trimesh), ("pillow", Image)) if module is None]
        LOG.error("missing dependencies: %s - python -m pip install %s", ", ".join(missing), " ".join(missing))
        return 2

    try:
        server = resolve_server(watch_dir, args)
    except ConfigError as exc:
        LOG.error("invalid server settings: %s", exc)
        return 2

    # Fetch before resolution so precedence is one flat decision:
    # CLI flag > remote rules > local rules.toml > defaults.
    remote_document = None
    if server.remote_rules_url:
        try:
            remote_document = fetch_remote_rules(
                server.remote_rules_url, watch_dir / RULES_CACHE_FILENAME, token=server.token
            )
        except ConfigError as exc:
            # Exit 2 ("the gate could not run"), never a traceback and exit 1,
            # which CI would read as "the assets failed".
            LOG.error("%s", exc)
            return 2

    try:
        budget, rules_source, overrides = resolve_budget(watch_dir, args, remote_document)
    except ConfigError as exc:
        LOG.error("invalid rules file: %s", exc)
        return 2

    if remote_document is not None:
        LOG.info("rules   %s (remote, cached as %s)",
                 server.remote_rules_url, RULES_CACHE_FILENAME)
    else:
        LOG.info("rules   %s", rules_source if rules_source is not None else "none (defaults)")
    if overrides:
        LOG.info("cli     overriding %s", ", ".join(overrides))
    LOG.info("budget  %s", describe_budget(budget))

    if getattr(args, "bake_polypaint", False):
        return run_bake_polypaint(root, budget, args)

    try:
        heal_settings = resolve_healer(watch_dir, args, remote_document)
    except ConfigError as exc:
        LOG.error("invalid healer settings: %s", exc)
        return 2

    one_file = bool(getattr(args, "heal", False))
    if one_file:
        heal_settings = replace(heal_settings, enabled=True)
    healer = None
    if heal_settings.enabled:
        try:
            healer = load_healer(budget, heal_settings,
                                 root=heal_project if one_file else watch_dir,
                                 dry_run=bool(getattr(args, "dry_run", False)))
        except ConfigError as exc:
            LOG.error("%s", exc)
            return 2
        if one_file:
            LOG.info("heal    %s%s  mode=%s  project=%s", root.name,
                     " (dry run: nothing is written)" if args.dry_run else "",
                     heal_settings.texture_mode, heal_project)
        else:
            describe_auto_heal(watch_dir, heal_settings, args, remote_document)

    telemetry = None
    if server.telemetry_enabled:
        telemetry = Telemetry(server.telemetry_url, root=watch_dir, token=server.token,
                              identity=server.identity)
        LOG.info("report  %s", server.telemetry_url)

    try:
        tag_budgets, tag_mode, tag_similarity = resolve_tagging(watch_dir, args, remote_document)
    except ConfigError as exc:
        LOG.error("invalid tag configuration: %s", exc)
        return 2
    if tag_budgets:
        LOG.info("tags    %s  (%s matching, %.2f similarity)",
                 ", ".join(sorted(tag_budgets)), tag_mode, tag_similarity)

    try:
        exceptions = resolve_exceptions(watch_dir, args, remote_document)
    except ConfigError as exc:
        LOG.error("invalid exception: %s", exc)
        return 2
    if exceptions:
        today = date.today()
        live = [item for item in exceptions if item.active(today)]
        soon = [item for item in live if (item.expires - today).days <= EXCEPTION_WARN_DAYS]
        LOG.info("except  %d active, %d expired%s", len(live), len(exceptions) - len(live),
                 f", {len(soon)} ending within {EXCEPTION_WARN_DAYS} days" if soon else "")

    # Toasts default on for a human watching a folder, off for CI and hooks:
    # a build agent has nobody to interrupt.
    notify_wanted = (args.notify if args.notify is not None
                     else (not args.once and root.is_dir()))
    notifier = None
    if notify_wanted:
        try:
            import notifier as notifier_module
            notifier = notifier_module.Notifier(max_per_minute=args.notify_max or 6)
            if not notifier.available:
                LOG.debug("no desktop notification backend on this machine")
                notifier = None
        except Exception as exc:      # missing module, no backend, locked session
            LOG.debug("desktop notifications unavailable: %s", exc)
            notifier = None
    if notifier is not None:
        LOG.info("toasts  on (%d/min)", notifier.max_per_minute)

    try:
        totals = resolve_totals(watch_dir, args, remote_document)
    except ConfigError as exc:
        LOG.error("invalid totals: %s", exc)
        return 2
    gatekeeper = Gatekeeper(budget, healer=healer, overrides=overrides,
                            tag_budgets=tag_budgets,
                            tag_mode=tag_mode, tag_similarity=tag_similarity,
                            tag_root=watch_dir, exceptions=exceptions)
    gatekeeper.want_memory = bool(totals)
    reporter = Reporter(color=not args.no_color and not args.json, root=watch_dir,
                        json_mode=args.json, telemetry=telemetry, notifier=notifier)
    reporter.keep_verdicts = bool(args.report)

    if root.is_file():
        if not is_candidate(root):
            LOG.error("not a tracked asset type: %s", root.name)
            return 2
        if getattr(args, "exception_path", None) and exceptions:
            relative = str(args.exception_path).replace("\\", "/").lstrip("/")
            if ".." in relative.split("/"):
                LOG.error("--exception-path must not contain '..'")
                return 2
            virtual = Path("/agk-exception-root")
            gatekeeper.exceptions = tuple(replace(item, base=virtual) for item in exceptions)
            gatekeeper.exception_paths[root] = virtual / relative
        verdict = gatekeeper.check(root)
        reporter.report(verdict)
        reporter.close()
        if args.report:
            write_reports(args.report, reporter.verdicts, watch_dir)
        if one_file and not verdict.healed and not verdict.passed:
            reason = getattr(healer, "last_refusal", "")
            if not any(note.startswith("--heal would:") for note in verdict.warnings):
                LOG.warning("not healed: %s. %s is unchanged.",
                            reason or "the healer had nothing safe to do", root.name)
        return 0 if verdict.passed else 1

    if args.once:
        jobs = resolve_jobs(args.jobs)
        if jobs > 1 and healer is not None:
            LOG.warning("--jobs %d ignored: healing rewrites files, so it runs one file at "
                        "a time", jobs)
            jobs = 1
        cache = None
        if not args.no_cache:
            cache_path = (Path(args.cache).expanduser() if args.cache
                          else default_cache_path(watch_dir))
            if cache_inside(watch_dir, cache_path):
                LOG.error("--cache %s is inside the folder being checked: anyone who can "
                          "commit there could write a cache that passes any file. Put it "
                          "outside (CI: the runner's temp folder), or use --no-cache",
                          cache_path)
                return 2
            cache = VerdictCache(cache_path, watch_dir, rules_fingerprint(gatekeeper),
                                 reuse_failures=healer is None)
            if cache.reset_reason:
                LOG.info("cache   %s: %s", cache_path.name, cache.reset_reason)
        status = run_once(watch_dir, gatekeeper, reporter, args.recursive, budget.exclude,
                          follow_symlinks=getattr(args, "follow_symlinks", False),
                          allow_empty=getattr(args, "allow_empty", False), totals=totals,
                          cache=cache, jobs=jobs)
        if args.report:
            write_reports(args.report, reporter.verdicts, watch_dir)
    else:
        if args.report:
            LOG.warning("--report is written by --once runs only; ignored while watching")
        status = run_watch(watch_dir, gatekeeper, reporter, args.recursive, budget.exclude)
    reporter.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
