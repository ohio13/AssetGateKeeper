#!/usr/bin/env python3
"""LOD generation by vertex clustering. Standard library only.

The brief asks for a *lightweight* decimator, and that is exactly what vertex
clustering is: snap every vertex to a grid cell, merge the ones that share a
cell, drop the faces that collapsed, and dedupe. It is not a quadric-error
decimator — it will not preserve a sharp bevel the way Blender's Decimate does —
but it is deterministic, has no dependencies, never produces non-manifold
garbage, and its output triangle count is controllable to within a few percent.

That trade is deliberate: LODs generated here exist so a technical artist can
*see* the cost of a level in the viewer and argue about it in a note. Levels
that ship come from ``engine/lod_shippable.py`` (UVs, normals and materials kept)
or a DCC decimation pass.

Usage
    python engine/lod_manager.py model.obj --out-dir out --ratios 0.5,0.25,0.125
    python engine/lod_manager.py model.glb --max-tris 20000,5000,1200

Ratios are fractions of the source triangle count; --max-tris are hard caps per
level. Both can be combined: the cap wins when it is the tighter constraint.
"""

from __future__ import annotations

import argparse
import array
import base64
import json
import math
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import asset_gatekeeper as agk  # noqa: E402 - the validated glTF/GLB reader

try:                       # numpy ships with trimesh, which the gate already uses;
    import numpy as np     # without it the same algorithm runs in pure Python
except ImportError:        # pragma: no cover - environment dependent
    np = None

DEFAULT_RATIOS = (0.5, 0.25, 0.125)
LEVEL_NAMES = ("LOD1", "LOD2", "LOD3", "LOD4")

GLTF_SUFFIXES = {".gltf", ".glb"}
OBJ_SUFFIXES = {".obj"}

# glTF 2.0 binary container, GLB spec section 4. A .glb that does not begin with
# this magic is not a GLB: viewers and DCC importers reject it outright, so the
# bytes we write must match the extension we write them under.
GLB_MAGIC = 0x46546C67        # "glTF" little-endian
GLB_VERSION = 2
GLB_CHUNK_JSON = 0x4E4F534A   # "JSON"
GLB_CHUNK_BIN = 0x004E4942    # "BIN\0"


class LodError(Exception):
    """The source cannot be decimated (unknown format, unreadable, empty)."""


@dataclass(frozen=True)
class Mesh:
    positions: list[tuple[float, float, float]]
    faces: list[tuple[int, int, int]]

    @property
    def triangles(self) -> int:
        return len(self.faces)

    @property
    def vertices(self) -> int:
        return len(self.positions)

    def bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        if not self.positions:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        xs = [p[0] for p in self.positions]
        ys = [p[1] for p in self.positions]
        zs = [p[2] for p in self.positions]
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

    def diagonal(self) -> float:
        low, high = self.bounds()
        return math.dist(low, high) or 1.0


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def load_obj(path: Path) -> Mesh:
    """Parse an OBJ. Polygons are fan-triangulated so every face is a triangle."""
    positions: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise LodError(f"cannot read {path}: {exc}") from exc

    for line in text.splitlines():
        if line.startswith("v "):
            parts = line.split()
            if len(parts) >= 4:
                try:
                    positions.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    positions.append((0.0, 0.0, 0.0))
        elif line.startswith("f "):
            refs: list[int] = []
            for token in line.split()[1:]:
                head = token.split("/")[0]
                try:
                    raw = int(head)
                except ValueError:
                    refs = []
                    break
                index = raw - 1 if raw > 0 else len(positions) + raw
                if not 0 <= index < len(positions):
                    refs = []
                    break
                refs.append(index)
            for i in range(1, len(refs) - 1):          # fan
                faces.append((refs[0], refs[i], refs[i + 1]))
    if not positions or not faces:
        raise LodError(f"{path.name} contains no usable geometry")
    return Mesh(positions, faces)


def _node_matrix(node: dict) -> list:
    """A glTF node's local transform as a row-major 4x4 (spec: matrix or TRS)."""
    matrix = node.get("matrix")
    if isinstance(matrix, list) and len(matrix) == 16:
        return [[float(matrix[col * 4 + row]) for col in range(4)] for row in range(4)]
    tx, ty, tz = (node.get("translation") or [0, 0, 0])[:3]
    qx, qy, qz, qw = (node.get("rotation") or [0, 0, 0, 1])[:4]
    sx, sy, sz = (node.get("scale") or [1, 1, 1])[:3]
    rotation = [
        [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
        [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
        [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)],
    ]
    scale = (sx, sy, sz)
    return [[rotation[r][0] * scale[0], rotation[r][1] * scale[1], rotation[r][2] * scale[2],
             (tx, ty, tz)[r]] for r in range(3)] + [[0.0, 0.0, 0.0, 1.0]]


def _multiply(a: list, b: list) -> list:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _mesh_placements(document: dict) -> dict:
    """mesh index -> world matrix of the first node that uses it.

    LODs used to ignore nodes entirely, so a scaled or offset model's LODs sat at
    a different size and place from the source. Each mesh is placed once - the
    gate counts a mesh used by several nodes once, and the LOD percentages are
    against that count - with its first node's transform.
    """
    nodes = document.get("nodes") or []
    scenes = document.get("scenes") or []
    if scenes:
        index = document.get("scene", 0)
        scene = scenes[index] if isinstance(index, int) and 0 <= index < len(scenes) else scenes[0]
        roots = list(scene.get("nodes") or [])
    else:
        children = {child for node in nodes for child in (node.get("children") or [])}
        roots = [i for i in range(len(nodes)) if i not in children]
    identity = [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]
    placements: dict = {}
    stack = [(root, identity, 0) for root in reversed(roots)]
    while stack:
        index, parent, depth = stack.pop()
        if not isinstance(index, int) or not 0 <= index < len(nodes) or depth > 64:
            continue
        node = nodes[index]
        world = _multiply(parent, _node_matrix(node))
        mesh = node.get("mesh")
        if isinstance(mesh, int) and mesh not in placements:
            placements[mesh] = world
        for child in reversed(node.get("children") or []):
            stack.append((child, world, depth + 1))
    return placements


def load_gltf(path: Path) -> Mesh:
    """Decode every TRIANGLES primitive of a .gltf/.glb into one mesh, placed.

    Reuses the gate's accessor reader, which already handles GLB chunks, data
    URIs, sibling .bin files and interleaved byteStride buffers. Node transforms
    are applied (see :func:`_mesh_placements`); a mirrored node keeps its faces
    pointing outward.
    """
    try:
        document = agk.read_gltf_document(path)
    except Exception as exc:  # AssetError and friends
        raise LodError(f"cannot read {path.name}: {exc}") from exc

    placements = _mesh_placements(document)
    positions: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    for mesh_index, mesh in enumerate(document.get("meshes", [])):
        matrix = placements.get(mesh_index)
        flip = False
        if matrix is not None:
            m = matrix
            determinant = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                           - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                           + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
            flip = determinant < 0
        for primitive in mesh.get("primitives", []):
            mode = primitive.get("mode", 4)
            if mode != 4:                      # strips/fans are rare in exports
                continue
            attributes = primitive.get("attributes", {})
            if "POSITION" not in attributes:
                continue
            try:
                raw_positions = agk.read_accessor(path, document, int(attributes["POSITION"]))
                offset = len(positions)
                if matrix is None:
                    positions.extend((float(p[0]), float(p[1]), float(p[2])) for p in raw_positions)
                else:
                    m = matrix
                    positions.extend((
                        m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2] + m[0][3],
                        m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2] + m[1][3],
                        m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2] + m[2][3])
                        for p in ((float(q[0]), float(q[1]), float(q[2])) for q in raw_positions))
                if "indices" in primitive:
                    indices = agk.read_accessor(path, document, int(primitive["indices"]))
                    flat = [int(item[0]) for item in indices]
                else:
                    flat = list(range(len(raw_positions)))
            except Exception as exc:
                raise LodError(f"cannot decode a primitive of {path.name}: {exc}") from exc
            for i in range(0, len(flat) - 2, 3):
                a, b, c = offset + flat[i], offset + flat[i + 1], offset + flat[i + 2]
                faces.append((a, c, b) if flip else (a, b, c))

    if not positions or not faces:
        raise LodError(f"{path.name} contains no triangle geometry")
    return Mesh(positions, faces)


def load_mesh(path: Path) -> Mesh:
    suffix = path.suffix.lower()
    if suffix in OBJ_SUFFIXES:
        mesh = load_obj(path)
    elif suffix in GLTF_SUFFIXES:
        mesh = load_gltf(path)
    else:
        raise LodError(f"unsupported format {suffix} (use .obj, .gltf or .glb)")
    # A NaN or infinite coordinate cannot be snapped to a grid cell: clustering
    # died with "cannot convert float NaN to integer" and the server said 500.
    for index, point in enumerate(mesh.positions):
        if not all(math.isfinite(value) for value in point):
            raise LodError(f"{path.name}: vertex {index + 1} has a non-finite coordinate "
                           f"{tuple(point)}; fix the export")
    return mesh


# --------------------------------------------------------------------------- #
# Decimation
# --------------------------------------------------------------------------- #

def cluster(mesh: Mesh, cell: float) -> Mesh:
    """Merge all vertices sharing a grid cell of `cell` world units."""
    if cell <= 0:
        return mesh
    buckets: dict[tuple[int, int, int], int] = {}
    sums: list[list[float]] = []
    counts: list[int] = []
    remap: list[int] = []

    for x, y, z in mesh.positions:
        key = (math.floor(x / cell), math.floor(y / cell), math.floor(z / cell))
        index = buckets.get(key)
        if index is None:
            index = len(sums)
            buckets[key] = index
            sums.append([x, y, z])
            counts.append(1)
        else:
            target = sums[index]
            target[0] += x
            target[1] += y
            target[2] += z
            counts[index] += 1
        remap.append(index)

    merged = [(s[0] / c, s[1] / c, s[2] / c) for s, c in zip(sums, counts)]

    faces: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int, int]] = set()
    for a, b, c in mesh.faces:
        na, nb, nc = remap[a], remap[b], remap[c]
        if na == nb or nb == nc or na == nc:       # collapsed: this face has no area
            continue
        key = (na, nb, nc) if na < nb < nc else tuple(sorted((na, nb, nc)))
        if key in seen:                            # duplicated after merging
            continue
        seen.add(key)
        faces.append((na, nb, nc))

    # Compact away vertices no surviving face references.
    used = sorted({index for face in faces for index in face})
    renumber = {old: new for new, old in enumerate(used)}
    return Mesh([merged[old] for old in used],
                [(renumber[a], renumber[b], renumber[c]) for a, b, c in faces])


def _score(triangles: int, target: int) -> float:
    """How bad is this result? Going *under* the target is penalised.

    Clustering has cliffs: a cell size small enough to keep every triangle apart,
    then one that swallows a whole cluster of them at once, with nothing in
    between. On a mesh of disconnected triangles there may be no cell size that
    lands near the target at all. Overshooting the count is recoverable (the level
    is simply coarser than asked); collapsing to a handful of faces is not, so a
    shortfall costs extra - and collapsing to *nothing* is not a LOD at all, so it
    scores worst of all and is never chosen.
    """
    if triangles <= 0:
        return float("inf")
    if triangles >= target:
        return float(triangles - target)
    return float(target - triangles) * 1.5


def _cluster_arrays(positions, faces, cell: float, count_only: bool = False):
    """numpy version of :func:`cluster`: same grid, same averaging, same dedupe.

    One pass is a handful of array operations instead of a Python loop per
    vertex, so the search below can afford ~60 trial cell sizes on a million
    triangles (the loop version took 201 s for 26).
    """
    low = positions.min(axis=0)
    keys = np.floor((positions - low) / cell).astype(np.int64)
    span = keys.max(axis=0) + 1
    if float(span[0]) * float(span[1]) * float(span[2]) < 9.0e18:
        flat = (keys[:, 0] * span[1] + keys[:, 1]) * span[2] + keys[:, 2]
        _, inverse = np.unique(flat, return_inverse=True)
    else:                                            # pragma: no cover - absurd extents
        _, inverse = np.unique(keys, axis=0, return_inverse=True)
    inverse = inverse.reshape(-1)
    remapped = inverse[faces]
    keep = ((remapped[:, 0] != remapped[:, 1]) & (remapped[:, 1] != remapped[:, 2])
            & (remapped[:, 0] != remapped[:, 2]))
    remapped = remapped[keep]
    if remapped.size == 0:
        return 0 if count_only else (positions[:0], remapped)
    ordered = np.sort(remapped, axis=1)
    clusters = int(inverse.max()) + 1
    if float(clusters) ** 3 < 9.0e18:
        # One int64 per face: a 1-D unique is several times faster than axis=0.
        packed = (ordered[:, 0] * clusters + ordered[:, 1]) * clusters + ordered[:, 2]
        _, first = np.unique(packed, return_index=True)
    else:                                            # pragma: no cover - > 2M clusters
        _, first = np.unique(ordered, axis=0, return_index=True)
    if count_only:
        return int(first.size)
    remapped = remapped[np.sort(first)]                  # keep each face's winding
    counts = np.bincount(inverse, minlength=clusters).astype(np.float64)
    merged = np.stack([np.bincount(inverse, weights=positions[:, axis], minlength=clusters)
                       for axis in range(3)], axis=1) / counts[:, None]
    used, renumbered = np.unique(remapped, return_inverse=True)
    return merged[used], renumbered.reshape(-1, 3)


def _pick(results: list, target: int, hard: bool):
    """Choose among (cell, triangles) trials.

    A LOD target is a budget, so the result should not exceed it: the closest
    count at or under the target wins. A ratio gets 2% of slack (a budget
    written as "half" is not broken by one triangle); a cap from ``--max-tris``
    gets none. Only when nothing usable is under the line - clustering has
    cliffs, and a mesh of loose triangles can jump from far above to nearly
    nothing - does the closest overshoot win, and never for a hard cap.
    """
    ceiling = target if hard else int(target * 1.02)
    under = [item for item in results if 0 < item[1] <= ceiling]
    if under:
        best = max(under, key=lambda item: item[1])
        if hard or best[1] >= target * 0.5:
            return best
    usable = [item for item in results if item[1] > 0]
    if hard:
        # Nothing reaches the cap without collapsing the mesh: return the closest
        # result anyway; the server flags the level as over its target rather
        # than refusing the whole ladder.
        return min(usable, key=lambda item: item[1]) if usable else None
    return min(usable, key=lambda item: _score(item[1], target)) if usable else None


def decimate_to(mesh: Mesh, target_triangles: int, samples: int = 26, hard: bool = False,
                cache: dict | None = None) -> Mesh:
    """Find the grid cell size whose clustering lands at (or just under) the target.

    A sampled sweep, then a bisection inside the bracket where the count crosses
    the target: clustering is *mostly* monotonic in cell size, but the crossings
    are cliffs, so the sweep finds the right neighbourhood and the bisection
    walks to the edge of the cliff instead of jumping over it. ``hard`` makes the
    target a ceiling that is never exceeded (a platform cap).
    """
    target = max(1, min(target_triangles, mesh.triangles))
    if target >= mesh.triangles:
        return mesh
    if np is None:                                   # pragma: no cover - no numpy
        return _decimate_to_python(mesh, target, samples)

    # The levels of one ladder share the welded arrays and every trial count:
    # LOD2's search starts where LOD1's left off.
    cache = cache if cache is not None else {}
    diagonal = mesh.diagonal()
    weld_cell = diagonal * 1e-5
    if "welded" not in cache:
        positions = np.asarray(mesh.positions, dtype=np.float64)
        faces = np.asarray(mesh.faces, dtype=np.int64)
        # Weld first: exporters split vertices per face corner (see the Python path).
        cache["welded"] = _cluster_arrays(positions, faces, weld_cell)
        cache["counts"] = {}
    positions, faces = cache["welded"]
    counts = cache["counts"]

    def count_at(cell: float) -> int:
        if cell not in counts:
            counts[cell] = _cluster_arrays(positions, faces, cell, count_only=True)
        return counts[cell]
    welded_count = len(faces)
    if welded_count <= target:
        return Mesh([tuple(p) for p in positions.tolist()], [tuple(f) for f in faces.tolist()])

    trials = [(weld_cell, welded_count)]
    cells = [weld_cell * (10.0 ** (5.0 * i / (samples - 1))) for i in range(1, samples)]
    for cell in cells:
        count = count_at(cell)
        trials.append((cell, count))
        if count < target / 4:
            break
    trials += [(cell, count) for cell, count in counts.items() if (cell, count) not in trials]
    # Bisect between the last cell above the target and the first at/under it.
    above = [item for item in trials if item[1] > target]
    below = [item for item in trials if 0 < item[1] <= target]
    if above and below:
        low_cell = max(above, key=lambda item: item[0])[0]
        high_cell = min((item for item in below if item[0] > low_cell),
                        key=lambda item: item[0], default=below[0])[0]
        for _ in range(20):
            middle = math.sqrt(low_cell * high_cell)
            count = count_at(middle)
            trials.append((middle, count))
            if count > target:
                low_cell = middle
            else:
                high_cell = middle
            if high_cell / low_cell < 1.001:
                break
    chosen = _pick(trials, target, hard)
    if chosen is None:
        raise LodError(f"no cell size reaches {target:,} triangles or fewer without "
                       "collapsing the mesh")
    merged, kept = _cluster_arrays(positions, faces, chosen[0])
    return Mesh([tuple(p) for p in merged.tolist()], [tuple(f) for f in kept.tolist()])


def _decimate_to_python(mesh: Mesh, target_triangles: int, samples: int = 26) -> Mesh:
    """The original pure-Python sweep, kept for installs without numpy."""
    target = max(1, min(target_triangles, mesh.triangles))
    if target >= mesh.triangles:
        return mesh

    diagonal = mesh.diagonal()
    # Weld first. DCC exporters split vertices per face corner (one normal or UV
    # per face), so a 72,500-triangle sphere arrives with 217,500 vertices. Those
    # duplicates are *not* in the same position by accident — merging them is
    # mandatory, because a grid cell small enough to keep two separate triangles
    # apart must also keep a triangle's own three corners apart, and clustering
    # then collapses whole triangles at once instead of grading downward.
    weld_cell = diagonal * 1e-5
    welded = cluster(mesh, weld_cell)
    if welded.triangles <= target:
        return welded

    # Geometric sweep upward from the weld size to "half the model in one cell".
    cells = [weld_cell * (10.0 ** (5.0 * i / (samples - 1))) for i in range(samples)]
    best, best_score = welded, _score(welded.triangles, target)

    for cell in cells:
        candidate = cluster(mesh, cell)
        if candidate.triangles == target:
            return candidate
        candidate_score = _score(candidate.triangles, target)
        if candidate_score < best_score:
            best, best_score = candidate, candidate_score
        if candidate.triangles < target / 4:      # already collapsed; stop digging
            break

    # One refinement pass between the winner and its neighbours, in cell space.
    if best is not welded:
        step = cells[1]
        for scale in (0.4, 0.7, 1.4, 2.0):
            candidate = cluster(mesh, step * scale)
            candidate_score = _score(candidate.triangles, target)
            if candidate_score < best_score:
                best, best_score = candidate, candidate_score
    return best


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def write_obj(path: Path, mesh: Mesh, name: str = "") -> None:
    lines = [f"# LOD generated by engine/lod_manager.py from {name or 'mesh'}"]
    lines += [f"v {x:.6f} {y:.6f} {z:.6f}" for x, y, z in mesh.positions]
    lines += ["o lod"] + [f"f {a + 1} {b + 1} {c + 1}" for a, b, c in mesh.faces]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _gltf_arrays(mesh: Mesh) -> tuple[array.array, array.array, bytes, bytes]:
    """POSITION as float32 VEC3 and the index buffer as uint32 SCALARs."""
    positions = array.array("f", [coordinate for point in mesh.positions for coordinate in point])
    indices = array.array("I", [index for face in mesh.faces for index in face])
    return positions, indices, positions.tobytes(), indices.tobytes()


def _gltf_document(mesh: Mesh, name: str, stem: str, buffer_entry: dict,
                   position_bytes: int, index_bytes: int) -> dict:
    """The JSON half of a minimal glTF 2.0: one primitive, no UVs.

    LODs drop materials by design, so one acceptor for POSITION and one index
    buffer is the whole scene graph. The caller supplies ``buffer_entry``
    because that is the only part that differs between .gltf and .glb.
    """
    # min/max as the float32 values actually stored: float64 bounds of the
    # source differ in the last digits, and the glTF validator reports
    # ACCESSOR_MIN_MISMATCH / ACCESSOR_MAX_MISMATCH on every level.
    low, high = mesh.bounds()
    low = list(array.array("f", low))
    high = list(array.array("f", high))
    scene_name = name or stem
    return {
        "asset": {"version": "2.0", "generator": "engine/lod_manager.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": scene_name}],
        "meshes": [{"name": scene_name, "primitives": [
            {"attributes": {"POSITION": 0}, "indices": 1, "material": 0}]}],
        "materials": [{"name": "lod", "pbrMetallicRoughness": {
            "baseColorFactor": [0.6, 0.6, 0.62, 1.0]}}],
        "buffers": [buffer_entry],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": position_bytes, "target": 34962},
            {"buffer": 0, "byteOffset": position_bytes, "byteLength": index_bytes,
             "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": mesh.vertices, "type": "VEC3",
             "min": list(low), "max": list(high)},
            {"bufferView": 1, "componentType": 5125, "count": index_bytes // 4,
             "type": "SCALAR"},
        ],
    }


def write_gltf(path: Path, mesh: Mesh, name: str = "") -> None:
    """A minimal but valid glTF 2.0, buffer embedded as a base64 data URI."""
    _positions, _indices, position_bytes, index_bytes = _gltf_arrays(mesh)
    blob = position_bytes + index_bytes
    buffer_entry = {
        "byteLength": len(blob),
        "uri": "data:application/octet-stream;base64,"
               + base64.b64encode(blob).decode("ascii"),
    }
    document = _gltf_document(mesh, name, path.stem, buffer_entry,
                              len(position_bytes), len(index_bytes))
    path.write_text(json.dumps(document, indent=1), encoding="utf-8")


def _pad_to_word(blob: bytes, filler: bytes) -> bytes:
    """Pad a chunk to a 4-byte boundary. JSON pads with spaces, BIN with zeros."""
    return blob + filler * (-len(blob) % 4)


def write_glb(path: Path, mesh: Mesh, name: str = "") -> None:
    """A real GLB: 12-byte header, JSON chunk, BIN chunk.

    Writing glTF *text* under a .glb name is the bug this function exists to
    fix: every viewer sniffs the magic, sees JSON, and refuses the file - after
    the LOD ladder has already been recorded as generated in the database.
    """
    _positions, _indices, position_bytes, index_bytes = _gltf_arrays(mesh)
    blob = position_bytes + index_bytes
    # No uri: in a GLB the buffer *is* the BIN chunk that follows the JSON.
    document = _gltf_document(mesh, name, path.stem, {"byteLength": len(blob)},
                              len(position_bytes), len(index_bytes))
    compact = json.dumps(document, separators=(",", ":"))
    json_blob = _pad_to_word(compact.encode("utf-8"), b" ")
    bin_blob = _pad_to_word(blob, b"\x00")
    total = 12 + 8 + len(json_blob) + 8 + len(bin_blob)
    path.write_bytes(b"".join((
        struct.pack("<III", GLB_MAGIC, GLB_VERSION, total),
        struct.pack("<II", len(json_blob), GLB_CHUNK_JSON), json_blob,
        struct.pack("<II", len(bin_blob), GLB_CHUNK_BIN), bin_blob,
    )))


def write_mesh(path: Path, mesh: Mesh, like: Path, name: str = "") -> None:
    """Write a LOD in the container the *filename* promises.

    The writer used to be chosen from the source suffix, which meant a ``.glb``
    LOD was written by the text writer: the name said binary, the bytes said
    JSON. The name is the contract the viewer reads, so the name decides.
    """
    container = path.suffix.lower() or like.suffix.lower()
    if container in OBJ_SUFFIXES:
        write_obj(path, mesh, name)
    elif container == ".glb":
        write_glb(path, mesh, name)
    else:
        write_gltf(path, mesh, name)


# --------------------------------------------------------------------------- #
# The pipeline used by the server
# --------------------------------------------------------------------------- #

def plan_targets(source_triangles: int, ratios: Sequence[float] = DEFAULT_RATIOS,
                 max_triangles: Sequence[int] | None = None) -> list[tuple[str, int, float]]:
    """Decide (level, target triangles, ratio) for each LOD.

    A ratio is the director's intent ("half the detail"), a cap is the platform's
    hard ceiling ("mobile cannot exceed 5k"). When both are given the tighter one
    wins, which is the only interpretation that cannot surprise anyone.
    """
    targets: list[tuple[str, int, float]] = []
    caps = list(max_triangles or [])
    for index, ratio in enumerate(ratios[: len(LEVEL_NAMES)]):
        if not 0 < ratio < 1:
            raise LodError(f"ratio must be between 0 and 1, got {ratio}")
        target = int(round(source_triangles * ratio))
        if index < len(caps) and caps[index]:
            target = min(target, int(caps[index]))
        target = max(1, target)
        targets.append((LEVEL_NAMES[index], target, float(ratio)))
    return targets


@dataclass
class GeneratedLod:
    level: str
    path: Path
    ratio: float
    target_triangles: int
    triangle_count: int
    vertex_count: int

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "path": str(self.path),
            "ratio": self.ratio,
            "target_triangles": self.target_triangles,
            "triangle_count": self.triangle_count,
            "vertex_count": self.vertex_count,
        }


def generate(source: Path, out_dir: Path, ratios: Sequence[float] = DEFAULT_RATIOS,
             max_triangles: Sequence[int] | None = None,
             name: str = "", progress=None) -> list[GeneratedLod]:
    """Decimate `source` into LOD files under `out_dir`.

    Every level is decimated and checked *before* the first file is written, so
    a refusal leaves no half-generated ladder behind: the server records what
    this returns, and files without rows are files the viewer never lists.

    ``progress(level, index, total)`` is called before each level is decimated,
    so a long job can say where it is.
    """
    mesh = load_mesh(source)
    planned: list[tuple[str, int, float, Mesh]] = []
    shared: dict = {}
    plan = plan_targets(mesh.triangles, ratios, max_triangles)
    caps = list(max_triangles or [])
    for index, (level, target, ratio) in enumerate(plan):
        if progress is not None:
            progress(level, index, len(plan))
        # A cap that is the tighter constraint is a platform ceiling: never over it.
        hard = index < len(caps) and bool(caps[index]) and int(caps[index]) <= target
        if target >= mesh.triangles:
            # Nothing to gain: still emit the level so the viewer shows it as 100%.
            reduced = mesh
        else:
            try:
                reduced = decimate_to(mesh, target, hard=hard, cache=shared)
            except LodError as exc:
                raise LodError(f"{level}: {exc}") from exc
        if reduced.triangles == 0:
            # Clustering can collapse a mesh with no connected geometry. A 0-tri
            # LOD loads as a hole in the viewer, so refuse instead of shipping it.
            raise LodError(
                f"{level} would be empty: {target} tris requested from "
                f"{mesh.triangles:,} collapses to 0 (try a larger ratio)")
        planned.append((level, target, ratio, reduced))

    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[GeneratedLod] = []
    for level, target, ratio, reduced in planned:
        path = out_dir / f"{level}_{source.stem}{source.suffix}"
        write_mesh(path, reduced, source, name or source.stem)
        results.append(GeneratedLod(level, path, ratio, target,
                                    reduced.triangles, reduced.vertices))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lod_manager",
        description="Generate LOD files by vertex-cluster decimation (stdlib only).")
    parser.add_argument("source", help="input .obj, .gltf or .glb")
    parser.add_argument("--out-dir", default="lod_out", help="where to write the LODs")
    parser.add_argument("--ratios", default="0.5,0.25,0.125",
                        help="comma-separated triangle ratios (default 0.5,0.25,0.125)")
    parser.add_argument("--max-tris", default="",
                        help="comma-separated hard caps per level, e.g. 20000,5000,1200")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    options = parser.parse_args(argv)

    source = Path(options.source)
    ratios = [float(item) for item in options.ratios.split(",") if item.strip()]
    caps = [int(item) for item in options.max_tris.split(",") if item.strip()]
    try:
        source_mesh = load_mesh(source)
        results = generate(source, Path(options.out_dir), ratios, caps or None)
    except LodError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if options.json:
        print(json.dumps([result.as_dict() for result in results], indent=2))
        return 0
    print(f"{source.name}: {source_mesh.triangles:,} tris, {source_mesh.vertices:,} verts")
    for result in results:
        share = (result.triangle_count / source_mesh.triangles * 100) if source_mesh.triangles else 0
        note = "" if result.triangle_count < source_mesh.triangles else "  (no reduction possible)"
        print(f"  {result.level:<5} {result.triangle_count:>8,} tris "
              f"({share:5.1f}% of source, target {result.ratio:.2f}) -> {result.path.name}{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
