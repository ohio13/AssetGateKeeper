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
2. unwrap UVs (xatlas);
3. bake the sculpt's colours into a BaseColor texture: every texel of the
   reduced mesh takes the colour of the nearest original vertices;
4. write a .glb (texture inside, ready for Unreal, Unity, Godot or Blender) and
   the texture as a .png beside it.

Steps 1-3 need ``pip install "asset-gatekeeper[bake]"`` (fast-simplification,
xatlas, scipy). Reading and previewing need only numpy and trimesh, which the
gate already requires. The source file is only ever read.

    python engine/polypaint.py SM_Hero_HP_03.obj --triangles 30000 --texture 2048
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import asset_gatekeeper as agk  # noqa: E402 - shared detection, so the gate and this agree

#: The exact command for the Python that is running this, so a Rules.bat user
#: installs into the interpreter the servers use, not some other one on PATH.
BAKE_EXTRA = f'"{sys.executable}" -m pip install fast-simplification xatlas scipy'
#: Grow coloured texels this many pixels past each UV island, so mip-maps and
#: bilinear filtering never pull in the black background along a seam.
SEAM_PADDING = 8
#: Neighbours averaged per texel: one is noisy on a dense sculpt, eight blurs.
NEIGHBOURS = 4
DEFAULT_TRIANGLES = 30_000
DEFAULT_TEXTURE = 2048


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
                             f"Install them with: {BAKE_EXTRA}")
    return modules["fast_simplification"], modules["xatlas"], modules["scipy.spatial"]


def _smooth_normals(positions, faces):
    import numpy as np
    corners = positions[faces]
    face_normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    normals = np.zeros_like(positions)
    for k in range(3):
        np.add.at(normals, faces[:, k], face_normals)
    length = np.linalg.norm(normals, axis=1, keepdims=True)
    return normals / np.where(length > 0, length, 1.0)


def _bake_texture(uvs, triangles, positions, tree, colours, size: int):
    """Rasterise each triangle in UV space; colour every texel from the sculpt."""
    import numpy as np
    image = np.zeros((size, size, 3), np.float32)
    filled = np.zeros((size, size), bool)
    pixels = uvs * size - 0.5
    for corner_ids in triangles:
        a, b, c = pixels[corner_ids]
        x0 = int(max(0, np.floor(min(a[0], b[0], c[0]))))
        x1 = int(min(size - 1, np.ceil(max(a[0], b[0], c[0]))))
        y0 = int(max(0, np.floor(min(a[1], b[1], c[1]))))
        y1 = int(min(size - 1, np.ceil(max(a[1], b[1], c[1]))))
        if x1 < x0 or y1 < y0:
            continue
        xs, ys = np.meshgrid(np.arange(x0, x1 + 1), np.arange(y0, y1 + 1))
        px, py = xs.ravel().astype(np.float64), ys.ravel().astype(np.float64)
        area = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(area) < 1e-12:
            continue
        w0 = ((b[1] - c[1]) * (px - c[0]) + (c[0] - b[0]) * (py - c[1])) / area
        w1 = ((c[1] - a[1]) * (px - c[0]) + (a[0] - c[0]) * (py - c[1])) / area
        w2 = 1.0 - w0 - w1
        inside = (w0 >= -0.02) & (w1 >= -0.02) & (w2 >= -0.02)   # a hair over: no gaps
        if not inside.any():
            continue
        p = positions[corner_ids]
        points = (w0[inside, None] * p[0] + w1[inside, None] * p[1] + w2[inside, None] * p[2])
        _, nearest = tree.query(points, k=NEIGHBOURS)
        xi, yi = xs.ravel()[inside], ys.ravel()[inside]
        image[yi, xi] = colours[nearest].mean(axis=1)
        filled[yi, xi] = True
    for _ in range(SEAM_PADDING):
        grown, reached = image.copy(), filled.copy()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            shifted = np.roll(np.roll(image, dy, 0), dx, 1)
            shifted_filled = np.roll(np.roll(filled, dy, 0), dx, 1)
            take = shifted_filled & ~reached
            grown[take] = shifted[take]
            reached |= take
        image, filled = grown, reached
    return image


def bake(source: Path, out_dir: Optional[Path] = None, triangles: int = DEFAULT_TRIANGLES,
         texture: int = DEFAULT_TEXTURE,
         progress: Optional[Callable[[str, int, int], None]] = None) -> dict:
    """Reduce, unwrap and bake ``source`` into a game asset. Returns what it wrote."""
    import numpy as np
    from PIL import Image
    import trimesh
    if texture < 16 or texture & (texture - 1):
        raise PolypaintError(f"the texture size must be a power of two (got {texture})")
    if triangles < 100:
        raise PolypaintError(f"a triangle budget of {triangles} is too small to bake into")
    simplify, xatlas, spatial = _require_bake_libraries()
    steps = 4

    def step(name: str, index: int) -> None:
        if progress is not None:
            progress(name, index, steps)

    started = time.time()
    step("reading the sculpt", 0)
    obj = read_obj(Path(source))
    step("reducing the mesh", 1)
    if len(obj.faces) > triangles:
        low_positions, low_faces = simplify.simplify(
            obj.vertices.astype(np.float32), obj.faces.astype(np.int32),
            target_reduction=1.0 - triangles / len(obj.faces))
    else:
        low_positions, low_faces = obj.vertices.astype(np.float32), obj.faces.astype(np.int32)
    low_positions = np.asarray(low_positions, dtype=np.float64)
    low_faces = np.asarray(low_faces, dtype=np.int64)
    # Reduction keeps flat or collapsed triangles it is given (a ZBrush pole, a
    # fan of slivers); the gate fails a mesh for them, so the bake drops them.
    corners = low_positions[low_faces]
    flat = agk.degenerate_mask(corners[:, 0], corners[:, 1], corners[:, 2])
    if flat.any():
        low_faces = low_faces[~flat]
    normals = _smooth_normals(low_positions, low_faces)
    step("unwrapping UVs", 2)
    mapping, uv_faces, uvs = xatlas.parametrize(low_positions.astype(np.float32),
                                                low_faces.astype(np.uint32))
    positions = low_positions[mapping]
    step("baking the colour texture", 3)
    tree = spatial.cKDTree(obj.vertices)
    pixels = _bake_texture(np.asarray(uvs, np.float64), np.asarray(uv_faces, np.int64),
                           positions, tree, obj.colours, texture)
    # Rows were filled with v going up; an image's first row is its top.
    image = Image.fromarray((np.flipud(pixels) * 255).clip(0, 255).round().astype(np.uint8))

    out_dir = Path(out_dir) if out_dir is not None else Path(source).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    mesh_name, texture_name = output_names(Path(source))
    texture_path = out_dir / texture_name
    image.save(texture_path)
    material = trimesh.visual.material.PBRMaterial(
        name=texture_name[:-len("_BaseColor.png")], baseColorTexture=image,
        metallicFactor=0.0, roughnessFactor=0.8)
    mesh = trimesh.Trimesh(vertices=positions, faces=np.asarray(uv_faces, np.int64),
                           vertex_normals=normals[mapping], process=False)
    mesh.visual = trimesh.visual.TextureVisuals(uv=np.asarray(uvs, np.float64),
                                                material=material)
    mesh_path = out_dir / mesh_name
    mesh.export(mesh_path, file_type="glb", include_normals=True)
    return {
        "source": str(source), "colour": obj.source,
        "mesh": str(mesh_path), "texture": str(texture_path),
        "source_triangles": int(len(obj.faces)), "triangles": int(len(uv_faces)),
        "texture_size": texture, "seconds": round(time.time() - started, 1),
        "note": "colour only: the sculpt's surface detail is not baked into a normal map yet",
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="an .obj with ZBrush Polypaint or vertex colours")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="where to write (default: beside the source)")
    parser.add_argument("--triangles", type=int, default=DEFAULT_TRIANGLES)
    parser.add_argument("--texture", type=int, default=DEFAULT_TEXTURE)
    args = parser.parse_args(argv)
    try:
        result = bake(args.source, args.out_dir, args.triangles, args.texture,
                      progress=lambda name, i, n: print(f"  {i + 1}/{n} {name}", flush=True))
    except PolypaintError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{Path(result['mesh']).name}: {result['triangles']:,} triangles "
          f"(from {result['source_triangles']:,}), "
          f"{Path(result['texture']).name} {result['texture_size']}x{result['texture_size']}, "
          f"{result['seconds']} s")
    print(f"note: {result['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
