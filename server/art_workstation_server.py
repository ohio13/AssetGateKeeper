#!/usr/bin/env python3
"""Art Workstation - a technical-art review tool built on the gate's ecosystem.

Standard library on the server (``http.server``, ``sqlite3``, ``json``), plus the
gate's own readers. The browser side uses the Three.js copy vendored in
``static/vendor`` and nothing else.

What it is for: an asset lands in the repository, and someone has to answer
"does this look right, is the LOD ladder sane, does the collision hull actually
contain the mesh, and who signed off on it?". This server stores the asset, the
LODs, the collision metadata and the review notes, and serves a viewer that puts
all four on one screen.

Solo or studio
    No ``AUTH_TOKEN``  -> a single-developer desktop tool. Everything is open on
                          loopback, which is what a solo dev wants.
    ``AUTH_TOKEN`` set -> every ``/api`` call needs ``Authorization: Bearer``
                          (or ``?token=``, for a browser tab). The pages stay open
                          so login can happen in the UI.

Routes
    GET  /                          -> /viewer
    GET  /viewer                    -> the viewer page
    GET  /static/<file>             -> css/js
    GET  /api/health                -> liveness + counts
    GET  /api/assets                -> every asset with its LOD count
    POST /api/assets?filename=...   -> upload any accepted file (raw body)
    GET  /api/assets/<id>           -> metadata + lods + collision + note count
    GET  /api/assets/<id>/file      -> the bytes
    DEL  /api/assets/<id>           -> delete the row and its files
    GET  /api/assets/<id>/lods      -> LODs (generated and uploaded, with warnings)
    POST /api/assets/<id>/lods      -> generate them ({"ratios": [...]}; ?async=1 -> a job)
    POST /api/assets/<id>/lods/<lvl>?filename=...  -> upload your own LOD file
    DEL  /api/assets/<id>/lods/<lvl>
    GET  /api/assets/<id>/lods/<lvl>/file
    GET  /api/jobs/<id>             -> a background job's state and progress
    POST /api/assets/<id>/files?filename=...       -> add a file the asset references
    GET  /api/assets/<id>/files/<path>             -> serve it
    GET  /api/assets/<id>/preview   -> converted preview (FBX .glb, texture .png)
    GET  /api/assets/<id>/heal      -> what "Fix" would change (dry run, nothing stored)
    POST /api/assets/<id>/heal      -> make the fixed copy as a new asset (original kept)
    GET  /api/assets/<id>/collision
    POST /api/assets/<id>/collision -> {"kind": "box"|"sphere"|"hull", ...}
    GET  /api/assets/<id>/notes
    POST /api/notes                 -> {"asset_id", "category", "note_text"}
    DEL  /api/notes/<id>
    GET  /api/stats                 -> repository totals
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import secrets
import json
import math
import os
import queue
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
import socketserver
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
for path in (str(HERE), str(REPO_ROOT), str(REPO_ROOT / "engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

import asset_gatekeeper as agk  # noqa: E402 - validated glTF/OBJ analysers
import fbx_preview  # noqa: E402 - FBX -> .glb for the 3D preview
import lod_manager  # noqa: E402 - the decimator
from server import netguard  # noqa: E402 - shared Host policy with the dashboard

DEFAULT_DB = "art_workstation.db"
DEFAULT_DATA = "workstation_data"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8100
STATIC_DIR = HERE / "static"
TOKEN_ENV = "AUTH_TOKEN"
RULES_ENV = "ASSET_GATEKEEPER_RULES"    # same name the health server uses
# So --port-auto never walks onto the dashboard's port (duplicated by design:
# this module must stay importable without the dashboard present).
ASSET_HEALTH_PORT = 8080

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
#: Suffixes we hand straight to trimesh. The installed build decides what it can
#: actually open (see MESH_SUFFIXES below, extended at import time).
MESH_SUFFIXES = {".obj", ".gltf", ".glb", ".ply", ".stl", ".off", ".dae", ".3mf",
                 ".xaml", ".xyz", ".pts"}
#: Textures are assets too: a reviewer needs to look at the 2048px skybox that
#: failed the budget, not be told the format is unsupported.
TEXTURE_SUFFIXES = {".png", ".tga", ".dds", ".jpg", ".jpeg", ".bmp", ".tif",
                    ".tiff", ".exr", ".hdr", ".ktx", ".ktx2", ".webp"}
#: FBX is measured by the gate's own reader and previewed through a converted
#: .glb (engine/fbx_preview.py); the viewer says plainly that it is a conversion.
FBX_SUFFIXES = {".fbx"}
#: Real game exports we cannot parse in-process. They are *stored and reviewable*
#: - geometry counts unknown, preview absent - because refusing a valid .blend is
#: the thing that makes a review tool untrustworthy.
UNVERIFIED_SUFFIXES = {".abc", ".usd", ".usda", ".usdc", ".usdz", ".blend",
                       ".max", ".ma", ".mb", ".lwo", ".lws", ".x", ".c4d", ".mdl",
                       ".psk", ".pskx", ".smd", ".vta", ".bhv", ".wrl", ".vrml"}
#: Never accept these from a browser, whatever the name looks like.
REFUSED_SUFFIXES = {".exe", ".dll", ".bat", ".cmd", ".ps1", ".sh", ".scr", ".msi",
                    ".vbs", ".js", ".jar", ".com", ".pif"}

try:                                          # ask trimesh what this build reads
    import trimesh as _trimesh_for_formats    # noqa: PLC0415
    MESH_SUFFIXES |= {f".{name.lower()}" for name in _trimesh_for_formats.available_formats()}
except Exception:                             # pragma: no cover - trimesh missing
    pass

#: Image formats a browser draws as-is; other textures get a PNG preview.
BROWSER_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
#: Per-asset folder entries the server writes itself (previews), kept apart from
#: the artist's files so a sidecar can never overwrite them.
PREVIEW_DIR = ".agk"
MAX_SIDECARS = 64


def asset_kind(path: Path) -> str:
    """mesh | texture | unverified, from the suffix alone.

    A fourth kind, "broken", comes from analysis: a measurable format whose
    bytes cannot be read. It is not the same as a .blend nobody here can open,
    and the library says which one it is.
    """
    suffix = path.suffix.lower()
    if suffix in TEXTURE_SUFFIXES:
        return "texture"
    if suffix in MESH_SUFFIXES or suffix in FBX_SUFFIXES:
        return "mesh"
    return "unverified"
CATEGORIES = ("Geometry", "Texture", "LOD", "Collision", "Shading", "Other")

#: Same reasoning as the dashboard: this page renders artist-supplied names and
#: notes, and it is a local review tool, so it must not be framed or allowed to
#: load anything from another origin. Inline script/style stay allowed because
#: the viewer is built from Python strings. data: and blob: are allowed for
#: fetches because three.js loads a glTF's embedded buffers (``data:`` URIs - every
#: .gltf LOD this server writes) and a GLB's embedded textures (``blob:`` URLs)
#: through fetch(); with ``connect-src 'self'`` alone they were refused and the
#: model failed to load or rendered untextured. Neither scheme reaches the network.
WORKSTATION_SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    # Same reasoning as the dashboard: no-referrer turns a same-origin form POST
    # into "Origin: null" in Chromium, which reads as cross-origin to the server.
    ("Referrer-Policy", "same-origin"),
    ("Content-Security-Policy",
     "default-src 'none'; img-src 'self' data: blob:; media-src 'self' blob:; "
     "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
     "connect-src 'self' data: blob:; worker-src 'self' blob:; frame-ancestors 'none'; "
     "base-uri 'none'; form-action 'self'"),
)
COLLISION_KINDS = ("box", "sphere", "hull")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def html_escape(value: object) -> str:
    """Every string in a report is artist-supplied: escape all of it."""
    return html.escape("" if value is None else str(value))


# --- #
# Database
# --- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    filename       TEXT    NOT NULL,
    file_path      TEXT    NOT NULL,
    filesize       INTEGER NOT NULL,
    triangle_count INTEGER NOT NULL,
    vertex_count   INTEGER NOT NULL,
    material_slots INTEGER NOT NULL,
    uploader       TEXT    NOT NULL DEFAULT '',
    uploaded_at    TEXT    NOT NULL,
    -- Provenance from AssetHealth's "Send to Workstation": what the gate said
    -- about this exact file, and which categories applied when it was sent.
    verdict_status   TEXT NOT NULL DEFAULT '',
    verdict_problems TEXT NOT NULL DEFAULT '',
    verdict_tags     TEXT NOT NULL DEFAULT '',
    source           TEXT NOT NULL DEFAULT '',
    kind             TEXT NOT NULL DEFAULT 'mesh',
    analyse_note     TEXT NOT NULL DEFAULT '',
    -- How far the verdict above can be trusted: "token" (sent with this
    -- workstation's token), "local" (loopback-only server) or "open" (a
    -- server reachable from the network with no token: anyone could have
    -- typed it).
    provenance_auth  TEXT NOT NULL DEFAULT '',
    -- A copy made by the healer ("Fix" in the viewer): the asset it was made
    -- from, and what was changed. The original is never modified.
    healed_from      TEXT NOT NULL DEFAULT '',
    heal_notes       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lod_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id       INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    level          TEXT    NOT NULL,          -- LOD1..LOD4
    ratio          REAL    NOT NULL,
    target_triangles INTEGER NOT NULL,
    triangle_count INTEGER NOT NULL,
    vertex_count   INTEGER NOT NULL,
    file_path      TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    UNIQUE (asset_id, level)
);

CREATE TABLE IF NOT EXISTS reviews_and_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id   INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    author     TEXT    NOT NULL DEFAULT '',
    category   TEXT    NOT NULL,
    note_text  TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS collisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id   INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,
    data_json  TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    -- One active proxy per asset: the inspector shows one overlay, so allowing
    -- three rows would mean guessing which one the artist meant.
    UNIQUE (asset_id)
);

-- Sign-off (W2): one row per decision, newest wins. "verified" is 1 when a
-- signed-in account made it, 0 when the name was only typed (no accounts).
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id   INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    status     TEXT    NOT NULL,
    note       TEXT    NOT NULL DEFAULT '',
    reviewer   TEXT    NOT NULL DEFAULT '',
    verified   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);

-- Named reviewers (W3). No row = no accounts: the server behaves as before
-- (solo on loopback, or one shared token).
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT    NOT NULL DEFAULT '',
    role          TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT    PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_asset ON reviews(asset_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_lod_asset   ON lod_versions(asset_id);
CREATE INDEX IF NOT EXISTS idx_notes_asset ON reviews_and_notes(asset_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_coll_asset  ON collisions(asset_id);
"""

_WRITE_LOCK = threading.Lock()


class Broadcaster:
    """Server-Sent Events fan-out: one bounded queue per open viewer tab."""

    def __init__(self, queue_max: int = 100) -> None:
        self._subscribers: set = set()
        self._lock = threading.Lock()
        self._queue_max = queue_max

    def subscribe(self):
        channel: "queue.Queue[dict]" = queue.Queue(maxsize=self._queue_max)
        with self._lock:
            self._subscribers.add(channel)
        return channel

    def unsubscribe(self, channel) -> None:
        with self._lock:
            self._subscribers.discard(channel)

    @property
    def listeners(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, event: dict) -> None:
        with self._lock:
            channels = list(self._subscribers)
        for channel in channels:
            try:
                channel.put_nowait(event)
            except queue.Full:
                try:                       # keep the newest, drop the stale one
                    channel.get_nowait()
                    channel.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass


class WorkstationError(Exception):
    """A request that should become a 4xx with a readable message."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


import contextlib  # noqa: E402 - kept next to its only use


@contextlib.contextmanager
def open_db(db_path: str | Path):
    """One short-lived, always-closed connection per unit of work."""
    connection = sqlite3.connect(str(db_path), timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db(db_path: str | Path) -> None:
    with open_db(db_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
        # A review library outlives the code that created it: add the bridge
        # columns in place rather than asking anyone to drop their database.
        have = {row["name"] for row in connection.execute("PRAGMA table_info(assets)")}
        for column in ("verdict_status", "verdict_problems", "verdict_tags", "source",
                       "kind", "analyse_note", "provenance_auth", "healed_from",
                       "heal_notes"):
            if column not in have:
                connection.execute(
                    f"ALTER TABLE assets ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        # Versions (W1) and where the file came from (W8). An old library gets
        # one family per asset, each at version 1.
        for column, kind in (("family_id", "INTEGER NOT NULL DEFAULT 0"),
                             ("version", "INTEGER NOT NULL DEFAULT 1"),
                             ("family_key", "TEXT NOT NULL DEFAULT ''"),
                             ("sha256", "TEXT NOT NULL DEFAULT ''"),
                             ("source_path", "TEXT NOT NULL DEFAULT ''"),
                             ("source_rev", "TEXT NOT NULL DEFAULT ''"),
                             ("changelist", "TEXT NOT NULL DEFAULT ''")):
            if column not in have:
                connection.execute(f"ALTER TABLE assets ADD COLUMN {column} {kind}")
        connection.execute("UPDATE assets SET family_id = id WHERE family_id = 0")
        connection.execute("UPDATE assets SET family_key = lower(filename) WHERE family_key = ''")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_assets_family"
                           " ON assets(family_id, version)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_assets_key ON assets(family_key)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_assets_sha ON assets(sha256)")
        lod_columns = {row["name"] for row in connection.execute("PRAGMA table_info(lod_versions)")}
        if "origin" not in lod_columns:
            # "generated" by the decimator, or "uploaded": an artist's own LOD file.
            connection.execute(
                "ALTER TABLE lod_versions ADD COLUMN origin TEXT NOT NULL DEFAULT 'generated'")


# --- #
# Mesh facts - reuses the gate's analysers so the numbers agree everywhere
# --- #

def mesh_vertices(path: Path) -> int:
    """Vertex count: POSITION accessor totals for glTF, ``v`` lines for OBJ."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".obj":
            total = 0
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("v "):
                        total += 1
            return total
        document = agk.read_gltf_document(path)
    except Exception:
        return 0

    total = 0
    accessors = document.get("accessors", [])
    for mesh in document.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            position = primitive.get("attributes", {}).get("POSITION")
            if isinstance(position, int) and position < len(accessors):
                total += int(accessors[position].get("count", 0))
    return total


def mesh_bounds(path: Path) -> tuple[list[float], list[float]] | None:
    """Axis-aligned bounds, from the file itself (not from a loaded scene)."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".obj":
            low = [float("inf")] * 3
            high = [float("-inf")] * 3
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.startswith("v "):
                        continue
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    for axis in range(3):
                        value = float(parts[axis + 1])
                        low[axis] = min(low[axis], value)
                        high[axis] = max(high[axis], value)
            if low[0] == float("inf"):
                return None
            return low, high

        document = agk.read_gltf_document(path)
        accessors = document.get("accessors", [])
        # Placed by each mesh's node, as the viewer draws it: the accessor box
        # alone put the seeded collision box somewhere else for any model with
        # a translated or scaled node.
        placements = lod_manager._mesh_placements(document)
        low = [float("inf")] * 3
        high = [float("-inf")] * 3
        for mesh_index, mesh in enumerate(document.get("meshes", [])):
            matrix = placements.get(mesh_index)
            for primitive in mesh.get("primitives", []):
                position = primitive.get("attributes", {}).get("POSITION")
                if not isinstance(position, int) or position >= len(accessors):
                    continue
                accessor = accessors[position]
                if "min" in accessor and "max" in accessor and len(accessor["min"]) >= 3:
                    box_low = [float(v) for v in accessor["min"][:3]]
                    box_high = [float(v) for v in accessor["max"][:3]]
                    corners = [(x, y, z) for x in (box_low[0], box_high[0])
                               for y in (box_low[1], box_high[1]) for z in (box_low[2], box_high[2])]
                    if matrix is not None:
                        corners = [tuple(sum(matrix[r][c] * (point + (1.0,))[c] for c in range(4))
                                         for r in range(3)) for point in corners]
                    for point in corners:
                        for axis in range(3):
                            low[axis] = min(low[axis], point[axis])
                            high[axis] = max(high[axis], point[axis])
        if low[0] == float("inf"):
            return None
        return low, high
    except Exception:
        return None


def missing_references(path: Path) -> list[str]:
    """References the file makes that are not on disk next to it, as written."""
    try:
        return [written for written, target in agk.resolve_references(path)
                if not target.is_file()]
    except Exception:
        return []


def analyse_asset(path: Path, preview_dir: Path | None = None) -> dict:
    """Everything the assets table needs, in one pass.

    Never raises for an accepted file. The kinds:

    * ``mesh``      measured (and, for FBX, previewed through a converted .glb);
    * ``texture``   sized, previewed as an image;
    * ``unverified`` a format nothing here can open (.blend, .max, .ma), or a
                    file still waiting for the sidecars it references - stored,
                    reviewable, kept out of triangle totals;
    * ``broken``    a measurable format whose bytes cannot be read: a corrupt
                    .glb is a problem to fix, not a source file to wait on.

    Only the *upload policy* refuses files, and only executables.
    """
    kind = asset_kind(path)
    suffix = path.suffix.lower()
    if preview_dir is not None:
        for stale in ("preview.glb", "preview.png"):
            (preview_dir / stale).unlink(missing_ok=True)

    if kind == "texture":
        try:                                   # Pillow is a gate dependency
            from PIL import Image
            with Image.open(path) as image:
                width, height = image.size
                if preview_dir is not None and suffix not in BROWSER_IMAGE_SUFFIXES:
                    preview_dir.mkdir(parents=True, exist_ok=True)
                    converted = image.convert("RGBA") if image.mode not in ("RGB", "RGBA", "L") else image
                    converted.save(preview_dir / "preview.png")
        except Exception as exc:
            return {"triangles": 0, "vertices": 0, "materials": 0, "bounds": None,
                    "kind": "broken", "note": f"the image cannot be read: {exc}"}
        pot = width > 0 and height > 0 and not width & (width - 1) and not height & (height - 1)
        return {"triangles": 0, "vertices": 0, "materials": 0, "bounds": None,
                "kind": "texture",
                "note": f"{width}x{height} texture" + ("" if pot else ", not a power of two")}

    if kind == "unverified":
        return {"triangles": 0, "vertices": 0, "materials": 0, "bounds": None,
                "kind": "unverified",
                "note": f"{suffix or 'no extension'} needs a DCC to "
                        "convert - stored for review without a 3D preview"}

    if suffix in FBX_SUFFIXES:
        try:
            summary = agk.read_fbx(path)
        except Exception as exc:
            return {"triangles": 0, "vertices": 0, "materials": 0, "bounds": None,
                    "kind": "broken", "note": f"the FBX cannot be read: {exc}"}
        notes = []
        if summary.collision_meshes:
            notes.append(f"{summary.collision_meshes} collision mesh(es) not counted")
        if summary.lower_lod_meshes:
            notes.append(f"{summary.lower_lod_meshes} LOD1+ mesh(es) not counted")
        vertices, bounds = 0, None
        if preview_dir is not None:
            try:
                info = fbx_preview.convert(path, preview_dir / "preview.glb")
                vertices, bounds = int(info["vertices"]), info["bounds"]
                notes.insert(0, f"measured from the FBX ({info['axes']}); "
                                "3D preview is a converted .glb")
            except Exception as exc:
                notes.insert(0, f"measured from the FBX; no 3D preview: {exc}")
        return {"triangles": int(summary.triangles), "vertices": vertices,
                "materials": int(summary.material_slots), "bounds": bounds,
                "kind": "mesh", "note": "; ".join(notes)}

    try:
        triangles = agk.count_triangles(path)
    except Exception as exc:
        missing = missing_references(path)
        if missing:
            return {"triangles": 0, "vertices": 0, "materials": 0, "bounds": None,
                    "kind": "unverified",
                    "note": "waiting for its files: " + ", ".join(missing[:5])
                            + (" ..." if len(missing) > 5 else "")
                            + " - upload them with it"}
        return {"triangles": 0, "vertices": 0, "materials": 0, "bounds": None,
                "kind": "broken", "note": f"the file cannot be read: {exc}"}
    try:
        materials = agk.count_material_slots(path)
    except Exception:
        materials = 1
    try:
        vertices = mesh_vertices(path)
        bounds = mesh_bounds(path)
    except Exception:
        vertices, bounds = 0, None
    missing = missing_references(path)
    note = ("missing: " + ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")) \
        if missing else ""
    return {
        "triangles": int(triangles),
        "vertices": vertices,
        "materials": int(materials),
        "bounds": bounds,
        "kind": "mesh",
        "note": note,
    }


# --- #
# Storage
# --- #

SAFE_NAME = re.compile(r"[^\w. -]+")          # \w is Unicode-aware: 岩, наш, café stay
SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,12}$")
MAX_NAME = 120


def safe_filename(name: str) -> str:
    """Strip directories, control characters and anything a shell would enjoy.

    The stem and the extension are cleaned separately. Cleaning the whole name
    at once turned ``岩.obj`` into ``obj`` and cut a 300-character name to 120 -
    both lost their extension, so a valid mesh was stored as "unverified".
    """
    base = Path(name.replace("\\", "/")).name.strip()
    base = "".join(ch for ch in base if ch.isprintable())
    stem, dot, suffix = base.rpartition(".")
    if not dot or not SAFE_SUFFIX.match("." + suffix):
        stem, suffix = base, ""
    stem = SAFE_NAME.sub("_", stem).strip(" ._") or "asset"
    suffix = ("." + suffix) if suffix else ""
    return stem[:MAX_NAME - len(suffix)] + suffix


def safe_relative(name: str) -> str:
    """A sidecar's path inside its asset folder, as the asset references it.

    ``textures/wood_Albedo.png`` keeps its folder so the relative reference in
    the .gltf/.mtl still resolves; anything climbing out (``..``), absolute
    paths and drive letters are reduced to the file name.
    """
    parts = [part for part in name.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts:
        return ""
    if ".." in parts or ":" in parts[0] or name.replace("\\", "/").startswith("/") or len(parts) > 5:
        parts = parts[-1:]
    cleaned = [safe_filename(part) for part in parts]
    if cleaned[0].lower() == PREVIEW_DIR:
        cleaned[0] = "_" + cleaned[0]
    return "/".join(cleaned)


def asset_folder(data_dir: Path, asset: dict) -> Path | None:
    """``assets/<id>/`` for assets stored since folders; None for the old flat layout."""
    parts = asset["file_path"].split("/")
    if len(parts) == 3 and parts[0] == "assets" and parts[1] == str(asset["id"]):
        return resolve_inside(Path(data_dir), f"assets/{asset['id']}")
    return None


def preview_of(data_dir: Path, asset: dict) -> dict:
    """What the viewer should draw: {"type": "glb"|"image"|"", "converted": bool}."""
    folder = asset_folder(Path(data_dir), asset)
    suffix = Path(asset["filename"]).suffix.lower()
    if folder is not None and (folder / PREVIEW_DIR / "preview.glb").is_file():
        return {"type": "glb", "converted": True, "warning": fbx_preview.PREVIEW_WARNING}
    if asset.get("kind") == "texture":
        if suffix in BROWSER_IMAGE_SUFFIXES:
            return {"type": "image", "converted": False, "warning": ""}
        if folder is not None and (folder / PREVIEW_DIR / "preview.png").is_file():
            return {"type": "image", "converted": True,
                    "warning": f"{suffix} converted to PNG for preview"}
    return {"type": "", "converted": False, "warning": ""}


def asset_files(data_dir: Path, asset: dict) -> list[str]:
    """The artist's files in the asset folder (the primary and its sidecars)."""
    folder = asset_folder(Path(data_dir), asset)
    if folder is None or not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder).as_posix()
        if path.is_file() and not relative.startswith(PREVIEW_DIR + "/"):
            found.append(relative)
    return found


def asset_paths(data_dir: Path) -> tuple[Path, Path]:
    assets = data_dir / "assets"
    lods = data_dir / "lods"
    assets.mkdir(parents=True, exist_ok=True)
    lods.mkdir(parents=True, exist_ok=True)
    return assets, lods


def resolve_inside(root: Path, relative: str) -> Path | None:
    """Resolve a stored relative path, refusing anything that escapes the root."""
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate


# --- #
# Queries
# --- #

LATEST_REVIEW = ("(SELECT r.status FROM reviews r WHERE r.asset_id = a.id"
                 " ORDER BY r.id DESC LIMIT 1)")


def list_assets(db_path: str | Path, all_versions: bool = False) -> list[dict]:
    """The library: the newest version of each asset (every version with
    ``all_versions``), with LOD, note and version counts and its review status."""
    where = "" if all_versions else (
        " WHERE a.version = (SELECT MAX(b.version) FROM assets b"
        " WHERE b.family_id = a.family_id)")
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT a.*,"
            "       (SELECT COUNT(*) FROM lod_versions l WHERE l.asset_id = a.id) AS lod_count,"
            "       (SELECT COUNT(*) FROM reviews_and_notes n WHERE n.asset_id = a.id) AS note_count,"
            "       (SELECT COUNT(*) FROM assets v WHERE v.family_id = a.family_id) AS version_count,"
            f"       COALESCE({LATEST_REVIEW}, '') AS review_status"
            f" FROM assets a{where} ORDER BY a.uploaded_at DESC, a.id DESC").fetchall()
    return [dict(row) for row in rows]


VERSION_FIELDS = ("id", "version", "filename", "uploaded_at", "uploader", "kind", "filesize",
                  "triangle_count", "vertex_count", "material_slots", "analyse_note",
                  "sha256", "healed_from", "heal_notes", "source_path", "source_rev",
                  "changelist")


def list_versions(db_path: str | Path, family_id: int) -> list[dict]:
    """Every version of one asset, oldest first, each with its review status."""
    columns = ", ".join(f"a.{name}" for name in VERSION_FIELDS)
    with open_db(db_path) as connection:
        rows = connection.execute(
            f"SELECT {columns}, COALESCE({LATEST_REVIEW}, '') AS review_status"
            " FROM assets a WHERE a.family_id = ? ORDER BY a.version", (family_id,)).fetchall()
    return [dict(row) for row in rows]


def get_asset(db_path: str | Path, asset_id: int) -> dict:
    with open_db(db_path) as connection:
        row = connection.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise WorkstationError(f"no asset {asset_id}", 404)
    return dict(row)


def list_lods(db_path: str | Path, asset_id: int) -> list[dict]:
    """The LOD ladder, each level with a ``warning`` when it is not a saving.

    A LOD that costs as much as (or more than) the level above it, or the
    source, is a bug in the chain - the docs promised it would be flagged and
    nothing did. Generated levels of a 12-triangle box came out as three more
    12-triangle "LODs" without a word.
    """
    with open_db(db_path) as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM lod_versions WHERE asset_id = ? ORDER BY level", (asset_id,)).fetchall()]
        source = connection.execute(
            "SELECT triangle_count FROM assets WHERE id = ?", (asset_id,)).fetchone()
    source_tris = int(source["triangle_count"]) if source is not None else 0
    previous = None
    for row in rows:
        row.setdefault("origin", "generated")
        warning = ""
        tris = int(row["triangle_count"])
        if source_tris and tris >= source_tris:
            warning = ("same triangle count as the source" if tris == source_tris
                       else f"heavier than the source ({tris:,} > {source_tris:,})")
        elif previous is not None and tris > int(previous["triangle_count"]):
            warning = f"heavier than {previous['level']} ({tris:,} > {int(previous['triangle_count']):,})"
        elif previous is not None and tris == int(previous["triangle_count"]):
            warning = f"same triangle count as {previous['level']}"
        elif row.get("origin") != "uploaded" and tris > int(row["target_triangles"]) * 1.02:
            warning = (f"over its {int(row['target_triangles']):,}-triangle target "
                       f"({tris:,}): the mesh cannot be reduced that far without collapsing")
        row["warning"] = warning
        previous = row
    return rows


def list_notes(db_path: str | Path, asset_id: int) -> list[dict]:
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM reviews_and_notes WHERE asset_id = ?"
            " ORDER BY created_at DESC, id DESC", (asset_id,)).fetchall()
    return [dict(row) for row in rows]


HULL_POINT_LIMIT = 64
#: Directions used to pick a hull's support points (a Fibonacci sphere). Engines
#: cap convex hulls far below a mesh's vertex count anyway (PhysX: 255 vertices).
HULL_DIRECTIONS = 64


def _support_points(points: list[list[float]], limit: int = HULL_POINT_LIMIT) -> list[list[float]]:
    """Reduce a point cloud to at most ``limit`` points on its convex hull.

    For each of HULL_DIRECTIONS evenly spread directions, keep the point that
    reaches furthest that way. The result is a convex proxy inside the true
    hull that touches it in every direction - what "Rebuild from mesh" means.
    Before, the button sent up to 4,000 points and every export of that hull
    was refused with 413 (export allowed 64).
    """
    unique: list[list[float]] = []
    seen = set()
    for point in points:
        key = (round(point[0], 6), round(point[1], 6), round(point[2], 6))
        if key not in seen:
            seen.add(key)
            unique.append(point)
    if len(unique) <= limit:
        return unique
    chosen: list[int] = []
    golden = math.pi * (3.0 - math.sqrt(5.0))
    for n in range(HULL_DIRECTIONS):
        y = 1.0 - 2.0 * (n + 0.5) / HULL_DIRECTIONS
        r = math.sqrt(max(0.0, 1.0 - y * y))
        direction = (math.cos(golden * n) * r, y, math.sin(golden * n) * r)
        best = max(range(len(unique)), key=lambda i: _dot(direction, unique[i]))
        if best not in chosen:
            chosen.append(best)
    return [unique[i] for i in sorted(chosen)[:limit]]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def convex_hull_triangles(points: list[list[float]]) -> list[tuple[int, int, int]]:
    """Exact convex hull by supporting-plane test (stdlib, brute force).

    Every vertex triple defines a candidate plane; it is a hull facet when no
    point sits strictly on both sides. Points sharing a facet plane (a cube's
    four corners per side) are collected and triangulated once, as a fan
    around the facet, wound outward - testing triples alone produced 24
    overlapping, inconsistently wound triangles for a cube instead of 12.
    Cubic in the point count, so it is capped: a collision proxy is a handful
    of points, not a mesh.
    """
    unique: list[tuple[float, float, float]] = []
    seen = set()
    for point in points:
        key = (round(float(point[0]), 6), round(float(point[1]), 6), round(float(point[2]), 6))
        if key not in seen:
            seen.add(key)
            unique.append(key)
    if len(unique) > HULL_POINT_LIMIT:
        raise WorkstationError(
            f"exact hull export supports {HULL_POINT_LIMIT} points, got {len(unique)}; "
            f"decimate the proxy in the DCC first", 413)

    count = len(unique)
    scale = max((abs(c) for point in unique for c in point), default=1.0) or 1.0
    eps = 1e-9 * scale
    planes: dict[tuple, tuple[tuple[float, float, float], list[int]]] = {}
    for i in range(count):
        for j in range(i + 1, count):
            for k in range(j + 1, count):
                a, b, c = unique[i], unique[j], unique[k]
                normal = _cross((b[0] - a[0], b[1] - a[1], b[2] - a[2]),
                                (c[0] - a[0], c[1] - a[1], c[2] - a[2]))
                length = math.sqrt(_dot(normal, normal))
                if length <= 1e-12 * scale * scale:
                    continue                      # collinear triple
                normal = (normal[0] / length, normal[1] / length, normal[2] / length)
                offset = _dot(normal, a)
                above = below = False
                for m in range(count):
                    side = _dot(normal, unique[m]) - offset
                    if side > eps:
                        above = True
                    elif side < -eps:
                        below = True
                    if above and below:
                        break
                if above and below:
                    continue
                if above:                          # make the normal point outward
                    normal, offset = (-normal[0], -normal[1], -normal[2]), -offset
                key = (round(normal[0], 6), round(normal[1], 6), round(normal[2], 6),
                       round(offset / scale, 6))
                if key in planes:
                    continue
                on_plane = [m for m in range(count)
                            if abs(_dot(normal, unique[m]) - offset) <= eps * 10]
                planes[key] = (normal, on_plane)

    faces: list[tuple[int, int, int]] = []
    for normal, members in planes.values():
        centre = [sum(unique[m][axis] for m in members) / len(members) for axis in range(3)]
        seed = (1.0, 0.0, 0.0) if abs(normal[0]) < 0.9 else (0.0, 1.0, 0.0)
        u = _cross(normal, seed)
        u_len = math.sqrt(_dot(u, u))
        u = (u[0] / u_len, u[1] / u_len, u[2] / u_len)
        v = _cross(normal, u)
        def angle(m: int) -> float:
            d = (unique[m][0] - centre[0], unique[m][1] - centre[1], unique[m][2] - centre[2])
            return math.atan2(_dot(d, v), _dot(d, u))
        ring = sorted(members, key=angle)        # counter-clockwise seen from outside
        for n in range(1, len(ring) - 1):
            faces.append((ring[0], ring[n], ring[n + 1]))
    return faces, unique  # type: ignore[return-value]


def collision_obj(asset: dict, collision: dict) -> str:
    """Serialise a collision proxy as a standalone Wavefront .obj."""
    kind = collision["kind"]
    data = collision["data"]
    lines = ["# AssetGatekeeper collision proxy",
             f"# asset: {asset['filename']}",
             f"# kind:  {kind}",
             f"# generated: {utc_now()}",
             f"o collision_{kind}"]

    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, ...]] = []

    if kind == "box":
        low, high = data["min"], data["max"]
        vertices = [(low[0], low[1], low[2]), (high[0], low[1], low[2]),
                    (high[0], high[1], low[2]), (low[0], high[1], low[2]),
                    (low[0], low[1], high[2]), (high[0], low[1], high[2]),
                    (high[0], high[1], high[2]), (low[0], high[1], high[2])]
        # 0-based like the hull's; the writer below adds 1 for OBJ. These were
        # written 1-based and then shifted again: index 9 with 8 vertices.
        faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
                 (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    elif kind == "sphere":
        center = data["center"]
        radius = float(data["radius"])
        segments, rings = 24, 16
        for ring in range(rings + 1):
            phi = math.pi * ring / rings
            for segment in range(segments):
                theta = 2 * math.pi * segment / segments
                vertices.append((
                    center[0] + radius * math.sin(phi) * math.cos(theta),
                    center[1] + radius * math.cos(phi),
                    center[2] + radius * math.sin(phi) * math.sin(theta)))
        def index(ring: int, segment: int) -> int:
            return ring * segments + (segment % segments)
        for ring in range(rings):
            for segment in range(segments):
                faces.append((index(ring, segment), index(ring, segment + 1),
                              index(ring + 1, segment + 1), index(ring + 1, segment)))
    else:
        faces, vertices = convex_hull_triangles(data["points"])

    for x, y, z in vertices:
        lines.append(f"v {x:.6g} {y:.6g} {z:.6g}")
    for face in faces:
        lines.append("f " + " ".join(str(i + 1) for i in face))
    return "\n".join(lines) + "\n"


def asset_references(db_path: str | Path, data_dir: Path, asset_id: int) -> dict:
    """Broken links and orphaned textures for one asset (Step 1, on demand).

    Recomputed per request rather than stored: the answer depends on what is on
    disk *now*, and a cached "references are fine" is exactly the lie this check
    exists to prevent.
    """
    asset = get_asset(db_path, asset_id)
    stored = resolve_inside(data_dir, asset["file_path"])
    if stored is None or not stored.is_file():
        raise WorkstationError("the stored file is missing", 410)

    references = []
    for written, target in agk.resolve_references(stored):
        references.append({
            "uri": written,
            "resolved": str(target),
            "exists": target.is_file(),
        })
    missing = [item["uri"] for item in references if not item["exists"]]

    # Orphans are scoped to the asset's own folder: a file uploaded with it that
    # it never references. (Store-wide, every separately uploaded texture was
    # reported as an orphan of every mesh.)
    folder = asset_folder(Path(data_dir), asset)
    used = {Path(item["resolved"]).resolve() for item in references}
    orphaned = []
    if folder is not None:
        for relative in asset_files(Path(data_dir), asset):
            candidate = (folder / relative).resolve()
            if candidate != stored.resolve() and candidate not in used:
                orphaned.append(relative)

    return {
        "asset_id": asset_id,
        "filename": asset["filename"],
        "references": references,
        "missing": missing,
        "orphaned": orphaned,
        "ok": not missing and not orphaned,
    }


def get_collision(db_path: str | Path, asset_id: int) -> dict | None:
    with open_db(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM collisions WHERE asset_id = ? ORDER BY id LIMIT 1", (asset_id,)).fetchone()
    if row is None:
        return None
    return {"kind": row["kind"], "data": json.loads(row["data_json"]),
            "created_at": row["created_at"]}


# --- #
# Mutations
# --- #

def _record_facts(db_path: str | Path, asset_id: int, file_path: str, facts: dict,
                  seed_collision: bool) -> None:
    with _WRITE_LOCK, open_db(db_path) as connection:
        connection.execute(
            "UPDATE assets SET file_path = ?, triangle_count = ?, vertex_count = ?,"
            " material_slots = ?, kind = ?, analyse_note = ? WHERE id = ?",
            (file_path, facts["triangles"], facts["vertices"],
             facts["materials"], facts["kind"], facts["note"], asset_id))
        if facts["bounds"] is not None:
            existing = connection.execute(
                "SELECT 1 FROM collisions WHERE asset_id = ?", (asset_id,)).fetchone()
            if seed_collision or existing is None:
                low, high = facts["bounds"]
                connection.execute(
                    "INSERT OR REPLACE INTO collisions (asset_id, kind, data_json, created_at)"
                    " VALUES (?, 'box', ?, ?)",
                    (asset_id, json.dumps({"min": low, "max": high}), utc_now()))


def _clean_source(source: dict | None) -> tuple[str, str, str]:
    """Depot path, revision and changelist as sent (W8), trimmed and bounded."""
    source = source or {}

    def text(key: str, limit: int) -> str:
        value = str(source.get(key, "") or "").strip()
        return "".join(ch for ch in value if ch >= " ")[:limit]

    return text("path", 512), text("rev", 32), text("changelist", 32)


def create_asset(db_path: str | Path, data_dir: Path, filename: str, payload: bytes,
                 uploader: str = "", provenance: dict | None = None,
                 version_of: int | None = None, new_asset: bool = False,
                 source: dict | None = None) -> dict:
    """Store an uploaded file in its own folder, measure it, seed its collision.

    Each asset lives in ``assets/<id>/`` with the name it was uploaded under, so
    a .gltf's .bin and textures can be added next to it (add_sidecar) and its
    relative references resolve - in the measurement and in the browser.

    Versions (W1): a file with the same depot path - or, without one, the same
    name - becomes the next version of that asset instead of a stranger in the
    library. ``version_of`` names the asset explicitly (a healed copy);
    ``new_asset`` forces a separate one. Re-sending the exact bytes of the
    newest version adds nothing: it returns that version (``unchanged``), with
    a newly sent verdict recorded on it.
    """
    name = safe_filename(filename)
    suffix = Path(name).suffix.lower()
    if suffix in REFUSED_SUFFIXES:
        raise WorkstationError(
            f"{suffix} is not an asset format - refusing to store it", 415)
    if not payload:
        raise WorkstationError("empty upload", 400)
    digest = hashlib.sha256(payload).hexdigest()
    source_path, source_rev, changelist = _clean_source(source)
    extra = provenance or {}
    verdict_fields = (str(extra.get("verdict", "")), str(extra.get("problems", "")),
                      str(extra.get("tags", "")), str(extra.get("source", "")),
                      str(extra.get("auth", "")) if (extra.get("verdict") or extra.get("source"))
                      else "")

    with _WRITE_LOCK, open_db(db_path) as connection:
        family_id, key = 0, (source_path or name).lower()
        if version_of is not None:
            parent = connection.execute("SELECT family_id, family_key FROM assets WHERE id = ?",
                                        (int(version_of),)).fetchone()
            if parent is None:
                raise WorkstationError(f"no asset {version_of}", 404)
            family_id, key = int(parent["family_id"]), parent["family_key"]
        elif not new_asset:
            latest = connection.execute(
                "SELECT id, family_id, sha256 FROM assets WHERE family_key = ?"
                " ORDER BY version DESC, id DESC LIMIT 1", (key,)).fetchone()
            if latest is not None:
                if latest["sha256"] == digest:
                    if verdict_fields[0] or verdict_fields[3]:
                        connection.execute(
                            "UPDATE assets SET verdict_status = ?, verdict_problems = ?,"
                            " verdict_tags = ?, source = ?, provenance_auth = ? WHERE id = ?",
                            (*verdict_fields, latest["id"]))
                    if source_path or changelist:
                        connection.execute(
                            "UPDATE assets SET source_path = COALESCE(NULLIF(?, ''), source_path),"
                            " source_rev = COALESCE(NULLIF(?, ''), source_rev),"
                            " changelist = COALESCE(NULLIF(?, ''), changelist) WHERE id = ?",
                            (source_path, source_rev, changelist, latest["id"]))
                    unchanged = dict(connection.execute(
                        "SELECT * FROM assets WHERE id = ?", (latest["id"],)).fetchone())
                    unchanged["unchanged"] = True
                    return unchanged
                family_id = int(latest["family_id"])
        version = 1
        if family_id:
            version = int(connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM assets WHERE family_id = ?",
                (family_id,)).fetchone()[0]) + 1
        cursor = connection.execute(
            "INSERT INTO assets (filename, file_path, filesize, triangle_count,"
            " vertex_count, material_slots, uploader, uploaded_at,"
            " verdict_status, verdict_problems, verdict_tags, source, provenance_auth,"
            " family_id, version, family_key, sha256, source_path, source_rev, changelist)"
            " VALUES (?, '', ?, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, len(payload), uploader, utc_now(), *verdict_fields,
             family_id, version, key, digest, source_path, source_rev, changelist))
        asset_id = int(cursor.lastrowid or 0)
        if not family_id:
            connection.execute("UPDATE assets SET family_id = ? WHERE id = ?",
                               (asset_id, asset_id))

    assets_dir, _ = asset_paths(data_dir)
    folder = assets_dir / str(asset_id)
    folder.mkdir(parents=True, exist_ok=True)
    stored = folder / name
    stored.write_bytes(payload)
    facts = analyse_asset(stored, folder / PREVIEW_DIR)
    _record_facts(db_path, asset_id, f"assets/{asset_id}/{name}", facts, seed_collision=True)
    return get_asset(db_path, asset_id)


def _ensure_folder(db_path: str | Path, data_dir: Path, asset: dict) -> Path:
    """Move an asset stored in the old flat layout into its own folder."""
    folder = asset_folder(data_dir, asset)
    if folder is not None:
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    assets_dir, _ = asset_paths(data_dir)
    old = resolve_inside(Path(data_dir), asset["file_path"])
    if old is None or not old.is_file():
        raise WorkstationError("the stored file is missing", 410)
    folder = assets_dir / str(asset["id"])
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / asset["filename"]
    shutil.move(str(old), str(target))
    with _WRITE_LOCK, open_db(db_path) as connection:
        connection.execute("UPDATE assets SET file_path = ? WHERE id = ?",
                           (f"assets/{asset['id']}/{asset['filename']}", asset["id"]))
    asset["file_path"] = f"assets/{asset['id']}/{asset['filename']}"
    return folder


def add_sidecar(db_path: str | Path, data_dir: Path, asset_id: int, filename: str,
                payload: bytes) -> dict:
    """Store a file an asset references (a .bin, a .mtl, a texture) next to it.

    The relative path the asset uses is kept (``textures/wood.png``), the asset
    is measured again, and its "missing" list shrinks. Until now there was no
    way to do this on any path: the browser, the bulk uploader and AssetHealth
    all sent one file, so a .gltf with a .bin arrived unreadable.
    """
    asset = get_asset(db_path, asset_id)
    relative = safe_relative(filename)
    if not relative:
        raise WorkstationError("filename is required", 400)
    if Path(relative).suffix.lower() in REFUSED_SUFFIXES:
        raise WorkstationError(
            f"{Path(relative).suffix.lower()} is not an asset format - refusing to store it", 415)
    if not payload:
        raise WorkstationError("empty upload", 400)
    folder = _ensure_folder(db_path, data_dir, asset)
    if relative == asset["filename"]:
        raise WorkstationError("that is the asset itself; upload a new asset instead", 409)
    target = resolve_inside(folder, relative)
    if target is None:
        raise WorkstationError("that path is outside the asset's folder", 400)
    if not target.exists() and len(asset_files(data_dir, asset)) > MAX_SIDECARS:
        raise WorkstationError(f"an asset keeps at most {MAX_SIDECARS} files", 413)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    stored = folder / asset["filename"]
    facts = analyse_asset(stored, folder / PREVIEW_DIR)
    _record_facts(db_path, asset_id, asset["file_path"], facts, seed_collision=False)
    return get_asset(db_path, asset_id)


# --- #
# Healing: a fixed copy, never the original
# --- #

# What the healer can change: .png names and sizes, zero-area .obj faces.
HEALABLE_SUFFIXES = (".png", ".obj")
HEAL_PREFIX = "--heal would: "


def heal_rules(rules_path: str = "", rules_root: str = "") -> tuple:
    """The budget a fix is verified against, the healer settings, and a label.

    The project's rules.toml when the workstation was started with one (the same
    file the Rules & Budgets tab shows), else the gate's default budget. Said
    out loud in every answer: a copy that passes the defaults may still fail a
    stricter project.
    """
    path = Path(rules_path) if rules_path else Path(rules_root or ".") / agk.RULES_FILENAME
    if path.is_file():
        try:
            document = agk.parse_rules(path)
            return (agk.Budget.from_mapping(document),
                    agk.HealSettings.from_mapping(document.get("healer", {})),
                    f"{path.name} ({path})")
        except (agk.ConfigError, ValueError, OSError) as exc:
            raise WorkstationError(f"cannot read {path}: {exc}", 409) from exc
    return agk.Budget(), agk.HealSettings(), "the gate's default budget (no rules.toml)"


def heal_asset(db_path: str | Path, data_dir: Path, asset_id: int, apply: bool,
               rules_path: str = "", rules_root: str = "") -> dict:
    """Preview (``apply=False``) or make a healed copy of an asset.

    The heal runs on a throwaway copy of the asset's folder, verified by the gate
    with the budget from ``heal_rules``. Applying it stores the result as a *new*
    asset linked to this one; the original - and the artist's disk - are never
    written.
    """
    data_dir = Path(data_dir)
    asset = get_asset(db_path, asset_id)
    suffix = Path(asset["filename"]).suffix.lower()
    budget, settings, label = heal_rules(rules_path, rules_root)
    answer = {"asset_id": asset_id, "healable": False, "changes": [], "problems": [],
              "budget": label, "texture_mode": settings.texture_mode, "reason": ""}
    if suffix not in HEALABLE_SUFFIXES or asset.get("kind") == "broken":
        answer["reason"] = ("the healer only fixes .png textures (name suffix, power of two) "
                            "and .obj meshes (zero-area faces)")
        if apply:
            raise WorkstationError(answer["reason"], 422)
        return answer
    folder = _ensure_folder(db_path, data_dir, asset)
    scratch = Path(tempfile.mkdtemp(prefix="agk-ws-heal-"))
    try:
        for relative in asset_files(data_dir, asset):
            target = scratch / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(folder / relative, target)
        staged = scratch / asset["filename"]
        if not staged.is_file():
            raise WorkstationError("the stored file is missing", 410)
        before = agk.Gatekeeper(budget).check(staged)
        answer["problems"] = list(before.problems)
        if before.passed:
            answer["reason"] = f"it already passes {label}"
            if apply:
                raise WorkstationError(answer["reason"], 409)
            return answer
        try:
            healer = agk.load_healer(budget, settings, root=scratch, backups=False,
                                     dry_run=not apply)
        except agk.ConfigError as exc:
            raise WorkstationError(str(exc), 503) from exc
        verdict = agk.Gatekeeper(budget, healer=healer).check(staged)
        if not apply:
            answer["changes"] = [note[len(HEAL_PREFIX):] for note in verdict.warnings
                                 if note.startswith(HEAL_PREFIX)]
            answer["healable"] = bool(answer["changes"])
            if not answer["healable"]:
                answer["reason"] = healer.last_refusal or "the healer had nothing safe to do"
            return answer
        if not verdict.healed:
            raise WorkstationError(
                f"not fixed: {healer.last_refusal or 'the healer had nothing safe to do'}", 422)
        healed = Path(verdict.path)
        notes = "; ".join(verdict.heal_notes)
        copy = create_asset(db_path, data_dir, healed.name, healed.read_bytes(),
                            uploader="workstation healer",
                            provenance={"verdict": "passed", "source": "workstation healer",
                                        "tags": asset.get("verdict_tags", ""),
                                        "problems": "", "auth": "server"},
                            version_of=asset_id,
                            source={"path": asset.get("source_path", ""),
                                    "rev": asset.get("source_rev", ""),
                                    "changelist": asset.get("changelist", "")})
        for path in sorted(scratch.rglob("*")):
            relative = path.relative_to(scratch).as_posix()
            if (path.is_file() and path != healed
                    and not relative.startswith((".agk", "agk-"))):
                add_sidecar(db_path, data_dir, copy["id"], relative, path.read_bytes())
        with _WRITE_LOCK, open_db(db_path) as connection:
            connection.execute("UPDATE assets SET healed_from = ?, heal_notes = ? WHERE id = ?",
                               (str(asset_id), notes, copy["id"]))
        answer.update(healable=True, changes=list(verdict.heal_notes), copy_id=copy["id"])
        return answer
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def asset_detail(db_path: str | Path, data_dir: Path, asset_id: int) -> dict:
    """The asset as the viewer needs it: row + LODs + collision + notes + files."""
    asset = get_asset(db_path, asset_id)
    asset["lods"] = list_lods(db_path, asset_id)
    asset["collision"] = get_collision(db_path, asset_id)
    asset["notes"] = list_notes(db_path, asset_id)
    asset["preview"] = preview_of(Path(data_dir), asset)
    asset["files"] = asset_files(Path(data_dir), asset)
    stored = resolve_inside(Path(data_dir), asset["file_path"])
    asset["missing"] = missing_references(stored) if stored is not None and stored.is_file() else []
    asset["versions"] = list_versions(db_path, int(asset["family_id"] or asset["id"]))
    asset["reviews"] = list_reviews(db_path, asset_id)
    asset["review"] = asset["reviews"][0] if asset["reviews"] else None
    return asset


# --- #
# Sign-off (W2)
# --- #

REVIEW_STATUSES = ("in_review", "changes_requested", "approved")
REVIEW_LABELS = {"in_review": "In review", "changes_requested": "Changes requested",
                 "approved": "Approved"}


def list_reviews(db_path: str | Path, asset_id: int) -> list[dict]:
    """Every sign-off decision on one version, newest first."""
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT id, asset_id, status, note, reviewer, verified, created_at FROM reviews"
            " WHERE asset_id = ? ORDER BY id DESC", (asset_id,)).fetchall()
    return [dict(row, verified=bool(row["verified"])) for row in rows]


def set_review(db_path: str | Path, asset_id: int, status: str, note: str = "",
               reviewer: str = "", verified: bool = False) -> dict:
    """Record a decision on one version. Every change is kept, newest wins."""
    get_asset(db_path, asset_id)
    if status not in REVIEW_STATUSES:
        raise WorkstationError(f"status must be one of {', '.join(REVIEW_STATUSES)}", 400)
    note = str(note or "").strip()[:2000]
    reviewer = str(reviewer or "").strip()[:80]
    if not verified and not reviewer:
        raise WorkstationError("reviewer is required when nobody is signed in", 400)
    with _WRITE_LOCK, open_db(db_path) as connection:
        cursor = connection.execute(
            "INSERT INTO reviews (asset_id, status, note, reviewer, verified, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (asset_id, status, note, reviewer, 1 if verified else 0, utc_now()))
        review_id = int(cursor.lastrowid or 0)
    return next(row for row in list_reviews(db_path, asset_id) if row["id"] == review_id)


def approval_for(db_path: str | Path, sha256: str) -> dict:
    """Is this exact file content approved? For the Perforce trigger (W2).

    Content, not name: an approved rock.fbx says nothing about the different
    rock.fbx someone is submitting now.
    """
    digest = str(sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise WorkstationError("sha256 must be 64 hex characters", 400)
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT a.id, a.filename, a.version, a.family_id FROM assets a WHERE a.sha256 = ?"
            " ORDER BY a.id DESC", (digest,)).fetchall()
    for row in rows:
        reviews = list_reviews(db_path, int(row["id"]))
        if reviews and reviews[0]["status"] == "approved":
            return {"approved": True, "asset_id": row["id"], "filename": row["filename"],
                    "version": row["version"], "reviewer": reviews[0]["reviewer"],
                    "verified": reviews[0]["verified"], "at": reviews[0]["created_at"]}
    if rows:
        reviews = list_reviews(db_path, int(rows[0]["id"]))
        return {"approved": False, "asset_id": rows[0]["id"], "filename": rows[0]["filename"],
                "version": rows[0]["version"],
                "status": reviews[0]["status"] if reviews else "",
                "reason": (f"{rows[0]['filename']} v{rows[0]['version']} is "
                           f"{REVIEW_LABELS.get(reviews[0]['status'], 'not reviewed').lower()}"
                           if reviews else
                           f"{rows[0]['filename']} v{rows[0]['version']} has not been reviewed")}
    return {"approved": False, "reason": "this file has never been sent to the Art Workstation"}


# --- #
# Named reviewers (W3): local accounts, the same PBKDF2 format AssetHealth uses
# --- #

ROLES = ("viewer", "reviewer", "lead")
PERMISSIONS = {
    # what each kind of caller may do; "service" is the shared token once
    # accounts exist (the bulk uploader, AssetHealth's bridge, the P4 trigger)
    "viewer": {"read", "note"},
    "reviewer": {"read", "note", "upload", "edit", "review"},
    "lead": {"read", "note", "upload", "edit", "review", "delete"},
    "service": {"read", "upload", "edit"},
    "open": {"read", "note", "upload", "edit", "review", "delete"},
}
PBKDF2_ROUNDS = 240_000
MIN_PASSWORD = 10
SESSION_DAYS = 7
SESSION_COOKIE = "agk_ws_session"
_DUMMY_HASH = ""


def _b64(raw: bytes) -> str:
    import base64
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    import base64
    try:
        algorithm, rounds, salt, expected = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                        base64.b64decode(salt), int(rounds))
        return hmac.compare_digest(candidate, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def account_count(db_path: str | Path) -> int:
    with open_db(db_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])


def add_account(db_path: str | Path, username: str, role: str, password: str,
                display_name: str = "") -> dict:
    username = str(username or "").strip()
    if not re.fullmatch(r"[\w.@-]{2,64}", username):
        raise WorkstationError("username: 2-64 letters, digits, . _ - or @", 400)
    if role not in ROLES:
        raise WorkstationError(f"role must be one of {', '.join(ROLES)}", 400)
    if len(password or "") < MIN_PASSWORD:
        raise WorkstationError(f"password must be at least {MIN_PASSWORD} characters", 400)
    try:
        with _WRITE_LOCK, open_db(db_path) as connection:
            connection.execute(
                "INSERT INTO accounts (username, display_name, role, password_hash, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (username, str(display_name or username).strip()[:80], role,
                 hash_password(password), utc_now()))
    except sqlite3.IntegrityError as exc:
        raise WorkstationError(f"{username} already exists", 409) from exc
    return next(row for row in list_accounts(db_path) if row["username"].lower() == username.lower())


def list_accounts(db_path: str | Path) -> list[dict]:
    with open_db(db_path) as connection:
        rows = connection.execute("SELECT id, username, display_name, role, created_at"
                                  " FROM accounts ORDER BY username").fetchall()
    return [dict(row) for row in rows]


def set_account(db_path: str | Path, username: str, role: str | None = None,
                password: str | None = None) -> dict:
    if role is not None and role not in ROLES:
        raise WorkstationError(f"role must be one of {', '.join(ROLES)}", 400)
    if password is not None and len(password) < MIN_PASSWORD:
        raise WorkstationError(f"password must be at least {MIN_PASSWORD} characters", 400)
    with _WRITE_LOCK, open_db(db_path) as connection:
        row = connection.execute("SELECT id FROM accounts WHERE username = ?",
                                 (username,)).fetchone()
        if row is None:
            raise WorkstationError(f"no account {username}", 404)
        if role is not None:
            connection.execute("UPDATE accounts SET role = ? WHERE id = ?", (role, row["id"]))
        if password is not None:
            connection.execute("UPDATE accounts SET password_hash = ? WHERE id = ?",
                               (hash_password(password), row["id"]))
            # A new password signs every browser out.
            connection.execute("DELETE FROM sessions WHERE account_id = ?", (row["id"],))
    return next(item for item in list_accounts(db_path) if item["id"] == row["id"])


def remove_account(db_path: str | Path, username: str) -> None:
    with _WRITE_LOCK, open_db(db_path) as connection:
        removed = connection.execute("DELETE FROM accounts WHERE username = ?",
                                     (username,)).rowcount
    if not removed:
        raise WorkstationError(f"no account {username}", 404)


def sign_in(db_path: str | Path, username: str, password: str) -> tuple[str, dict]:
    """Check a password; on success a new session token (stored hashed)."""
    global _DUMMY_HASH
    with open_db(db_path) as connection:
        row = connection.execute("SELECT * FROM accounts WHERE username = ?",
                                 (str(username or "").strip(),)).fetchone()
    if row is None:
        # Spend the same time as a real check, so a timing probe cannot tell
        # which usernames exist.
        _DUMMY_HASH = _DUMMY_HASH or hash_password(secrets.token_hex(8))
        verify_password(str(password or ""), _DUMMY_HASH)
        raise WorkstationError("wrong username or password", 401)
    if not verify_password(str(password or ""), row["password_hash"]):
        raise WorkstationError("wrong username or password", 401)
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now.timestamp() + SESSION_DAYS * 86400
    with _WRITE_LOCK, open_db(db_path) as connection:
        connection.execute("DELETE FROM sessions WHERE expires_at < ?", (utc_now(),))
        connection.execute(
            "INSERT INTO sessions (token_hash, account_id, created_at, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (hashlib.sha256(token.encode()).hexdigest(), row["id"], utc_now(),
             datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="seconds")))
    return token, {"username": row["username"], "display_name": row["display_name"],
                   "role": row["role"]}


def session_account(db_path: str | Path, token: str) -> dict | None:
    if not token:
        return None
    with open_db(db_path) as connection:
        row = connection.execute(
            "SELECT a.username, a.display_name, a.role, s.expires_at FROM sessions s"
            " JOIN accounts a ON a.id = s.account_id WHERE s.token_hash = ?",
            (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if row is None or row["expires_at"] < utc_now():
        return None
    return {"username": row["username"], "display_name": row["display_name"],
            "role": row["role"]}


def sign_out(db_path: str | Path, token: str) -> None:
    with _WRITE_LOCK, open_db(db_path) as connection:
        connection.execute("DELETE FROM sessions WHERE token_hash = ?",
                           (hashlib.sha256(str(token).encode()).hexdigest(),))


def delete_asset(db_path: str | Path, data_dir: Path, asset_id: int) -> None:
    asset = get_asset(db_path, asset_id)
    assets_dir, lods_dir = asset_paths(data_dir)
    with _WRITE_LOCK, open_db(db_path) as connection:
        lods = connection.execute(
            "SELECT file_path FROM lod_versions WHERE asset_id = ?", (asset_id,)).fetchall()
        connection.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    for row in lods:
        path = resolve_inside(data_dir, row["file_path"])
        if path is not None:
            path.unlink(missing_ok=True)
    folder = asset_folder(Path(data_dir), asset)
    if folder is not None:
        shutil.rmtree(folder, ignore_errors=True)      # the asset, its sidecars, previews
    else:
        stored = resolve_inside(assets_dir, asset["file_path"].replace("assets/", "", 1))
        if stored is not None:
            stored.unlink(missing_ok=True)
    shutil.rmtree(lods_dir / str(asset_id), ignore_errors=True)


def generate_lods(db_path: str | Path, data_dir: Path, asset_id: int,
                  ratios: list[float] | None = None,
                  max_triangles: list[int] | None = None, progress=None) -> list[dict]:
    """Decimate the asset into LOD files and record them.

    Levels an artist uploaded by hand are never overwritten or removed by a
    generation: asking for one of those levels is a 409, and the "stale level"
    clean-up only touches generated levels.
    """
    asset = get_asset(db_path, asset_id)
    ratios = list(ratios or lod_manager.DEFAULT_RATIOS)
    if any(later >= earlier for earlier, later in zip(ratios, ratios[1:])):
        raise WorkstationError("ratios must get smaller level by level (e.g. 0.5, 0.25, 0.125); "
                               "otherwise a LOD would cost more than the one above it", 400)
    wanted = set(lod_manager.LEVEL_NAMES[:len(ratios)])
    hand_made = {row["level"] for row in list_lods(db_path, asset_id) if row["origin"] == "uploaded"}
    clash = sorted(wanted & hand_made)
    if clash:
        raise WorkstationError(
            f"{', '.join(clash)} {'was' if len(clash) == 1 else 'were'} uploaded by hand; delete "
            f"{'it' if len(clash) == 1 else 'them'} first or generate fewer levels", 409)
    _, lods_dir = asset_paths(data_dir)
    source = resolve_inside(data_dir, asset["file_path"])
    if source is None or not source.is_file():
        raise WorkstationError("the stored file is missing", 410)
    if source.suffix.lower() in FBX_SUFFIXES:
        # Decimate the converted preview: lod_manager reads glTF, not FBX. The
        # viewer keeps the "converted from FBX" warning on these levels too.
        folder = asset_folder(Path(data_dir), asset)
        preview = folder / PREVIEW_DIR / "preview.glb" if folder is not None else None
        if preview is None or not preview.is_file():
            raise WorkstationError("this FBX has no 3D preview to build LODs from", 422)
        source = preview

    out_dir = lods_dir / str(asset_id)
    try:
        lod_name = asset["filename"]
        if source.suffix.lower() == ".glb" and Path(lod_name).suffix.lower() in FBX_SUFFIXES:
            lod_name = Path(lod_name).stem + ".glb"
        results = lod_manager.generate(source, out_dir, ratios, max_triangles or None,
                                       name=lod_name, progress=progress)
    except lod_manager.LodError as exc:
        raise WorkstationError(str(exc), 422) from exc

    produced = {result.level for result in results}
    with _WRITE_LOCK, open_db(db_path) as connection:
        # A regeneration with fewer ratios must not leave the old levels behind:
        # a stale "LOD3  4,328 tris" button would be a lie the viewer repeats.
        stale = [row["level"] for row in connection.execute(
            "SELECT level FROM lod_versions WHERE asset_id = ? AND origin = 'generated'",
            (asset_id,)).fetchall()
            if row["level"] not in produced]
        for level in stale:
            row = connection.execute(
                "SELECT file_path FROM lod_versions WHERE asset_id = ? AND level = ?",
                (asset_id, level)).fetchone()
            connection.execute(
                "DELETE FROM lod_versions WHERE asset_id = ? AND level = ?", (asset_id, level))
            if row is not None:
                path = resolve_inside(data_dir, row["file_path"])
                if path is not None:
                    path.unlink(missing_ok=True)

        for result in results:
            relative = result.path.relative_to(data_dir).as_posix()
            connection.execute(
                "INSERT INTO lod_versions (asset_id, level, ratio, target_triangles,"
                " triangle_count, vertex_count, file_path, created_at, origin)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'generated')"
                " ON CONFLICT(asset_id, level) DO UPDATE SET"
                " ratio = excluded.ratio, target_triangles = excluded.target_triangles,"
                " triangle_count = excluded.triangle_count,"
                " vertex_count = excluded.vertex_count, file_path = excluded.file_path,"
                " created_at = excluded.created_at, origin = 'generated'",
                (asset_id, result.level, result.ratio, result.target_triangles,
                 result.triangle_count, result.vertex_count, relative, utc_now()))
    return list_lods(db_path, asset_id)


LOD_UPLOAD_SUFFIXES = {".glb", ".gltf", ".obj", ".fbx"}


def upload_lod(db_path: str | Path, data_dir: Path, asset_id: int, level: str,
               filename: str, payload: bytes) -> list[dict]:
    """Store an artist's own LOD file as ``level`` of an asset.

    The docs promised this ("upload LODs ... stored as a level, not as a new
    asset"); every upload created a new asset instead. The file is measured with
    the gate's readers; an FBX is converted to a .glb for the viewer, as the
    source is. A self-contained file is required: a LOD has no folder of its own
    for a .bin to live in.
    """
    if level not in lod_manager.LEVEL_NAMES:
        raise WorkstationError(f"level must be one of {', '.join(lod_manager.LEVEL_NAMES)}", 400)
    asset = get_asset(db_path, asset_id)
    name = safe_filename(filename)
    suffix = Path(name).suffix.lower()
    if suffix not in LOD_UPLOAD_SUFFIXES:
        raise WorkstationError("a LOD must be a .glb, a self-contained .gltf, an .obj or an .fbx", 415)
    if not payload:
        raise WorkstationError("empty upload", 400)
    _, lods_dir = asset_paths(data_dir)
    out_dir = lods_dir / str(asset_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    stored = out_dir / f"{level}_uploaded_{name}"
    stored.write_bytes(payload)
    viewable = stored
    try:
        if suffix in FBX_SUFFIXES:
            triangles = int(agk.read_fbx(stored).triangles)
            viewable = out_dir / f"{level}_uploaded_{Path(name).stem}.glb"
            info = fbx_preview.convert(stored, viewable)
            vertices = int(info["vertices"])
        else:
            triangles = int(agk.count_triangles(stored))
            vertices = mesh_vertices(stored)
    except Exception as exc:
        for path in {stored, viewable}:
            path.unlink(missing_ok=True)
        raise WorkstationError(f"the LOD cannot be read: {exc}", 422) from exc
    source_tris = int(asset["triangle_count"]) or 0
    ratio = triangles / source_tris if source_tris else 0.0
    with _WRITE_LOCK, open_db(db_path) as connection:
        old = connection.execute(
            "SELECT file_path FROM lod_versions WHERE asset_id = ? AND level = ?",
            (asset_id, level)).fetchone()
        connection.execute(
            "INSERT INTO lod_versions (asset_id, level, ratio, target_triangles,"
            " triangle_count, vertex_count, file_path, created_at, origin)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'uploaded')"
            " ON CONFLICT(asset_id, level) DO UPDATE SET"
            " ratio = excluded.ratio, target_triangles = excluded.target_triangles,"
            " triangle_count = excluded.triangle_count, vertex_count = excluded.vertex_count,"
            " file_path = excluded.file_path, created_at = excluded.created_at,"
            " origin = 'uploaded'",
            (asset_id, level, ratio, triangles, triangles, vertices,
             viewable.relative_to(data_dir).as_posix(), utc_now()))
    if old is not None:
        previous = resolve_inside(Path(data_dir), old["file_path"])
        if previous is not None and previous not in (stored, viewable):
            previous.unlink(missing_ok=True)
    return list_lods(db_path, asset_id)


def delete_lod(db_path: str | Path, data_dir: Path, asset_id: int, level: str) -> list[dict]:
    get_asset(db_path, asset_id)
    with _WRITE_LOCK, open_db(db_path) as connection:
        row = connection.execute(
            "SELECT file_path FROM lod_versions WHERE asset_id = ? AND level = ?",
            (asset_id, level)).fetchone()
        if row is None:
            raise WorkstationError(f"no {level} for asset {asset_id}", 404)
        connection.execute("DELETE FROM lod_versions WHERE asset_id = ? AND level = ?",
                           (asset_id, level))
    path = resolve_inside(Path(data_dir), row["file_path"])
    if path is not None:
        for sibling in path.parent.glob(f"{level}_*"):
            sibling.unlink(missing_ok=True)
    return list_lods(db_path, asset_id)


class JobRunner:
    """LOD generation off the request thread, one job per asset at a time.

    A 1.3-million-triangle mesh took 201 s; as a request that is a browser tab
    waiting three minutes with no feedback, and a proxy timing out. Jobs run on
    one worker thread (the decimator is pure Python, so more threads would not
    be faster) and report the level they are on.
    """

    KEEP = 200

    def __init__(self, publish=None) -> None:
        self._jobs: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._queue: "queue.Queue[tuple[int, object]]" = queue.Queue()
        self._next = 1
        self._publish = publish
        self._thread = threading.Thread(target=self._work, name="lod-jobs", daemon=True)
        self._thread.start()

    def submit(self, asset_id: int, work) -> dict:
        with self._lock:
            for job in self._jobs.values():
                if job["asset_id"] == asset_id and job["state"] in ("queued", "running"):
                    raise WorkstationError(
                        f"LODs for asset {asset_id} are already being generated (job {job['id']})", 409)
            job_id = self._next
            self._next += 1
            job = {"id": job_id, "asset_id": asset_id, "kind": "lods", "state": "queued",
                   "level": "", "step": 0, "steps": 0, "error": "", "result": None,
                   "created_at": utc_now()}
            self._jobs[job_id] = job
            for old in sorted(self._jobs)[:-self.KEEP]:
                if self._jobs[old]["state"] in ("done", "failed"):
                    del self._jobs[old]
        self._queue.put((job_id, work))
        return dict(job)

    def get(self, job_id: int) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise WorkstationError(f"no job {job_id}", 404)
            return dict(job)

    def _update(self, job_id: int, **fields) -> None:
        with self._lock:
            self._jobs[job_id].update(fields)
            job = dict(self._jobs[job_id])
        if self._publish is not None:
            self._publish({"type": "job", "asset_id": job["asset_id"], "job": job})

    def _work(self) -> None:
        while True:
            job_id, work = self._queue.get()
            self._update(job_id, state="running")

            def progress(level, index, total, _job=job_id):
                self._update(_job, level=level, step=index + 1, steps=total)
            try:
                result = work(progress)
                self._update(job_id, state="done", result=result)
            except WorkstationError as exc:
                self._update(job_id, state="failed", error=str(exc))
            except Exception as exc:                 # a job must never kill the worker
                self._update(job_id, state="failed", error=f"internal error: {exc}")


def add_note(db_path: str | Path, asset_id: int, category: str, note_text: str,
             author: str = "") -> dict:
    if category not in CATEGORIES:
        raise WorkstationError(f"category must be one of {', '.join(CATEGORIES)}", 400)
    text = (note_text or "").strip()
    if not text:
        raise WorkstationError("note_text must not be empty", 400)
    if len(text) > 20_000:
        raise WorkstationError("note_text is too long (20,000 characters max)", 413)
    get_asset(db_path, asset_id)          # 404 before we insert anything
    with _WRITE_LOCK, open_db(db_path) as connection:
        cursor = connection.execute(
            "INSERT INTO reviews_and_notes (asset_id, author, category, note_text, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (asset_id, author.strip()[:80], category, text, utc_now()))
        note_id = int(cursor.lastrowid or 0)
        row = connection.execute(
            "SELECT * FROM reviews_and_notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row)


def delete_note(db_path: str | Path, note_id: int) -> None:
    with _WRITE_LOCK, open_db(db_path) as connection:
        cursor = connection.execute("DELETE FROM reviews_and_notes WHERE id = ?", (note_id,))
        if cursor.rowcount == 0:
            raise WorkstationError(f"no note {note_id}", 404)


def _number(value, what: str) -> float:
    """A finite float, or a 400. ``True`` is not a number, and neither is 1e999."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkstationError(f"{what} must be a number", 400)
    number = float(value)
    if not math.isfinite(number):
        raise WorkstationError(f"{what} must be a finite number", 400)
    return number


def _vector(value, what: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise WorkstationError(f"{what} must be [x, y, z]", 400)
    return [_number(item, what) for item in value]


def set_collision(db_path: str | Path, asset_id: int, kind: str, data: dict) -> dict:
    """Store a collision proxy: an AABB, a sphere, or an explicit hull.

    Values are checked before they are stored: ``1e999`` used to be saved as
    ``Infinity``, which is not JSON, and the asset could no longer be opened in
    the viewer; a non-number coordinate was a 500.
    """
    if kind not in COLLISION_KINDS:
        raise WorkstationError(f"kind must be one of {', '.join(COLLISION_KINDS)}", 400)
    if not isinstance(data, dict):
        raise WorkstationError("collision data must be a JSON object", 400)
    get_asset(db_path, asset_id)

    if kind == "box":
        if "min" not in data or "max" not in data:
            raise WorkstationError("box collision needs min:[x,y,z] and max:[x,y,z]", 400)
        low, high = _vector(data["min"], "box min"), _vector(data["max"], "box max")
        if any(lo > hi for lo, hi in zip(low, high)):
            raise WorkstationError("box min must not exceed box max on any axis", 400)
        payload = {"min": low, "max": high}
    elif kind == "sphere":
        if "center" not in data or "radius" not in data:
            raise WorkstationError("sphere collision needs center:[x,y,z] and radius", 400)
        center = _vector(data["center"], "sphere center")
        radius = _number(data["radius"], "sphere radius")
        if radius <= 0:
            raise WorkstationError("sphere radius must be greater than 0", 400)
        payload = {"center": center, "radius": radius}
    else:
        points = data.get("points")
        if not isinstance(points, list) or len(points) < 4 or len(points) > 5000:
            raise WorkstationError("hull collision needs 4..5000 points", 400)
        cleaned = [_vector(point, "every hull point") for point in points]
        reduced = _support_points(cleaned)
        if len(reduced) < 4:
            raise WorkstationError("hull collision needs at least 4 distinct points", 400)
        payload = {"points": reduced}

    with _WRITE_LOCK, open_db(db_path) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO collisions (asset_id, kind, data_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (asset_id, kind, json.dumps(payload, allow_nan=False), utc_now()))
    return {"kind": kind, "data": payload}


def repository_stats(db_path: str | Path) -> dict:
    """Library totals.

    Unverified assets - a .blend scene, a .max or .ma source file - are stored
    but cannot be measured without the DCC that made them. Their triangle count
    is 0 because nobody counted, not because the mesh is cheap, so they are
    excluded from the triangle total and counted separately. A total that
    quietly included them would understate the project's cost.
    """
    # The newest version of each asset is the project; older versions are history
    # and would double-count every re-export.
    latest = ("SELECT * FROM assets a WHERE a.version = (SELECT MAX(b.version)"
              " FROM assets b WHERE b.family_id = a.family_id)")
    with open_db(db_path) as connection:
        assets = connection.execute(f"SELECT COUNT(*) FROM ({latest})").fetchone()[0]
        versions = connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        unverified = connection.execute(
            f"SELECT COUNT(*) FROM ({latest}) WHERE kind = 'unverified'").fetchone()[0]
        broken = connection.execute(
            f"SELECT COUNT(*) FROM ({latest}) WHERE kind = 'broken'").fetchone()[0]
        triangles = connection.execute(
            f"SELECT COALESCE(SUM(triangle_count), 0) FROM ({latest})"
            " WHERE kind NOT IN ('unverified', 'broken')").fetchone()[0]
        reviews = connection.execute(
            f"SELECT COALESCE({LATEST_REVIEW}, '') AS status, COUNT(*) AS n"
            f" FROM ({latest}) a GROUP BY status").fetchall()
        lods = connection.execute("SELECT COUNT(*) FROM lod_versions").fetchone()[0]
        notes = connection.execute("SELECT COUNT(*) FROM reviews_and_notes").fetchone()[0]
        by_category = connection.execute(
            "SELECT category, COUNT(*) AS n FROM reviews_and_notes"
            " GROUP BY category ORDER BY n DESC").fetchall()
    return {
        "assets": int(assets),
        "versions": int(versions),
        "reviews": {row["status"] or "none": row["n"] for row in reviews},
        # measured + unverified + broken == assets; triangles covers measured only.
        "measured": int(assets) - int(unverified) - int(broken),
        "unverified": int(unverified),
        "broken": int(broken),
        "triangles": int(triangles),
        "lods": int(lods),
        "notes": int(notes),
        "notes_by_category": {row["category"]: row["n"] for row in by_category},
    }


REPORT_CSS = """
:root { --ink:#11161f; --muted:#5b667a; --line:#d9dee8; --good:#127a4d; --bad:#b3261e; }
* { box-sizing:border-box; }
body { margin:0; padding:32px 28px 64px; color:var(--ink); background:#fff;
       font:14px/1.55 ui-sans-serif,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
.wrap { max-width:900px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 2px; letter-spacing:-.01em; }
h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted);
     margin:30px 0 8px; border-bottom:1px solid var(--line); padding-bottom:6px; }
.sub { color:var(--muted); font-size:12px; }
.badge { display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px;
         font-weight:600; border:1px solid currentColor; }
.pass { color:var(--good); } .fail { color:var(--bad); }
table { width:100%; border-collapse:collapse; font-size:13px; margin-top:6px; }
th { text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.06em;
     color:var(--muted); border-bottom:1px solid var(--line); padding:6px 8px; }
td { padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top;
     font-variant-numeric:tabular-nums; }
ul { margin:6px 0 0 18px; padding:0; }
li { margin:2px 0; }
.note { border:1px solid var(--line); border-radius:8px; padding:10px 12px; margin-top:8px; }
.note .meta { color:var(--muted); font-size:11px; margin-bottom:4px; }
.mono { font-family:ui-monospace,Consolas,monospace; font-size:12px; }
footer { margin-top:36px; color:var(--muted); font-size:11px; }
@media print { body { padding:0; } h2 { break-after:avoid; } }
"""


def render_report(db_path: str | Path, data_dir: Path, asset_id: int) -> str:
    """One standalone HTML file: facts, verdict, LODs, collision, notes.

    Everything is inline - no CSS file, no script, no image - so the file can be
    attached to a ticket or printed and still look the same in five years.

    The verdict comes from running the *gate* over the stored file with the
    default budget. That is stated in the report: the workstation does not own
    budgets, and pretending otherwise would invent numbers.
    """
    asset = get_asset(db_path, asset_id)
    lods = list_lods(db_path, asset_id)
    notes = list_notes(db_path, asset_id)
    collision = get_collision(db_path, asset_id)
    references = asset_references(db_path, data_dir, asset_id)

    stored = resolve_inside(data_dir, asset["file_path"])
    # A .blend/.max/.ma is stored but nothing in this process can open it. Its
    # zero triangle count is an absence of measurement, not a measurement of
    # zero, so the report must not dress it up as a pass or as a cheap mesh.
    unmeasured = (asset.get("kind") or "") == "unverified"
    verdict_html = "<li>the stored file is missing</li>"
    status_class, status_label = "fail", "UNREADABLE"
    problems: list[str] = []
    if unmeasured:
        reason = (asset.get("analyse_note") or "").strip()
        verdict_html = (f"<li>not validated: {html_escape(reason or 'this format needs a DCC to measure')}</li>"
                        "<li>no triangle or slot budget applies until the file is exported"
                        " to a mesh format</li>")
        status_class, status_label = "sub", "UNVERIFIED"
    elif stored is not None and stored.is_file():
        try:
            verdict = agk.Gatekeeper(agk.Budget()).check(stored)
            problems = list(verdict.problems)
            status_label = verdict.status
            status_class = "pass" if verdict.passed else "fail"
            verdict_html = "".join(f"<li>{html_escape(problem)}</li>" for problem in problems)
            if not problems:
                verdict_html = "<li>no violations under the default budget</li>"
        except Exception as exc:                  # a report must always render
            verdict_html = f"<li>the gate could not read this file: {html_escape(str(exc))}</li>"
            status_class, status_label = "fail", "UNREADABLE"

    def rows(items: list[tuple[str, object]]) -> str:
        return "".join(f"<tr><th>{html_escape(k)}</th><td>{html_escape(v)}</td></tr>"
                       for k, v in items)

    lod_rows = "".join(
        f"<tr><td>{html_escape(lod['level'])}</td><td>{lod['triangle_count']:,}</td>"
        f"<td>{lod['vertex_count']:,}</td><td>{lod['ratio']:.2f}</td>"
        f"<td>{lod['triangle_count'] / asset['triangle_count'] * 100:.1f}%</td></tr>"
        if asset["triangle_count"] else
        f"<tr><td>{html_escape(lod['level'])}</td><td>{lod['triangle_count']:,}</td>"
        f"<td>{lod['vertex_count']:,}</td><td>{lod['ratio']:.2f}</td><td>-</td></tr>"
        for lod in lods)

    reference_rows = "".join(
        f"<tr><td class='mono'>{html_escape(item['uri'])}</td>"
        f"<td class='{'pass' if item['exists'] else 'fail'}'>"
        f"{'present' if item['exists'] else 'MISSING'}</td></tr>"
        for item in references["references"])

    collision_html = "<p class='sub'>No collision proxy stored.</p>"
    if collision:
        data = collision["data"]
        if collision["kind"] == "box":
            size = [round(data["max"][i] - data["min"][i], 3) for i in range(3)]
            detail = f"min {data['min']}  max {data['max']}  size {size}"
        elif collision["kind"] == "sphere":
            detail = f"center {data['center']}  radius {data['radius']}"
        else:
            detail = f"{len(data['points'])} hull points"
        collision_html = (f"<p><strong>{html_escape(collision['kind'])}</strong> "
                          f"<span class='sub mono'>{html_escape(detail)}</span></p>")

    notes_html = "".join(
        f"<div class='note'><div class='meta'><strong>{html_escape(note['category'])}</strong>"
        f"  {html_escape(note['author'] or 'anonymous')}"
        f"  {html_escape(note['created_at'].replace('T', ' '))}</div>"
        f"{html_escape(note['note_text'])}</div>"
        for note in notes) or "<p class='sub'>No review notes.</p>"

    geometry_rows = [
        ("triangles", "not measured" if unmeasured else f"{asset['triangle_count']:,}"),
        ("vertices", "not measured" if unmeasured else f"{asset['vertex_count']:,}"),
        ("material slots", "not measured" if unmeasured else asset["material_slots"]),
        ("file size", f"{asset['filesize']:,} bytes"),
        ("stored path", asset["file_path"]),
    ]
    geometry_note = ("<p class='sub'>Unmeasured assets are left out of the library triangle"
                     " total, so a scene full of .blend files cannot look cheap.</p>"
                     if unmeasured else "")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{html_escape(asset['filename'])} - asset report</title>
<style>{REPORT_CSS}</style></head>
<body><div class="wrap">
  <h1>{html_escape(asset['filename'])}
    <span class="badge {status_class}">{html_escape(status_label)}</span></h1>
  <div class="sub">Asset Health report  asset #{asset['id']} 
    uploaded {html_escape(asset['uploaded_at'].replace('T', ' '))}
    by {html_escape(asset['uploader'] or 'unknown')}</div>

  <h2>Geometry</h2>
  <table><tbody>{rows(geometry_rows)}</tbody></table>
  {geometry_note}

  <h2>Validation (default budget: 50,000 tris  3 slots  2048px)</h2>
  <ul>{verdict_html}</ul>

  <h2>Reference integrity</h2>
  <table><thead><tr><th>reference</th><th>state</th></tr></thead>
  <tbody>{reference_rows or "<tr><td class='sub'>no external references</td><td></td></tr>"}
  </tbody></table>
  <p class="sub">{len(references['missing'])} missing 
     {len(references['orphaned'])} orphaned texture(s) in the asset store</p>

  <h2>LOD breakdown</h2>
  <table><thead><tr><th>level</th><th>triangles</th><th>vertices</th>
    <th>ratio</th><th>of source</th></tr></thead>
  <tbody>{lod_rows or "<tr><td class='sub' colspan='5'>no LODs generated</td></tr>"}</tbody></table>

  <h2>Collision</h2>
  {collision_html}

  <h2>Review notes</h2>
  {notes_html}

  <footer>Generated by Art Workstation 
    {html_escape(datetime.now(timezone.utc).isoformat(timespec="seconds"))} 
    this file is self-contained (no external assets).</footer>
</div></body></html>
"""


# --- #
# HTTP
# --- #

def content_disposition(kind: str, name: str) -> str:
    """Header-safe: an ASCII fallback plus the UTF-8 name (RFC 6266 / 5987).

    Headers are Latin-1 on the wire; a raw "岩.obj" raised UnicodeEncodeError
    inside send_header and the download became a 500.
    """
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name)
    quoted = urllib.parse.quote(name, safe="")
    return f'{kind}; filename="{fallback}"; filename*=UTF-8\'\'{quoted}'


MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gltf": "model/gltf+json",
    ".glb": "model/gltf-binary",
    ".obj": "text/plain; charset=utf-8",
    ".bin": "application/octet-stream",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mtl": "text/plain; charset=utf-8",
}

ASSET_RE = re.compile(r"^/api/assets/(\d+)$")
ASSET_FILE_RE = re.compile(r"^/api/assets/(\d+)/file$")
ASSET_PREVIEW_RE = re.compile(r"^/api/assets/(\d+)/preview$")
ASSET_SIDECAR_RE = re.compile(r"^/api/assets/(\d+)/files/(.+)$")
ASSET_SIDECARS_RE = re.compile(r"^/api/assets/(\d+)/files$")
ASSET_LODS_RE = re.compile(r"^/api/assets/(\d+)/lods$")
ASSET_LOD_FILE_RE = re.compile(r"^/api/assets/(\d+)/lods/(LOD[1-4])/file$")
ASSET_LOD_RE = re.compile(r"^/api/assets/(\d+)/lods/(LOD[1-4])$")
JOB_RE = re.compile(r"^/api/jobs/(\d+)$")
ASSET_NOTES_RE = re.compile(r"^/api/assets/(\d+)/notes$")
ASSET_COLLISION_RE = re.compile(r"^/api/assets/(\d+)/collision$")
ASSET_REFERENCES_RE = re.compile(r"^/api/assets/(\d+)/references$")
ASSET_REPORT_RE = re.compile(r"^/api/assets/(\d+)/report$")
ASSET_HEAL_RE = re.compile(r"^/api/assets/(\d+)/heal$")
COLLISION_OBJ_RE = re.compile(r"^/api/assets/(\d+)/collision\.obj$")
ASSET_REVIEW_RE = re.compile(r"^/api/assets/(\d+)/reviews?$")
APPROVAL_RE = re.compile(r"^/api/approvals/([0-9a-fA-F]{64})$")
LOGIN_WINDOW = 300          # seconds
LOGIN_FAILURES = 10         # per address per window, then 429
NOTE_RE = re.compile(r"^/api/notes/(\d+)$")


class Handler(BaseHTTPRequestHandler):
    server_version = "ArtWorkstation/1.0"
    protocol_version = "HTTP/1.1"
    # Seconds a socket read or write may stall. Without it a client that
    # announced a body and never sent it held a server thread forever. The live
    # event stream writes a ping every 15 s, well inside this.
    timeout = 60

    # - plumbing - #

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib hook
        if getattr(self.server, "quiet", False):
            return
        sys.stderr.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def _drain_request_body(self) -> None:
        """Read and discard a body we are about to refuse (see the dashboard).

        An unread body is parsed as the next request on a keep-alive connection:
        that is request smuggling, and it applies to this server too.
        """
        if getattr(self, "_body_read", False) or self.command not in ("POST", "PUT"):
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return
        if length > MAX_UPLOAD_BYTES:
            self.close_connection = True
            return
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                self.close_connection = True
                return
            remaining -= len(chunk)

    def _send(self, status: int, body: bytes, content_type: str,
              headers: dict[str, str] | None = None) -> None:
        if status >= 400:
            self._drain_request_body()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in WORKSTATION_SECURITY_HEADERS:
            self.send_header(key, value)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _query(self) -> dict:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def _token(self) -> str:
        header = self.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() == "bearer" and presented:
            return presented.strip()
        return (self._query().get("token", [""])[0] or "").strip()

    def _authorised(self) -> bool:
        expected = self.server.token  # type: ignore[attr-defined]
        if not expected:
            return True              # solo mode: loopback, no ceremony
        presented = self._token()
        return bool(presented) and hmac.compare_digest(presented, expected)

    def _cookie(self, name: str) -> str:
        from http.cookies import CookieError, SimpleCookie
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        return jar[name].value if name in jar else ""

    def _principal(self) -> dict | None:
        """Who is calling, and with which role - or None (401).

        No accounts: as before - everyone on a solo server, or whoever holds
        the token - with every permission. Accounts: a signed-in session
        carries its account's role; the shared token still works for scripts
        (the bulk uploader, AssetHealth's bridge, the Perforce trigger) as the
        "service" role, which can upload but never sign off or delete.
        """
        server = self.server  # type: ignore[attr-defined]
        if account_count(server.db_path):
            account = session_account(server.db_path, self._cookie(SESSION_COOKIE))
            if account is not None:
                return {"kind": "account", "name": account["username"],
                        "display": account["display_name"], "role": account["role"]}
            if server.token and self._authorised() and self._token():
                return {"kind": "service", "name": "token", "display": "", "role": "service"}
            return None
        if not self._authorised():
            return None
        return {"kind": "token" if server.token else "open", "name": "", "display": "",
                "role": "open"}

    def _forbidden(self, who: dict, permission: str) -> bool:
        if permission in PERMISSIONS.get(who["role"], set()):
            return False
        verbs = {"upload": "upload", "edit": "change LODs, collision or fixes",
                 "review": "sign off", "delete": "delete", "note": "write notes",
                 "read": "read"}
        self._json(403, {"error": f"your role ({who['role']}) cannot "
                                  f"{verbs.get(permission, permission)}",
                         "role": who["role"]})
        return True

    @staticmethod
    def _permission_for(method: str, path: str) -> str:
        if method == "DELETE":
            return "delete" if ASSET_RE.match(path) else "edit"
        if path in ("/api/assets",) or ASSET_SIDECARS_RE.match(path):
            return "upload"
        if path == "/api/notes":
            return "note"
        if ASSET_REVIEW_RE.match(path):
            return "review"
        return "edit"

    def _login(self) -> None:
        server = self.server  # type: ignore[attr-defined]
        address = self.client_address[0] if self.client_address else ""
        now = time.monotonic()
        failures = server.login_failures.setdefault(address, [])
        failures[:] = [moment for moment in failures if now - moment < LOGIN_WINDOW]
        if len(failures) >= LOGIN_FAILURES:
            self._drain_request_body()
            self._json(429, {"error": "too many failed sign-ins; wait a few minutes"})
            return
        payload = self._json_body()
        if payload is None:
            return
        try:
            token, account = sign_in(server.db_path, str(payload.get("username", "")),
                                     str(payload.get("password", "")))
        except WorkstationError as exc:
            failures.append(now)
            self._json(exc.status, {"error": str(exc)})
            return
        cookie = (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
                  f"Max-Age={SESSION_DAYS * 86400}")
        self._send(200, json.dumps({"user": account}).encode("utf-8"),
                   "application/json; charset=utf-8", {"Set-Cookie": cookie})

    def _me(self) -> None:
        server = self.server  # type: ignore[attr-defined]
        who = self._principal()
        accounts = bool(account_count(server.db_path))
        self._json(200, {
            "accounts": accounts,
            "signed_in": bool(who and who["kind"] == "account"),
            "user": ({"username": who["name"], "display_name": who["display"],
                      "role": who["role"]} if who and who["kind"] == "account" else None),
            "role": who["role"] if who else None,
            "can": sorted(PERMISSIONS.get(who["role"], set())) if who else [],
        })

    def _body(self, limit: int) -> bytes | None:
        header = self.headers.get("Content-Length")
        if header is None:
            self._json(411, {"error": "Content-Length required"})
            return None
        try:
            length = int(header)
        except ValueError:
            self._json(400, {"error": "invalid Content-Length"})
            return None
        if length < 0 or length > limit:
            self._json(413, {"error": f"body must be at most {limit} bytes"})
            return None
        raw = self.rfile.read(length)
        self._body_read = True
        return raw

    def _json_body(self) -> dict | None:
        raw = self._body(MAX_JSON_BYTES)
        if raw is None:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._json(400, {"error": "body is not valid JSON"})
            return None
        if not isinstance(payload, dict):
            self._json(400, {"error": "body must be a JSON object"})
            return None
        return payload

    # - GET - #

    def handle_error(self, request, client_address) -> None:
        """Same quiet handling as the dashboard (see its comment)."""
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

    def _host_allowed(self) -> bool:
        """Same Host policy as the dashboard (server/netguard.py).

        This server was left open to DNS rebinding: any Host was answered, which
        is exactly what the release notes said did not happen.
        """
        return netguard.host_allowed(self.headers.get("Host", ""),
                                     self.server.server_address[0],
                                     getattr(self.server, "allow_hosts", ()))

    def _cross_site(self) -> bool:
        """True when a browser sent this write from another site.

        In solo mode there is no token, so without this any page open in the
        reviewer's browser could post here as a "simple" request (text/plain):
        it added notes, and planted an asset marked "passed, source ci".
        Browsers say where a request came from (Origin, or Sec-Fetch-Site);
        non-browser clients - the bulk uploader, AssetHealth's bridge, curl -
        send neither and are unaffected. Behind a proxy that rewrites Host, add
        the public name with --allow-host.
        """
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            if origin == "null":
                return True
            try:
                parts = urllib.parse.urlsplit(origin)
            except ValueError:
                return True
            if parts.netloc.lower() == self.headers.get("Host", "").strip().lower():
                return False
            allowed = {name.lower() for name in getattr(self.server, "allow_hosts", ())}
            return (parts.hostname or "").lower() not in allowed
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        return fetch_site not in ("", "same-origin", "none")

    def _reject_cross_site(self) -> None:
        self._json(403, {"error": "cross-site request refused",
                         "detail": "this request came from another website; the Art "
                                   "Workstation only accepts changes from its own pages"})

    def _reject_host(self) -> None:
        self._json(403, {"error": "unexpected Host header",
                         "detail": netguard.refusal_detail(self.headers.get("Host", "")),
                         "host": self.headers.get("Host", "")})

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_allowed():
            self._reject_host()
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        try:
            if path in ("/", "", "/viewer"):
                if path != "/viewer":
                    self._send(302, b"", "text/plain; charset=utf-8",
                               {"Location": "/viewer"})
                    return
                self._static("viewer.html")
                return
            if path.startswith("/static/"):
                self._static(path[len("/static/"):])
                return
            if path == "/api/me":
                self._me()
                return
            who = self._principal()
            if who is None:
                self._json(401, {"error": "unauthorised",
                                 "detail": "sign in" if account_count(self.server.db_path)
                                 else "Bearer token required"})
                return
            self._who = who
            self._api_get(path)
        except WorkstationError as exc:
            self._json(exc.status, {"error": str(exc)})
        except BrokenPipeError:
            pass
        except Exception as exc:                    # never kill the handler thread
            self._json(500, {"error": "internal error", "detail": str(exc)})

    def _api_get(self, path: str) -> None:
        server = self.server  # type: ignore[attr-defined]
        if path == "/api/health":
            self._json(200, {"ok": True, "auth": bool(server.token),
                             "accounts": bool(account_count(server.db_path)),
                             "exposed": bool(getattr(server, "exposed", False)),
                             "stats": repository_stats(server.db_path)})
            return
        if path == "/api/stats":
            self._json(200, repository_stats(server.db_path))
            return
        if path == "/api/rules":
            self._json(200, describe_rules(server))
            return
        if path == "/api/assets":
            every = (self._query().get("all_versions", ["0"])[0] or "") in ("1", "true", "yes")
            self._json(200, {"assets": list_assets(server.db_path, all_versions=every)})
            return

        match = ASSET_RE.match(path)
        if match:
            self._json(200, asset_detail(server.db_path, Path(server.data_dir),
                                         int(match.group(1))))
            return

        match = ASSET_PREVIEW_RE.match(path)
        if match:
            self._serve_preview(int(match.group(1)))
            return

        match = ASSET_SIDECAR_RE.match(path)
        if match:
            self._serve_sidecar(int(match.group(1)), urllib.parse.unquote(match.group(2)))
            return

        match = ASSET_FILE_RE.match(path)
        if match:
            self._serve_asset_file(int(match.group(1)))
            return

        match = ASSET_LODS_RE.match(path)
        if match:
            get_asset(server.db_path, int(match.group(1)))      # 404, not an empty list
            self._json(200, {"lods": list_lods(server.db_path, int(match.group(1)))})
            return

        match = ASSET_LOD_FILE_RE.match(path)
        if match:
            self._serve_lod_file(int(match.group(1)), match.group(2))
            return

        match = JOB_RE.match(path)
        if match:
            self._json(200, server.jobs.get(int(match.group(1))))
            return

        match = ASSET_NOTES_RE.match(path)
        if match:
            get_asset(server.db_path, int(match.group(1)))      # 404, not an empty list
            self._json(200, {"notes": list_notes(server.db_path, int(match.group(1)))})
            return

        if path == "/api/events":
            self._stream()
            return

        match = COLLISION_OBJ_RE.match(path)
        if match:
            asset_id = int(match.group(1))
            server = self.server  # type: ignore[attr-defined]
            asset = get_asset(server.db_path, asset_id)
            collision = get_collision(server.db_path, asset_id)
            if collision is None:
                raise WorkstationError("no collision proxy for this asset", 404)
            body = collision_obj(asset, collision).encode("utf-8")
            stem = safe_filename(asset["filename"]).rsplit(".", 1)[0]
            self._send(200, body, "text/plain; charset=utf-8",
                       {"Content-Disposition":
                        content_disposition("attachment", f"{stem}_collision.obj")})
            return

        match = ASSET_REPORT_RE.match(path)
        if match:
            self._report(int(match.group(1)))
            return

        match = ASSET_REFERENCES_RE.match(path)
        if match:
            self._json(200, asset_references(server.db_path, Path(server.data_dir),
                                             int(match.group(1))))
            return

        match = ASSET_REVIEW_RE.match(path)
        if match:
            self._json(200, {"reviews": list_reviews(server.db_path, int(match.group(1)))})
            return

        match = APPROVAL_RE.match(path)
        if match:
            self._json(200, approval_for(server.db_path, match.group(1)))
            return

        match = ASSET_HEAL_RE.match(path)
        if match:
            self._json(200, heal_asset(server.db_path, Path(server.data_dir),
                                       int(match.group(1)), apply=False,
                                       rules_path=server.rules_path,
                                       rules_root=server.rules_root))
            return

        match = ASSET_COLLISION_RE.match(path)
        if match:
            collision = get_collision(server.db_path, int(match.group(1)))
            if collision is None:
                raise WorkstationError("no collision proxy for this asset", 404)
            self._json(200, collision)
            return

        self._json(404, {"error": "not found", "path": path})

    def _stream(self) -> None:
        """SSE for the viewer: new assets, new notes, new collisions."""
        server = self.server  # type: ignore[attr-defined]
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        channel = server.broadcaster.subscribe()
        try:
            self.wfile.write(b"retry: 3000\n\n: connected\n\n")
            self.wfile.flush()
            while not getattr(server, "stopping", False):
                try:
                    event = channel.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"event: {event.get('type', 'message')}\n"
                                 f"data: {json.dumps(event, default=str)}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass
        finally:
            server.broadcaster.unsubscribe(channel)

    def _report(self, asset_id: int) -> None:
        server = self.server  # type: ignore[attr-defined]
        body = render_report(server.db_path, Path(server.data_dir), asset_id).encode("utf-8")
        headers = {"Content-Disposition": 'attachment; filename="report.html"'} \
            if self._query().get("download", ["0"])[0] in ("1", "true", "yes") else {}
        self._send(200, body, "text/html; charset=utf-8", headers)

    def _serve_asset_file(self, asset_id: int) -> None:
        server = self.server  # type: ignore[attr-defined]
        asset = get_asset(server.db_path, asset_id)
        path = resolve_inside(Path(server.data_dir), asset["file_path"])
        if path is None or not path.is_file():
            raise WorkstationError("the stored file is missing", 410)
        self._send_file(path, download_name=asset["filename"])

    def _serve_preview(self, asset_id: int) -> None:
        server = self.server  # type: ignore[attr-defined]
        asset = get_asset(server.db_path, asset_id)
        preview = preview_of(Path(server.data_dir), asset)
        folder = asset_folder(Path(server.data_dir), asset)
        if preview["type"] == "glb" and folder is not None:
            self._send_file(folder / PREVIEW_DIR / "preview.glb")
            return
        if preview["type"] == "image":
            if preview["converted"] and folder is not None:
                self._send_file(folder / PREVIEW_DIR / "preview.png")
            else:
                self._serve_asset_file(asset_id)
            return
        raise WorkstationError("this asset has no preview", 404)

    def _serve_sidecar(self, asset_id: int, relative: str) -> None:
        """A file inside the asset's folder: how a .gltf finds its .bin."""
        server = self.server  # type: ignore[attr-defined]
        asset = get_asset(server.db_path, asset_id)
        folder = asset_folder(Path(server.data_dir), asset)
        if folder is None or relative.split("/")[0] == PREVIEW_DIR:
            raise WorkstationError("no such file", 404)
        target = resolve_inside(folder, relative)
        if target is None or not target.is_file():
            raise WorkstationError(f"{relative} was not uploaded with this asset", 404)
        self._send_file(target)

    def _serve_lod_file(self, asset_id: int, level: str) -> None:
        server = self.server  # type: ignore[attr-defined]
        with open_db(server.db_path) as connection:
            row = connection.execute(
                "SELECT file_path FROM lod_versions WHERE asset_id = ? AND level = ?",
                (asset_id, level)).fetchone()
        if row is None:
            raise WorkstationError(f"no {level} for asset {asset_id}", 404)
        path = resolve_inside(Path(server.data_dir), row["file_path"])
        if path is None or not path.is_file():
            raise WorkstationError("the LOD file is missing", 410)
        self._send_file(path, download_name=path.name)

    def _send_file(self, path: Path, download_name: str = "") -> None:
        body = path.read_bytes()
        headers = {}
        if download_name:
            headers["Content-Disposition"] = content_disposition("inline", download_name)
        self._send(200, body, MIME.get(path.suffix.lower(), "application/octet-stream"),
                   headers)

    def _static(self, relative: str) -> None:
        """Serve the viewer's own files, refusing anything outside static/."""
        candidate = resolve_inside(STATIC_DIR, relative)
        if candidate is None or not candidate.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send(200, candidate.read_bytes(),
                   MIME.get(candidate.suffix.lower(), "application/octet-stream"))

    def _publish(self, event_type: str, asset_id: int | None = None) -> None:
        broadcaster = getattr(self.server, "broadcaster", None)  # type: ignore[attr-defined]
        if broadcaster is not None:
            broadcaster.publish({"type": event_type, "asset_id": asset_id})

    # - POST - #

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_allowed():
            self._reject_host()
            return
        if self._cross_site():
            self._reject_cross_site()
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/session":
                self._login()
                return
            who = self._principal()
            if who is None:
                self._drain_request_body()
                self._json(401, {"error": "unauthorised",
                                 "detail": "sign in" if account_count(self.server.db_path)
                                 else "Bearer token required"})
                return
            if self._forbidden(who, self._permission_for("POST", path)):
                self._drain_request_body()
                return
            server = self.server  # type: ignore[attr-defined]

            match = ASSET_REVIEW_RE.match(path)
            if match:
                payload = self._json_body()
                if payload is None:
                    return
                signed = who["kind"] == "account"
                review = set_review(server.db_path, int(match.group(1)),
                                    str(payload.get("status", "")), str(payload.get("note", "")),
                                    reviewer=(who["display"] or who["name"]) if signed
                                    else str(payload.get("reviewer", "")),
                                    verified=signed)
                self._publish("review", int(match.group(1)))
                self._json(201, review)
                return

            if path == "/api/assets":
                raw = self._body(MAX_UPLOAD_BYTES)
                if raw is None:
                    return
                filename = (self._query().get("filename", [""])[0]
                            or self.headers.get("X-Filename", ""))
                if not filename:
                    raise WorkstationError("filename is required (query or X-Filename)", 400)
                # Provenance arrives from AssetHealth's bridge: what the gate said
                # about this file and which categories applied when it was sent.
                query = self._query()
                provenance = {
                    "verdict": (query.get("verdict", [""])[0] or "").strip(),
                    "problems": (query.get("problems", [""])[0] or "").strip(),
                    "tags": (query.get("tags", [""])[0] or "").strip(),
                    "source": (query.get("source", [""])[0] or "").strip(),
                    # Anyone who can reach an open server can claim "source=ci";
                    # record how much this claim is worth.
                    "auth": ("token" if server.token
                             else "open" if getattr(server, "exposed", False) else "local"),
                }
                version_of = (query.get("version_of", [""])[0] or "").strip()
                if version_of and not version_of.isdigit():
                    raise WorkstationError("version_of must be an asset id", 400)
                uploader = query.get("uploader", [""])[0]
                if who["kind"] == "account":
                    uploader = who["display"] or who["name"]
                asset = create_asset(
                    server.db_path, Path(server.data_dir), filename, raw, uploader=uploader,
                    provenance=provenance,
                    version_of=int(version_of) if version_of else None,
                    new_asset=(query.get("new_asset", [""])[0] or "") in ("1", "true", "yes"),
                    source={"path": query.get("source_path", [""])[0],
                            "rev": query.get("source_rev", [""])[0],
                            "changelist": query.get("changelist", [""])[0]})
                self._publish("asset", asset["id"])
                # The detail view, so a client sees "missing" and can send the
                # sidecars the file references straight after it.
                detail = asset_detail(server.db_path, Path(server.data_dir), asset["id"])
                detail["unchanged"] = bool(asset.get("unchanged"))
                self._json(200 if detail["unchanged"] else 201, detail)
                return

            match = ASSET_HEAL_RE.match(path)
            if match:
                result = heal_asset(server.db_path, Path(server.data_dir), int(match.group(1)),
                                    apply=True, rules_path=server.rules_path,
                                    rules_root=server.rules_root)
                self._publish("asset", result["copy_id"])
                result["asset"] = asset_detail(server.db_path, Path(server.data_dir),
                                               result["copy_id"])
                self._json(201, result)
                return

            match = ASSET_SIDECARS_RE.match(path)
            if match:
                raw = self._body(MAX_UPLOAD_BYTES)
                if raw is None:
                    return
                filename = (self._query().get("filename", [""])[0]
                            or self.headers.get("X-Filename", ""))
                if not filename:
                    raise WorkstationError("filename is required (query or X-Filename)", 400)
                asset = add_sidecar(server.db_path, Path(server.data_dir), int(match.group(1)),
                                    filename, raw)
                self._publish("asset", asset["id"])
                self._json(201, asset_detail(server.db_path, Path(server.data_dir), asset["id"]))
                return

            if path == "/api/notes":
                payload = self._json_body()
                if payload is None:
                    return
                asset_id = payload.get("asset_id")
                if not isinstance(asset_id, int):
                    raise WorkstationError("asset_id must be an integer", 400)
                note = add_note(server.db_path, asset_id, str(payload.get("category", "")),
                                str(payload.get("note_text", "")),
                                # signed in: the account, never a typed name
                                (who["display"] or who["name"]) if who["kind"] == "account"
                                else str(payload.get("author", "")))
                self._publish("notes", asset_id)
                self._json(201, note)
                return

            match = ASSET_LODS_RE.match(path)
            if match:
                payload = self._json_body()
                if payload is None:
                    # _json_body already answered (400/411/413). Carrying on with
                    # "or {}" generated default LODs and wrote a second response
                    # on the same keep-alive connection.
                    return
                unknown = sorted(set(payload) - {"ratios", "max_tris"})
                if unknown:
                    raise WorkstationError(
                        f"unknown field(s) {', '.join(unknown)}; expected ratios and/or max_tris", 400)
                ratios = payload.get("ratios") or list(lod_manager.DEFAULT_RATIOS)
                if not isinstance(ratios, list) or not all(
                        isinstance(r, (int, float)) and not isinstance(r, bool)
                        and 0 < r < 1 for r in ratios):
                    raise WorkstationError("ratios must be a list of numbers between 0 and 1", 400)
                if len(ratios) > len(lod_manager.LEVEL_NAMES):
                    raise WorkstationError(
                        f"at most {len(lod_manager.LEVEL_NAMES)} LOD levels, got {len(ratios)}", 400)
                caps = payload.get("max_tris") or None
                if caps is not None and (not isinstance(caps, list) or not all(
                        isinstance(c, int) and not isinstance(c, bool) and c > 0 for c in caps)):
                    raise WorkstationError("max_tris must be a list of positive integers", 400)
                asset_id = int(match.group(1))
                get_asset(server.db_path, asset_id)                 # 404 before queueing
                ratio_list = [float(r) for r in ratios]
                if any(later >= earlier for earlier, later in zip(ratio_list, ratio_list[1:])):
                    raise WorkstationError(
                        "ratios must get smaller level by level (e.g. 0.5, 0.25, 0.125); "
                        "otherwise a LOD would cost more than the one above it", 400)
                if (self._query().get("async", [""])[0] or "") in ("1", "true", "yes"):
                    db_path, data_dir = server.db_path, Path(server.data_dir)
                    publish = self._publish

                    def work(progress, _asset=asset_id, _ratios=ratio_list, _caps=caps):
                        lods = generate_lods(db_path, data_dir, _asset, _ratios, _caps, progress)
                        publish("lod", _asset)
                        return {"lods": lods}
                    job = server.jobs.submit(asset_id, work)
                    self._json(202, {"job": job})
                    return
                lods = generate_lods(server.db_path, Path(server.data_dir),
                                     asset_id, ratio_list, caps)
                self._publish("lod", asset_id)
                self._json(201, {"lods": lods})
                return

            match = ASSET_LOD_RE.match(path)
            if match:
                raw = self._body(MAX_UPLOAD_BYTES)
                if raw is None:
                    return
                filename = (self._query().get("filename", [""])[0]
                            or self.headers.get("X-Filename", ""))
                if not filename:
                    raise WorkstationError("filename is required (query or X-Filename)", 400)
                lods = upload_lod(server.db_path, Path(server.data_dir), int(match.group(1)),
                                  match.group(2), filename, raw)
                self._publish("lod", int(match.group(1)))
                self._json(201, {"lods": lods})
                return

            match = ASSET_COLLISION_RE.match(path)
            if match:
                payload = self._json_body()
                if payload is None:
                    return
                kind = str(payload.get("kind", ""))
                data = payload["data"] if "data" in payload else payload
                collision = set_collision(server.db_path, int(match.group(1)), kind, data)
                self._publish("collision", int(match.group(1)))
                self._json(200, collision)
                return

            self._json(404, {"error": "not found", "path": path})
        except WorkstationError as exc:
            self._json(exc.status, {"error": str(exc)})
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._json(500, {"error": "internal error", "detail": str(exc)})

    # - DELETE - #

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._host_allowed():
            self._reject_host()
            return
        if self._cross_site():
            self._reject_cross_site()
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            server = self.server  # type: ignore[attr-defined]
            if path == "/api/session":
                sign_out(server.db_path, self._cookie(SESSION_COOKIE))
                self._send(200, b'{"signed_out": true}', "application/json; charset=utf-8",
                           {"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; "
                                          "SameSite=Strict; Max-Age=0"})
                return
            who = self._principal()
            if who is None:
                self._json(401, {"error": "unauthorised"})
                return
            if self._forbidden(who, self._permission_for("DELETE", path)):
                return
            match = ASSET_RE.match(path)
            if match:
                delete_asset(server.db_path, Path(server.data_dir), int(match.group(1)))
                self._publish("asset-deleted", int(match.group(1)))
                self._json(200, {"deleted": True})
                return
            match = ASSET_LOD_RE.match(path)
            if match:
                lods = delete_lod(server.db_path, Path(server.data_dir), int(match.group(1)),
                                  match.group(2))
                self._publish("lod", int(match.group(1)))
                self._json(200, {"lods": lods})
                return
            match = NOTE_RE.match(path)
            if match:
                delete_note(server.db_path, int(match.group(1)))
                self._publish("notes")
                self._json(200, {"deleted": True})
                return
            self._json(404, {"error": "not found", "path": path})
        except WorkstationError as exc:
            self._json(exc.status, {"error": str(exc)})
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._json(500, {"error": "internal error", "detail": str(exc)})

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_PUT(self) -> None:  # noqa: N802
        if not self._host_allowed():
            self._reject_host()
            return
        self._json(405, {"error": "method not allowed"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        """No CORS: a preflight gets a plain 405 and no Access-Control headers,
        so a browser on another site never gets permission to call the API."""
        if not self._host_allowed():
            self._reject_host()
            return
        self._send(405, json.dumps({"error": "method not allowed"}).encode("utf-8"),
                   "application/json; charset=utf-8", {"Allow": "GET, HEAD, POST, DELETE"})


class WorkstationServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], db_path: str | Path, data_dir: str | Path,
                 token: str = "", quiet: bool = False,
                 rules_path: str | Path = "", rules_root: str | Path = "",
                 allow_hosts: tuple[str, ...] = ()):
        self.db_path = str(db_path)
        self.data_dir = str(data_dir)
        self.token = token
        self.quiet = quiet
        # Host header allow-list, same policy as the dashboard: loopback names,
        # IP literals and this machine's own name are always accepted.
        self.allow_hosts = tuple(str(item).strip().lower() for item in allow_hosts if item)
        # The /api/rules inspector reports this rules.toml read-only. It is never
        # written and never gates an upload - it is a reference card for artists.
        self.rules_path = str(rules_path or "")
        self.rules_root = str(rules_root or Path.cwd())
        bound = str(address[0] or "")
        # Reachable from other machines: anything but a loopback address.
        self.exposed = bound not in ("127.0.0.1", "localhost", "::1") and not bound.startswith("127.")
        self.broadcaster = Broadcaster()   # /api/events
        self.login_failures: dict[str, list[float]] = {}
        self.jobs = JobRunner(self.broadcaster.publish)
        self.stopping = False
        super().__init__(address, Handler)

    def shutdown(self) -> None:            # noqa: D401 - stdlib override
        self.stopping = True
        super().shutdown()


def build_server(host: str, port: int, db_path: str | Path, data_dir: str | Path,
                 token: str = "", quiet: bool = False,
                 rules_path: str | Path = "", rules_root: str | Path = "",
                 allow_hosts: tuple[str, ...] = ()) -> WorkstationServer:
    return WorkstationServer((host, port), db_path, data_dir, token, quiet,
                             rules_path, rules_root, allow_hosts)


def describe_rules(server: "WorkstationServer") -> dict:
    """The read-only rules snapshot for the in-page inspector panel.

    Served by the workstation itself - not cross-origin from AssetHealth - so
    the viewer needs no CORS and works with zero extra configuration. A broken
    rules.toml is returned as data, so the panel can explain the problem instead
    of showing an empty table.
    """
    try:
        return agk.describe_rules(Path(server.rules_root), server.rules_path or None)
    except (agk.ConfigError, SystemExit) as exc:
        return {"read_only": True,
                "error": f"cannot read rules: {exc}",
                "source": server.rules_path or "built-in defaults",
                "global_budget": None,
                "tag_budgets": []}


# --- #
# CLI
# --- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="art_workstation_server",
        description="Technical-art workstation: 3D viewer, LOD manager, collision "
                    "inspector and review notes (stdlib only).")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"interface to bind (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port (default {DEFAULT_PORT})")
    parser.add_argument("--port-auto", action="store_true",
                        help="if the port is busy, move to the next free one instead of "
                             "failing (prints what it chose)")
    parser.add_argument("--port-tries", type=int, default=10, metavar="N",
                        help="how many ports --port-auto may walk past (default 10)")
    parser.add_argument("--db", default=os.environ.get("WORKSTATION_DB", DEFAULT_DB),
                        help=f"SQLite file (default {DEFAULT_DB})")
    parser.add_argument("--data", default=os.environ.get("WORKSTATION_DATA", DEFAULT_DATA),
                        help=f"asset storage folder (default {DEFAULT_DATA})")
    parser.add_argument("--token", default=os.environ.get(TOKEN_ENV, ""),
                        help=f"require a bearer token (default ${TOKEN_ENV}; empty = solo mode)")
    parser.add_argument("--quiet", action="store_true", help="only log errors")
    parser.add_argument("--rules", metavar="PATH", default=os.environ.get(RULES_ENV, ""),
                        help="rules.toml the in-page Rules & Budgets panel reports on, "
                             f"read-only (default: ./{agk.RULES_FILENAME} in the working "
                             f"directory, or ${RULES_ENV})")
    parser.add_argument("--allow-host", metavar="NAME", action="append", default=[],
                        help="accept this Host header as well as IP literals, localhost "
                             "and this machine's own name (repeatable). Requests whose "
                             "Host names anything else are refused, which is what stops a "
                             "DNS-rebinding page from driving the viewer")
    accounts = parser.add_argument_group(
        "accounts (named reviewers; with none, the server works as before)")
    accounts.add_argument("--add-user", metavar="NAME",
                          help="create an account and exit; asks for the password twice")
    accounts.add_argument("--role", choices=ROLES, default="reviewer",
                          help="with --add-user or --set-role: viewer (read + notes), "
                               "reviewer (+ upload, LODs, fixes, sign-off) or lead (+ delete)")
    accounts.add_argument("--set-password", metavar="NAME",
                          help="change an account's password (signs it out everywhere) and exit")
    accounts.add_argument("--set-role", metavar="NAME", help="change an account's role and exit")
    accounts.add_argument("--remove-user", metavar="NAME", help="delete an account and exit")
    accounts.add_argument("--list-users", action="store_true", help="list accounts and exit")
    accounts.add_argument("--password-stdin", action="store_true",
                          help="read the password from standard input instead of prompting "
                               "(for provisioning scripts)")
    return parser


def _read_password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    import getpass
    first = getpass.getpass("password (10+ characters): ")
    if getpass.getpass("again: ") != first:
        raise WorkstationError("the two passwords do not match", 400)
    return first


def manage_accounts(args, db_path: Path) -> int:
    """--add-user / --set-password / --set-role / --remove-user / --list-users."""
    try:
        if args.list_users:
            rows = list_accounts(db_path)
            if not rows:
                print("no accounts: the workstation is in solo/token mode")
            for row in rows:
                print(f"{row['username']:<24}{row['role']:<10}{row['created_at']}")
            return 0
        if args.add_user:
            account = add_account(db_path, args.add_user, args.role, _read_password(args))
            print(f"added {account['username']} ({account['role']})")
            if len(list_accounts(db_path)) == 1:
                print("accounts are now on: every reviewer signs in; scripts (the bulk "
                      "uploader, AssetHealth, the P4 trigger) need --token")
            return 0
        if args.set_password:
            set_account(db_path, args.set_password, password=_read_password(args))
            print(f"password changed for {args.set_password}; its sessions were signed out")
            return 0
        if args.set_role:
            account = set_account(db_path, args.set_role, role=args.role)
            print(f"{account['username']} is now {account['role']}")
            return 0
        remove_account(db_path, args.remove_user)
        print(f"removed {args.remove_user}")
        if not list_accounts(db_path):
            print("no accounts left: the workstation is back in solo/token mode")
        return 0
    except WorkstationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db).expanduser()
    data_dir = Path(args.data).expanduser()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        asset_paths(data_dir)
        init_db(db_path)
    except (OSError, sqlite3.Error) as exc:
        print(f"error: cannot initialise storage: {exc}", file=sys.stderr)
        return 2
    if not (STATIC_DIR / "viewer.html").is_file():
        print(f"warning: {STATIC_DIR / 'viewer.html'} is missing; /viewer will 404",
              file=sys.stderr)
    if any((args.add_user, args.set_password, args.set_role, args.remove_user,
            args.list_users)):
        return manage_accounts(args, db_path)

    def _moved(old: int, new: int, exc: BaseException) -> None:
        print(f"note: port {old} is busy ({exc.__class__.__name__}), using {new}",
              file=sys.stderr)

    try:
        httpd, bound_port = agk.bind_with_fallback(
            lambda host, port: build_server(host, port, db_path, data_dir, args.token,
                                            args.quiet, rules_path=args.rules,
                                            rules_root=Path.cwd(),
                                            allow_hosts=tuple(args.allow_host)),
            args.host, args.port, auto=args.port_auto, tries=args.port_tries,
            reserved=(ASSET_HEALTH_PORT,), report=_moved)
    except OSError as exc:
        print(f"error: cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        print(agk.port_in_use_hint(args.port), file=sys.stderr)
        if not args.port_auto:
            print("      or let it move by itself: --port-auto", file=sys.stderr)
        return 2

    host, port = httpd.server_address[0], httpd.server_address[1]
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"Art Workstation  http://{shown}:{port}/viewer")
    print(f"  database   {db_path.resolve()}")
    print(f"  storage    {data_dir.resolve()}")
    rules_shown = Path(args.rules).resolve() if args.rules else Path.cwd() / agk.RULES_FILENAME
    print(f"  rules      {rules_shown}  (GET /api/rules, read-only)")
    print(f"  auth       {'bearer token required' if args.token else 'solo mode (no token)'}")
    if httpd.exposed and not args.token:
        print("WARNING: listening on the network with no token. Anyone who can reach "
              f"{host}:{port} can upload, edit notes, set verdicts and delete assets.\n"
              "         Restart with --token <secret> (or AUTH_TOKEN), or bind 127.0.0.1.",
              file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
