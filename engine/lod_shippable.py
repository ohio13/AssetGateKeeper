#!/usr/bin/env python3
"""Shippable LODs: decimation that keeps UVs, normals and materials.

The preview decimator (``lod_manager``) clusters vertices on a grid. It is fast
and its triangle counts are exact enough to argue about cost, but it throws away
UVs, normals and material slots, so its levels cannot ship. This module is the
second engine: a quadric-error edge collapser that only ever collapses a vertex
*onto one of its neighbours* (a half-edge collapse). Every vertex that survives
is an original vertex, so it keeps its exact UV, normal and material - nothing
is re-interpolated, nothing slides across a texture.

What it does, in the terms a technical artist would check:

* **UV seams, hard edges and material borders are locked.** The mesh is read
  the way the engine draws it: a vertex is split wherever its UV or normal
  changes, and every material slot is its own set of vertices. An edge used by
  one triangle of that split mesh is a seam, a material border or an open edge;
  its vertices never move, so the texture cannot tear and two materials cannot
  pull apart. The cost: a mesh that is mostly seams cannot get as low, and the
  level then says how far it got.
* **Quadric error** (Garland and Heckbert) picks the cheapest collapse first:
  flat areas go before silhouettes and bevels.
* **No flips, no slivers, no non-manifold edges.** A collapse that would turn a
  face over (or tilt it more than 60 degrees), leave a zero-area triangle, or
  join two surfaces that only touch at a corner is refused.
* **Geometric error is measured.** Each level records the largest quadric error
  it accepted, as a distance in metres (``error_m``) and as a fraction of the
  mesh's size.
* **One pass for the ladder.** LOD1, LOD2... are snapshots of the same
  collapse sequence, so each level is contained in the one above it.

Output is a ``.glb`` with the source's materials and textures embedded, placed
in world space (node transforms applied), which Unreal, Unity and Blender import
as-is.

    python engine/lod_shippable.py model.gltf --out-dir out --ratios 0.5,0.25,0.125
"""

from __future__ import annotations

import argparse
import heapq
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LEVEL_NAMES = ("LOD1", "LOD2", "LOD3", "LOD4")


def lod_file_name(stem: str, level: str, suffix: str) -> str:
    """<name>_LOD<n><suffix>, as lod_manager.lod_file_name names a preview LOD."""
    return f"{stem}_{level}{suffix}"
DEFAULT_RATIOS = (0.5, 0.25, 0.125)
#: A collapse may tilt a neighbouring face by at most this much.
MAX_FACE_TURN_DEGREES = 60.0
SUPPORTED_SUFFIXES = {".glb", ".gltf", ".obj"}


class ShippableLodError(Exception):
    """The source cannot be decimated (unreadable, empty, unsupported)."""


@dataclass
class Part:
    """One material slot of the source, as the engine draws it (split vertices)."""
    positions: list            # [(x, y, z)], world space
    faces: list                # [[a, b, c]] indices into positions
    geometry: object           # the trimesh geometry it came from (visual, normals)


@dataclass
class ShippableLod:
    level: str
    path: Path
    ratio: float
    target_triangles: int
    triangle_count: int
    vertex_count: int
    error_m: float
    error_ratio: float
    reached: bool
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"level": self.level, "path": str(self.path), "ratio": self.ratio,
                "target_triangles": self.target_triangles,
                "triangle_count": self.triangle_count, "vertex_count": self.vertex_count,
                "error_m": self.error_m, "error_ratio": self.error_ratio,
                "reached": self.reached, "notes": list(self.notes)}


def load_parts(path: Path, resources: Optional[Path] = None) -> list[Part]:
    """Every drawable mesh of the file, one Part per material slot, world space.

    ``resources`` is where its textures and buffers are, when not beside it (the
    Art Workstation keeps an FBX's converted preview in a subfolder).
    """
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ShippableLodError(f"{path.suffix or 'this file'} is not supported "
                                f"(use {', '.join(sorted(SUPPORTED_SUFFIXES))})")
    try:
        import trimesh
        # load_scene, not load(force="scene"): trimesh marks the latter as a
        # compatibility wrapper (9 Nov 2026 audit, B7).
        load = getattr(trimesh, "load_scene", None) or (
            lambda *a, **k: trimesh.load(*a, force="scene", **k))
        if resources is not None:
            resolver = trimesh.resolvers.FilePathResolver(str(Path(resources) / "x"))
            with open(path, "rb") as handle:
                scene = load(handle, file_type=path.suffix.lower().lstrip("."),
                             resolver=resolver, process=False)
        else:
            # The gate's loader: an .obj with "-1.#QNAN0" normals (a game rip) reads.
            import asset_gatekeeper as agk
            scene = agk.load_trimesh_scene(str(path), process=False)
        geometries = scene.dump(concatenate=False)
    except Exception as exc:      # trimesh raises many types for broken files
        raise ShippableLodError(f"could not read {path.name}: {exc}") from exc
    parts = []
    for geometry in geometries:
        faces = getattr(geometry, "faces", None)
        if faces is None or len(faces) == 0:
            continue                       # points, lines: nothing to decimate
        parts.append(Part([tuple(map(float, v)) for v in geometry.vertices],
                          [list(map(int, f)) for f in faces], geometry))
    if not parts:
        raise ShippableLodError(f"{path.name} has no triangles")
    return parts


# - quadrics: 10 coefficients of sum((a x + b y + c z + d)^2) - #

def _plane_quadric(p0, p1, p2) -> Optional[tuple]:
    ux, uy, uz = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
    vx, vy, vz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length == 0.0:
        return None
    area = length / 2.0
    a, b, c = nx / length, ny / length, nz / length
    d = -(a * p0[0] + b * p0[1] + c * p0[2])
    # Area-weighted, so a big flat face resists more than a sliver. The 11th
    # slot is the area itself: error / area is a squared distance in metres.
    return (area * a * a, area * a * b, area * a * c, area * a * d, area * b * b,
            area * b * c, area * b * d, area * c * c, area * c * d, area * d * d, area)


def _quadric_error(q, p) -> float:
    x, y, z = p
    return max(0.0, q[0] * x * x + 2 * q[1] * x * y + 2 * q[2] * x * z + 2 * q[3] * x
               + q[4] * y * y + 2 * q[5] * y * z + 2 * q[6] * y + q[7] * z * z
               + 2 * q[8] * z + q[9])


def _normal(p0, p1, p2):
    ux, uy, uz = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
    vx, vy, vz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
    return (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)


class Collapser:
    """Half-edge collapses over the split mesh, cheapest first."""

    def __init__(self, parts: Sequence[Part],
                 tick: Optional[Callable[[float], None]] = None) -> None:
        # ``tick(fraction)`` is called through the set-up, which is a quarter of
        # the run on a sculpt (35 s for the test sculpt): long enough to need a bar and a
        # chance to cancel (it may raise to stop).
        self.positions: list = []
        self.faces: list = []
        self.part_of_vertex: list[int] = []
        for index, part in enumerate(parts):
            offset = len(self.positions)
            self.positions.extend(part.positions)
            self.part_of_vertex.extend([index] * len(part.positions))
            self.faces.extend([[a + offset, b + offset, c + offset] for a, b, c in part.faces])
        count = len(self.positions)
        self.alive = [True] * len(self.faces)
        self.live_faces = 0
        self.vertex_faces: list[set] = [set() for _ in range(count)]
        self.quadrics = [[0.0] * 11 for _ in range(count)]
        edge_use: dict = {}
        every = max(1, len(self.faces) // 50)
        for face_id, (a, b, c) in enumerate(self.faces):
            if tick is not None and face_id % every == 0:
                tick(0.7 * face_id / max(len(self.faces), 1))
            if a == b or b == c or a == c:
                self.alive[face_id] = False      # degenerate in the source: dropped
                continue
            self.live_faces += 1
            for vertex in (a, b, c):
                self.vertex_faces[vertex].add(face_id)
            quadric = _plane_quadric(self.positions[a], self.positions[b], self.positions[c])
            if quadric is not None:
                for vertex in (a, b, c):
                    target = self.quadrics[vertex]
                    for k in range(11):
                        target[k] += quadric[k]
            for edge in ((a, b), (b, c), (c, a)):
                key = (edge[0], edge[1]) if edge[0] < edge[1] else (edge[1], edge[0])
                edge_use[key] = edge_use.get(key, 0) + 1
        # Seams, hard edges, material borders and open edges: used by one face.
        self.locked = [False] * count
        for (a, b), uses in edge_use.items():
            if uses != 2:
                self.locked[a] = self.locked[b] = True
        lows = [min(p[i] for p in self.positions) for i in range(3)]
        highs = [max(p[i] for p in self.positions) for i in range(3)]
        self.diagonal = math.dist(lows, highs) or 1.0
        self.min_area = (self.diagonal * 1e-7) ** 2
        self.cos_turn = math.cos(math.radians(MAX_FACE_TURN_DEGREES))
        self.max_error = 0.0
        self.stamp = [0] * count
        self.heap: list = []
        every = max(1, count // 20)
        for vertex in range(count):
            if tick is not None and vertex % every == 0:
                tick(0.7 + 0.3 * vertex / max(count, 1))
            self._push(vertex)

    def neighbours(self, vertex: int) -> set:
        found = set()
        for face_id in self.vertex_faces[vertex]:
            found.update(self.faces[face_id])
        found.discard(vertex)
        return found

    def _best_target(self, u: int, validate: bool = False):
        if self.locked[u] or not self.vertex_faces[u]:
            return None
        qu = self.quadrics[u]
        options = []
        for v in self.neighbours(u):
            qv = self.quadrics[v]
            q = [qu[k] + qv[k] for k in range(11)]
            cost = _quadric_error(q, self.positions[v])
            options.append((cost, v, cost / q[10] if q[10] > 0 else 0.0))
        options.sort()
        if not validate:
            # Checked when it reaches the top of the heap: most entries are
            # replaced before then, and checking them all is most of the run time.
            return options[0] if options else None
        for cost, v, distance2 in options:
            if self._valid(u, v):
                return cost, v, distance2
        return None

    def _push(self, u: int, validate: bool = False) -> None:
        self.stamp[u] += 1
        best = self._best_target(u, validate)
        if best is not None:
            heapq.heappush(self.heap, (best[0], u, best[1], self.stamp[u], best[2]))

    def _valid(self, u: int, v: int) -> bool:
        shared = [face_id for face_id in self.vertex_faces[u] if v in self.faces[face_id]]
        if not shared:
            return False
        # Link condition: u and v may share only the vertices opposite their
        # shared faces, or the collapse pinches the surface into a non-manifold edge.
        if len(self.neighbours(u) & self.neighbours(v)) != len(shared):
            return False
        target = self.positions[v]
        for face_id in self.vertex_faces[u]:
            if face_id in shared:
                continue
            corners = self.faces[face_id]
            positions = self.positions
            p0, p1, p2 = positions[corners[0]], positions[corners[1]], positions[corners[2]]
            bx, by, bz = _normal(p0, p1, p2)
            q0 = target if corners[0] == u else p0
            q1 = target if corners[1] == u else p1
            q2 = target if corners[2] == u else p2
            ax, ay, az = _normal(q0, q1, q2)
            length_after = math.sqrt(ax * ax + ay * ay + az * az)
            if length_after / 2.0 < self.min_area:
                return False
            length_before = math.sqrt(bx * bx + by * by + bz * bz) or 1.0
            if (bx * ax + by * ay + bz * az) < self.cos_turn * length_before * length_after:
                return False
        return True

    def run(self, target_faces: int, deadline: Optional[Callable[[], None]] = None) -> None:
        """Collapse until at most ``target_faces`` triangles remain (or none can go)."""
        steps = 0
        while self.live_faces > target_faces and self.heap:
            cost, u, v, stamp, distance2 = heapq.heappop(self.heap)
            if stamp != self.stamp[u] or not self.vertex_faces[u]:
                continue                          # an old entry: u changed since
            if not self._valid(u, v):
                self._push(u, validate=True)      # the next neighbour that is safe
                continue
            self._collapse(u, v)
            self.max_error = max(self.max_error, distance2)
            steps += 1
            if deadline is not None and steps % 2000 == 0:
                deadline()

    def _collapse(self, u: int, v: int) -> None:
        touched = self.neighbours(u) | self.neighbours(v)
        for face_id in list(self.vertex_faces[u]):
            corners = self.faces[face_id]
            if v in corners:
                self.alive[face_id] = False
                self.live_faces -= 1
                for corner in corners:
                    self.vertex_faces[corner].discard(face_id)
            else:
                corners[corners.index(u)] = v
                self.vertex_faces[v].add(face_id)
        self.vertex_faces[u].clear()
        qu, qv = self.quadrics[u], self.quadrics[v]
        for k in range(11):
            qv[k] += qu[k]
        self.stamp[u] += 1
        for vertex in touched | {v}:
            if vertex != u:
                self._push(vertex)

    def snapshot(self) -> list[tuple[int, list, list]]:
        """[(part index, kept original vertex indices, faces in those)] per part."""
        by_part: dict[int, list] = {}
        for face_id, alive in enumerate(self.alive):
            if alive:
                corners = self.faces[face_id]
                by_part.setdefault(self.part_of_vertex[corners[0]], []).append(corners)
        result = []
        for part, faces in sorted(by_part.items()):
            used = sorted({vertex for face in faces for vertex in face})
            remap = {vertex: index for index, vertex in enumerate(used)}
            result.append((part, used, [[remap[a], remap[b], remap[c]] for a, b, c in faces]))
        return result

    def error_m(self) -> float:
        # Area-weighted quadric over its area: an RMS distance to the planes
        # the collapsed vertices stood on, in the mesh's units (metres).
        return math.sqrt(self.max_error) if self.max_error > 0 else 0.0


def _export(path: Path, parts: Sequence[Part], snapshot, offsets: Sequence[int]) -> int:
    import numpy as np
    import trimesh
    scene = trimesh.Scene()
    vertices = 0
    for part_index, used, faces in snapshot:
        part = parts[part_index]
        local = np.asarray(used, dtype=np.int64) - offsets[part_index]
        geometry = part.geometry
        kwargs = {}
        normals = getattr(geometry, "vertex_normals", None)
        if normals is not None and len(normals) == len(geometry.vertices):
            kwargs["vertex_normals"] = np.asarray(normals)[local]
        visual = geometry.visual
        uv = getattr(visual, "uv", None)
        if uv is not None and len(uv) == len(geometry.vertices):
            new_visual = trimesh.visual.TextureVisuals(uv=np.asarray(uv)[local],
                                                       material=visual.material)
        elif getattr(visual, "material", None) is not None:
            new_visual = trimesh.visual.TextureVisuals(material=visual.material)
        else:
            new_visual = None
        mesh = trimesh.Trimesh(vertices=np.asarray(geometry.vertices)[local],
                               faces=np.asarray(faces, dtype=np.int64),
                               visual=new_visual, process=False, **kwargs)
        scene.add_geometry(mesh, geom_name=f"part{part_index}")
        vertices += len(local)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(scene.export(file_type="glb"))
    return vertices


def plan(source_triangles: int, ratios: Sequence[float],
         max_triangles: Sequence[int] | None = None) -> list[tuple[str, int, float]]:
    targets = []
    caps = list(max_triangles or [])
    for index, ratio in enumerate(list(ratios)[:len(LEVEL_NAMES)]):
        if not 0 < ratio < 1:
            raise ShippableLodError(f"ratio must be between 0 and 1, got {ratio}")
        target = int(round(source_triangles * ratio))
        if index < len(caps) and caps[index]:
            target = min(target, int(caps[index]))
        targets.append((LEVEL_NAMES[index], max(1, target), float(ratio)))
    if any(later[1] > earlier[1] for earlier, later in zip(targets, targets[1:])):
        raise ShippableLodError("each level must be smaller than the one above it")
    return targets


#: How a run divides, measured on the test sculpt (1.08 M triangles) on the developer's PC: reading
#: 9 s, set-up 27 s, collapsing 2.3 min to 3 %, writing a few seconds. Used to
#: turn each phase's own progress into one bar.
PHASES = (("reading the model", 0.05), ("preparing", 0.15), ("reducing", 0.77),
          ("writing the files", 0.03))


def generate(source: Path, out_dir: Path, ratios: Sequence[float] = DEFAULT_RATIOS,
             max_triangles: Sequence[int] | None = None, name: str = "",
             progress: Optional[Callable[[str, int, int], None]] = None,
             resources: Optional[Path] = None,
             report: Optional[Callable[[float, str], None]] = None) -> list[ShippableLod]:
    """Decimate ``source`` into shippable ``.glb`` levels under ``out_dir``.

    ``progress(level, index, total)`` names the level being reduced;
    ``report(fraction, detail)`` (0..1 over the whole run) is called often
    enough to drive a bar - every 2,000 collapses - and may raise to cancel."""
    starts, at = {}, 0.0
    for phase, share in PHASES:
        starts[phase] = (at, share)
        at += share

    def say(phase: str, within: float, detail: str = "") -> None:
        if report is not None:
            begin, share = starts[phase]
            report(min(1.0, begin + share * max(0.0, min(1.0, within))), detail or phase)

    say("reading the model", 0.0)
    parts = load_parts(source, resources)
    say("preparing", 0.0)
    collapser = Collapser(parts, tick=lambda f: say("preparing", f))
    offsets, total = [], 0
    for part in parts:
        offsets.append(total)
        total += len(part.positions)
    source_triangles = collapser.live_faces
    if source_triangles == 0:
        raise ShippableLodError(f"{source.name} has no triangles")
    targets = plan(source_triangles, ratios, max_triangles)
    stem = Path(name or source.name).stem
    levels: list[tuple[str, int, float, list, float, int]] = []
    final = min(target for _, target, _ in targets)
    to_remove = max(source_triangles - final, 1)
    for index, (level, target, ratio) in enumerate(targets):
        if progress is not None:
            progress(level, index, len(targets))

        def tick(_level=level, _target=target) -> None:
            say("reducing", (source_triangles - collapser.live_faces) / to_remove,
                f"{_level}: {collapser.live_faces:,} triangles, going to {_target:,}")
        tick()
        collapser.run(target, tick)
        levels.append((level, target, ratio, collapser.snapshot(), collapser.error_m(),
                       collapser.live_faces))
    results = []
    for number, (level, target, ratio, snapshot, error, triangles) in enumerate(levels):
        say("writing the files", number / max(len(levels), 1), f"writing {level}")
        path = out_dir / lod_file_name(stem, level, ".glb")
        vertices = _export(path, parts, snapshot, offsets)
        reached = triangles <= target * 1.02
        notes = [] if reached else [
            f"stopped at {triangles:,} tris (target {target:,}): the rest are UV seams, "
            "hard edges or material borders, which are kept so the texture cannot tear"]
        results.append(ShippableLod(level, path, ratio, target, triangles, vertices,
                                    round(error, 6), round(error / collapser.diagonal, 6),
                                    reached, notes))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("lods"))
    parser.add_argument("--ratios", default="0.5,0.25,0.125",
                        help="fractions of the source triangle count, largest first")
    parser.add_argument("--max-tris", default="", help="hard caps per level, e.g. 20000,5000")
    args = parser.parse_args(argv)
    try:
        ratios = [float(item) for item in args.ratios.split(",") if item.strip()]
        caps = [int(item) for item in args.max_tris.split(",") if item.strip()]
        for lod in generate(args.source, args.out_dir, ratios, caps or None):
            flag = "" if lod.reached else "  (" + "; ".join(lod.notes) + ")"
            print(f"{lod.level}: {lod.triangle_count:,} tris (target {lod.target_triangles:,}), "
                  f"error {lod.error_m * 1000:.2f} mm  -> {lod.path}{flag}")
    except (ShippableLodError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
