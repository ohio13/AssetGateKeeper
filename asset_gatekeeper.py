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

import argparse
import array
import base64
import json
import logging
import os
import queue
import re
import struct
import sys
import threading
import time
from dataclasses import dataclass, replace
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
IGNORED_PREFIXES = ("~$", ".~", ".git")

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

    @classmethod
    def from_mapping(cls, document: dict) -> "Budget":
        """Build a Budget from a parsed rules file, rejecting typos loudly.

        Keys that are absent fall back to the dataclass defaults, so a rules file
        only ever has to state what differs from the shipped policy.
        """
        if not isinstance(document, dict):
            raise ConfigError("the rules file must contain a table of settings")
        _reject_unknown(document, ("mesh", "texture"), "the rules file")

        mesh = document.get("mesh", {})
        texture = document.get("texture", {})
        if not isinstance(mesh, dict):
            raise ConfigError("[mesh] must be a table")
        if not isinstance(texture, dict):
            raise ConfigError("[texture] must be a table")
        _reject_unknown(mesh, ("max_triangles", "max_material_slots", "max_bone_influences",
                               "max_uv_channels", "allow_degenerate", "degenerate_epsilon"), "[mesh]")
        _reject_unknown(texture, ("max_edge", "power_of_two", "naming", "suffixes"), "[texture]")

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
        )


@dataclass(frozen=True)
class Verdict:
    """Result of inspecting one asset: zero reasons = passed."""

    path: Path
    problems: tuple[str, ...] = ()
    metric: str = ""

    @property
    def passed(self) -> bool:
        return not self.problems

    @property
    def reason(self) -> str:
        return "; ".join(self.problems)

    @property
    def tag(self) -> str:
        return "PASSED" if self.passed else f"FAILED: {self.reason}"


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
        return json.loads(text)

    if tomllib is not None:
        return tomllib.loads(text)

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


def resolve_budget(root: Path, args: argparse.Namespace) -> tuple[Budget, Optional[Path], list[str]]:
    """Decide the effective budget.

    Precedence: explicit CLI flag > ``rules.toml`` > shipped defaults. That keeps
    the artist's folder authoritative for everyday work while letting CI tighten
    or relax a single value without editing a file that artists own.
    """
    rules_path = Path(args.rules).expanduser() if args.rules else root / RULES_FILENAME
    loaded = load_rules(rules_path)
    budget, source = loaded if loaded is not None else (Budget(), None)

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

    if not overrides:
        return budget, source, []
    return replace(budget, **overrides), source, sorted(overrides)  # type: ignore[arg-type]


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

class Gatekeeper:
    """Applies the budget rules and returns a Verdict. No I/O side effects."""

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self._naming = build_naming_pattern(budget.texture_suffixes) if budget.enforce_texture_naming else None

    def check(self, path: Path) -> Verdict:
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
                limit = self.budget.max_uv_channels
                if uv_channels > limit:
                    problems.append(f"{uv_channels} UV channels exceeds the {limit} channel limit")

        if not self.budget.allow_degenerate:
            try:
                degenerate = count_degenerate_triangles(path, self.budget.degenerate_epsilon)
            except AssetError as exc:
                problems.append(str(exc))
            else:
                if degenerate:
                    problems.append(
                        f"{degenerate:,} degenerate triangles (zero-area) - Nanite and ray "
                        f"tracing compute garbage from these"
                    )

        return Verdict(path, tuple(problems), " / ".join(metrics))

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

        return Verdict(path, tuple(problems), f"{width}x{height}")


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

class Reporter:
    """Formats verdicts as one terminal line each, with optional ANSI colour."""

    GREEN = "\033[32m"
    RED = "\033[31m"
    DIM = "\033[2m"
    RESET = "\033[0m"

    def __init__(self, color: bool = True, root: Optional[Path] = None,
                 json_mode: bool = False) -> None:
        self.color = color and self._ansi_supported()
        self.root = root
        self.json_mode = json_mode
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
        if self.json_mode:
            # Machine-readable contract for the DCC plugins: one object per file.
            with self._lock:
                self.passed += int(verdict.passed)
                self.failed += int(not verdict.passed)
                print(json.dumps({
                    "file": str(verdict.path),
                    "name": verdict.path.name,
                    "passed": verdict.passed,
                    "metric": verdict.metric,
                    "problems": list(verdict.problems),
                }), flush=True)
            return

        with self._lock:
            if verdict.passed:
                self.passed += 1
                tag = self._paint("PASSED", self.GREEN)
            else:
                self.failed += 1
                tag = self._paint(verdict.tag, self.RED)

            detail = f" {self._paint(verdict.metric, self.DIM)}" if verdict.metric else ""
            LOG.info("%s  %s%s", tag, self.display_path(verdict.path), detail)

    def summary(self) -> None:
        total = self.passed + self.failed
        if self.json_mode:
            print(json.dumps({"summary": {"total": total, "passed": self.passed,
                                          "failed": self.failed}}), flush=True)
            return
        LOG.info("%d asset(s): %d passed, %d failed", total, self.passed, self.failed)


# --------------------------------------------------------------------------- #
# Watching
# --------------------------------------------------------------------------- #

def is_candidate(path: Path) -> bool:
    """Cheap pre-filter: right extension, not a hidden/scratch file."""
    name = path.name
    if name.startswith(IGNORED_PREFIXES):
        return False
    if name.lower().endswith(IGNORED_SUFFIXES):
        return False
    return path.suffix.lower() in TRACKED_SUFFIXES


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
        if is_candidate(path):
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
        if not is_candidate(path) or not path.exists():
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
        f"naming={'on' if budget.enforce_texture_naming else 'off'}"
    )


def run_once(root: Path, gatekeeper: Gatekeeper, reporter: Reporter, recursive: bool) -> int:
    pattern = "**/*" if recursive else "*"
    candidates = sorted(p for p in root.glob(pattern) if p.is_file() and is_candidate(p))
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


def run_watch(root: Path, gatekeeper: Gatekeeper, reporter: Reporter, recursive: bool) -> int:
    if Observer is None:
        LOG.error("watchdog is not installed: python -m pip install watchdog")
        return 2

    work_queue: "queue.Queue[Optional[Path]]" = queue.Queue()
    worker = WatchWorker(work_queue, gatekeeper, reporter)
    worker.start()

    observer = Observer()
    observer.schedule(AssetEventHandler(work_queue), str(root), recursive=recursive)
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
    parser.add_argument("folder", nargs="?", default=".", help="folder to watch (default: current directory)")
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

    root = Path(args.folder).expanduser().resolve()
    if not root.exists():
        LOG.error("not found: %s", root)
        return 2
    # Single-file mode lets a DCC plugin gate one temporary export directly.
    watch_dir = root if root.is_dir() else root.parent
    if trimesh is None or Image is None:
        missing = [name for name, module in (("trimesh", trimesh), ("pillow", Image)) if module is None]
        LOG.error("missing dependencies: %s - python -m pip install %s", ", ".join(missing), " ".join(missing))
        return 2

    try:
        budget, rules_source, overrides = resolve_budget(watch_dir, args)
    except ConfigError as exc:
        LOG.error("invalid rules file: %s", exc)
        return 2

    LOG.info("rules   %s", rules_source if rules_source is not None else "none (defaults)")
    if overrides:
        LOG.info("cli     overriding %s", ", ".join(overrides))
    LOG.info("budget  %s", describe_budget(budget))

    gatekeeper = Gatekeeper(budget)
    reporter = Reporter(color=not args.no_color and not args.json, root=watch_dir,
                        json_mode=args.json)

    if root.is_file():
        if not is_candidate(root):
            LOG.error("not a tracked asset type: %s", root.name)
            return 2
        verdict = gatekeeper.check(root)
        reporter.report(verdict)
        return 0 if verdict.passed else 1

    if args.once:
        return run_once(watch_dir, gatekeeper, reporter, args.recursive)
    return run_watch(watch_dir, gatekeeper, reporter, args.recursive)


if __name__ == "__main__":
    sys.exit(main())
