"""AssetGatekeeper, part 1 of 7: imports, optional dependencies, shared constants and the engine/ loader.

Split out of asset_gatekeeper.py on 11 Nov 2026 with no change in behaviour;
asset_gatekeeper.py still exposes every name. See agk_gate/__init__.py.
"""

from __future__ import annotations


# Single source of truth for the version: pyproject.toml reads it from here
# (setuptools parses this literal statically), so it cannot drift.
from agk_gate import __version__  # noqa: F401 - the version lives in agk_gate/__init__.py

import fnmatch
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

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
    FileSystemEvent = object  # type: ignore[assignment,misc] - annotations only; cli imports it
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
        _engine_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

#: The folder asset_gatekeeper.py is in (the repo, the drop-in ZIP, or
#: site-packages): healer.py, engine/ and agk_vendor/ sit next to it.
GATE_HOME = Path(__file__).resolve().parent.parent

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


def engine_module(name: str):
    """engine/<name>.py from a downloaded copy, or agk_suite.engine when installed."""
    import importlib
    try:
        return importlib.import_module(f"agk_suite.engine.{name}")
    except ImportError:
        engine = GATE_HOME / "engine"
        if str(engine) not in sys.path:
            sys.path.insert(0, str(engine))
        return importlib.import_module(name)


def polypaint_module():
    """engine/polypaint.py from a downloaded copy, or agk_suite.engine when installed."""
    return engine_module("polypaint")


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


# Imported here for the later parts, which take them from this one.
_FOR_LATER_PARTS = (__version__,)
