#!/usr/bin/env python3
"""
AssetGatekeeper
===============

A tiny, dependency-light "budget gate" that sits on an artist's export folder and
fails fast on assets that would blow a project's technical budget. Written to be
readable and hackable, not clever.

Rules enforced
--------------
    .obj / .gltf / .glb   triangles     <= 50,000
                          material slots <= 3          (draw-call proxy)
                          at least 1 triangle          (catches truncated writes)
    .png                  max edge      <= 2,048 px
                          both edges are a power of two
                          filename ends with _Albedo / _Normal / _Roughness

Usage
-----
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
-------------
If ``rules.toml`` sits in the watched folder it supplies the baseline budget;
CLI flags still win, so CI can tighten one value without editing a file the
artists own. Precedence: explicit CLI flag > rules.toml > built-in defaults.
See ``asset_gatekeeper.example.toml`` for the schema.

Exit codes (--once): 0 = every asset passed, 1 = at least one failed, 2 = bad setup.

Install
-------
    python -m pip install watchdog trimesh pillow

Notes for extending
-------------------
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
__version__ = "1.0.0"

import argparse
import array
import base64
import difflib
import fnmatch
import getpass
import json
import logging
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
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import unquote

# --------------------------------------------------------------------------- #
# Optional dependencies — fail with a readable message instead of a traceback.
# --------------------------------------------------------------------------- #

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
except ModuleNotFoundError:  # pragma: no cover - Python 3.9 / 3.10
    tomllib = None  # type: ignore[assignment]

try:  # numpy arrives with trimesh; used only for vectorised geometry maths
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

RULES_FILENAME = "rules.toml"

# --- enterprise plumbing ---------------------------------------------------- #
# Everything below is standard library only: the tool still has to run from a
# bare interpreter on a studio workstation with no wheels of its own.
SERVER_ENV_URL = "ASSET_GATEKEEPER_SERVER_URL"
SERVER_ENV_TOKEN = "ASSET_GATEKEEPER_TOKEN"
REMOTE_RULES_ENV = "ASSET_GATEKEEPER_REMOTE_RULES"
RULES_CACHE_FILENAME = ".rules.cache.json"

# A strict ceiling: a slow studio endpoint must never hold an artist's terminal.
HTTP_TIMEOUT = 2.0
# Bounded queue: an overnight 200k-file sweep must not grow memory without limit.
TELEMETRY_QUEUE_MAX = 1_000
PAYLOAD_SCHEMA = 1
USER_AGENT = "AssetGatekeeper/1.0 (+https://github.com/ohio13/asset-gatekeeper)"


LOG = logging.getLogger("assetgatekeeper")

MESH_SUFFIXES = frozenset({".obj", ".gltf", ".glb"})
GLTF_SUFFIXES = frozenset({".gltf", ".glb"})
TEXTURE_SUFFIXES = frozenset({".png"})
TRACKED_SUFFIXES = MESH_SUFFIXES | TEXTURE_SUFFIXES

# Partial writes and DCC scratch files that must never be inspected.
IGNORED_SUFFIXES = (
    ".tmp", ".temp", ".part", ".partial", ".crdownload", ".download",
    ".swp", ".lock", ".cache", ".blend1",
)
# ".agk-heal-" is the healer's staging folder: it lives inside the watch dir so the
# atomic rename is same-filesystem, and it must never be inspected as an asset.
IGNORED_PREFIXES = ("~$", ".~", ".git", ".agk-heal-")

GLB_MAGIC = b"glTF"
GLB_JSON_CHUNK = 0x4E4F534A  # 'JSON'


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Budget:
    """The hardcoded technical budget this gate enforces."""

    max_triangles: int = 50_000
    max_texture_edge: int = 2_048
    max_material_slots: int = 3
    require_power_of_two: bool = True
    enforce_texture_naming: bool = True
    texture_suffixes: tuple[str, ...] = ("_Albedo", "_Normal", "_Roughness")
    # Deep data profiling: the bottlenecks that survive an art review.
    max_bone_influences: int = 4          # per-vertex weights; mobile/web GPUs cap at 4
    max_uv_channels: int = 4              # TEXCOORD_n sets; most engines stop at 4-8
    allow_degenerate: bool = False        # zero-area triangles: poison for Nanite / RT
    degenerate_epsilon: float = 1e-8      # area below which a triangle counts as degenerate

    # -- filters ------------------------------------------------------------ #
    # Globs that look like assets but are not: review renders, 4x paint sources,
    # hashed build caches. From [filters] exclude. Empty by default.
    exclude: tuple[str, ...] = ()

    # -- reference & hierarchy auditing ------------------------------------ #
    # On by default: a .gltf pointing at a texture nobody shipped is broken in
    # every engine, on every platform, with no ambiguity to argue about.
    check_references: bool = True

    # -- PBR value sanitisation --------------------------------------------- #
    # Albedo is *not* a painting. Real-world base colour reflectance lives in a
    # narrow band; pure black absorbs everything and pure white blinds the
    # lighting model, which is why graders ask for it to be clamped.
    pbr_check: bool = True
    pbr_floor: float = 0.02               # 5/255: below this, albedo is "pure black"
    pbr_ceiling: float = 0.94             # 240/255: above this, albedo is "pure white"
    pbr_suffixes: tuple[str, ...] = ("_Albedo", "_Diffuse")

    # -- geometric boundary profiling --------------------------------------- #
    # Off by default, on purpose: plenty of legitimate exports are authored with
    # an offset pivot (a door hinged at its edge) or as a triangle soup. That is
    # a conversation with the artist, not a violation, so it is opt-in.
    audit_transforms: bool = False
    require_indexed: bool = False
    pivot_tolerance: float = 0.05         # fraction of the bbox diagonal
    transform_epsilon: float = 1e-4       # metres; below this a translation is "baked"

    # Which platform profile produced this budget (for the log line only).
    platform: str = ""

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
                                   "preset", "notify", "filters"), "the rules file")
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
                               "max_uv_channels", "allow_degenerate", "degenerate_epsilon",
                               "check_references", "audit_transforms", "require_indexed",
                               "pivot_tolerance", "transform_epsilon"), "[mesh]")
        _reject_unknown(texture, ("max_edge", "power_of_two", "naming", "suffixes",
                                  "pbr", "pbr_floor", "pbr_ceiling", "pbr_suffixes"), "[texture]")

        max_triangles = _read_int(mesh, "max_triangles", "[mesh]")
        max_material_slots = _read_int(mesh, "max_material_slots", "[mesh]", minimum=0)
        max_bone_influences = _read_int(mesh, "max_bone_influences", "[mesh]")
        max_uv_channels = _read_int(mesh, "max_uv_channels", "[mesh]", minimum=1)
        allow_degenerate = _read_bool(mesh, "allow_degenerate", "[mesh]")
        degenerate_epsilon = _read_float(mesh, "degenerate_epsilon", "[mesh]")
        max_texture_edge = _read_int(texture, "max_edge", "[texture]")
        require_power_of_two = _read_bool(texture, "power_of_two", "[texture]")
        enforce_texture_naming = _read_bool(texture, "naming", "[texture]")
        texture_suffixes = _read_strings(texture, "suffixes", "[texture]")

        # Reference / hierarchy auditing
        check_references = _read_bool(mesh, "check_references", "[mesh]")

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
            degenerate_epsilon=(
                defaults.degenerate_epsilon if degenerate_epsilon is None else degenerate_epsilon
            ),
            check_references=(
                defaults.check_references if check_references is None else check_references
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
        )


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

    @property
    def passed(self) -> bool:
        return not self.problems

    @property
    def healed(self) -> bool:
        return bool(self.heal_notes)

    @property
    def status(self) -> str:
        """PASSED / HEALED / FAILED - the word a dashboard and a hook can branch on."""
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
        return f"FAILED: {self.reason}"


class AssetError(Exception):
    """Raised when a file cannot be inspected at all (corrupt, unreadable, ...)."""


class ConfigError(Exception):
    """Raised when a rules file exists but cannot be used."""


# --------------------------------------------------------------------------- #
# Rules file: optional per-folder configuration (``rules.toml``)
# --------------------------------------------------------------------------- #

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
# make a check disappear — that would let one platform ship unaudited assets.
PLATFORM_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "mesh": ("pivot_tolerance", "transform_epsilon"),
    "texture": ("pbr_floor", "pbr_ceiling"),
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


# --------------------------------------------------------------------------- #
# Shipped presets and tag-based profiles
# --------------------------------------------------------------------------- #

# A preset is a known-good starting point that lives in code, so a project file
# cannot quietly degrade it: `--preset retro` beats anything in rules.toml.
PRESETS: dict[str, dict[str, object]] = {
    "retro": {
        # No max_bone_influences here: the budget validator refuses 0, and "this
        # platform does not skin" is a rules-file policy, not a preset value.
        "max_triangles": 1_500, "max_material_slots": 1, "max_texture_edge": 256,
        "max_uv_channels": 1,
    },
    "mobile": {
        "max_triangles": 15_000, "max_material_slots": 2, "max_texture_edge": 1_024,
        "max_bone_influences": 4, "max_uv_channels": 2,
    },
    "pc": {
        "max_triangles": 120_000, "max_material_slots": 6, "max_texture_edge": 4_096,
        "max_bone_influences": 8, "max_uv_channels": 4,
    },
}

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

    Used by `[platforms.*]`, `[tags.*]` and the shipped presets — one merge
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
            merged[key].update(value)
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
        if document is None:
            budget, source = Budget(), None
            platform = requested_platform
        else:
            platform = requested_platform or str(document.get("platform", "") or "")
            try:
                document = apply_preset(apply_platform(document, platform),
                                        requested_preset or str(document.get("preset", "") or ""))
                budget, source = Budget.from_mapping(document), rules_path
            except ConfigError as exc:
                raise ConfigError(f"{rules_path}: {exc}") from exc

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
        return budget, source, []
    return replace(budget, **overrides), source, sorted(overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# `--init`: the five-question setup wizard
# --------------------------------------------------------------------------- #

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
# Off by default: the gate fails and locks. Turn this on only if you want
# textures padded and degenerate faces stripped, verified before adoption.
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
    hitting EOF falls back to its default — ``--init`` inside a CI script must
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


# --------------------------------------------------------------------------- #
# Repository audit: the whole folder, not one asset
# --------------------------------------------------------------------------- #

def run_audit(root: Path) -> int:
    """Report broken references and orphaned textures across a repository.

    Exit 0 when the repository is clean, 1 when anything is wrong — a folder that
    looks shut but is missing its textures is exactly the failure this catches.
    """
    assets = sorted(p for p in root.rglob("*")
                    if p.is_file() and p.suffix.lower() in MESH_SUFFIXES)
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


# --------------------------------------------------------------------------- #
# Enterprise: central rules endpoint, offline cache, server settings
# --------------------------------------------------------------------------- #

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

    @property
    def telemetry_enabled(self) -> bool:
        return bool(self.telemetry_url)

    @classmethod
    def from_mapping(cls, table: dict, where: str = "[server]") -> "ServerSettings":
        if not isinstance(table, dict):
            raise ConfigError(f"{where} must be a table")
        _reject_unknown(table, ("url", "token", "remote_rules_url"), where)
        return cls(
            telemetry_url=_read_str(table, "url", where) or "",
            remote_rules_url=_read_str(table, "remote_rules_url", where) or "",
            token=_read_str(table, "token", where) or "",
        )


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
            document = json.loads(body)
        elif tomllib is not None:
            document = tomllib.loads(body)
        else:
            raise ConfigError(
                f"{origin}: payload is not JSON and TOML needs Python 3.11+ (tomllib)"
            )
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
    """GET the central budget, falling back to the cached copy. Never raises.

    The failure contract is the whole point of this function: a studio's network
    being down must degrade to "yesterday's budget", not to a traceback that
    stops the artists working. The broad ``except`` is deliberate.
    """
    try:
        text = _http_body(url, timeout=timeout, token=token,
                          accept="application/json, application/toml")
        document = parse_rules_text(text, url)
        Budget.from_mapping(document)  # validate before caching: a broken payload
        write_rules_cache(cache_path, url, document)  # must not poison the good cache
        LOG.debug("remote rules %s: %d bytes accepted", url, len(text))
        return document
    except Exception as exc:  # network, HTTP status, malformed payload, bad types
        LOG.debug("remote rules fetch failed: %s: %s", type(exc).__name__, exc)
        cached = read_rules_cache(cache_path)
        if cached is not None:
            LOG.warning("Central server offline; using cached rules profile (%s)", cache_path.name)
            return cached
        LOG.warning("Central server offline and no cached rules profile; using local settings")
        return None


@dataclass(frozen=True)
class HealSettings:
    """Opt-in healing policy.

    ``enabled`` is False unless a human says otherwise — via ``--auto-heal`` or
    ``[healer] enabled = true``. Nothing in this tool ever repairs an asset as a
    side effect of failing it; automated mesh decimation and resampling destroy
    artistic intent, so the default answer is "no".
    """

    enabled: bool = False
    textures: bool = True       # NPOT + missing-suffix renames
    meshes: bool = True         # zero-area faces, .obj only
    texture_mode: str = "pad"   # pad preserves pixels; resize resamples

    @classmethod
    def from_mapping(cls, table: dict, where: str = "[healer]") -> "HealSettings":
        if not isinstance(table, dict):
            raise ConfigError(f"{where} must be a table")
        _reject_unknown(table, ("enabled", "textures", "meshes", "texture_mode"), where)
        mode = _read_str(table, "texture_mode", where)
        if mode is not None and mode not in ("pad", "resize"):
            raise ConfigError(f"{where}.texture_mode must be 'pad' or 'resize', got {mode!r}")
        enabled = _read_bool(table, "enabled", where)
        textures = _read_bool(table, "textures", where)
        meshes = _read_bool(table, "meshes", where)
        defaults = cls()
        return cls(
            enabled=defaults.enabled if enabled is None else enabled,
            textures=defaults.textures if textures is None else textures,
            meshes=defaults.meshes if meshes is None else meshes,
            texture_mode=defaults.texture_mode if mode is None else mode,
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


def load_healer(budget: Budget, settings: HealSettings):
    """Import the companion ``healer.py`` next to this file — only when asked.

    Loaded by path rather than by import name so the tool still works when it is
    copied alone into a studio's tools folder next to its healer.
    """
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
    try:
        return module.Healer(budget, textures=settings.textures, meshes=settings.meshes,
                             texture_mode=settings.texture_mode)
    except module.HealError as exc:
        raise ConfigError(f"healer cannot start: {exc}") from exc


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
    questions — "what is allowed" versus "where does it come from, where does it
    report" — and a studio may centralise one without the other.
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
    )


# --------------------------------------------------------------------------- #
# Analyzers — one concern each, no logging, no watching.
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=8)
def _load_scene_cached(path_string: str, modified_ns: int) -> object:
    """One load per (path, mtime). The mtime keeps the cache honest on re-export."""
    return trimesh.load(path_string, force="scene", process=False)


def load_scene(path: Path) -> object:
    """Load a mesh once and reuse it across the triangle/material/geometry checks."""
    if trimesh is None:
        raise AssetError("trimesh is not installed; mesh data cannot be analysed")
    try:
        return _load_scene_cached(str(path), path.stat().st_mtime_ns)
    except Exception as exc:
        raise AssetError(f"could not read mesh ({type(exc).__name__}: {exc})") from exc


def _geometries(scene: object) -> list:
    if trimesh is not None and isinstance(scene, trimesh.Scene):
        return list(scene.geometry.values())
    return [scene]


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
    if path.suffix.lower() == ".gltf":
        return json.loads(path.read_text(encoding="utf-8"))

    blob = path.read_bytes()
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


def _count_triangles_from_document(document: dict) -> int:
    """Count triangles from a glTF JSON document, walking the node graph.

    Pure and dependency-free, so it also serves as the fallback when trimesh (or
    one of its optional loaders) is unavailable.
    """
    accessors = document.get("accessors", [])

    per_mesh: dict[int, int] = {}
    for mesh_index, mesh in enumerate(document.get("meshes", [])):
        subtotal = 0
        for primitive in mesh.get("primitives", []):
            index_accessor = primitive.get("indices")
            if index_accessor is not None:
                subtotal += accessors[index_accessor].get("count", 0) // 3
            else:
                position_accessor = primitive.get("attributes", {}).get("POSITION")
                if position_accessor is not None:
                    subtotal += accessors[position_accessor].get("count", 0) // 3
        per_mesh[mesh_index] = subtotal

    nodes = document.get("nodes", [])

    def node_total(node_index: int, seen: frozenset[int] = frozenset()) -> int:
        if node_index in seen:  # defensive: malformed cyclic graph
            return 0
        node = nodes[node_index]
        total = per_mesh.get(node.get("mesh", -1), 0)
        for child in node.get("children", []):
            total += node_total(child, seen | {node_index})
        return total

    roots = [root for scene in document.get("scenes", []) for root in scene.get("nodes", [])]
    if roots:
        return sum(node_total(root) for root in roots)
    return sum(per_mesh.values())


def count_triangles(path: Path) -> int:
    """Triangle count for .obj / .gltf / .glb (trimesh first, JSON fallback)."""
    if trimesh is not None:
        try:
            return _count_triangles_trimesh(path)
        except AssetError:
            raise
        except Exception as exc:  # trimesh raises a wide variety of errors
            if path.suffix.lower() not in GLTF_SUFFIXES:
                raise AssetError(f"could not read mesh ({type(exc).__name__}: {exc})") from exc
            LOG.debug("trimesh failed on %s, falling back to JSON: %s", path.name, exc)

    if path.suffix.lower() in GLTF_SUFFIXES:
        try:
            return _count_triangles_from_document(read_gltf_document(path))
        except (AssetError, ValueError, OSError, struct.error) as exc:
            raise AssetError(f"could not parse glTF ({type(exc).__name__}: {exc})") from exc

    raise AssetError("trimesh is not installed; .obj/.glb cannot be analysed")


def _count_material_slots_trimesh(path: Path) -> int:
    """Fallback: distinct material objects across a loaded scene."""
    if trimesh is None:
        raise AssetError("trimesh is not installed; material slots cannot be counted")
    loaded = trimesh.load(str(path), force="scene", process=False)
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


# --------------------------------------------------------------------------- #
# Deep data profiling: skin weights, UV sets, degenerate geometry
# --------------------------------------------------------------------------- #

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


def count_uv_channels(path: Path) -> int:
    """Number of distinct texture-coordinate sets (TEXCOORD_n / OBJ vt)."""
    suffix = path.suffix.lower()

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
    second set (JOINTS_1/WEIGHTS_1). We sum non-zero weights across all sets —
    which is exactly what a mobile GPU cannot afford above four.
    """
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


def count_degenerate_triangles(path: Path, epsilon: float = 1e-8) -> int:
    """Triangles whose area is ~zero (collinear or coincident corners)."""
    if np is None:  # pragma: no cover - numpy ships with trimesh
        LOG.debug("numpy is unavailable; skipping degenerate geometry check")
        return 0

    total = 0
    for geometry in _geometries(load_scene(path)):
        faces = getattr(geometry, "faces", None)
        vertices = getattr(geometry, "vertices", None)
        if faces is None or vertices is None or len(faces) == 0:
            continue
        corners = np.asarray(vertices)[np.asarray(faces)]
        edge_a = corners[:, 1] - corners[:, 0]
        edge_b = corners[:, 2] - corners[:, 0]
        areas = 0.5 * np.linalg.norm(np.cross(edge_a, edge_b), axis=1)
        total += int(np.count_nonzero(areas <= epsilon))
    return total


def measure_texture(path: Path) -> tuple[int, int]:
    """Read a PNG's resolution from its header without decoding pixel data."""
    if Image is None:
        raise AssetError("Pillow is not installed; textures cannot be analysed")
    try:
        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format
    except Image.DecompressionBombError as exc:  # pragma: no cover - extreme input
        raise AssetError(f"image dimensions are unsafe to inspect ({exc})") from exc
    except OSError as exc:
        raise AssetError(f"could not read image ({exc})") from exc

    if image_format != "PNG":
        raise AssetError(f"file has a .png extension but is actually {image_format}")
    return width, height


def is_power_of_two(value: int) -> bool:
    """True for 1, 2, 4, ... 1024, 2048, 4096."""
    return value > 0 and value & (value - 1) == 0


def build_naming_pattern(suffixes: Sequence[str]) -> Optional[re.Pattern[str]]:
    """Case-insensitive ``_Albedo``-style suffix matcher.

    Tolerates a trailing index (``_Albedo_02``) because exported atlases and
    variant sets legitimately number their maps.
    """
    tokens = [re.escape(suffix.lstrip("_")) for suffix in suffixes if suffix.strip("_")]
    if not tokens:
        return None
    return re.compile(rf"_(?:{'|'.join(tokens)})(?:_?\d+)?$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# The gate itself
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Reference & hierarchy auditing
# --------------------------------------------------------------------------- #

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
    """Buffer and image URIs — the two places glTF stores a sibling file."""
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


def orphaned_files(root: Path) -> list[Path]:
    """Texture files in the repository that no mesh references.

    Repository-level, not per-asset: an orphan is only visible when you compare
    every asset against every texture on disk.
    """
    root = Path(root)
    referenced: set[Path] = set()
    for candidate in root.rglob("*"):
        if not candidate.is_file() or candidate.suffix.lower() not in MESH_SUFFIXES:
            continue
        for _written, target in resolve_references(candidate):
            try:
                referenced.add(target.resolve())
            except OSError:      # pragma: no cover - unreadable path
                continue
    orphans = []
    for candidate in root.rglob("*"):
        if not candidate.is_file() or candidate.suffix.lower() not in TEXTURE_DISK_SUFFIXES:
            continue
        try:
            if candidate.resolve() not in referenced:
                orphans.append(candidate)
        except OSError:          # pragma: no cover
            continue
    return sorted(orphans)


# --------------------------------------------------------------------------- #
# PBR value sanitisation
# --------------------------------------------------------------------------- #

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
            pixels = list(image.getdata())
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


# --------------------------------------------------------------------------- #
# Geometric boundary profiling
# --------------------------------------------------------------------------- #

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


class Gatekeeper:
    """Applies the budget rules and returns a Verdict. No I/O side effects.

    The single exception is healing, and it is opt-in: pass a ``Healer`` and a
    failing asset is repaired *only if* the repaired copy then verifies as
    passing. With ``healer=None`` (the default) this class never writes a byte.
    """

    def __init__(self, budget: Budget, healer: Optional[object] = None,
                 tag_budgets: Optional[dict[str, Budget]] = None,
                 tag_mode: str = "suggest",
                 tag_similarity: float = DEFAULT_TAG_SIMILARITY,
                 tag_root: Optional[Path] = None) -> None:
        self.budget = budget
        self.healer = healer
        self.tag_budgets = tag_budgets or {}
        self.tag_mode = tag_mode if tag_mode in TAG_MODES else "suggest"
        self.tag_similarity = tag_similarity
        self.tag_root = tag_root
        self._naming = build_naming_pattern(budget.texture_suffixes) if budget.enforce_texture_naming else None

    def check(self, path: Path) -> Verdict:
        budget, tag, warning = self._budget_for(path)
        previous = self.budget
        if budget is not previous:
            # One asset is inspected at a time (single worker thread), so a
            # scoped swap is safe and avoids threading a budget through every
            # analyzer signature.
            self.budget = budget
            self._naming = (build_naming_pattern(budget.texture_suffixes)
                            if budget.enforce_texture_naming else None)
        try:
            verdict = self._inspect(path)
            if verdict.problems and self.healer is not None:
                verdict = self._heal(path, verdict)
        finally:
            self.budget = previous
            self._naming = (build_naming_pattern(previous.texture_suffixes)
                            if previous.enforce_texture_naming else None)

        notes = list(verdict.warnings)
        if warning:
            if self.tag_mode == "strict":
                # Strict mode turns a near miss into a failure, with the fix in
                # the message: that is the whole point of not guessing politely.
                problems = (warning + " (strict tag mode: rename the folder to match)",) + verdict.problems
                return Verdict(verdict.path, problems, verdict.metric, verdict.stats,
                               verdict.heal_notes, tuple(notes))
            notes.append(warning)
        if notes:
            return Verdict(verdict.path, verdict.problems, verdict.metric, verdict.stats,
                           verdict.heal_notes, tuple(notes))
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

    # -- meshes ------------------------------------------------------------ #

    def _check_mesh(self, path: Path) -> Verdict:
        try:
            triangles = count_triangles(path)
        except AssetError as exc:
            return Verdict(path, (str(exc),))

        problems: list[str] = []
        metrics = [f"{triangles:,} tris"]
        stats: list[tuple[str, int]] = [("triangles", triangles)]

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
                    f"{slots} material slots exceeds the {limit} slot budget (draw calls)"
                )

        try:
            influences = max_bone_influences(path)
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

        if not self.budget.allow_degenerate:
            try:
                degenerate = count_degenerate_triangles(path, self.budget.degenerate_epsilon)
            except AssetError as exc:
                problems.append(str(exc))
            else:
                stats.append(("degenerate_triangles", degenerate))
                if degenerate:
                    problems.append(
                        f"{degenerate:,} degenerate triangles (zero-area) - Nanite and ray "
                        f"tracing compute garbage from these"
                    )

        # Broken references are unconditional when enabled: a .gltf pointing at a
        # texture nobody shipped fails to load in every engine, on every platform.
        if self.budget.check_references:
            broken = missing_references(path)
            for written, _target in broken:
                problems.append(
                    f"missing reference '{written}' (broken link: the engine cannot load it)")
            if broken:
                stats.append(("broken_references", len(broken)))

        # Transform/pivot/index audits are opt-in (see Budget.audit_transforms).
        if self.budget.audit_transforms or self.budget.require_indexed:
            try:
                problems.extend(transform_audit(path, self.budget))
            except Exception as exc:      # an audit must never break the verdict
                LOG.debug("transform audit failed for %s: %s", path.name, exc)

        return Verdict(path, tuple(problems), " / ".join(metrics), tuple(stats))

    # -- textures ---------------------------------------------------------- #

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

        if self._naming is not None and not self._naming.search(path.stem):
            expected = ", ".join(self.budget.texture_suffixes)
            problems.append(f"name does not end with a standard suffix ({expected})")

        # PBR sanitisation applies to albedo/diffuse only: a normal map is
        # allowed to be flat violet-blue, and a roughness map is allowed to be
        # pure white, because neither is a reflectance colour.
        if self.budget.pbr_check and self._is_pbr(path):
            violation = pbr_violation(path, self.budget)
            if violation:
                problems.append(violation)

        return Verdict(path, tuple(problems), f"{width}x{height}",
                       (("width", width), ("height", height)))

    def _is_pbr(self, path: Path) -> bool:
        stem = path.stem.lower()
        return any(stem.endswith(suffix.lower()) for suffix in self.budget.pbr_suffixes)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _safe_username() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no passwd entry / locked-down container
        return ""


def build_telemetry_payload(verdict: Verdict, root: Optional[Path] = None) -> dict:
    """One flat JSON object per asset: the studio's record of what was gated.

    Deliberately flat — a data warehouse can ingest it without a nested schema,
    and the plugin contract (``--json``) stays a superset of these keys.
    """
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
    return {
        "schema": PAYLOAD_SCHEMA,
        "timestamp": _utc_now(),
        "hostname": platform.node(),
        "username": _safe_username(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "name": path.name,
        "path": relative,
        "size_bytes": size_bytes,
        "passed": verdict.passed,
        # PASSED / HEALED / FAILED: a healed asset passed, but the studio still
        # wants to know it was rewritten, and what was done to it.
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

    1. **Never raise.** A dead endpoint is a debug line, not a crash — the artist
       still gets their PASSED/FAILED verdict either way.
    2. **Never block the gate.** ``submit()`` only enqueues; one daemon thread
       owns the sockets. A 2 s timeout can therefore delay the *network*, never
       the inspection.
    3. **Never grow without bound.** The queue is capped; past that, payloads are
       dropped and counted rather than slowing the local run.
    """

    def __init__(self, url: str, root: Optional[Path] = None, token: str = "",
                 timeout: float = HTTP_TIMEOUT, queue_max: int = TELEMETRY_QUEUE_MAX) -> None:
        self.url = url
        self.root = root
        self.token = token
        self.timeout = float(timeout)
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=queue_max)
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        if not url.lower().startswith("https://"):
            # Asset names and paths are shipping in clear text; say it once.
            LOG.warning("telemetry endpoint is not https - payloads are sent in clear text")

    # -- producer side ------------------------------------------------------ #

    def submit(self, verdict: Verdict, root: Optional[Path] = None) -> None:
        """Enqueue one verdict. Never blocks, never raises."""
        if self._closed:
            return
        try:
            payload = build_telemetry_payload(verdict, self.root if root is None else root)
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

    # -- worker side -------------------------------------------------------- #

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
        except Exception as exc:
            # Silent by contract. The artist's terminal is not a network monitor,
            # and -v turns this into a visible line for whoever is debugging it.
            self.failed += 1
            LOG.debug("telemetry: POST failed (%s): %s", type(exc).__name__, exc)

    # -- lifecycle ---------------------------------------------------------- #

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

    def summary(self) -> None:
        total = self.passed + self.failed
        if self.json_mode:
            print(json.dumps({"summary": {"total": total, "passed": self.passed,
                                          "failed": self.failed}}), flush=True)
            return
        LOG.info("%d asset(s): %d passed, %d failed", total, self.passed, self.failed)

    def close(self, timeout: Optional[float] = None) -> None:
        """Flush pending telemetry and toasts. Bounded, and a no-op when off."""
        if self.telemetry is not None:
            self.telemetry.close(timeout)
        if self.notifier is not None:
            self.notifier.close(timeout)


# --------------------------------------------------------------------------- #
# Watching
# --------------------------------------------------------------------------- #

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


def wait_until_stable(path: Path, quiet: float = 0.35, timeout: float = 30.0) -> bool:
    """Block until the file stops growing and can be opened.

    Exporters (Blender, Substance, Unreal, Maya) fire a create/modify event the
    instant the handle opens, so inspecting immediately means reading a truncated
    file. Poll size until it has been unchanged for ``quiet`` seconds.
    """
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


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def describe_budget(budget: Budget) -> str:
    """One-line summary of the active rules, printed at startup."""
    return (
        f"tris<={budget.max_triangles:,}  "
        f"texture<={budget.max_texture_edge}px  "
        f"pot={'on' if budget.require_power_of_two else 'off'}  "
        f"materials<={budget.max_material_slots}  "
        f"influences<={budget.max_bone_influences}  "
        f"uvs<={budget.max_uv_channels}  "
        f"degenerate={'allowed' if budget.allow_degenerate else 'blocked'}  "
        f"naming={'on' if budget.enforce_texture_naming else 'off'}  "
        f"refs={'checked' if budget.check_references else 'ignored'}  "
        f"pbr={'on' if budget.pbr_check else 'off'}  "
        f"audit={'on' if budget.audit_transforms else 'off'}"
        + (f"  platform={budget.platform}" if budget.platform else "")
    )


def run_once(root: Path, gatekeeper: Gatekeeper, reporter: Reporter,
             recursive: bool, exclude: Sequence[str] = ()) -> int:
    pattern = "**/*" if recursive else "*"
    candidates = sorted(p for p in root.glob(pattern)
                        if p.is_file() and is_candidate(p, exclude))
    if not candidates:
        LOG.info("no tracked assets found in %s", root)
        return 0

    for path in candidates:
        if wait_until_stable(path):
            reporter.report(gatekeeper.check(path))
        else:
            LOG.warning("%s is still being written by another process; skipped", path.name)

    reporter.summary()
    return 1 if reporter.failed else 0


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asset_gatekeeper",
        description="Watch a folder of art exports and enforce asset budgets.",
    )
    parser.add_argument("folder", nargs="?", default=None,
                        help="folder to watch (default: current directory)")
    parser.add_argument("--triangles", type=int, default=None,
                        help="max triangles per mesh (overrides rules.toml; default 50,000)")
    parser.add_argument("--max-texture", type=int, default=None, metavar="EDGE",
                        help="max texture width/height in pixels (overrides rules.toml; default 2,048)")
    parser.add_argument("--max-materials", type=int, default=None, metavar="N",
                        help="max material slots per mesh (overrides rules.toml; default 3)")
    parser.add_argument("--max-influences", type=int, default=None, metavar="N",
                        help="max bone weights per vertex (overrides rules.toml; default 4)")
    parser.add_argument("--max-uvs", type=int, default=None, metavar="N",
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
    parser.add_argument("--notify-max", type=int, default=None, metavar="N",
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
    parser.add_argument("--init", action="store_true",
                        help="interactive setup: create the watch folder and its rules.toml")
    parser.add_argument("--yes", action="store_true",
                        help="with --init: accept every default instead of prompting")
    parser.add_argument("--force", action="store_true",
                        help="with --init: overwrite an existing rules.toml")
    parser.add_argument("--audit", action="store_true",
                        help="audit the whole folder for broken references and orphaned "
                             "textures, then exit")
    parser.add_argument("--auto-heal", action="store_true", dest="auto_heal", default=None,
                        help="repair failing assets in place (NPOT textures, missing texture "
                             "suffixes, zero-area .obj faces) - off unless this or "
                             "[healer] enabled = true is set")
    parser.add_argument("--no-auto-heal", action="store_false", dest="auto_heal",
                        help="force healing off, even if rules.toml enables it (use in CI)")
    parser.add_argument("--remote-rules", metavar="URL", default=None,
                        help="fetch the budget from a central endpoint; cached locally for "
                             f"offline use (or set {REMOTE_RULES_ENV})")
    parser.add_argument("--telemetry-url", metavar="URL", default=None,
                        help="POST every verdict to this endpoint (or set "
                             f"{SERVER_ENV_URL})")
    parser.add_argument("--once", action="store_true", help="scan existing files and exit")
    parser.add_argument("--recursive", action="store_true", help="include subfolders")
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

    # The wizard runs before anything touches the folder: it may be creating it.
    if getattr(args, "init", False):
        target = Path(args.folder).expanduser() if args.folder else None
        return run_init(target, ask=input, assume_yes=args.yes, force=args.force,
                        max_triangles=None, max_edge=None, max_material_slots=None)

    root = Path(args.folder or ".").expanduser().resolve()
    if not root.exists():
        LOG.error("not found: %s", root)
        return 2
    # Single-file mode lets a DCC plugin gate one temporary export directly.
    watch_dir = root if root.is_dir() else root.parent

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
        remote_document = fetch_remote_rules(
            server.remote_rules_url, watch_dir / RULES_CACHE_FILENAME, token=server.token
        )

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

    try:
        heal_settings = resolve_healer(watch_dir, args, remote_document)
    except ConfigError as exc:
        LOG.error("invalid healer settings: %s", exc)
        return 2

    healer = None
    if heal_settings.enabled:
        try:
            healer = load_healer(budget, heal_settings)
        except ConfigError as exc:
            LOG.error("%s", exc)
            return 2
        LOG.info("healing enabled  textures=%s meshes=%s mode=%s (opt-in)",
                 "on" if heal_settings.textures else "off",
                 "on" if heal_settings.meshes else "off",
                 heal_settings.texture_mode)

    telemetry = None
    if server.telemetry_enabled:
        telemetry = Telemetry(server.telemetry_url, root=watch_dir, token=server.token)
        LOG.info("report  %s", server.telemetry_url)

    try:
        tag_budgets, tag_mode, tag_similarity = resolve_tagging(watch_dir, args, remote_document)
    except ConfigError as exc:
        LOG.error("invalid tag configuration: %s", exc)
        return 2
    if tag_budgets:
        LOG.info("tags    %s  (%s matching, %.2f similarity)",
                 ", ".join(sorted(tag_budgets)), tag_mode, tag_similarity)

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

    gatekeeper = Gatekeeper(budget, healer=healer, tag_budgets=tag_budgets,
                            tag_mode=tag_mode, tag_similarity=tag_similarity,
                            tag_root=watch_dir)
    reporter = Reporter(color=not args.no_color and not args.json, root=watch_dir,
                        json_mode=args.json, telemetry=telemetry, notifier=notifier)

    if root.is_file():
        if not is_candidate(root):
            LOG.error("not a tracked asset type: %s", root.name)
            return 2
        verdict = gatekeeper.check(root)
        reporter.report(verdict)
        reporter.close()
        return 0 if verdict.passed else 1

    if args.once:
        status = run_once(watch_dir, gatekeeper, reporter, args.recursive, budget.exclude)
    else:
        status = run_watch(watch_dir, gatekeeper, reporter, args.recursive, budget.exclude)
    reporter.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
