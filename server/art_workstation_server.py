#!/usr/bin/env python3
"""Art Workstation — a technical-art review tool built on the gate's ecosystem.

Zero external dependencies: ``http.server``, ``sqlite3``, ``json``. The browser
side loads Three.js from a CDN and nothing else.

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
    POST /api/assets?filename=...   -> upload a .obj/.gltf/.glb (raw body)
    GET  /api/assets/<id>           -> metadata + lods + collision + note count
    GET  /api/assets/<id>/file      -> the bytes
    DEL  /api/assets/<id>           -> delete the row and its files
    GET  /api/assets/<id>/lods      -> generated LODs
    POST /api/assets/<id>/lods      -> generate them ({"ratios": [...]})
    GET  /api/assets/<id>/lods/<lvl>/file
    GET  /api/assets/<id>/collision
    POST /api/assets/<id>/collision -> {"kind": "box"|"sphere"|"hull", ...}
    GET  /api/assets/<id>/notes
    POST /api/notes                 -> {"asset_id", "category", "note_text"}
    DEL  /api/notes/<id>
    GET  /api/stats                 -> repository totals
"""

from __future__ import annotations

import argparse
import html
import json
import os
import queue
import re
import shutil
import sqlite3
import sys
import threading
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
import lod_manager  # noqa: E402 - the decimator

DEFAULT_DB = "art_workstation.db"
DEFAULT_DATA = "workstation_data"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8100
STATIC_DIR = HERE / "static"
TOKEN_ENV = "AUTH_TOKEN"

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
ALLOWED_SUFFIXES = {".obj", ".gltf", ".glb"}
CATEGORIES = ("Geometry", "Texture", "LOD", "Collision", "Shading", "Other")
COLLISION_KINDS = ("box", "sphere", "hull")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def html_escape(value: object) -> str:
    """Every string in a report is artist-supplied: escape all of it."""
    return html.escape("" if value is None else str(value))


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

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
    uploaded_at    TEXT    NOT NULL
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


# --------------------------------------------------------------------------- #
# Mesh facts — reuses the gate's analysers so the numbers agree everywhere
# --------------------------------------------------------------------------- #

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
        low = [float("inf")] * 3
        high = [float("-inf")] * 3
        for mesh in document.get("meshes", []):
            for primitive in mesh.get("primitives", []):
                position = primitive.get("attributes", {}).get("POSITION")
                if not isinstance(position, int) or position >= len(accessors):
                    continue
                accessor = accessors[position]
                if "min" in accessor and "max" in accessor and len(accessor["min"]) >= 3:
                    for axis in range(3):
                        low[axis] = min(low[axis], float(accessor["min"][axis]))
                        high[axis] = max(high[axis], float(accessor["max"][axis]))
        if low[0] == float("inf"):
            return None
        return low, high
    except Exception:
        return None


def analyse_asset(path: Path) -> dict:
    """Everything the assets table needs, in one pass."""
    try:
        triangles = agk.count_triangles(path)
    except Exception as exc:
        raise WorkstationError(f"cannot read geometry: {exc}", 422) from exc
    try:
        materials = agk.count_material_slots(path)
    except Exception:
        materials = 1
    return {
        "triangles": int(triangles),
        "vertices": mesh_vertices(path),
        "materials": int(materials),
        "bounds": mesh_bounds(path),
    }


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str) -> str:
    """Strip directories, control characters and anything a shell would enjoy."""
    base = Path(name.replace("\\", "/")).name.strip()
    base = SAFE_NAME.sub("_", base).strip(" ._") or "asset"
    return base[:120]


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


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #

def list_assets(db_path: str | Path) -> list[dict]:
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT a.*,"
            "       (SELECT COUNT(*) FROM lod_versions l WHERE l.asset_id = a.id) AS lod_count,"
            "       (SELECT COUNT(*) FROM reviews_and_notes n WHERE n.asset_id = a.id) AS note_count"
            " FROM assets a ORDER BY a.uploaded_at DESC, a.id DESC").fetchall()
    return [dict(row) for row in rows]


def get_asset(db_path: str | Path, asset_id: int) -> dict:
    with open_db(db_path) as connection:
        row = connection.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise WorkstationError(f"no asset {asset_id}", 404)
    return dict(row)


def list_lods(db_path: str | Path, asset_id: int) -> list[dict]:
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM lod_versions WHERE asset_id = ? ORDER BY level", (asset_id,)).fetchall()
    return [dict(row) for row in rows]


def list_notes(db_path: str | Path, asset_id: int) -> list[dict]:
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM reviews_and_notes WHERE asset_id = ?"
            " ORDER BY created_at DESC, id DESC", (asset_id,)).fetchall()
    return [dict(row) for row in rows]


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

    # Orphans are repository-level, but scoped to the asset store: a texture in
    # the store that no stored mesh points at is dead weight nobody will delete.
    assets_dir, _ = asset_paths(data_dir)
    orphaned = [path.name for path in agk.orphaned_files(assets_dir)]

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


# --------------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------------- #

def create_asset(db_path: str | Path, data_dir: Path, filename: str, payload: bytes,
                 uploader: str = "") -> dict:
    """Store an uploaded file, measure it, and seed its bounding-box collision."""
    name = safe_filename(filename)
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise WorkstationError(
            f"{suffix or 'no extension'} is not a supported asset type "
            f"({', '.join(sorted(ALLOWED_SUFFIXES))})", 415)
    if not payload:
        raise WorkstationError("empty upload", 400)

    with _WRITE_LOCK, open_db(db_path) as connection:
        cursor = connection.execute(
            "INSERT INTO assets (filename, file_path, filesize, triangle_count,"
            " vertex_count, material_slots, uploader, uploaded_at)"
            " VALUES (?, '', ?, 0, 0, 0, ?, ?)",
            (name, len(payload), uploader, utc_now()))
        asset_id = int(cursor.lastrowid or 0)

    assets_dir, _ = asset_paths(data_dir)
    stored = assets_dir / f"{asset_id}_{name}"
    stored.write_bytes(payload)

    try:
        facts = analyse_asset(stored)
    except WorkstationError:
        stored.unlink(missing_ok=True)
        with _WRITE_LOCK, open_db(db_path) as connection:
            connection.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        raise

    with _WRITE_LOCK, open_db(db_path) as connection:
        connection.execute(
            "UPDATE assets SET file_path = ?, triangle_count = ?, vertex_count = ?,"
            " material_slots = ? WHERE id = ?",
            (f"assets/{stored.name}", facts["triangles"], facts["vertices"],
             facts["materials"], asset_id))
        if facts["bounds"] is not None:
            low, high = facts["bounds"]
            connection.execute(
                "INSERT OR REPLACE INTO collisions (asset_id, kind, data_json, created_at)"
                " VALUES (?, 'box', ?, ?)",
                (asset_id, json.dumps({"min": low, "max": high}), utc_now()))
    return get_asset(db_path, asset_id)


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
    stored = resolve_inside(assets_dir, asset["file_path"].replace("assets/", "", 1))
    if stored is not None:
        stored.unlink(missing_ok=True)
    shutil.rmtree(lods_dir / str(asset_id), ignore_errors=True)


def generate_lods(db_path: str | Path, data_dir: Path, asset_id: int,
                  ratios: list[float] | None = None,
                  max_triangles: list[int] | None = None) -> list[dict]:
    """Decimate the asset into LOD files and record them."""
    asset = get_asset(db_path, asset_id)
    _, lods_dir = asset_paths(data_dir)
    source = resolve_inside(data_dir, asset["file_path"])
    if source is None or not source.is_file():
        raise WorkstationError("the stored file is missing", 410)

    out_dir = lods_dir / str(asset_id)
    try:
        results = lod_manager.generate(source, out_dir,
                                       ratios or lod_manager.DEFAULT_RATIOS,
                                       max_triangles or None, name=asset["filename"])
    except lod_manager.LodError as exc:
        raise WorkstationError(str(exc), 422) from exc

    produced = {result.level for result in results}
    with _WRITE_LOCK, open_db(db_path) as connection:
        # A regeneration with fewer ratios must not leave the old levels behind:
        # a stale "LOD3 · 4,328 tris" button would be a lie the viewer repeats.
        stale = [row["level"] for row in connection.execute(
            "SELECT level FROM lod_versions WHERE asset_id = ?", (asset_id,)).fetchall()
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
                " triangle_count, vertex_count, file_path, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(asset_id, level) DO UPDATE SET"
                " ratio = excluded.ratio, target_triangles = excluded.target_triangles,"
                " triangle_count = excluded.triangle_count,"
                " vertex_count = excluded.vertex_count, file_path = excluded.file_path,"
                " created_at = excluded.created_at",
                (asset_id, result.level, result.ratio, result.target_triangles,
                 result.triangle_count, result.vertex_count, relative, utc_now()))
    return list_lods(db_path, asset_id)


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


def set_collision(db_path: str | Path, asset_id: int, kind: str, data: dict) -> dict:
    """Store a collision proxy: an AABB, a sphere, or an explicit hull."""
    if kind not in COLLISION_KINDS:
        raise WorkstationError(f"kind must be one of {', '.join(COLLISION_KINDS)}", 400)
    get_asset(db_path, asset_id)

    if kind == "box":
        low, high = data.get("min"), data.get("max")
        if not (isinstance(low, list) and isinstance(high, list) and len(low) == 3 == len(high)):
            raise WorkstationError("box collision needs min:[x,y,z] and max:[x,y,z]", 400)
        payload = {"min": [float(v) for v in low], "max": [float(v) for v in high]}
    elif kind == "sphere":
        center = data.get("center")
        radius = data.get("radius")
        if not (isinstance(center, list) and len(center) == 3) or not isinstance(radius, (int, float)):
            raise WorkstationError("sphere collision needs center:[x,y,z] and radius", 400)
        payload = {"center": [float(v) for v in center], "radius": float(radius)}
    else:
        points = data.get("points")
        if not isinstance(points, list) or len(points) < 4 or len(points) > 5000:
            raise WorkstationError("hull collision needs 4..5000 points", 400)
        cleaned = []
        for point in points:
            if not isinstance(point, list) or len(point) != 3:
                raise WorkstationError("every hull point must be [x, y, z]", 400)
            cleaned.append([float(v) for v in point])
        payload = {"points": cleaned}

    with _WRITE_LOCK, open_db(db_path) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO collisions (asset_id, kind, data_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (asset_id, kind, json.dumps(payload), utc_now()))
    return {"kind": kind, "data": payload}


def repository_stats(db_path: str | Path) -> dict:
    with open_db(db_path) as connection:
        assets = connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        triangles = connection.execute(
            "SELECT COALESCE(SUM(triangle_count), 0) FROM assets").fetchone()[0]
        lods = connection.execute("SELECT COUNT(*) FROM lod_versions").fetchone()[0]
        notes = connection.execute("SELECT COUNT(*) FROM reviews_and_notes").fetchone()[0]
        by_category = connection.execute(
            "SELECT category, COUNT(*) AS n FROM reviews_and_notes"
            " GROUP BY category ORDER BY n DESC").fetchall()
    return {
        "assets": int(assets),
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

    Everything is inline — no CSS file, no script, no image — so the file can be
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
    verdict_html = "<li>the stored file is missing</li>"
    status_class, status_label = "fail", "UNREADABLE"
    problems: list[str] = []
    if stored is not None and stored.is_file():
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
            detail = f"min {data['min']} · max {data['max']} · size {size}"
        elif collision["kind"] == "sphere":
            detail = f"center {data['center']} · radius {data['radius']}"
        else:
            detail = f"{len(data['points'])} hull points"
        collision_html = (f"<p><strong>{html_escape(collision['kind'])}</strong> "
                          f"<span class='sub mono'>{html_escape(detail)}</span></p>")

    notes_html = "".join(
        f"<div class='note'><div class='meta'><strong>{html_escape(note['category'])}</strong>"
        f" · {html_escape(note['author'] or 'anonymous')}"
        f" · {html_escape(note['created_at'].replace('T', ' '))}</div>"
        f"{html_escape(note['note_text'])}</div>"
        for note in notes) or "<p class='sub'>No review notes.</p>"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{html_escape(asset['filename'])} — asset report</title>
<style>{REPORT_CSS}</style></head>
<body><div class="wrap">
  <h1>{html_escape(asset['filename'])}
    <span class="badge {status_class}">{html_escape(status_label)}</span></h1>
  <div class="sub">Asset Health report · asset #{asset['id']} ·
    uploaded {html_escape(asset['uploaded_at'].replace('T', ' '))}
    by {html_escape(asset['uploader'] or 'unknown')}</div>

  <h2>Geometry</h2>
  <table><tbody>{rows([
      ("triangles", f"{asset['triangle_count']:,}"),
      ("vertices", f"{asset['vertex_count']:,}"),
      ("material slots", asset["material_slots"]),
      ("file size", f"{asset['filesize']:,} bytes"),
      ("stored path", asset["file_path"]),
  ])}</tbody></table>

  <h2>Validation (default budget: 50,000 tris · 3 slots · 2048px)</h2>
  <ul>{verdict_html}</ul>

  <h2>Reference integrity</h2>
  <table><thead><tr><th>reference</th><th>state</th></tr></thead>
  <tbody>{reference_rows or "<tr><td class='sub'>no external references</td><td></td></tr>"}
  </tbody></table>
  <p class="sub">{len(references['missing'])} missing ·
     {len(references['orphaned'])} orphaned texture(s) in the asset store</p>

  <h2>LOD breakdown</h2>
  <table><thead><tr><th>level</th><th>triangles</th><th>vertices</th>
    <th>ratio</th><th>of source</th></tr></thead>
  <tbody>{lod_rows or "<tr><td class='sub' colspan='5'>no LODs generated</td></tr>"}</tbody></table>

  <h2>Collision</h2>
  {collision_html}

  <h2>Review notes</h2>
  {notes_html}

  <footer>Generated by Art Workstation ·
    {html_escape(datetime.now(timezone.utc).isoformat(timespec="seconds"))} ·
    this file is self-contained (no external assets).</footer>
</div></body></html>
"""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

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
}

ASSET_RE = re.compile(r"^/api/assets/(\d+)$")
ASSET_FILE_RE = re.compile(r"^/api/assets/(\d+)/file$")
ASSET_LODS_RE = re.compile(r"^/api/assets/(\d+)/lods$")
ASSET_LOD_FILE_RE = re.compile(r"^/api/assets/(\d+)/lods/(LOD[1-4])/file$")
ASSET_NOTES_RE = re.compile(r"^/api/assets/(\d+)/notes$")
ASSET_COLLISION_RE = re.compile(r"^/api/assets/(\d+)/collision$")
ASSET_REFERENCES_RE = re.compile(r"^/api/assets/(\d+)/references$")
ASSET_REPORT_RE = re.compile(r"^/api/assets/(\d+)/report$")
NOTE_RE = re.compile(r"^/api/notes/(\d+)$")


class Handler(BaseHTTPRequestHandler):
    server_version = "ArtWorkstation/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------- #

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib hook
        if getattr(self.server, "quiet", False):
            return
        sys.stderr.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str,
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
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
        import hmac

        presented = self._token()
        return bool(presented) and hmac.compare_digest(presented, expected)

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
        return self.rfile.read(length)

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

    # -- GET ---------------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
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
            if not self._authorised():
                self._json(401, {"error": "unauthorised", "detail": "Bearer token required"})
                return
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
                             "stats": repository_stats(server.db_path)})
            return
        if path == "/api/stats":
            self._json(200, repository_stats(server.db_path))
            return
        if path == "/api/assets":
            self._json(200, {"assets": list_assets(server.db_path)})
            return

        match = ASSET_RE.match(path)
        if match:
            asset = get_asset(server.db_path, int(match.group(1)))
            asset["lods"] = list_lods(server.db_path, asset["id"])
            asset["collision"] = get_collision(server.db_path, asset["id"])
            asset["notes"] = list_notes(server.db_path, asset["id"])
            self._json(200, asset)
            return

        match = ASSET_FILE_RE.match(path)
        if match:
            self._serve_asset_file(int(match.group(1)))
            return

        match = ASSET_LODS_RE.match(path)
        if match:
            self._json(200, {"lods": list_lods(server.db_path, int(match.group(1)))})
            return

        match = ASSET_LOD_FILE_RE.match(path)
        if match:
            self._serve_lod_file(int(match.group(1)), match.group(2))
            return

        match = ASSET_NOTES_RE.match(path)
        if match:
            self._json(200, {"notes": list_notes(server.db_path, int(match.group(1)))})
            return

        if path == "/api/events":
            self._stream()
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
            headers["Content-Disposition"] = f'inline; filename="{download_name}"'
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

    # -- POST --------------------------------------------------------------- #

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        try:
            if not self._authorised():
                self._json(401, {"error": "unauthorised", "detail": "Bearer token required"})
                return
            server = self.server  # type: ignore[attr-defined]

            if path == "/api/assets":
                raw = self._body(MAX_UPLOAD_BYTES)
                if raw is None:
                    return
                filename = (self._query().get("filename", [""])[0]
                            or self.headers.get("X-Filename", ""))
                if not filename:
                    raise WorkstationError("filename is required (query or X-Filename)", 400)
                asset = create_asset(server.db_path, Path(server.data_dir), filename, raw,
                                     uploader=self._query().get("uploader", [""])[0])
                self._publish("asset", asset["id"])
                self._json(201, asset)
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
                                str(payload.get("author", "")))
                self._publish("notes", asset_id)
                self._json(201, note)
                return

            match = ASSET_LODS_RE.match(path)
            if match:
                payload = self._json_body() or {}
                ratios = payload.get("ratios") or list(lod_manager.DEFAULT_RATIOS)
                if not isinstance(ratios, list) or not all(
                        isinstance(r, (int, float)) and 0 < r < 1 for r in ratios):
                    raise WorkstationError("ratios must be a list of numbers between 0 and 1", 400)
                caps = payload.get("max_tris") or None
                if caps is not None and (not isinstance(caps, list)
                                         or not all(isinstance(c, int) for c in caps)):
                    raise WorkstationError("max_tris must be a list of integers", 400)
                lods = generate_lods(server.db_path, Path(server.data_dir),
                                     int(match.group(1)),
                                     [float(r) for r in ratios], caps)
                self._publish("lod", int(match.group(1)))
                self._json(201, {"lods": lods})
                return

            match = ASSET_COLLISION_RE.match(path)
            if match:
                payload = self._json_body()
                if payload is None:
                    return
                kind = str(payload.get("kind", ""))
                data = payload.get("data", payload)
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

    # -- DELETE ------------------------------------------------------------- #

    def do_DELETE(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        try:
            if not self._authorised():
                self._json(401, {"error": "unauthorised"})
                return
            server = self.server  # type: ignore[attr-defined]
            match = ASSET_RE.match(path)
            if match:
                delete_asset(server.db_path, Path(server.data_dir), int(match.group(1)))
                self._publish("asset-deleted", int(match.group(1)))
                self._json(200, {"deleted": True})
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
        self._json(405, {"error": "method not allowed"})


class WorkstationServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], db_path: str | Path, data_dir: str | Path,
                 token: str = "", quiet: bool = False):
        self.db_path = str(db_path)
        self.data_dir = str(data_dir)
        self.token = token
        self.quiet = quiet
        self.broadcaster = Broadcaster()   # /api/events
        self.stopping = False
        super().__init__(address, Handler)

    def shutdown(self) -> None:            # noqa: D401 - stdlib override
        self.stopping = True
        super().shutdown()


def build_server(host: str, port: int, db_path: str | Path, data_dir: str | Path,
                 token: str = "", quiet: bool = False) -> WorkstationServer:
    return WorkstationServer((host, port), db_path, data_dir, token, quiet)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="art_workstation_server",
        description="Technical-art workstation: 3D viewer, LOD manager, collision "
                    "inspector and review notes (stdlib only).")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"interface to bind (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port (default {DEFAULT_PORT})")
    parser.add_argument("--db", default=os.environ.get("WORKSTATION_DB", DEFAULT_DB),
                        help=f"SQLite file (default {DEFAULT_DB})")
    parser.add_argument("--data", default=os.environ.get("WORKSTATION_DATA", DEFAULT_DATA),
                        help=f"asset storage folder (default {DEFAULT_DATA})")
    parser.add_argument("--token", default=os.environ.get(TOKEN_ENV, ""),
                        help=f"require a bearer token (default ${TOKEN_ENV}; empty = solo mode)")
    parser.add_argument("--quiet", action="store_true", help="only log errors")
    return parser


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

    try:
        httpd = build_server(args.host, args.port, db_path, data_dir, args.token, args.quiet)
    except OSError as exc:
        print(f"error: cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 2

    host, port = httpd.server_address[0], httpd.server_address[1]
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"Art Workstation  http://{shown}:{port}/viewer")
    print(f"  database   {db_path.resolve()}")
    print(f"  storage    {data_dir.resolve()}")
    print(f"  auth       {'bearer token required' if args.token else 'solo mode (no token)'}")
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
