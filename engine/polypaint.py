#!/usr/bin/env python3
"""ZBrush Polypaint in .obj files: read it, preview it, bake it into a game asset.

ZBrush writes Polypaint into an .obj as comment lines, ``#MRGB MMRRGGBB...``, one
8-digit group per vertex in vertex order (mask, red, green, blue). Houdini and
Blender 3.3+ read them; Maya, 3ds Max and three.js skip them as comments, so the
same sculpt shows in colour in one program and grey in the next (9 Nov 2026).
Some exporters write colours on the vertex line instead, ``v x y z r g b``;
both are read here.

Vertex colours are not how a game asset carries colour: an engine needs a
vertex-colour material to show them, and when the mesh is reduced from a
million triangles to thirty thousand most of the colour goes with the
vertices. ``bake`` makes the usual game asset instead:

1. reduce the mesh to a triangle budget (quadric error, fast-simplification);
2. unwrap UVs (xatlas), packed to the texture size with a few texels of gap;
   from 512 px up, regions of fine colour detail (eyes, brows, lips) get up
   to twice the texels along each side;
3. bake the sculpt onto the reduced mesh, on a grid finer than the texture
   (2x2 samples a texel at 2048, up to 8x8 at 64 px): the sculpt's own
   triangles are drawn into UV space on the reduced triangles nearest them,
   the outermost wins where they overlap (as a ray from a cage would see it),
   and colours are blended across each sculpt triangle. Samples nothing
   reached are projected along the reduced mesh's normal, or to the closest
   point. (10 Nov 2026: the old nearest-four-vertices average turned the test sculpt's
   floating eyebrow shells into hollow outlines and lost the eyeliner.)
4. a tangent-space normal map from the same samples, in the glTF/OpenGL
   convention (green up) with MikkTSpace-style tangents written into the file;
5. write a .glb (both textures inside, ready for Unreal, Unity, Godot or
   Blender) and the textures as .png files beside it.

``preset="n64"`` makes a Nintendo 64 style asset instead: about 1,200
triangles, a 64 px texture in 16 colours (a 4-bit texture that fits the N64's
4 KB texture memory), no normal map, nearest-texel filtering.

Steps 1-3 need ``pip install "asset-gatekeeper[bake]"`` (fast-simplification,
xatlas, scipy). Reading and previewing need only numpy and trimesh, which the
gate already requires. The source file is only ever read.

    python engine/polypaint.py SM_Hero_HP_03.obj --triangles 30000 --texture 2048
    python engine/polypaint.py SM_Hero_HP_03.obj --preset n64
"""

from __future__ import annotations

import argparse
import io
import json
import os
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import asset_gatekeeper as agk  # noqa: E402 - shared detection, so the gate and this agree

#: The exact command for the Python that is running this, so a Start AssetGatekeeper.bat user
#: installs into the interpreter the servers use, not some other one on PATH.
BAKE_EXTRA = f'"{sys.executable}" -m pip install fast-simplification xatlas scipy'


def install_hint() -> str:
    """How this user installs the bake libraries: the double-click file in a
    downloaded Windows copy (it picks the same Python as Start AssetGatekeeper.bat), else the
    exact pip command for the running Python."""
    if os.name == "nt" and (REPO_ROOT / "Install_Extras.bat").is_file():
        return (f"double-click Install_Extras.bat in {REPO_ROOT}, "
                f"or run in a Command Prompt: {BAKE_EXTRA}")
    return BAKE_EXTRA
DEFAULT_TRIANGLES = 30_000
DEFAULT_TEXTURE = 2048
#: Bake presets: what each one sets unless the caller says otherwise.
#: n64: the console's 4 KB texture memory holds a 64x64 texture only at 4 bits
#: a texel (16 colours); it has no normal mapping; a character is ~1,000 tris.
#: The n64 preset also gives the face a texture of its own (a second material):
#: in one 64 px texture a whole character's face was about 6 x 6 texels.
PRESETS: dict[str, dict] = {
    "n64": {"label": "Nintendo 64", "triangles": 1_200, "texture": 64,
            "normal_map": False, "colours": 16, "face_texture": True},
}
#: Colour counts a texture can be reduced to (0 keeps every colour).
COLOUR_CHOICES = (0, 16, 32, 64, 256)
#: Sculpt triangles that meet at a vertex, at most, considered when projecting.
#: A ZBrush mesh has 4-8; poles more. Extra ones are skipped (the neighbours'
#: triangles still cover them).
MAX_VALENCE = 16
#: Samples projected at once, and kd-tree queries per batch: memory stays at a
#: few hundred MB whatever the sculpt or texture size.
SAMPLE_BATCH = 1 << 18
QUERY_BATCH = 1 << 15
#: A reduced triangle whose nearest sculpt colours spread this much (RMS, 0-1
#: per channel) holds fine detail; such regions get up to DETAIL_BOOST times the
#: texels along each side (never more than DETAIL_SHARE of the texture).
DETAIL_SPREAD = 0.2
DETAIL_BOOST = 2.0
DETAIL_SHARE = 0.4
DETAIL_MIN_TEXTURE = 512
#: At this size and below the .glb asks for nearest-texel filtering: islands are
#: a texel or two wide and smooth filtering would blend in the next island.
NEAREST_TEXTURE = 128
#: Face texture (second material): the region around the sculpt's densest colour
#: detail (eyes, brows, lips) that gets a texture of its own: colours counted in
#: small cells, summed on a FACE_GRID-cell grid over the model (_face_centre);
#: the face texture is that face seen from the front (_front_projection), out
#: to FACE_REACH times the detail's reach. FACE_SHARE of the reduced surface
#: nearest the centre (about a head on a full figure: the rule of nines gives
#: the head 9%) is the region unwrapped instead when the face has no front.
#: A sculpt edge whose two ends differ by FACE_EDGE_CONTRAST (0-1, largest
#: channel) is a detail edge; with fewer than FACE_MIN_EDGES there is no face.
FACE_EDGE_CONTRAST = 0.25
FACE_GRID = 48
#: Colours are counted in cells this many across the model (a test figure
#: 12 units tall: 3 cm cells on a 1.7 m person), or 2.5 typical edges wide if
#: that is larger, with each channel in FACE_COLOUR_LEVELS steps.
FACE_FINE_GRID = 400
FACE_COLOUR_LEVELS = 4
FACE_SHARE = 0.10
#: Fewer detail edges than this: no face to speak of, so one texture.
FACE_MIN_EDGES = 200
#: Flat normal (0, 0, 1) as stored in a normal map.
FLAT_NORMAL = (0.5, 0.5, 1.0)


class PolypaintError(Exception):
    """The file has no colours to bake, cannot be read, or a dependency is missing."""


@dataclass
class ObjColours:
    vertices: "object"           # numpy (n, 3) float64
    faces: "object"              # numpy (m, 3) int64, zero-based
    colours: "object"            # numpy (n, 3) float32, 0..1
    source: str                  # "polypaint" (#MRGB) or "vertex colours" (v x y z r g b)


def colour_source(path: Path) -> str:
    """'polypaint', 'vertex colours' or '' - cheap, shared with the gate."""
    return agk.obj_colour_source(path)


def read_obj(path: Path) -> ObjColours:
    """Positions, triangles and one colour per vertex from an .obj."""
    import numpy as np
    vertices: list = []
    inline: list = []
    faces: list = []
    painted: list = []
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise PolypaintError(f"cannot read {path.name}: {exc}") from exc
    with handle:
        for line in handle:
            if line.startswith("v "):
                parts = line.split()
                vertices.append(parts[1:4])
                if len(parts) >= 7:
                    inline.append(parts[4:7])
            elif line.startswith("f "):
                count = len(vertices)
                corners = []
                for token in line.split()[1:]:
                    index = int(token.split("/", 1)[0])
                    corners.append(index - 1 if index > 0 else count + index)
                for k in range(1, len(corners) - 1):
                    faces.append((corners[0], corners[k], corners[k + 1]))
            elif line.startswith("#MRGB "):
                block = line[6:].strip()
                for start in range(0, len(block) - 7, 8):
                    group = block[start:start + 8]
                    painted.append((int(group[2:4], 16), int(group[4:6], 16),
                                    int(group[6:8], 16)))
    if not vertices or not faces:
        raise PolypaintError(f"{path.name} has no triangles")
    positions = np.asarray(vertices, dtype=np.float64)
    triangles = np.asarray(faces, dtype=np.int64)
    if len(painted) >= len(positions):
        colours = np.asarray(painted[:len(positions)], dtype=np.float32) / 255.0
        source = "polypaint"
    elif len(inline) == len(positions):
        colours = np.asarray(inline, dtype=np.float32)
        if colours.max() > 1.0:                       # some writers use 0..255
            colours = colours / 255.0
        source = "vertex colours"
    else:
        raise PolypaintError(f"{path.name} has no Polypaint or vertex colours to use")
    return ObjColours(positions, triangles, colours.clip(0.0, 1.0), source)


def write_preview_glb(obj: ObjColours, target: Path) -> None:
    """The mesh with its vertex colours as a .glb, for the review page's 3D view."""
    import numpy as np
    import trimesh
    rgba = np.concatenate([(obj.colours * 255).round().astype(np.uint8),
                           np.full((len(obj.colours), 1), 255, np.uint8)], axis=1)
    mesh = trimesh.Trimesh(vertices=obj.vertices, faces=obj.faces, vertex_colors=rgba,
                           process=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(target, file_type="glb")


def output_names(source: Path) -> tuple[str, str]:
    """(mesh file, texture file): SM_Hero_HP_03.obj -> SM_Hero_LP_03.glb and
    T_Hero_LP_03_BaseColor.png. Names follow the Unreal convention the gate's
    default naming rules accept."""
    stem = source.stem
    parts = stem.split("_")
    if "HP" in parts:
        parts[parts.index("HP")] = "LP"
        low = "_".join(parts)
    elif stem.lower().endswith("_high"):
        low = stem[:-5] + "_low"
    else:
        low = f"{stem}_LP"
    base = low[3:] if low.upper().startswith("SM_") else low
    return f"{low}.glb", f"T_{base}_BaseColor.png"


def normal_map_name(source: Path) -> str:
    """T_Hero_LP_03_Normal.png for SM_Hero_HP_03.obj, beside the BaseColor."""
    return output_names(source)[1][:-len("_BaseColor.png")] + "_Normal.png"


def face_texture_names(source: Path) -> tuple[str, str]:
    """(BaseColor, Normal) of the face's own material: T_Hero_LP_03_Face_BaseColor.png
    and T_Hero_LP_03_Face_Normal.png."""
    base = output_names(source)[1][:-len("_BaseColor.png")] + "_Face"
    return f"{base}_BaseColor.png", f"{base}_Normal.png"


def resolve_settings(preset: str = "", triangles: int = 0, texture: int = 0,
                     normal_map: Optional[bool] = None, colours: Optional[int] = None,
                     max_triangles: int = 0, max_texture: int = 0,
                     face_texture: Optional[bool] = None, max_materials: int = 0) -> dict:
    """What a bake will use: explicit values, else the preset's, else the defaults,
    and never more than ``max_triangles`` / ``max_texture`` when those are given
    (a folder's rules). A face texture is a second material, so ``max_materials``
    below 2 turns it off (``face_texture_note`` says why). Shared by the command
    line and the review page."""
    if preset and preset not in PRESETS:
        raise PolypaintError(f"unknown bake preset '{preset}' (choose: {', '.join(PRESETS)})")
    chosen = PRESETS.get(preset, {})
    settings = {
        "preset": preset,
        "triangles": int(triangles or chosen.get("triangles", DEFAULT_TRIANGLES)),
        "texture": int(texture or chosen.get("texture", DEFAULT_TEXTURE)),
        "normal_map": chosen.get("normal_map", True) if normal_map is None else bool(normal_map),
        "colours": int(chosen.get("colours", 0) if colours is None else colours),
        "face_texture": (bool(chosen.get("face_texture", False)) if face_texture is None
                         else bool(face_texture)),
        "face_texture_note": "",
    }
    if settings["face_texture"] and 0 < max_materials < 2:
        settings["face_texture"] = False
        settings["face_texture_note"] = (
            f"no separate face texture: the rules here allow {max_materials} material slot, "
            "and the face texture is a second one")
    if preset and max_triangles and not triangles:
        settings["triangles"] = min(settings["triangles"], max_triangles)
    if preset and max_texture and not texture:
        edge = settings["texture"]
        while edge > max_texture and edge > 16:
            edge //= 2
        settings["texture"] = edge
    return settings


def _require_bake_libraries():
    missing = []
    modules = {}
    for module, package in (("fast_simplification", "fast-simplification"),
                            ("xatlas", "xatlas"), ("scipy.spatial", "scipy")):
        try:
            modules[module] = __import__(module, fromlist=["_"])
        except ImportError:
            missing.append(package)
        except Exception as exc:          # installed but broken on this Python (3.9)
            raise PolypaintError(
                f"{package} is installed but cannot load on Python "
                f"{sys.version_info[0]}.{sys.version_info[1]} ({exc.__class__.__name__}: {exc}); "
                "baking needs Python 3.10 or newer") from exc
    if missing:
        raise PolypaintError(f"baking needs {', '.join(missing)}, which are not installed. "
                             f"Install them with: {install_hint()}")
    return modules["fast_simplification"], modules["xatlas"], modules["scipy.spatial"]


def _weld(vertices, faces):
    """(positions, faces) with vertices closer than a ten-millionth of the model's
    size merged, and the triangles that collapse dropped."""
    import numpy as np
    span = float(np.max(vertices.max(axis=0) - vertices.min(axis=0))) if len(vertices) else 0.0
    step = span * 1e-7 or 1e-12
    keys = np.round(np.asarray(vertices, np.float64) / step).astype(np.int64)
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.ravel()
    welded = inverse[faces]
    keep = ((welded[:, 0] != welded[:, 1]) & (welded[:, 1] != welded[:, 2])
            & (welded[:, 0] != welded[:, 2]))
    return np.asarray(vertices, np.float64)[first], welded[keep]


def _drop_stacked(positions, faces):
    """``faces`` without triangles that lie exactly on another one.

    Reducing a thin part (an ear, a strand of hair against the cheek) can fold
    it flat into two triangles on the same three points, facing opposite ways.
    Their normals cancel, so the one facing out is shaded as if lit from
    behind: the test sculpt's N64 bake had a black triangle on each side of the head
    (12 Nov 2026). Such a fold has no inside, so both go; a plain copy facing
    the same way keeps one."""
    import numpy as np
    if len(faces) == 0:
        return faces
    corners = np.asarray(positions, np.float64)[faces]
    span = float(np.max(corners.reshape(-1, 3).max(0) - corners.reshape(-1, 3).min(0))) or 1.0
    keys = np.round(corners / (span * 1e-6)).astype(np.int64)      # (faces, 3 corners, xyz)
    # The same three points in any order: sort each triangle's corners.
    corner_order = np.lexsort((keys[:, :, 2], keys[:, :, 1], keys[:, :, 0]), axis=1)
    sorted_keys = np.take_along_axis(keys, corner_order[:, :, None], axis=1).reshape(len(faces), -1)
    _, group, counts = np.unique(sorted_keys, axis=0, return_inverse=True, return_counts=True)
    group = group.ravel()
    if counts.max() < 2:
        return faces
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    keep = np.ones(len(faces), bool)
    for index in np.nonzero(counts > 1)[0]:
        members = np.nonzero(group == index)[0]
        reference = normals[members[0]]
        facing = np.einsum("ij,j->i", normals[members], reference) > 0
        if facing.all():
            keep[members[1:]] = False        # copies: one is enough
        else:
            keep[members] = False            # a fold: nothing inside it
    return faces[keep]


#: Neighbouring triangles further apart than this keep a hard edge between
#: them in the baked mesh's shading (Blender's "auto smooth" does the same).
CREASE_DEGREES = 60.0


def _corner_normals(positions, faces, crease_degrees: float = CREASE_DEGREES):
    """A normal for each corner of each triangle, shape (faces, 3, 3): the
    area-weighted average of the triangles at that corner's vertex that face
    within ``crease_degrees`` of this one.

    One averaged normal per vertex (what the bake used before) goes wrong where
    a 1,200-triangle head folds sharply - hair over the cheek, the jaw against
    the neck: the average points away from some of the triangles that share it,
    and they shade as if lit from behind. On the test sculpt's N64 bake that was a corner
    of 4 triangles in 10 on the body, and of most of the face (12 Nov 2026)."""
    import numpy as np
    corners = positions[faces]
    weighted = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    length = np.linalg.norm(weighted, axis=1, keepdims=True)
    unit = weighted / np.where(length > 0, length, 1.0)
    limit = np.cos(np.radians(crease_degrees))
    # Corner k of face f is row 3f+k; group the rows by the vertex they sit on.
    vertex = faces.ravel()
    row = np.argsort(vertex, kind="stable")
    vertex = vertex[row]
    starts = np.flatnonzero(np.r_[True, vertex[1:] != vertex[:-1]])
    ends = np.r_[starts[1:], len(vertex)]
    result = np.empty((len(faces) * 3, 3))
    for start, end in zip(starts, ends):
        rows = row[start:end]
        here = rows // 3
        close = unit[here] @ unit[here].T >= limit
        result[rows] = close.astype(np.float64) @ weighted[here]
    length = np.linalg.norm(result, axis=1, keepdims=True)
    fallback = np.repeat(unit, 3, axis=0)
    result = np.where(length > 0, result / np.where(length > 0, length, 1.0), fallback)
    return result.reshape(len(faces), 3, 3)


def _split_by_normal(positions, uvs, faces, corner_normals):
    """(positions, uvs, normals, faces) with a vertex wherever its corners want
    different normals: a hard edge needs its own copy of each vertex on it."""
    import numpy as np
    vertex = faces.ravel()
    normals = corner_normals.reshape(-1, 3)
    keys = np.concatenate([vertex[:, None].astype(np.float64),
                           np.round(normals * 1e4)], axis=1)
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.ravel()
    source = vertex[first]
    return (positions[source], uvs[source], normals[first],
            inverse.reshape(faces.shape).astype(faces.dtype))


def _smooth_normals(positions, faces):
    import numpy as np
    corners = positions[faces]
    face_normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    normals = np.zeros_like(positions)
    for k in range(3):
        np.add.at(normals, faces[:, k], face_normals)
    length = np.linalg.norm(normals, axis=1, keepdims=True)
    return normals / np.where(length > 0, length, 1.0)


def _normalise(vectors):
    import numpy as np
    length = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.where(length > 1e-20, length, 1.0)


def _dot(a, b):
    return (a * b).sum(axis=-1)


def _tangents(positions, uvs, faces, normals):
    """Per-vertex tangents (n, 4) for a mesh already split at its UV seams.

    MikkTSpace's recipe: each triangle's tangent from its UV gradient, projected
    onto each corner's normal plane and weighted by the corner's angle, summed
    per vertex; w is the bitangent's handedness. ``uvs`` have v going up (as
    xatlas gives them), so the bitangent cross(N, T) * w points up the image:
    the glTF / OpenGL "green up" convention (checked against Khronos'
    NormalTangentMirrorTest)."""
    import numpy as np
    p0, p1, p2 = (positions[faces[:, k]] for k in range(3))
    t0, t1, t2 = (uvs[faces[:, k]] for k in range(3))
    e1, e2 = p1 - p0, p2 - p0
    d1, d2 = t1 - t0, t2 - t0
    det = d1[:, 0] * d2[:, 1] - d2[:, 0] * d1[:, 1]
    safe = np.where(np.abs(det) > 1e-20, det, 1.0)[:, None]
    sdir = (e1 * d2[:, 1:2] - e2 * d1[:, 1:2]) / safe
    tdir = (e2 * d1[:, 0:1] - e1 * d2[:, 0:1]) / safe
    usable = np.abs(det) > 1e-20
    sdir[~usable] = 0.0
    tdir[~usable] = 0.0
    tangent = np.zeros_like(positions)
    bitangent = np.zeros_like(positions)
    corners = (p0, p1, p2)
    for k in range(3):
        here, after, before = corners[k], corners[(k + 1) % 3], corners[(k + 2) % 3]
        u, v = _normalise(after - here), _normalise(before - here)
        angle = np.arccos(np.clip(_dot(u, v), -1.0, 1.0))[:, None]
        n = normals[faces[:, k]]
        projected = _normalise(sdir - n * _dot(n, sdir)[:, None])
        np.add.at(tangent, faces[:, k], projected * angle)
        np.add.at(bitangent, faces[:, k], _normalise(tdir) * angle)
    tangent = tangent - normals * _dot(normals, tangent)[:, None]
    length = np.linalg.norm(tangent, axis=1)
    # A vertex with no usable UV gradient still needs a tangent: any one at
    # right angles to its normal.
    fallback = np.cross(normals, np.where(np.abs(normals[:, :1]) < 0.9,
                                          [[1.0, 0.0, 0.0]], [[0.0, 1.0, 0.0]]))
    tangent = np.where((length > 1e-12)[:, None], tangent, fallback)
    tangent = _normalise(tangent)
    handed = np.where(_dot(np.cross(normals, tangent), bitangent) < 0.0, -1.0, 1.0)
    return np.concatenate([tangent, handed[:, None]], axis=1)


def _closest_on_triangles(a, ab, ac):
    """Barycentric (v, w) of the point of each triangle closest to the origin.

    ``a`` is the first corner relative to the query point, ``ab``/``ac`` the
    edges; any leading shape. Ericson, Real-Time Collision Detection, 5.1.5."""
    import numpy as np
    with np.errstate(divide="ignore", invalid="ignore"):
        d1, d2 = -_dot(ab, a), -_dot(ac, a)
        b, c = a + ab, a + ac
        d3, d4 = -_dot(ab, b), -_dot(ac, b)
        d5, d6 = -_dot(ab, c), -_dot(ac, c)
        va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
        denom = va + vb + vc
        v, w = vb / denom, vc / denom                      # inside the triangle
        edge = (d4 - d3) / ((d4 - d3) + (d5 - d6))         # on BC
        region = (va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0)
        v, w = np.where(region, 1 - edge, v), np.where(region, edge, w)
        edge = d2 / (d2 - d6)                              # on AC
        region = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        v, w = np.where(region, 0.0, v), np.where(region, edge, w)
        region = (d6 >= 0) & (d5 <= d6)                    # corner C
        v, w = np.where(region, 0.0, v), np.where(region, 1.0, w)
        edge = d1 / (d1 - d3)                              # on AB
        region = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        v, w = np.where(region, edge, v), np.where(region, 0.0, w)
        region = (d3 >= 0) & (d4 <= d3)                    # corner B
        v, w = np.where(region, 1.0, v), np.where(region, 0.0, w)
        region = (d1 <= 0) & (d2 <= 0)                     # corner A
        v, w = np.where(region, 0.0, v), np.where(region, 0.0, w)
    return v, w


class Sculpt:
    """The high-poly surface, ready to be projected onto."""

    def __init__(self, vertices, faces, colours, spatial):
        import numpy as np
        self.faces = faces
        self.colours = colours.astype(np.float32)
        self.tree = spatial.cKDTree(vertices)
        corners = vertices[faces]
        self.a = corners[:, 0]
        self.ab = corners[:, 1] - corners[:, 0]
        self.ac = corners[:, 2] - corners[:, 0]
        del corners
        self.face_normals = _normalise(np.cross(self.ab, self.ac)).astype(np.float32)
        self.normals = _smooth_normals(vertices, faces).astype(np.float32)
        lengths = np.linalg.norm(self.ab, axis=1)
        self.edge = float(np.median(lengths)) if len(lengths) else 1.0
        # The triangles around each vertex, as a (vertices, MAX_VALENCE) table, -1 empty.
        flat = faces.ravel()
        order = np.argsort(flat, kind="stable")
        owner = flat[order]
        counts = np.bincount(owner, minlength=len(vertices))
        starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
        rank = np.arange(len(order)) - starts[owner]
        width = int(min(MAX_VALENCE, max(1, counts.max())))
        keep = rank < width
        self.around = np.full((len(vertices), width), -1, np.int32)
        self.around[owner[keep], rank[keep]] = (order[keep] // 3).astype(np.int32)

    def closest(self, points):
        """(face, barycentric (n, 3), candidates) of the closest sculpt point to each
        point: the nearest vertex's triangles are tried, which on a dense sculpt
        hold the closest point (or one a fraction of a triangle from it)."""
        import numpy as np
        count = len(points)
        face = np.empty(count, np.int64)
        bary = np.empty((count, 3), np.float64)
        candidates = np.empty((count, self.around.shape[1]), np.int32)
        for start in range(0, count, QUERY_BATCH):
            p = points[start:start + QUERY_BATCH]
            _, nearest = self.tree.query(p, k=1, workers=-1)
            ring = self.around[nearest]
            valid = ring >= 0
            ring_ids = np.where(valid, ring, 0)
            a = self.a[ring_ids] - p[:, None, :]
            ab, ac = self.ab[ring_ids], self.ac[ring_ids]
            v, w = _closest_on_triangles(a, ab, ac)
            offset = a + v[..., None] * ab + w[..., None] * ac
            distance = _dot(offset, offset)
            distance = np.where(valid & np.isfinite(distance), distance, np.inf)
            best = np.argmin(distance, axis=1)
            rows = np.arange(len(p))
            face[start:start + len(p)] = ring_ids[rows, best]
            bv, bw = v[rows, best], w[rows, best]
            bad = ~np.isfinite(bv + bw)
            bv[bad], bw[bad] = 1.0 / 3.0, 1.0 / 3.0
            bary[start:start + len(p)] = np.stack([1 - bv - bw, bv, bw], axis=1)
            candidates[start:start + len(p)] = ring
        return face, bary, candidates

    def point(self, face, bary):
        return self.a[face] + bary[:, 1:2] * self.ab[face] + bary[:, 2:3] * self.ac[face]

    def project(self, points, directions):
        """Where each point lands on the sculpt: along its direction (the reduced
        mesh's normal, either way) when that meets the surface near by, else
        the closest point. Returns (face, barycentric)."""
        import numpy as np
        face, bary, _ = self.closest(points)
        offset = self.point(face, bary) - points
        distance = np.linalg.norm(offset, axis=1)
        along = _dot(offset, directions)
        sideways = np.linalg.norm(offset - along[:, None] * directions, axis=1)
        # Only points whose closest spot is off to the side need the ray: for
        # the rest the two agree.
        todo = np.nonzero(sideways > 0.2 * distance + 0.05 * self.edge)[0]
        if not len(todo):
            return face, bary
        p, n = points[todo], directions[todo]
        reach = 3.0 * distance[todo] + 2.0 * self.edge
        t = along[todo]
        for _ in range(2):             # Newton steps along the ray onto the surface
            step_face, step_bary, _ = self.closest(p + t[:, None] * n)
            m = self.face_normals[step_face].astype(np.float64)
            height = _dot(p + t[:, None] * n - self.point(step_face, step_bary), m)
            facing = _dot(n, m)
            ok = np.abs(facing) > 0.25
            t = np.where(ok, t - height / np.where(ok, facing, 1.0), t)
            t = np.clip(t, -reach, reach)
        _, _, ring = self.closest(p + t[:, None] * n)
        valid = ring >= 0
        ids = np.where(valid, ring, 0)
        # Moller-Trumbore against the triangles round the landing spot.
        with np.errstate(divide="ignore", invalid="ignore"):
            e1, e2 = self.ab[ids], self.ac[ids]
            pvec = np.cross(n[:, None, :], e2)
            det = _dot(e1, pvec)
            inverse = 1.0 / det
            tvec = p[:, None, :] - self.a[ids]
            u = _dot(tvec, pvec) * inverse
            qvec = np.cross(tvec, e1)
            v = _dot(n[:, None, :], qvec) * inverse
            hit_t = _dot(e2, qvec) * inverse
            facing = _dot(self.face_normals[ids].astype(np.float64), n[:, None, :])
            hit = (valid & (np.abs(det) > 1e-30) & (u >= -1e-6) & (v >= -1e-6)
                   & (u + v <= 1 + 1e-6) & (np.abs(hit_t) <= reach[:, None]) & (facing > 0.0))
        score = np.where(hit, np.abs(hit_t), np.inf)
        best = np.argmin(score, axis=1)
        rows = np.arange(len(todo))
        found = np.isfinite(score[rows, best])
        chosen = todo[found]
        face[chosen] = ids[rows, best][found]
        bu, bv = u[rows, best][found], v[rows, best][found]
        bary[chosen] = np.stack([1 - bu - bv, bu, bv], axis=1)
        return face, bary

    def colour(self, face, bary):
        import numpy as np
        corners = self.faces[face]
        return np.einsum("nk,nkc->nc", bary.astype(np.float32), self.colours[corners])

    def normal(self, face, bary):
        import numpy as np
        corners = self.faces[face]
        return _normalise(np.einsum("nk,nkc->nc", bary.astype(np.float32),
                                    self.normals[corners]))


#: Bounding-box sizes (in pixels) triangles are grouped by when drawn; a larger
#: triangle is drawn on its own.
_RASTER_STEPS = (1, 2, 3, 4, 6, 8, 12, 16, 24, 32)


def _rasterise(xy, faces, size: int, depth=None, labels=None, label_map=None):
    """Which triangle covers each pixel centre of a ``size`` x ``size`` grid, and where.

    ``xy`` are vertex positions in pixels (pixel i's centre is at i + 0.5).
    Where triangles overlap, the one with the greatest ``depth`` wins (the
    first one drawn when there is no depth). With ``labels`` (one per
    triangle) and ``label_map`` (one per pixel, -1 for none), a triangle only
    draws on pixels of its own label or none. Returns (triangle (size*size,)
    int32, -1 where none, barycentric (size*size, 3) float32)."""
    import numpy as np
    pixels = size * size
    best = np.full(pixels, -np.inf, np.float32) if depth is not None else None
    owner = np.full(pixels, -1, np.int32)
    bary = np.zeros((pixels, 3), np.float32)
    corners = xy[faces]                                      # (f, 3, 2)
    # Pixels whose centres can be inside: ceil(min - 0.5) .. floor(max - 0.5).
    first = np.ceil(corners.min(axis=1) - 0.5).astype(np.int64)
    last = np.floor(corners.max(axis=1) - 0.5).astype(np.int64)
    first = np.maximum(first, 0)
    last = np.minimum(last, size - 1)
    span = last - first + 1                                  # (f, 2): width, height
    drawn = np.nonzero((span[:, 0] > 0) & (span[:, 1] > 0))[0]
    steps = np.asarray(_RASTER_STEPS)
    bucket_w = np.searchsorted(steps, span[drawn, 0])
    bucket_h = np.searchsorted(steps, span[drawn, 1])
    large = (bucket_w >= len(steps)) | (bucket_h >= len(steps))

    def resolve(pixel, tri, u0, u1, u2):
        """Keep, per pixel, the winning candidate, and write it if it beats what is there."""
        if labels is not None:
            here = label_map[pixel]
            keep = (here < 0) | (here == labels[tri])
            pixel, tri, u0, u1, u2 = pixel[keep], tri[keep], u0[keep], u1[keep], u2[keep]
        if depth is not None:
            d = (u0 * depth[faces[tri, 0]] + u1 * depth[faces[tri, 1]]
                 + u2 * depth[faces[tri, 2]]).astype(np.float32)
            order = np.lexsort((-d, pixel))
        else:
            order = np.argsort(pixel, kind="stable")
        pixel = pixel[order]
        winner = np.ones(len(pixel), bool)
        winner[1:] = pixel[1:] != pixel[:-1]
        if depth is not None:
            d = d[order]
            winner &= d > best[pixel]
            best[pixel[winner]] = d[winner]
        else:
            winner &= owner[pixel] < 0
        chosen = order[winner]
        pixel = pixel[winner]
        owner[pixel] = tri[chosen]
        bary[pixel, 0] = u0[chosen]
        bary[pixel, 1] = u1[chosen]
        bary[pixel, 2] = u2[chosen]

    def weights(ids, gx, gy):
        """Barycentric weights of pixel centres (gx, gy: (n, k) offsets from each
        triangle's first pixel), in coordinates local to the triangle so float32
        keeps its precision on a 8192-pixel grid."""
        origin = first[ids].astype(np.float64)[:, None, :]
        local = (corners[ids] - origin).astype(np.float32)        # (n, 3, 2)
        a, b, c = local[:, 0], local[:, 1], local[:, 2]
        cx, cy = gx.astype(np.float32) + 0.5, gy.astype(np.float32) + 0.5
        area = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
        usable = np.abs(area) > 1e-8
        area = np.where(usable, area, 1.0)[:, None]
        dx, dy = cx - c[:, 0:1], cy - c[:, 1:2]
        w0 = ((b[:, 1] - c[:, 1])[:, None] * dx + (c[:, 0] - b[:, 0])[:, None] * dy) / area
        w1 = ((c[:, 1] - a[:, 1])[:, None] * dx + (a[:, 0] - c[:, 0])[:, None] * dy) / area
        w2 = 1.0 - w0 - w1
        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0) & usable[:, None]
        return w0, w1, w2, inside

    def draw(ids, width: int, height: int):
        gx = np.tile(np.arange(width), height)[None, :]
        gy = np.repeat(np.arange(height), width)[None, :]
        gx = np.broadcast_to(gx, (len(ids), width * height))
        gy = np.broadcast_to(gy, (len(ids), width * height))
        w0, w1, w2, inside = weights(ids, gx, gy)
        inside &= (gx < span[ids, 0:1]) & (gy < span[ids, 1:2])
        row, col = np.nonzero(inside)
        if not len(row):
            return
        tri = ids[row]
        pixel = (first[tri, 1] + gy[row, col]) * size + first[tri, 0] + gx[row, col]
        resolve(pixel, tri.astype(np.int32), w0[row, col], w1[row, col], w2[row, col])

    small = drawn[~large]
    if len(small):
        keys = bucket_w[~large] * len(steps) + bucket_h[~large]
        order = np.argsort(keys, kind="stable")
        small, keys = small[order], keys[order]
        cuts = np.nonzero(np.diff(keys))[0] + 1
        for group in np.split(np.arange(len(small)), cuts):
            key = keys[group[0]]
            width, height = int(steps[key // len(steps)]), int(steps[key % len(steps)])
            per_batch = max(1, (1 << 21) // (width * height))
            for start in range(0, len(group), per_batch):
                draw(small[group[start:start + per_batch]], width, height)
    for index in drawn[large]:                 # big triangles one at a time, tight box
        draw(np.asarray([index]), int(span[index, 0]), int(span[index, 1]))
    return owner, bary


def _closest_low_triangles(points, positions, faces, spatial):
    """Index of the reduced mesh's triangle closest to each point. Candidates come
    from a kd-tree of points spread over every triangle (so a long thin one is
    found from its middle too); the exact closest point decides."""
    import numpy as np
    corners = positions[faces]
    a, ab, ac = corners[:, 0], corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]
    # Six spots per triangle: corners pulled in, edge midpoints and the centre.
    weights = np.array([[0.8, 0.1, 0.1], [0.1, 0.8, 0.1], [0.1, 0.1, 0.8],
                        [0.45, 0.45, 0.1], [0.1, 0.45, 0.45], [0.45, 0.1, 0.45],
                        [1 / 3, 1 / 3, 1 / 3]])
    spots = np.einsum("sk,fkc->fsc", weights, corners).reshape(-1, 3)
    owner = np.repeat(np.arange(len(faces)), len(weights))
    tree = spatial.cKDTree(spots)
    nearest = np.empty(len(points), np.int64)
    for start in range(0, len(points), QUERY_BATCH):
        p = points[start:start + QUERY_BATCH]
        _, found = tree.query(p, k=8, workers=-1)
        candidates = owner[found]
        rel = a[candidates] - p[:, None, :]
        v, w = _closest_on_triangles(rel, ab[candidates], ac[candidates])
        offset = rel + v[..., None] * ab[candidates] + w[..., None] * ac[candidates]
        distance = np.where(np.isfinite(_dot(offset, offset)), _dot(offset, offset), np.inf)
        nearest[start:start + len(p)] = candidates[np.arange(len(p)), np.argmin(distance, axis=1)]
    return nearest


def _plane_coordinates(points, a, ab, ac, normal):
    """Barycentric coordinates of each point's projection onto its triangle's
    plane (may fall outside the triangle), and its height above the plane."""
    import numpy as np
    rel = points - a
    d00, d01, d11 = _dot(ab, ab), _dot(ab, ac), _dot(ac, ac)
    d20, d21 = _dot(rel, ab), _dot(rel, ac)
    denom = d00 * d11 - d01 * d01
    denom = np.where(np.abs(denom) > 1e-30, denom, 1.0)
    v = (d11 * d20 - d01 * d21) / denom
    w = (d00 * d21 - d01 * d20) / denom
    return np.stack([1 - v - w, v, w], axis=-1), _dot(rel, normal)


def samples_per_texel(texture: int) -> int:
    """Samples along each side of a texel: more where a texel covers more of the
    sculpt, so a 64 px texture is an average of the surface, not a guess."""
    if texture <= 128:
        return 8
    if texture <= 512:
        return 4
    if texture <= 2048:
        return 2
    return 1


def _fill_background(image, filled):
    """Every texel outside the UV islands takes the colour of the nearest one
    inside, so filtering, mip-maps and texture compression never pull the
    empty background into a seam - at any distance."""
    from scipy import ndimage
    if filled.all() or not filled.any():
        return image
    rows, cols = ndimage.distance_transform_edt(~filled, return_distances=False,
                                                return_indices=True)
    return image[rows, cols]


def _chart_ids(faces, vertex_count: int):
    """Which UV island each triangle is on (xatlas splits vertices at seams, so
    triangles that share a vertex share an island)."""
    import numpy as np
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    rows = np.concatenate([faces[:, 0], faces[:, 1]])
    cols = np.concatenate([faces[:, 1], faces[:, 2]])
    graph = coo_matrix((np.ones(len(rows), np.int8), (rows, cols)),
                       shape=(vertex_count, vertex_count))
    _, labels = connected_components(graph, directed=False)
    return labels[faces[:, 0]]


def _tangent_space(high, n, t, handed):
    """The sculpt normal ``high`` in the reduced mesh's tangent frame (n, t
    interpolated, unnormalised): the inverse of the shader's
    normalize(x T + y B + z N), MikkTSpace style, so the map gives back the
    sculpt's normal."""
    import numpy as np
    b = np.cross(n, t) * handed[:, None]
    det = _dot(t, np.cross(b, n))
    safe = np.where(np.abs(det) > 1e-12, det, 1.0)[:, None]
    local = np.stack([_dot(high, np.cross(b, n)), _dot(high, np.cross(n, t)),
                      _dot(high, np.cross(t, b))], axis=1) / safe
    local = _normalise(local)
    local[:, 2] = np.maximum(local[:, 2], 0.05)          # never point into the surface
    return _normalise(local)


def _bake_maps(sculpt: Sculpt, sculpt_vertices, nearest, positions, normals, tangents, uvs,
               triangles, size: int, want_normal: bool, per_side: int):
    """BaseColor (size, size, 3) and, if wanted, a tangent-space normal map, both
    0..1 with v going up (row 0 is the bottom).

    Works on a grid ``per_side`` times finer than the texture, averaged down:
    1. draw the reduced mesh's UV triangles: which reduced triangle (and where
       on it) each sample is;
    2. draw the sculpt's own triangles into the same UV space: each sculpt
       triangle is laid onto the reduced triangle(s) nearest its corners, and
       where several overlap the outermost wins, as a ray cast from a cage
       outside the model would see it. Detail that floats above the skin
       (eyebrows, eyeliner, decals) lands on the texture this way;
    3. samples no sculpt triangle reached (a sliver along a seam) are projected
       from the reduced surface instead (Sculpt.project).
    """
    import numpy as np
    grid = size * per_side
    charts = _chart_ids(triangles, len(positions)).astype(np.int32)
    low_owner, low_bary = _rasterise(uvs * grid, triangles, grid)
    low_chart = np.where(low_owner >= 0, charts[np.maximum(low_owner, 0)], -1).astype(np.int32)

    # Lay each sculpt triangle onto the reduced triangle nearest each corner
    # (``nearest``, one per sculpt vertex), once per distinct one, so it
    # reaches across a reduced edge or a seam.
    low_corners = positions[triangles]
    low_a = low_corners[:, 0]
    low_ab = low_corners[:, 1] - low_a
    low_ac = low_corners[:, 2] - low_a
    low_normal = _normalise(np.cross(low_ab, low_ac))
    # -1: that sculpt vertex is nearest a part of the reduced mesh with another
    # texture (the face's own material); it is drawn there, not here.
    refs = nearest[sculpt.faces]                             # (f, 3)
    copies = [np.arange(len(refs))]
    which = [refs[:, 0]]
    for k in (1, 2):
        distinct = np.ones(len(refs), bool)
        for j in range(k):
            distinct &= refs[:, k] != refs[:, j]
        copies.append(np.nonzero(distinct)[0])
        which.append(refs[distinct, k])
    face_of = np.concatenate(copies)
    onto = np.concatenate(which)
    del copies, which, refs
    here = onto >= 0
    if not here.all():
        face_of, onto = face_of[here], onto[here]
    del here
    # Only the side of the sculpt that faces out from the reduced surface.
    front = _dot(sculpt.face_normals[face_of], low_normal[onto].astype(np.float32)) > 0.0
    face_of, onto = face_of[front], onto[front]
    del front
    uv_corners = np.empty((len(face_of), 3, 2), np.float64)
    height = np.empty((len(face_of), 3), np.float32)
    for start in range(0, len(face_of), SAMPLE_BATCH):
        part = slice(start, start + SAMPLE_BATCH)
        target = onto[part]
        bary, lift = _plane_coordinates(sculpt_vertices[sculpt.faces[face_of[part]]],
                                        low_a[target][:, None, :], low_ab[target][:, None, :],
                                        low_ac[target][:, None, :],
                                        low_normal[target][:, None, :])
        uv_corners[part] = np.einsum("ckj,cjd->ckd", bary, uvs[triangles[target]]) * grid
        height[part] = lift
    high_owner, high_bary = _rasterise(uv_corners.reshape(-1, 2),
                                       np.arange(3 * len(face_of)).reshape(-1, 3), grid,
                                       depth=height.reshape(-1), labels=charts[onto],
                                       label_map=low_chart)
    del uv_corners, height, low_chart, onto
    high_face = np.where(high_owner >= 0, face_of[np.maximum(high_owner, 0)], -1).astype(np.int32)
    del high_owner, face_of

    texels = size * size
    # Sums per texel, float32: a texel averages at most 64 samples.
    colour_total = np.zeros((texels, 3), np.float32)
    normal_total = np.zeros((texels, 3), np.float32) if want_normal else None
    weight = np.zeros(texels, np.float32)
    # Work through the grid a band of rows at a time: memory stays bounded.
    rows_per_band = max(per_side, (SAMPLE_BATCH * 4 // grid) // per_side * per_side)
    for top in range(0, grid, rows_per_band):
        bottom = min(grid, top + rows_per_band)
        band = np.arange(top * grid, bottom * grid)
        first_texel = (top // per_side) * size
        span = ((bottom - 1) // per_side + 1) * size - first_texel
        texel = (band // grid // per_side) * size + (band % grid) // per_side - first_texel
        lows, highs = low_owner[band], high_face[band]
        face = highs.astype(np.int64)
        bary = high_bary[band].astype(np.float64)
        # Samples on the reduced mesh that no sculpt triangle reached: project them.
        missing = np.nonzero((lows >= 0) & (highs < 0))[0]
        if len(missing):
            corners = triangles[lows[missing]]
            w = low_bary[band[missing]].astype(np.float64)
            p = np.einsum("nk,nkc->nc", w, positions[corners])
            n = _normalise(np.einsum("nk,nkc->nc", w, normals[corners]))
            face[missing], bary[missing] = sculpt.project(p, n)
        # Only samples on the reduced mesh: a sculpt triangle that spills past an
        # island's edge must not tint the gap a neighbouring island also uses
        # (at 64 px that gap is one texel, and filtering reads it).
        used = np.nonzero((face >= 0) & (lows >= 0))[0]
        if not len(used):
            continue
        where = slice(first_texel, first_texel + span)
        colour = sculpt.colour(face[used], bary[used])
        for channel in range(3):
            colour_total[where, channel] += np.bincount(texel[used], colour[:, channel],
                                                        minlength=span)
        weight[where] += np.bincount(texel[used], minlength=span)
        if want_normal:
            corners = triangles[lows[used]]
            w = low_bary[band[used]].astype(np.float64)
            n = np.einsum("nk,nkc->nc", w, normals[corners])
            frame = tangents[corners]
            t = np.einsum("nk,nkc->nc", w, frame[..., :3])
            handed = np.where(np.einsum("nk,nk->n", w, frame[..., 3]) < 0, -1.0, 1.0)
            high = sculpt.normal(face[used], bary[used]).astype(np.float64)
            local = _tangent_space(high, n, t, handed)
            for channel in range(3):
                normal_total[where, channel] += np.bincount(texel[used], local[:, channel],
                                                            minlength=span)
    del low_owner, low_bary, high_face, high_bary
    filled = weight > 0
    colour_total[filled] /= weight[filled, None]
    del weight
    texture = _fill_background(colour_total.reshape(size, size, 3), filled.reshape(size, size))
    del colour_total
    normal_image = None
    if want_normal:
        normal_total[filled] = _normalise(normal_total[filled]) * 0.5 + 0.5
        normal_total[~filled] = FLAT_NORMAL
        normal_image = _fill_background(normal_total.reshape(size, size, 3),
                                        filled.reshape(size, size))
    return texture, normal_image, filled.reshape(size, size)


def _unwrap(xatlas, positions, faces, size: int, padding: int, priority=None,
            boost: float = 1.0):
    """xatlas charts packed into ``size`` x ``size`` with ``padding`` texels between
    islands. Returns (vertex mapping, faces, uvs, order): ``order`` is the input
    triangle each output triangle came from.

    ``priority`` (one flag per triangle) is unwrapped as a mesh of its own,
    ``boost`` times larger, so its islands get boost^2 times the texels: the
    face and other fine colour detail. xatlas' first guess at the scale usually
    overshoots the size (and the padding then shrinks when the UVs are fitted
    back), so it is re-packed at the scale that fits."""
    import numpy as np
    groups = [np.arange(len(faces))]
    scales = [1.0]
    if priority is not None and priority.any() and not priority.all() and boost > 1.0:
        groups = [np.nonzero(~priority)[0], np.nonzero(priority)[0]]
        scales = [1.0, boost]
    parts = []
    for group, scale in zip(groups, scales):
        used, local = np.unique(faces[group], return_inverse=True)
        parts.append((used, local.reshape(-1, 3), positions[used] * scale))
    chart = xatlas.ChartOptions()
    texels_per_unit = 0.0
    # First pass: xatlas' own scale, with a wider gap so that shrinking a
    # slightly-too-big result to fit still leaves ``padding`` texels. Packing
    # again costs as much as the first time (5 s at 30,000 triangles).
    slack = 1.3
    gap = int(-(-padding * slack // 1))
    for attempt in range(4):
        atlas = xatlas.Atlas()
        for _, local, scaled in parts:
            atlas.add_mesh(scaled.astype(np.float32), local.astype(np.uint32))
        pack = xatlas.PackOptions()
        pack.resolution = size
        pack.padding = gap if attempt == 0 else padding
        pack.bilinear = True
        pack.texels_per_unit = texels_per_unit
        atlas.generate(chart, pack)
        largest = max(atlas.width, atlas.height)
        if atlas.atlas_count == 1 and largest <= (size * slack if attempt == 0 else size):
            break
        texels_per_unit = atlas.texels_per_unit * size / max(largest, 1) * (0.96 - 0.03 * attempt)
    mappings, all_faces, all_uvs = [], [], []
    offset = 0
    for index, (used, _, _) in enumerate(parts):
        mapping, uv_faces, uvs = atlas.get_mesh(index)
        mappings.append(used[np.asarray(mapping, np.int64)])
        all_faces.append(np.asarray(uv_faces, np.int64) + offset)
        all_uvs.append(np.asarray(uvs, np.float64))
        offset += len(mapping)
    uvs = np.concatenate(all_uvs)
    # The binding scales UVs to the atlas' own width and height; put them back
    # in texels, then into the square texture (shrunk evenly if the atlas came
    # out bigger), so every texel stays square.
    uvs[:, 0] *= atlas.width
    uvs[:, 1] *= atlas.height
    uvs /= max(size, largest)
    if atlas.atlas_count > 1:                        # never expected: squeeze in
        uvs /= max(1.0, uvs.max())
    return (np.concatenate(mappings), np.concatenate(all_faces), uvs,
            np.concatenate(groups))


def _detail(sculpt_colours, nearest, faces, positions):
    """Which reduced triangles carry fine colour detail (eyes, brows, lips, a
    logo): the colours of the sculpt vertices nearest each one vary a lot.
    Grown by one ring of neighbours so a detail region is one piece."""
    import numpy as np
    count = np.bincount(nearest, minlength=len(faces)).astype(np.float64)
    safe = np.maximum(count, 1.0)
    spread = np.zeros(len(faces))
    for channel in range(3):
        values = sculpt_colours[:, channel].astype(np.float64)
        mean = np.bincount(nearest, values, minlength=len(faces)) / safe
        square = np.bincount(nearest, values * values, minlength=len(faces)) / safe
        spread += np.clip(square - mean * mean, 0.0, None)
    marked = np.sqrt(spread) >= DETAIL_SPREAD
    touched = np.zeros(len(positions), bool)
    touched[faces[marked].ravel()] = True
    return touched[faces].any(axis=1)


def _face_centre(vertices, faces, colours, spatial=None, with_radius: bool = False):
    """Where the sculpt's colour detail is densest - on a character, the eyes,
    brows and lips - or None when it has too little detail to tell.

    The model is cut into small cells (FACE_FINE_GRID across). A cell's detail
    is how many different colours it holds (each channel in FACE_COLOUR_LEVELS
    steps, a colour counting once it paints two vertices there), less one: a
    plain cell scores 0, a seam between two colours 1, an eye (white, iris,
    pupil, lashes, skin) 4 or 5. Those are summed per coarse cell (FACE_GRID
    across) with the 26 neighbours, and the centre is the detail-weighted
    middle of the busiest neighbourhood.

    Tried on the test sculpt and dropped: counting sculpt edges whose ends differ in
    colour put the face on her chest (ZBrush sculptors give a head far more
    polygons, so each face edge changes colour only a little, while the suit's
    coarse panel lines change a lot per edge); colour spread per cell put it on
    her chin, pulled down by the suit collar's ring of blue against skin.

    ``faces`` only gates the decision: a sculpt with fewer than FACE_MIN_EDGES
    edges whose two ends differ in colour (FACE_EDGE_CONTRAST) has no detail
    worth a texture of its own."""
    import numpy as np
    colours = np.asarray(colours, np.float32)
    if colours.max(initial=0.0) > 1.0:
        colours = colours / 255.0
    edges = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    contrast = np.abs(colours[edges[:, 0]] - colours[edges[:, 1]]).max(axis=1)
    enough = int((contrast >= FACE_EDGE_CONTRAST).sum()) >= FACE_MIN_EDGES
    del edges, contrast
    if not enough:
        return None
    vertices = np.asarray(vertices, np.float64)
    low, high = vertices.min(axis=0), vertices.max(axis=0)
    span = float(np.max(high - low)) or 1.0
    # Cells a few edges wide at least, so a coarse sculpt still has several
    # vertices in each (the median edge: a dense head outvotes a coarse body).
    sample = faces[:: max(1, len(faces) // 200_000)]
    edge = float(np.median(np.linalg.norm(vertices[sample[:, 0]] - vertices[sample[:, 1]],
                                          axis=1))) if len(sample) else 0.0
    fine = max(span / FACE_FINE_GRID, 2.5 * edge)
    cells = np.floor((vertices - low) / fine).astype(np.int64)
    shape = cells.max(axis=0) + 1
    cell = np.ravel_multi_index(cells.T, shape)
    del cells
    levels = FACE_COLOUR_LEVELS
    step = np.minimum((colours * levels).astype(np.int64), levels - 1)
    colour = (step[:, 0] * levels + step[:, 1]) * levels + step[:, 2]
    pairs, counts = np.unique(cell * levels ** 3 + colour, return_counts=True)
    del cell, step, colour
    keys, kinds = np.unique(pairs[counts >= 2] // levels ** 3, return_counts=True)
    detail = (kinds - 1).astype(np.float64)
    keep = detail > 0
    keys, detail = keys[keep], detail[keep]
    if not len(keys):
        return None
    middles = (np.array(np.unravel_index(keys, shape)).T + 0.5) * fine + low
    coarse = span / FACE_GRID
    cshape = np.maximum(np.ceil((high - low) / coarse).astype(np.int64), 1) + 1
    index = np.minimum(((middles - low) / coarse).astype(np.int64), cshape - 1)
    grid = np.bincount(np.ravel_multi_index(index.T, cshape), weights=detail,
                       minlength=int(np.prod(cshape))).reshape(cshape)
    try:
        from scipy import ndimage
        grid = ndimage.uniform_filter(grid, size=3, mode="constant")
    except ImportError:                      # pragma: no cover - scipy comes with the bake
        pass
    busiest = np.array(np.unravel_index(int(np.argmax(grid)), cshape))
    near = np.all(np.abs(index - busiest) <= 1, axis=1)
    weight = detail[near]
    centre = (middles[near] * weight[:, None]).sum(axis=0) / weight.sum()
    if not with_radius:
        return centre
    # How far the detail reaches from the centre (detail-weighted RMS): about
    # half the span from brows to lips.
    reach = float(np.sqrt((weight * ((middles[near] - centre) ** 2).sum(axis=1)).sum()
                          / weight.sum()))
    return centre, reach


def _face_region(centre, positions, faces, share: float = FACE_SHARE):
    """The reduced triangles nearest ``centre`` that make up ``share`` of the
    reduced surface's area: the face (and on a full figure, about the head)."""
    import numpy as np
    corners = positions[faces]
    area = 0.5 * np.linalg.norm(np.cross(corners[:, 1] - corners[:, 0],
                                         corners[:, 2] - corners[:, 0]), axis=1)
    centroid = corners.mean(axis=1)
    distance = np.linalg.norm(centroid - np.asarray(centre, np.float64), axis=1)
    order = np.argsort(distance, kind="stable")
    reached = np.cumsum(area[order]) <= share * max(float(area.sum()), 1e-30)
    region = np.zeros(len(faces), bool)
    region[order[reached]] = True
    if not region.any():                     # one huge triangle: take it alone
        region[order[0]] = True
    return region


#: A face-texture triangle faces at most this far from the face's own direction;
#: the rest of the region (the sides of the head) goes on the main texture.
FACE_FRONT_DEGREES = 85.0
#: The face texture covers what is within this many times the reach of the
#: facial detail (eyes, brows, lips) from its centre: hairline to chin.
FACE_REACH = 1.8
#: Share of the face region, nearest its centre, whose facing says which way
#: the face looks.
FACE_DIRECTION_SHARE = 0.05
#: Grid used to find face triangles hidden from the front by others.
FACE_VISIBILITY_GRID = 384


def _front_projection(positions, faces, region, centre, size: int, padding: int):
    """The face mapped straight from the front, the way N64 faces were drawn:
    one island, upright, filling its texture. Returns (region, mapping, uv
    faces, uvs) for the triangles kept (``region`` narrowed to them), or None
    when the region has no front to map from.

    xatlas cut the test sculpt's face into dozens of islands, the hair fringe took much
    of the 64 px texture and her eyes came out two or three texels wide (12 Nov
    2026). From the front, eyes, brows and lips span most of the texture.
    Kept: triangles facing within FACE_FRONT_DEGREES of the face's direction
    and not hidden from the front behind another one (they would share its
    texels); the others go back to the main texture."""
    import numpy as np
    chosen = np.nonzero(region)[0]
    if len(chosen) < 8:
        return None
    corners = positions[faces[chosen]]
    normal = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    doubled = np.linalg.norm(normal, axis=1)
    # Which way the face looks: the triangles nearest its centre (eyes, nose,
    # lips), FACE_DIRECTION_SHARE of the region. The whole region is most of a
    # low-poly head - back and hair too - whose facings cancel out; even the
    # nearest ones point every way (nose sides, eye sockets), and agree only
    # to about 0.4 on the test sculpt, but their sum points out of the face.
    distance = np.linalg.norm(corners.mean(axis=1) - np.asarray(centre, np.float64), axis=1)
    order = np.argsort(distance, kind="stable")
    nearest = order[np.cumsum(doubled[order]) <= FACE_DIRECTION_SHARE * float(doubled.sum())]
    if len(nearest) < 12:
        nearest = order[:12]
    direction = normal[nearest].sum(axis=0)         # area-weighted
    length = float(np.linalg.norm(direction))
    if length < 0.25 * float(doubled[nearest].sum()):  # no one way it looks
        return None
    direction /= length
    up = np.array([0.0, 1.0, 0.0])
    if abs(float(up @ direction)) > 0.9:            # looking up or down: use depth
        up = np.array([0.0, 0.0, 1.0])
    up = up - direction * float(up @ direction)
    up /= np.linalg.norm(up)
    right = np.cross(up, direction)                 # seen from the front, left to right
    unit = normal / np.where(doubled > 0, doubled, 1.0)[:, None]
    keep = chosen[unit @ direction >= np.cos(np.radians(FACE_FRONT_DEGREES))]
    if len(keep) < 8:
        return None

    def project(ids):
        used, local = np.unique(faces[ids], return_inverse=True)
        points = positions[used]
        return used, local.reshape(-1, 3), np.stack([points @ right, points @ up], axis=1), points

    used, local, xy, points = project(keep)
    low, span = xy.min(axis=0), float((xy.max(axis=0) - xy.min(axis=0)).max()) or 1.0
    grid = FACE_VISIBILITY_GRID
    pixels = (xy - low) / span * (grid - 1) + 0.5
    owner, _ = _rasterise(pixels, local, grid, depth=(points @ direction).astype(np.float32))
    seen = np.bincount(owner[owner >= 0], minlength=len(keep))
    tri = pixels[local]
    covers = 0.5 * np.abs((tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1])
                          - (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1]))
    # Mostly hidden (a cheek behind a lock of hair) goes; edge-on slivers that
    # cover no pixel centre are kept, they share nothing.
    visible = (seen >= 0.5 * covers) | (covers < 2.0)
    keep = keep[visible]
    if len(keep) < 8:
        return None
    used, local, xy, _ = project(keep)
    low = xy.min(axis=0)
    extent = xy.max(axis=0) - low
    span = float(extent.max()) or 1.0
    # Into the texture with ``padding`` texels round it, centred, square texels.
    inner = (size - 2 * padding) / size
    uvs = (xy - low) / span * inner + padding / size
    uvs += (1.0 - 2 * padding / size - extent / span * inner) / 2.0
    narrowed = np.zeros(len(region), bool)
    narrowed[keep] = True
    return narrowed, used, local, uvs


def _png_bytes(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=6)
    return buffer.getvalue()


def write_glb(path: Path, positions, normals, uvs, faces, base_png: bytes, name: str,
              tangents=None, normal_png: Optional[bytes] = None,
              image_names: tuple = ("", ""), nearest: bool = False,
              extra_parts: tuple = ()) -> None:
    """A one-mesh .glb, one material per part. ``uvs`` have v going up (they are
    flipped to glTF's v-down here); tangents and the normal map are optional.
    ``nearest`` asks viewers for blocky nearest-texel filtering (small retro
    textures). ``extra_parts`` adds more primitives over the same vertices,
    each a dict with faces, base_png, name and optionally normal_png and
    image_names (the face texture's own material)."""
    import numpy as np
    blobs: list[bytes] = []
    views: list[dict] = []
    accessors: list[dict] = []

    def add_view(data: bytes, target: Optional[int] = None) -> int:
        offset = sum(len(blob) for blob in blobs)
        blobs.append(data + b"\0" * (-len(data) % 4))
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        views.append(view)
        return len(views) - 1

    def add_accessor(array, kind: str, component: int, target: int, bounds: bool = False) -> int:
        view = add_view(array.tobytes(), target)
        accessor = {"bufferView": view, "componentType": component, "count": int(len(array)),
                    "type": kind}
        if bounds:
            accessor["min"] = [float(x) for x in array.min(axis=0)]
            accessor["max"] = [float(x) for x in array.max(axis=0)]
        accessors.append(accessor)
        return len(accessors) - 1

    gl_uvs = np.asarray(uvs, np.float32).copy()
    gl_uvs[:, 1] = 1.0 - gl_uvs[:, 1]
    attributes = {
        "POSITION": add_accessor(np.ascontiguousarray(positions, np.float32), "VEC3", 5126,
                                 34962, bounds=True),
        "NORMAL": add_accessor(np.ascontiguousarray(_normalise(normals), np.float32), "VEC3",
                               5126, 34962),
        "TEXCOORD_0": add_accessor(gl_uvs, "VEC2", 5126, 34962),
    }
    if tangents is not None:
        attributes["TANGENT"] = add_accessor(np.ascontiguousarray(tangents, np.float32),
                                             "VEC4", 5126, 34962)
    big = len(positions) > 65535
    parts = [{"faces": faces, "base_png": base_png, "normal_png": normal_png, "name": name,
              "image_names": image_names}, *extra_parts]
    images, textures, materials, primitives = [], [], [], []

    def add_texture(data: bytes, label: str) -> int:
        image = {"bufferView": add_view(data), "mimeType": "image/png"}
        if label:
            image["name"] = label
        images.append(image)
        textures.append({"source": len(images) - 1, "sampler": 0})
        return len(textures) - 1

    for part in parts:
        labels = tuple(part.get("image_names") or ("", ""))
        indices = add_accessor(np.ascontiguousarray(part["faces"],
                                                    np.uint32 if big else np.uint16).ravel(),
                               "SCALAR", 5125 if big else 5123, 34963)
        material = {"name": part["name"], "pbrMetallicRoughness": {
            "baseColorTexture": {"index": add_texture(part["base_png"], labels[0])},
            "metallicFactor": 0.0, "roughnessFactor": 0.8}}
        if part.get("normal_png") is not None:
            material["normalTexture"] = {"index": add_texture(part["normal_png"], labels[1])}
        materials.append(material)
        primitives.append({"attributes": attributes, "indices": indices,
                           "material": len(materials) - 1, "mode": 4})
    document = {
        "asset": {"version": "2.0", "generator": "AssetGatekeeper polypaint bake"},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": name}],
        "meshes": [{"name": name, "primitives": primitives}],
        "materials": materials,
        "samplers": [{"magFilter": 9728 if nearest else 9729,
                      "minFilter": 9984 if nearest else 9987,
                      "wrapS": 10497, "wrapT": 10497}],
        "textures": textures, "images": images,
        "accessors": accessors, "bufferViews": views,
        "buffers": [{"byteLength": sum(len(blob) for blob in blobs)}],
    }
    text = json.dumps(document, separators=(",", ":")).encode("utf-8")
    text += b" " * (-len(text) % 4)
    binary = b"".join(blobs)
    total = 12 + 8 + len(text) + 8 + len(binary)
    with open(path, "wb") as handle:
        handle.write(struct.pack("<4sII", b"glTF", 2, total))
        handle.write(struct.pack("<I4s", len(text), b"JSON") + text)
        handle.write(struct.pack("<I4s", len(binary), b"BIN\0") + binary)


#: Of a reduced palette, this share is chosen last, one colour at a time, for
#: the colours the rest represent worst (see _reduce_colours).
DETAIL_COLOUR_SHARE = 3 / 8
#: Above this many distinct colours the detail colours are skipped (the search
#: compares every colour with every other); a 64-128 px texture is far below.
DETAIL_COLOUR_LIMIT = 8192


def _reduce_colours(image, colours: int, inside=None):
    """The texture in ``colours`` colours, no dithering: a palette texture like the
    N64's 4-bit (16) and 8-bit (256) colour-index formats.

    Most of the palette is a median cut of the colours on the UV islands
    (``inside``), each counted by the square root of how often it appears: a
    plain median cut spent 13 of 16 colours on the blue suit and turned the
    eyebrows grey. The last DETAIL_COLOUR_SHARE are then added one at a time,
    each the colour that most lowers the error left (counted the same way):
    small, distinct details - the test sculpt's teal irises, its lip colour - that a
    median cut merges into their dark or skin-toned neighbours (12 Nov 2026)."""
    import numpy as np
    from PIL import Image
    pixels = np.asarray(image.convert("RGB"))
    chosen = pixels[inside] if inside is not None and inside.any() else pixels.reshape(-1, 3)
    coarse = (chosen >> 2).astype(np.int64)
    keys, counts = np.unique(coarse[:, 0] * 4096 + coarse[:, 1] * 64 + coarse[:, 2],
                             return_counts=True)
    distinct = np.stack([keys // 4096, keys // 64 % 64, keys % 64], axis=1) * 4 + 2
    weight = np.sqrt(counts)
    extra = int(colours * DETAIL_COLOUR_SHARE)
    if len(distinct) > DETAIL_COLOUR_LIMIT or colours - extra < 2:
        extra = 0
    weighted = np.repeat(distinct, np.ceil(weight).astype(np.int64), axis=0)
    sample = Image.fromarray(weighted.reshape(1, -1, 3).astype(np.uint8))
    base = sample.quantize(colors=colours - extra, method=Image.Quantize.MEDIANCUT,
                           dither=Image.Dither.NONE)
    if extra:
        found = len(base.getcolors(1 << 16) or ()) or colours - extra
        palette = np.asarray(base.getpalette()[:3 * found], np.float64).reshape(-1, 3)
        points = distinct.astype(np.float64)
        error = ((points[:, None, :] - palette[None]) ** 2).sum(axis=2).min(axis=1)
        for _ in range(extra):
            best_gain, best = 0.0, -1
            for first in range(0, len(points), 256):
                candidates = points[first:first + 256]
                after = ((candidates[:, None, :] - points[None]) ** 2).sum(axis=2)
                gain = (np.maximum(error[None] - after, 0.0) * weight[None]).sum(axis=1)
                index = int(np.argmax(gain))
                if gain[index] > best_gain:
                    best_gain, best = float(gain[index]), first + index
            if best < 0:                    # every colour is already in the palette
                break
            palette = np.vstack([palette, points[best]])
            error = np.minimum(error, ((points - points[best]) ** 2).sum(axis=1))
        base = Image.new("P", (1, 1))
        # Exactly these colours: a 16-colour texture keeps a 16-entry palette.
        base.putpalette(palette.round().clip(0, 255).astype(np.uint8).ravel().tolist())
    return image.convert("RGB").quantize(palette=base, dither=Image.Dither.NONE)


def bake(source: Path, out_dir: Optional[Path] = None, triangles: int = DEFAULT_TRIANGLES,
         texture: int = DEFAULT_TEXTURE,
         progress: Optional[Callable[[str, int, int], None]] = None,
         normal_map: bool = True, colours: int = 0, preset: str = "",
         detail_boost: float = DETAIL_BOOST, face_texture: bool = False) -> dict:
    """Reduce, unwrap and bake ``source`` into a game asset. Returns what it wrote.

    ``normal_map`` adds T_<name>_Normal.png (and puts it in the .glb);
    ``colours`` reduces the BaseColor to that many colours (0 keeps them all).
    ``preset`` only labels the result here - resolve_settings() turns a preset
    into these values. ``detail_boost`` (1 turns it off) gives the face and
    other fine colour detail more of the texture. ``face_texture`` gives the
    face a texture of its own, the same size, as a second material
    (T_<name>_Face_BaseColor.png): the region around the densest colour detail,
    about a tenth of the surface. A sculpt with no such detail keeps one."""
    import numpy as np
    from PIL import Image
    if texture < 16:
        raise PolypaintError(f"the texture size must be at least 16 px (got {texture})")
    if texture & (texture - 1):
        raise PolypaintError(f"the texture size must be a power of two (got {texture})")
    if triangles < 100:
        raise PolypaintError(f"a triangle budget of {triangles} is too small to bake into")
    if colours and not 2 <= colours <= 256:
        raise PolypaintError(f"a texture can be reduced to 2-256 colours (got {colours})")
    simplify, xatlas, spatial = _require_bake_libraries()
    steps = 4

    def step(name: str, index: int) -> None:
        if progress is not None:
            progress(name, index, steps)

    started = time.time()
    step("reading the sculpt", 0)
    obj = read_obj(Path(source))
    step("reducing the mesh", 1)
    # Reduce a welded copy: vertices that sit on top of each other (a UV seam,
    # a pole fan, pieces exported separately) are open edges to the reducer,
    # which keeps them, and a 1,200-triangle bake came out at 4,628.
    weld_positions, weld_faces = _weld(obj.vertices, obj.faces)
    if len(weld_faces) > triangles:
        low_positions, low_faces = simplify.simplify(
            weld_positions.astype(np.float32), weld_faces.astype(np.int32),
            target_reduction=1.0 - triangles / len(weld_faces))
    else:
        low_positions, low_faces = weld_positions.astype(np.float32), weld_faces.astype(np.int32)
    del weld_positions, weld_faces
    if len(low_faces) > triangles:          # a few over: a budget is a ceiling
        low_positions, low_faces = simplify.simplify(
            np.asarray(low_positions, np.float32), np.asarray(low_faces, np.int32),
            target_reduction=1.0 - (triangles - 2) / len(low_faces))
    low_positions = np.asarray(low_positions, dtype=np.float64)
    low_faces = np.asarray(low_faces, dtype=np.int64)
    # Reduction keeps flat or collapsed triangles it is given (a ZBrush pole, a
    # fan of slivers); the gate fails a mesh for them, so the bake drops them.
    corners = low_positions[low_faces]
    flat = agk.degenerate_mask(corners[:, 0], corners[:, 1], corners[:, 2])
    if flat.any():
        low_faces = low_faces[~flat]
    low_faces = _drop_stacked(low_positions, low_faces)
    step("unwrapping UVs", 2)
    sculpt = Sculpt(obj.vertices, obj.faces, obj.colours, spatial)
    nearest = _closest_low_triangles(obj.vertices, low_positions, low_faces, spatial)
    corners = low_positions[low_faces]
    area = 0.5 * np.linalg.norm(np.cross(corners[:, 1] - corners[:, 0],
                                         corners[:, 2] - corners[:, 0]), axis=1)
    face_region = None
    face_note = ""
    if face_texture:
        found = _face_centre(obj.vertices, obj.faces, obj.colours, with_radius=True)
        if found is None:
            face_note = "no separate face texture: the sculpt has too little colour detail to find one"
        else:
            centre, reach = found
            face_region = _face_region(centre, low_positions, low_faces)
            if face_region.all() or not face_region.any():
                face_region = None
                face_note = "no separate face texture: the mesh is too small to split"
    priority, boost = None, 1.0
    # Below 512 px a face triangle is a texel or two: splitting the detail off
    # into islands of its own only adds edges to bleed across. With a face
    # texture the face has its own already.
    if detail_boost > 1.0 and texture >= DETAIL_MIN_TEXTURE and face_region is None:
        priority = _detail(obj.colours, nearest, low_faces, low_positions)
        share = float(area[priority].sum() / max(area.sum(), 1e-30))
        if 0.0 < share < 1.0:
            # boost^2 * share / (boost^2 * share + 1 - share) <= DETAIL_SHARE
            limit = DETAIL_SHARE * (1.0 - share) / ((1.0 - DETAIL_SHARE) * share)
            boost = max(1.0, min(detail_boost, limit ** 0.5))
    # Gaps between UV islands: a few texels at 512 px and up, one at 64-128 px
    # where every texel counts (the background is filled anyway, see below).
    padding = 1 if texture <= 128 else 2 if texture <= 256 else 4
    # One unwrap per material: the body, then (if any) the face, each packed
    # into a texture of its own. ``groups`` holds the reduced triangles of each.
    # The face texture is the face seen from the front, one island; xatlas
    # unwraps it only when the region has no front to map from.
    front = None
    if face_region is not None:
        # Hairline to chin: FACE_REACH times the reach of the eyes, brows and
        # lips from the centre (on the test sculpt, 3% of the surface rather than the 10%
        # an unwrapped face takes, where hair and collar filled the texture).
        middle = low_positions[low_faces].mean(axis=1)
        close = np.linalg.norm(middle - centre, axis=1) <= FACE_REACH * reach
        front = _front_projection(low_positions, low_faces, close, centre, texture, padding)
        if front is not None:
            face_region = front[0]
    groups = ([np.arange(len(low_faces))] if face_region is None
              else [np.nonzero(~face_region)[0], np.nonzero(face_region)[0]])
    mappings, part_faces, part_uvs, orders = [], [], [], []
    offset = 0
    for index, group in enumerate(groups):
        if index == 1 and front is not None:
            _, mapping, uv_faces, uvs = front
            order = np.arange(len(group))
        else:
            mapping, uv_faces, uvs, order = _unwrap(
                xatlas, low_positions, low_faces[group], texture, padding,
                priority[group] if priority is not None else None, boost)
        mappings.append(mapping)
        part_faces.append(uv_faces + offset)
        part_uvs.append(uvs)
        orders.append(group[order])
        offset += len(mapping)
    mapping = np.concatenate(mappings)
    uvs = np.concatenate(part_uvs)
    all_faces = np.concatenate(part_faces)
    order = np.concatenate(orders)
    renumber = np.empty(len(order), np.int64)
    renumber[order] = np.arange(len(order))
    nearest = renumber[nearest]
    # Shading normals per corner, hard across sharp folds; corners that disagree
    # get a vertex each. Neighbours are found through the reduced mesh's own
    # vertices, so a UV seam does not cut the shading.
    corner_normals = _corner_normals(low_positions, mapping[all_faces])
    positions, uvs, vertex_normals, all_faces = _split_by_normal(
        low_positions[mapping], uvs, all_faces, corner_normals)
    sizes = np.cumsum([len(part) for part in part_faces])[:-1]
    part_faces = np.split(all_faces, sizes)
    tangents = _tangents(positions, uvs, all_faces, vertex_normals)
    per_side = samples_per_texel(texture)
    step("baking the textures", 3)
    baked = []
    first = 0
    for uv_faces in part_faces:
        last = first + len(uv_faces)
        if len(part_faces) == 1:
            local = nearest
        else:
            local = np.where((nearest >= first) & (nearest < last), nearest - first, -1)
        colour, normal_pixels, inside = _bake_maps(sculpt, obj.vertices, local, positions,
                                                     vertex_normals, tangents, uvs, uv_faces,
                                                     texture, normal_map, per_side)
        # Rows were filled with v going up; an image's first row is its top.
        image = Image.fromarray((np.flipud(colour) * 255).clip(0, 255).round().astype(np.uint8))
        if colours:
            image = _reduce_colours(image, colours, np.flipud(inside))
        normal_image = None
        if normal_pixels is not None:
            normal_image = Image.fromarray(
                (np.flipud(normal_pixels) * 255).clip(0, 255).round().astype(np.uint8))
        baked.append((image, normal_image, inside))
        first = last
    del sculpt, nearest

    out_dir = Path(out_dir) if out_dir is not None else Path(source).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    mesh_name, texture_name = output_names(Path(source))
    names = [(texture_name, normal_map_name(Path(source)))]
    if len(baked) > 1:
        names.append(face_texture_names(Path(source)))
    written = []
    for (image, normal_image, _), (base_name, normal_name) in zip(baked, names):
        # One encoding of each texture, written beside the mesh and packed inside it.
        base_png = _png_bytes(image)
        (out_dir / base_name).write_bytes(base_png)
        normal_png = None
        if normal_image is not None:
            normal_png = _png_bytes(normal_image)
            (out_dir / normal_name).write_bytes(normal_png)
        written.append((base_name, base_png, normal_name if normal_png else "", normal_png))
    mesh_path = out_dir / mesh_name
    material = texture_name[:-len("_BaseColor.png")]
    extra = [{"faces": faces, "base_png": base_png, "normal_png": normal_png,
              "name": base_name[:-len("_BaseColor.png")],
              "image_names": (base_name[:-4], normal_name[:-4] if normal_name else "")}
             for faces, (base_name, base_png, normal_name, normal_png)
             in zip(part_faces[1:], written[1:])]
    base_name, base_png, normal_name, normal_png = written[0]
    write_glb(mesh_path, positions, vertex_normals, uvs, part_faces[0], base_png, material,
              tangents=tangents if normal_png is not None else None, normal_png=normal_png,
              image_names=(base_name[:-4], normal_name[:-4] if normal_name else ""),
              nearest=texture <= NEAREST_TEXTURE, extra_parts=tuple(extra))
    if normal_png is not None:
        note = "BaseColor and a tangent-space normal map (glTF/OpenGL, green up)"
    else:
        note = "colour only (no normal map)"
    if colours:
        note += f"; BaseColor reduced to {colours} colours"
    if len(all_faces) > triangles:
        note += (f"; WARNING: reduced to {len(all_faces):,} triangles, not {triangles:,} "
                 "(the sculpt has open edges the reducer keeps)")
    face_share = 0.0
    if len(written) > 1:
        face_share = float(area[face_region].sum() / max(area.sum(), 1e-30))
        note += (f"; the face has its own {texture}x{texture} texture (second material, "
                 f"{face_share:.0%} of the surface)")
    elif face_note:
        note += f"; {face_note}"
    inside = baked[0][2]
    return {
        "source": str(source), "colour": obj.source,
        "mesh": str(mesh_path), "texture": str(out_dir / written[0][0]),
        "normal": str(out_dir / written[0][2]) if written[0][2] else "",
        "face_texture": str(out_dir / written[1][0]) if len(written) > 1 else "",
        "face_normal": str(out_dir / written[1][2]) if len(written) > 1 and written[1][2] else "",
        "face_share": round(face_share, 3), "materials": len(written),
        "source_triangles": int(len(obj.faces)), "triangles": int(len(all_faces)),
        "texture_size": texture, "colours": colours, "preset": preset,
        "samples_per_texel": per_side * per_side, "coverage": round(float(inside.mean()), 3),
        "detail_boost": round(boost, 2),
        "seconds": round(time.time() - started, 1), "note": note,
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="an .obj with ZBrush Polypaint or vertex colours")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="where to write (default: beside the source)")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="",
                        help="n64: ~1,200 triangles, 64 px, 16 colours, no normal map")
    parser.add_argument("--triangles", type=int, default=0,
                        help=f"triangle budget (default {DEFAULT_TRIANGLES:,})")
    parser.add_argument("--texture", type=int, default=0,
                        help=f"texture size, a power of two (default {DEFAULT_TEXTURE})")
    parser.add_argument("--no-normal", action="store_false", dest="normal_map", default=None,
                        help="do not bake a normal map")
    parser.add_argument("--colours", type=int, default=None,
                        help="reduce the BaseColor to this many colours (0 keeps them all)")
    parser.add_argument("--face-texture", action="store_true", dest="face_texture", default=None,
                        help="give the face a texture of its own (a second material); "
                             "on with --preset n64")
    parser.add_argument("--no-face-texture", action="store_false", dest="face_texture",
                        help="one texture for the whole model")
    args = parser.parse_args(argv)
    try:
        settings = resolve_settings(args.preset, args.triangles, args.texture,
                                    args.normal_map, args.colours,
                                    face_texture=args.face_texture)
        result = bake(args.source, args.out_dir, settings["triangles"], settings["texture"],
                      progress=lambda name, i, n: print(f"  {i + 1}/{n} {name}", flush=True),
                      normal_map=settings["normal_map"], colours=settings["colours"],
                      preset=settings["preset"], face_texture=settings["face_texture"])
    except PolypaintError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{Path(result['mesh']).name}: {result['triangles']:,} triangles "
          f"(from {result['source_triangles']:,}), "
          f"{Path(result['texture']).name} {result['texture_size']}x{result['texture_size']}"
          + (f", {Path(result['normal']).name}" if result["normal"] else "")
          + (f", {Path(result['face_texture']).name}" if result["face_texture"] else "")
          + f", {result['seconds']} s")
    print(f"note: {result['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
