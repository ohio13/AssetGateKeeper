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

A model that carries its colour in textures - an .obj with its .mtl and
images, a .gltf or .glb - bakes the same way (``read_textured``,
``TexturedSculpt``): the high surface's colour at each sample is its own
material's texture there, through its UVs, so every material ends up in one
texture (a game rip with ten materials becomes an asset with one).

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
#: n64-pak: a game with the Expansion Pak (8 MB of RAM, not 4) that shows the
#: character with little else on screen - a cutscene, a title screen. The Pak
#: adds memory, not texture memory or speed: each texture still fits the 4 KB
#: TMEM, but more of them fit in RAM, so the face gets more of its own
#: (FACE_LAYOUTS), the body can have two (``body_textures``), and the head 60%
#: of 3,000 triangles.
PRESETS: dict[str, dict] = {
    "n64": {"label": "Nintendo 64", "triangles": 1_200, "texture": 64,
            "normal_map": False, "colours": 16, "face_texture": True, "painted_parts": True},
    "n64-pak": {"label": "Nintendo 64 + Expansion Pak", "triangles": 3_000, "texture": 64,
                "normal_map": False, "colours": 16, "face_texture": True,
                "head_share": 0.6, "face_layout": "eyes-mirror", "body_textures": 1,
                "painted_parts": True},
}
#: How the face's own textures are laid out (face_texture on):
#: single       one texture, the face seen from the front;
#: mirror       one texture holding half the face, drawn mirrored for the other
#:              half (the sampler's mirror setting): twice the texels across;
#: eyes         a strip of its own for the eyes and brows (128 x 32 at 64 px:
#:              the same 4,096 texels), the rest of the face on the face texture;
#: eyes-mirror  both: half the eyes and half the face, each mirrored.
FACE_LAYOUTS = ("single", "mirror", "eyes", "eyes-mirror")
#: Texture name suffixes of the materials past the first (the body): the
#: lower body (body_textures 2 on a model with no head found), the face, the
#: eye strip, and the rest of the head (body_textures 2 on one with a face).
PART_SUFFIXES = ("_Lower", "_Face", "_Eyes", "_Head")
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
#: _pack_texels halves in on the largest scale that fits this many times.
TEXEL_PACK_STEPS = 28
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


#: Model files whose textures can be baked (besides Polypaint .obj files).
TEXTURED_SUFFIXES = (".obj", ".gltf", ".glb")


@dataclass
class TexturedModel:
    """A model that carries colour in textures, ready to bake like a sculpt."""
    vertices: "object"           # numpy (n, 3) float64, split where UVs are
    faces: "object"              # numpy (m, 3) int64
    colours: "object"            # numpy (n, 3) float32: the texture at each vertex
    uvs: "object"                # numpy (n, 2) float64
    material: "object"           # numpy (m,) int64: which of ``images`` each face uses
    images: list                 # numpy (h, w, 3) float32 0..1 per material, or None
    tints: "object"              # numpy (materials, 3) float32: colour factor per material
    source: str = "texture"


def _map_name(line: str) -> str:
    """The image a ``map_Kd`` line names: everything after the keyword and its
    options, so a name with spaces ("holster mini rtm.png") stays whole."""
    rest = line.strip().split(None, 1)[1] if len(line.split()) > 1 else ""
    words = rest.split()
    while words and words[0].startswith("-"):      # -s 1 1 1, -bm 0.5, ...
        option = words.pop(0)
        count = {"-s": 3, "-o": 3, "-t": 3, "-mm": 2}.get(option, 1)
        del words[:count]
    return " ".join(words).replace("\\", "/")


def _obj_textures(path: Path):
    """(problem, textures found, textures missing) for an .obj's materials:
    ``problem`` is why none can be baked ('' when at least one texture is
    there); a missing one (a rip's mat5 named just ".png") bakes in the
    material's flat colour instead."""
    libraries, has_uvs = [], False
    try:
        # Line by line, and only until the first UV: the review page asks this
        # of every .obj it shows, and a scan can be tens of MB.
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("mtllib ") and len(line.split(None, 1)) > 1:
                    libraries.append(line.split(None, 1)[1].strip())
                elif line.startswith("vt "):
                    has_uvs = True
                    if libraries:
                        break
    except OSError as exc:
        return f"cannot read {path.name}: {exc}", [], []
    if not libraries:
        return f"{path.name} names no .mtl (material file), so it has no textures", [], []
    if not has_uvs:
        return f"{path.name} has no UVs (vt lines), so a texture cannot be placed on it", [], []
    found, missing = [], []
    for library in libraries:
        mtl = path.parent / library
        if not mtl.is_file():
            return (f"{path.name} names {library}, which is not beside it: put the .mtl "
                    "and the textures it names next to the .obj"), [], []
        for line in mtl.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.strip().split()
            if parts and parts[0].lower() == "map_kd":
                name = _map_name(line)
                image = path.parent / name
                (found if name and image.is_file() else missing).append(name or "(no name)")
    if found:
        return "", sorted(set(found)), sorted(set(missing))
    if missing:
        return (f"{', '.join(libraries)} names textures that are not there "
                f"({', '.join(sorted(set(missing))[:3])}): put them next to the .obj"), [], missing
    return f"{', '.join(libraries)} names no texture (map_Kd)", [], []


def _mtl_problem(path: Path) -> str:
    """Why an .obj's textures cannot be baked at all, or ''. Game rips often
    come without their .mtl or images."""
    return _obj_textures(path)[0]




def bake_source(path: Path) -> str:
    """What a bake would take colour from: 'polypaint', 'vertex colours',
    'texture', or '' when there is nothing to bake. Cheap: no mesh is read."""
    suffix = path.suffix.lower()
    if suffix == ".obj":
        painted = colour_source(path)
        if painted:
            return painted
        return "texture" if not _mtl_problem(path) else ""
    if suffix in (".gltf", ".glb"):
        try:
            if suffix == ".glb":                  # only the JSON chunk, not the mesh data
                with open(path, "rb") as handle:
                    header = handle.read(20)
                    length = int.from_bytes(header[12:16], "little")
                    if length > 64 * 1024 * 1024:
                        return ""
                    document = json.loads(handle.read(length))
            else:
                document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(document, dict):
            return ""
        for material in document.get("materials") or []:
            if (material.get("pbrMetallicRoughness") or {}).get("baseColorTexture"):
                return "texture"
    return ""


def bake_materials(path: Path) -> list:
    """[(material name, texture file or '')] of a textured .obj or .gltf/.glb,
    in file order: what the review page lists for "leave out" (a rip's
    materials are called mat1, mat2... - the texture says which is which)."""
    found: list = []
    suffix = path.suffix.lower()
    try:
        if suffix == ".obj":
            libraries = []
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("mtllib ") and len(line.split(None, 1)) > 1:
                        libraries.append(line.split(None, 1)[1].strip())
                    elif line.startswith(("v ", "vt ", "f ")) and libraries:
                        break
            for library in libraries:
                mtl = path.parent / library
                if not mtl.is_file():
                    continue
                for line in mtl.read_text(encoding="utf-8", errors="replace").splitlines():
                    words = line.strip().split()
                    if words and words[0] == "newmtl" and len(words) > 1:
                        found.append([line.strip().split(None, 1)[1], ""])
                    elif words and words[0].lower() == "map_kd" and found:
                        found[-1][1] = _map_name(line)
        elif suffix in (".gltf", ".glb"):
            if suffix == ".glb":
                with open(path, "rb") as handle:
                    header = handle.read(20)
                    document = json.loads(handle.read(int.from_bytes(header[12:16], "little")))
            else:
                document = json.loads(path.read_text(encoding="utf-8"))
            images = document.get("images") or []
            textures = document.get("textures") or []
            for index, material in enumerate(document.get("materials") or []):
                name = material.get("name") or f"material {index}"
                texture = ((material.get("pbrMetallicRoughness") or {})
                           .get("baseColorTexture") or {}).get("index")
                image = ""
                if isinstance(texture, int) and texture < len(textures):
                    source = textures[texture].get("source")
                    if isinstance(source, int) and source < len(images):
                        image = images[source].get("uri") or images[source].get("name") or ""
                found.append([name, image])
    except (OSError, ValueError):
        return []
    return [tuple(item) for item in found]


def bake_problem(path: Path) -> str:
    """Why ``path`` cannot be baked, in words, or '' when it can."""
    if path.suffix.lower() not in TEXTURED_SUFFIXES:
        return (f"{path.name}: only an .obj (ZBrush Polypaint, vertex colours or textures) "
                "or a textured .gltf/.glb can be baked")
    if bake_source(path):
        return ""
    if path.suffix.lower() == ".obj":
        return (f"{path.name} has no Polypaint or vertex colours, and no textures to bake: "
                + _mtl_problem(path))
    return f"{path.name} has no base colour texture to bake"


def _image_array(image):
    """RGBA, 0..1: the alpha says where a decal or a hair card is see-through."""
    import numpy as np
    if image is None:
        return None
    return np.asarray(image.convert("RGBA"), np.float32) / 255.0


#: A material whose texture is see-through (alpha under a half) on at least
#: this share of its texels is a decal or cut-out (hair cards, markings): the
#: surface under it shows through where it is see-through.
SEE_THROUGH_SHARE = 0.01


def read_textured(path: Path, skip_materials=()) -> TexturedModel:
    """Every mesh of a textured .obj (with its .mtl and images) or .gltf/.glb,
    placed by its node, with its UVs and its material's base colour texture.
    Meshes whose material is named in ``skip_materials`` are left out: a game
    rip's outline shell (a slightly larger copy of the body, drawn inside out
    for a cel-shaded edge), a helmet, a weapon."""
    import numpy as np
    import trimesh
    problem = bake_problem(path)
    if problem:
        raise PolypaintError(problem)
    try:
        # The gate's loader: an .obj with "-1.#QNAN0" normals (a game rip) reads.
        scene = agk.load_trimesh_scene(str(path), process=False)
    except Exception as exc:                     # noqa: BLE001 - any reader failure, said plainly
        raise PolypaintError(f"cannot read {path.name}: {exc}") from exc
    vertices, faces, uvs, material = [], [], [], []
    images, tints, keys = [], [], {}
    offset = 0
    for node in scene.graph.nodes_geometry:
        transform, name = scene.graph[node]
        mesh = scene.geometry[name]
        if not hasattr(mesh, "faces") or len(mesh.faces) == 0:
            continue
        visual = mesh.visual
        uv = getattr(visual, "uv", None)
        found = getattr(visual, "material", None)
        if skip_materials and getattr(found, "name", None) in skip_materials:
            continue
        if uv is None or found is None or len(uv) != len(mesh.vertices):
            uv = np.zeros((len(mesh.vertices), 2))
        image = getattr(found, "baseColorTexture", None)
        if image is None:
            image = getattr(found, "image", None)
        # glTF multiplies its texture by baseColorFactor; an .obj's Kd only
        # colours a material without a map_Kd (as Blender and engines read it:
        # trimesh writes Kd 0.4 beside a texture, which darkened a bake to 40%).
        factor = getattr(found, "baseColorFactor", None)
        if factor is None and image is None:
            factor = getattr(found, "diffuse", None)
        tint = (np.asarray(factor, np.float32)[:3] / 255.0 if factor is not None
                else np.ones(3, np.float32))
        key = id(found)
        if key not in keys:
            keys[key] = len(images)
            images.append(_image_array(image))
            tints.append(tint)
        points = trimesh.transformations.transform_points(np.asarray(mesh.vertices, np.float64),
                                                           transform)
        vertices.append(points)
        faces.append(np.asarray(mesh.faces, np.int64) + offset)
        uvs.append(np.asarray(uv, np.float64))
        material.append(np.full(len(mesh.faces), keys[key], np.int64))
        offset += len(points)
    if not faces:
        raise PolypaintError(f"{path.name} has no triangles")
    if all(image is None for image in images):
        raise PolypaintError(f"{path.name}: its textures could not be read")
    model = TexturedModel(np.concatenate(vertices), np.concatenate(faces), None,
                          np.concatenate(uvs), np.concatenate(material), images,
                          np.asarray(tints, np.float32))
    # One colour per vertex, sampled from its own material: what finds the face
    # and the fine detail, as Polypaint does on a sculpt.
    owner = np.zeros(len(model.vertices), np.int64)
    owner[model.faces.ravel()] = np.repeat(model.material, 3)
    model.colours = _sample_materials(model.images, model.tints, owner, model.uvs)
    return model


def _sample_materials(images, tints, material, uv, with_alpha: bool = False):
    """Base colour at each ``uv`` (n, 2) of each ``material`` (n,): bilinear,
    repeating past 0-1 (a tiling texture), v going up; a material with no
    image is its flat colour. With ``with_alpha``, (colour, alpha)."""
    import numpy as np
    out = np.empty((len(uv), 4), np.float32)
    out[:, 3] = 1.0
    for index in np.unique(material):
        rows = np.nonzero(material == index)[0]
        image = images[int(index)]
        if image is None:
            out[rows, :3] = tints[int(index)]
            continue
        height, width = image.shape[:2]
        x = (uv[rows, 0] % 1.0) * width - 0.5
        y = (1.0 - uv[rows, 1] % 1.0) * height - 0.5
        x0, y0 = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
        fx, fy = (x - x0)[:, None].astype(np.float32), (y - y0)[:, None].astype(np.float32)
        x1, y1 = (x0 + 1) % width, (y0 + 1) % height
        x0, y0 = x0 % width, y0 % height
        corners = [image[y0, x0], image[y0, x1], image[y1, x0], image[y1, x1]]
        if image.shape[2] == 4:
            # Colour weighted by how solid each texel is: the clear texels round
            # a hair strand are stored black, and blended in plainly they
            # darkened every strand's edge (blonde hair cards baked olive).
            corners = [np.concatenate([c[:, :3] * c[:, 3:4], c[:, 3:4]], axis=1) for c in corners]
        top = corners[0] * (1 - fx) + corners[1] * fx
        bottom = corners[2] * (1 - fx) + corners[3] * fx
        texel = top * (1 - fy) + bottom * fy
        if image.shape[2] == 4:
            texel[:, :3] /= np.maximum(texel[:, 3:4], 1e-6)
        out[rows, :3] = texel[:, :3] * tints[int(index)]
        out[rows, 3] = texel[:, 3] if texel.shape[1] == 4 else 1.0
    out = out.clip(0.0, 1.0)
    return (out[:, :3], out[:, 3]) if with_alpha else out[:, :3]


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


def part_texture_names(source: Path, suffix: str = "") -> tuple[str, str]:
    """(BaseColor, Normal) of one material of a bake: T_Hero_LP_03_BaseColor.png
    for the body, T_Hero_LP_03<suffix>_BaseColor.png for the others (PART_SUFFIXES)."""
    base = output_names(source)[1][:-len("_BaseColor.png")] + suffix
    return f"{base}_BaseColor.png", f"{base}_Normal.png"


def face_texture_names(source: Path) -> tuple[str, str]:
    """(BaseColor, Normal) of the face's own material: T_Hero_LP_03_Face_BaseColor.png
    and T_Hero_LP_03_Face_Normal.png."""
    return part_texture_names(source, "_Face")


def texture_names(source: Path) -> list:
    """Every texture file a bake of ``source`` can write, whatever its settings:
    what a later bake checks for left-overs."""
    names = [normal_map_name(source)]
    for suffix in PART_SUFFIXES:
        names.extend(part_texture_names(source, suffix))
    return names


def material_count(face_texture: bool, face_layout: str = "single", body_textures: int = 1) -> int:
    """Materials (one texture each) a bake with these settings makes, at most."""
    face = (2 if face_layout.startswith("eyes") else 1) if face_texture else 0
    return body_textures + face


def resolve_settings(preset: str = "", triangles: int = 0, texture: int = 0,
                     normal_map: Optional[bool] = None, colours: Optional[int] = None,
                     max_triangles: int = 0, max_texture: int = 0,
                     face_texture: Optional[bool] = None, max_materials: int = 0,
                     head_share: Optional[float] = None, face_layout: str = "",
                     body_textures: int = 0, painted_parts: Optional[bool] = None) -> dict:
    """What a bake will use: explicit values, else the preset's, else the defaults,
    and never more than ``max_triangles`` / ``max_texture`` when those are given
    (a folder's rules). Each face texture and a second body texture is another
    material, so ``max_materials`` turns them off - the lower body first, then
    the eye strip, then the face - and ``face_texture_note`` says why. Shared
    by the command line and the review page."""
    if preset and preset not in PRESETS:
        raise PolypaintError(f"unknown bake preset '{preset}' (choose: {', '.join(PRESETS)})")
    if face_layout and face_layout not in FACE_LAYOUTS:
        raise PolypaintError(f"unknown face layout '{face_layout}' (choose: {', '.join(FACE_LAYOUTS)})")
    if body_textures not in (0, 1, 2):
        raise PolypaintError(f"the body can have 1 or 2 textures (got {body_textures})")
    chosen = PRESETS.get(preset, {})
    settings = {
        "preset": preset,
        "triangles": int(triangles or chosen.get("triangles", DEFAULT_TRIANGLES)),
        "texture": int(texture or chosen.get("texture", DEFAULT_TEXTURE)),
        "normal_map": chosen.get("normal_map", True) if normal_map is None else bool(normal_map),
        "colours": int(chosen.get("colours", 0) if colours is None else colours),
        "face_texture": (bool(chosen.get("face_texture", False)) if face_texture is None
                         else bool(face_texture)),
        "head_share": float(chosen.get("head_share", 0.0) if head_share is None else head_share),
        "face_layout": face_layout or chosen.get("face_layout", "single"),
        "body_textures": int(body_textures or chosen.get("body_textures", 1)),
        "painted_parts": (bool(chosen.get("painted_parts", False)) if painted_parts is None
                          else bool(painted_parts)),
        "face_texture_note": "",
    }
    if max_materials > 0:
        dropped = []
        if material_count(settings["face_texture"], settings["face_layout"],
                          settings["body_textures"]) > max_materials and settings["body_textures"] > 1:
            settings["body_textures"] = 1
            dropped.append("no second body texture")
        if material_count(settings["face_texture"], settings["face_layout"], 1) > max_materials \
                and settings["face_layout"].startswith("eyes"):
            settings["face_layout"] = settings["face_layout"].replace("eyes-", "").replace(
                "eyes", "single")
            dropped.append("no eye strip")
        if settings["face_texture"] and max_materials < 2:
            settings["face_texture"] = False
            dropped.append("no separate face texture")
        if dropped:
            settings["face_texture_note"] = (
                f"{', '.join(dropped)}: the rules here allow {max_materials} material "
                f"slot{'s' if max_materials != 1 else ''}, and each texture is a material of its own")
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
SOFT_CREASE_DEGREES = 110.0
#: ... the two ends of a sliver's short side - shorter than this share of the
#: model's size, and than SOFT_SLIVER of the triangle's longest - shaded as one.
SOFT_REACH = 0.0025
SOFT_SLIVER = 0.3
#: ... and no normal of it turned further from the viewer than this (the cosine).
SOFT_FACING = 0.35


def _corner_normals(positions, faces, crease_degrees: float = CREASE_DEGREES, soft=None):
    """A normal for each corner of each triangle, shape (faces, 3, 3): the
    area-weighted average of the triangles at that corner's vertex that face
    within ``crease_degrees`` of this one.

    One averaged normal per vertex (what the bake used before) goes wrong where
    a 1,200-triangle head folds sharply - hair over the cheek, the jaw against
    the neck: the average points away from some of the triangles that share it,
    and they shade as if lit from behind. On the test sculpt's N64 bake that was a corner
    of 4 triangles in 10 on the body, and of most of the face (12 Nov 2026).

    ``soft`` (bool per triangle): those fold only past SOFT_CREASE_DEGREES - a
    face, where a reduced chin is a wedge whose two sides, each shaded flat,
    showed a line down its middle (13 Nov 2026)."""
    import numpy as np
    corners = positions[faces]
    weighted = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    length = np.linalg.norm(weighted, axis=1, keepdims=True)
    unit = weighted / np.where(length > 0, length, 1.0)
    limit = np.full(len(faces), np.cos(np.radians(crease_degrees)))
    if soft is not None:
        limit[np.asarray(soft, bool)] = np.cos(np.radians(SOFT_CREASE_DEGREES))
    # Corner k of face f is row 3f+k; group the rows by the vertex they sit on -
    # in a face, the two ends of a sliver's short side (shorter than SOFT_REACH)
    # count as one: a chin cut down the middle had a sliver between its halves, a vertex
    # to each side of it, and each half was shaded to its own side. (Any two
    # vertices that near, joined or not, was tried: the upper lip took in the
    # lower, across the mouth.)
    vertex = faces.ravel()
    if soft is not None and np.asarray(soft, bool).any():
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components
        soft = np.asarray(soft, bool)
        edges = np.stack([faces[soft][:, [0, 1]], faces[soft][:, [1, 2]], faces[soft][:, [2, 0]]],
                         axis=1)                                    # (f, 3, 2)
        long = np.linalg.norm(positions[edges[..., 0]] - positions[edges[..., 1]], axis=2)
        reach = SOFT_REACH * float(np.ptp(positions, axis=0).max())
        # Only the short side of a sliver: short edges of well-shaped small
        # triangles, joined in chains, made one spot of a whole nose.
        short = edges[(long < reach) & (long < SOFT_SLIVER * long.max(axis=1, keepdims=True))]
        if len(short):
            graph = coo_matrix((np.ones(len(short), np.int8), (short[:, 0], short[:, 1])),
                               shape=(len(positions), len(positions)))
            _, spot = connected_components(graph, directed=False)
            first = np.unique(spot, return_index=True)[1][spot]     # one vertex stands for its spot
            vertex = np.where(np.repeat(soft, 3), first[vertex], vertex)
    row = np.argsort(vertex, kind="stable")
    vertex = vertex[row]
    starts = np.flatnonzero(np.r_[True, vertex[1:] != vertex[:-1]])
    ends = np.r_[starts[1:], len(vertex)]
    result = np.empty((len(faces) * 3, 3))
    for start, end in zip(starts, ends):
        rows = row[start:end]
        here = rows // 3
        close = unit[here] @ unit[here].T >= limit[here][:, None]
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


#: Where a triangle of a see-through material is tested for being solid
#: itself: its corners (just inside), edge middles and centre.
_CLEAR_SAMPLES = ((0.9, 0.05, 0.05), (0.05, 0.9, 0.05), (0.05, 0.05, 0.9),
                  (0.5, 0.5, 0.0), (0.0, 0.5, 0.5), (0.5, 0.0, 0.5), (1 / 3, 1 / 3, 1 / 3))


class TexturedSculpt(Sculpt):
    """A textured model as the high surface: its colour at a point is its own
    texture there (through its UVs), not a blend of vertex colours."""

    def __init__(self, model: "TexturedModel", spatial, see_through: bool = True):
        import numpy as np
        super().__init__(model.vertices, model.faces, model.colours, spatial)
        self.uvs = model.uvs
        self.material = model.material
        self.images = model.images
        self.tints = model.tints
        # Decals and hair cards: where they are see-through, the opaque surface
        # under them shows. A rip's suit markings were a sheet over the body,
        # three quarters see-through, and its clear parts baked as colour
        # (12 Nov 2026).
        self.under = None
        #: Per triangle: solid all over (None: every triangle is).
        self.solid = None
        clear = [image is not None and image.shape[2] == 4
                 and float((image[..., 3] < 0.5).mean()) >= SEE_THROUGH_SHARE
                 for image in self.images]
        self.clear = np.asarray(clear, bool)
        if see_through and self.clear.any():
            # Opaque = every triangle that is not itself see-through: a face
            # texture that also holds the lashes (see-through round them) made
            # the whole face "clear", and what showed under the lashes was the
            # nearest hair (13 Nov 2026).
            opaque = ~self.clear[model.material]
            for index in np.nonzero(self.clear)[0]:
                rows = np.nonzero(model.material == index)[0]
                if not len(rows):
                    continue
                solid = np.ones(len(rows), bool)
                for weights in _CLEAR_SAMPLES:
                    uv = np.einsum("k,nkc->nc", np.asarray(weights), model.uvs[model.faces[rows]])
                    solid &= _sample_materials(self.images, self.tints, model.material[rows], uv,
                                               with_alpha=True)[1] >= 0.5
                opaque[rows] = solid
        if see_through and self.clear.any() and opaque.any() and not opaque.all():
            self.solid = opaque
            used, local = np.unique(model.faces[opaque], return_inverse=True)
            self.under = TexturedSculpt(
                TexturedModel(model.vertices[used], local.reshape(-1, 3), model.colours[used],
                              model.uvs[used], model.material[opaque], model.images, model.tints),
                spatial, see_through=False)

    def colour_alpha(self, face, bary):
        """(colour, how solid) of the surface itself at each point, nothing
        showing through."""
        import numpy as np
        uv = np.einsum("nk,nkc->nc", bary.astype(np.float64), self.uvs[self.faces[face]])
        return _sample_materials(self.images, self.tints, self.material[face], uv,
                                 with_alpha=True)

    def colour(self, face, bary):
        import numpy as np
        colour, alpha = self.colour_alpha(face, bary)
        if self.under is None:
            return colour
        blend = np.nonzero(alpha < 0.999)[0]
        if len(blend):
            below_face, below_bary, _ = self.under.closest(self.point(face[blend], bary[blend]))
            below = self.under.colour(below_face, below_bary)
            a = alpha[blend, None]
            colour[blend] = colour[blend] * a + below * (1.0 - a)
        return colour


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
        _, found = tree.query(p, k=min(8, len(spots)), workers=-1)
        candidates = owner[found.reshape(len(p), -1)]
        rel = a[candidates] - p[:, None, :]
        v, w = _closest_on_triangles(rel, ab[candidates], ac[candidates])
        offset = rel + v[..., None] * ab[candidates] + w[..., None] * ac[candidates]
        distance = np.where(np.isfinite(_dot(offset, offset)), _dot(offset, offset), np.inf)
        nearest[start:start + len(p)] = candidates[np.arange(len(p)), np.argmin(distance, axis=1)]
    return nearest


#: A point of the sculpt goes to its own piece's nearest reduced triangle when
#: that is no more than this share of the model's size further than the nearest of all.
PIECE_SLACK = 0.005


def _closest_by_piece(points, piece, positions, faces, spatial, origin=None):
    """_closest_low_triangles, each of the sculpt's pieces (``piece``: one
    label per point) kept to its own: a piece that has triangles of its own
    in the reduced mesh - a suit and its collar, a shell of hair - takes the
    nearest of those, not a nearer triangle of another piece. A reduced neck
    reached under the collar's rim, the collar's points were nearest it, and
    it was baked the collar's colour (13 Nov 2026). A piece with none (eyes
    painted on, a decal) takes the nearest triangle of all, as before.
    ``origin``: the sculpt piece each reduced vertex lies on."""
    import numpy as np
    nearest = _closest_low_triangles(points, positions, faces, spatial)
    count, part = _pieces(len(positions), faces)
    if count < 2 or piece is None or origin is None:
        return nearest
    # The piece each reduced part came from: the one most of its corners lie
    # on. (Not the one most of the points nearest it are of: four eyeballs of
    # 113,000 triangles each outnumber the face they sit in.)
    pieces = int(piece.max()) + 1
    pairs, tally = np.unique((np.repeat(part, 3).astype(np.int64) * pieces
                              + origin[faces].ravel()), return_counts=True)
    owner = np.full(count, -1, np.int64)
    best = np.zeros(count, np.int64)
    for pair, number in zip(pairs, tally):
        if number > best[pair // pieces]:
            best[pair // pieces], owner[pair // pieces] = number, pair % pieces
    owned = np.zeros(pieces, bool)
    owned[owner[owner >= 0]] = True
    astray = np.nonzero(owned[piece] & (owner[part[nearest]] != piece))[0]
    corners = positions[faces].astype(np.float64)

    def distance(rows, triangles):
        a = corners[triangles, 0]
        ab, ac = corners[triangles, 1] - a, corners[triangles, 2] - a
        rel = a - points[rows]
        v, w = _closest_on_triangles(rel, ab, ac)
        return np.linalg.norm(rel + v[:, None] * ab + w[:, None] * ac, axis=1)

    # ... if its own are about as near. Where a piece has none of its own left
    # near by (a fringe over a brow, the hair under it reduced away), it is
    # painted on what is there.
    slack = PIECE_SLACK * float(np.ptp(positions, axis=0).max())
    for label in np.unique(piece[astray]):
        rows = astray[piece[astray] == label]
        own = np.nonzero(owner[part] == label)[0]
        mine = own[_closest_low_triangles(points[rows], positions, faces[own], spatial)]
        near = distance(rows, mine) <= distance(rows, nearest[rows]) + slack
        nearest[rows[near]] = mine[near]
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


#: A sculpt triangle is drawn on the reduced triangle nearest it only when
#: it is no further above it than this share of the model's largest extent.
BAKE_LIFT = 0.012
#: A part seen from the front (_bake_view) is not painted with what lies more
#: than this share of its size behind it.
VIEW_BEHIND = 0.12


def _bake_maps(sculpt: Sculpt, sculpt_vertices, nearest, positions, normals, tangents, uvs,
               triangles, size: int, want_normal: bool, per_side: int, view=None):
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

    With ``view`` = (right, up, towards the viewer) the part's UVs are the
    model seen along that view (a face texture, _projected_uvs), and step 2
    draws the sculpt as seen along it too: each sculpt triangle where it is
    in the view, the one nearest the viewer on top. Laid onto reduced
    triangles one by one, an eyeball in its socket landed bent round the
    socket's walls, its side where the iris should be (13 Nov 2026).
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
    if view is not None:
        return _bake_view(sculpt, sculpt_vertices, refs, positions, normals, tangents, uvs,
                          triangles, size, want_normal, per_side, view, low_owner, low_bary,
                          low_chart, charts)
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
    # Not what hangs clear of the surface: a ponytail beside a neck whose own
    # triangles went in the reduction was painted on the neck.
    near_by = height.min(axis=1) <= BAKE_LIFT * float(np.ptp(positions, axis=0).max())
    if not near_by.all():
        face_of, onto = face_of[near_by], onto[near_by]
        uv_corners, height = uv_corners[near_by], height[near_by]
    high_owner, high_bary = _rasterise(uv_corners.reshape(-1, 2),
                                       np.arange(3 * len(face_of)).reshape(-1, 3), grid,
                                       depth=height.reshape(-1), labels=charts[onto],
                                       label_map=low_chart)
    del uv_corners, height, low_chart, onto
    high_face = np.where(high_owner >= 0, face_of[np.maximum(high_owner, 0)], -1).astype(np.int32)
    del high_owner, face_of
    return _bake_samples(sculpt, positions, normals, tangents, triangles, size, want_normal,
                         per_side, low_owner, low_bary, high_face, high_bary)


def _bake_view(sculpt: Sculpt, sculpt_vertices, refs, positions, normals, tangents, uvs,
               triangles, size: int, want_normal: bool, per_side: int, view, low_owner, low_bary,
               low_chart, charts):
    """_bake_maps for a part seen along ``view`` (right, up, towards the
    viewer): the sculpt's triangles that belong to the part (a corner nearest
    it), facing the viewer, drawn where the view puts them."""
    import numpy as np
    right, up, towards = (np.asarray(axis, np.float64) for axis in view[:3])
    grid = size * per_side
    # The part's UVs are its positions along right and up, scaled and shifted
    # (_projected_uvs); the same for the sculpt, found from the part's corners.
    used = np.unique(triangles)
    if len(view) > 3 and view[3] is not None:
        # Folded about a line (_folded_uvs): the half the texture is drawn
        # from says how; the sculpt's other half lands off the texture.
        axis, side = view[3]
        kept = used[side * (positions[used] @ right - axis) >= -1e-9]
        used = kept if len(kept) >= 3 else used
    # Two boxes side by side (_paired_uvs): the gap between them closed, for
    # the sculpt as for the part.
    gap = view[4] if len(view) > 4 and view[4] is not None else None

    def across_of(points):
        x = points @ right
        return _close_gap(x, gap[0], gap[1]) if gap is not None else x

    flat = np.stack([across_of(positions[used]), positions[used] @ up, np.ones(len(used))], axis=1)
    to_uv, *_ = np.linalg.lstsq(flat, uvs[used], rcond=None)            # (3, 2)
    face_of = np.nonzero((refs >= 0).any(axis=1))[0]
    face_of = face_of[_dot(sculpt.face_normals[face_of].astype(np.float64), towards[None, :]) > 0.0]
    corners = sculpt_vertices[sculpt.faces[face_of]].reshape(-1, 3)
    uv_corners = (np.stack([across_of(corners), corners @ up, np.ones(len(corners))], axis=1)
                  @ to_uv) * grid
    depth = (corners @ towards).astype(np.float32)
    # One island: every triangle may draw on every sample of the part.
    high_owner, high_bary = _rasterise(uv_corners, np.arange(len(corners)).reshape(-1, 3), grid,
                                       depth=depth)
    high_face = np.where(high_owner >= 0, face_of[np.maximum(high_owner, 0)], -1).astype(np.int32)
    # Not what lies far behind the part: where the reduced chin reaches past
    # the sculpt's, the view shows the neck under it, in shadow. Those samples
    # take the nearest surface instead (_bake_samples).
    reach = VIEW_BEHIND * max(float(np.ptp(positions[used] @ right)),
                              float(np.ptp(positions[used] @ up)))
    shown = np.nonzero((high_owner >= 0) & (low_owner >= 0))[0]
    if len(shown):
        seen_depth = (depth[high_owner[shown][:, None] * 3 + np.arange(3)]
                      * high_bary[shown]).sum(axis=1)
        part_depth = ((positions[triangles[low_owner[shown]]] @ towards)
                      * low_bary[shown]).sum(axis=1)
        high_face[shown[seen_depth < part_depth - reach]] = -1
    below = None
    solid = getattr(sculpt, "solid", None)
    if solid is not None:
        # What shows through a see-through triangle (a lash card) is what is
        # behind it in the view - the eye - not the solid surface nearest it
        # (the lid above): the solid triangles drawn on their own.
        keep = np.nonzero(solid[face_of])[0]
        if len(keep):
            rows = (keep[:, None] * 3 + np.arange(3)).ravel()
            owner, below_bary = _rasterise(uv_corners[rows], np.arange(len(rows)).reshape(-1, 3),
                                           grid, depth=depth[rows])
            below = (np.where(owner >= 0, face_of[keep[np.maximum(owner, 0)]], -1).astype(np.int32),
                     below_bary)
    del uv_corners, depth, corners
    return _bake_samples(sculpt, positions, normals, tangents, triangles, size, want_normal,
                         per_side, low_owner, low_bary, high_face, high_bary, below)


def _bake_samples(sculpt: Sculpt, positions, normals, tangents, triangles, size: int,
                  want_normal: bool, per_side: int, low_owner, low_bary, high_face, high_bary,
                  below=None):
    """The textures from the two drawings of _bake_maps: per sample, the reduced
    triangle (``low_owner``, ``low_bary``) and the sculpt triangle on it
    (``high_face``, ``high_bary``). ``below``: per sample, the solid sculpt
    triangle behind it (triangle, barycentric), shown through a see-through one."""
    import numpy as np
    grid = size * per_side

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
        if below is not None:
            own, alpha = sculpt.colour_alpha(face[used], bary[used])
            behind = below[0][band][used].astype(np.int64)
            thin = np.nonzero((alpha < 0.999) & (behind >= 0) & (highs[used] >= 0))[0]
            if len(thin):
                under, _ = sculpt.colour_alpha(behind[thin],
                                               below[1][band][used][thin].astype(np.float64))
                a = alpha[thin, None]
                colour[thin] = own[thin] * a + under * (1.0 - a)
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


def _pack_texels(uvs, faces, width: int, height: int):
    """UV islands (xatlas', any scale) packed into a ``width`` x ``height`` texture
    a whole block of texels to each: no texel holds two islands, and no gap is
    needed between them. Returns (uvs, bake uvs), both 0-1 across the texture
    (the bake's across a square of the longer side).

    The UVs to draw with keep each island between the centres of its block's
    outer texels, so a filtered sample at its very edge is still all its own;
    the bake's spread it over the whole block, so every texel of the block is
    filled from it. An island's shape is stretched to its block, each way on
    its own - at a texel or two an island that costs nothing.

    Why (14 Nov 2026): xatlas packs to a tenth of a texel and leaves gaps, and
    with an island to every seven triangles a 64x32 body texture had a third
    of its texels in use, one in four of those shared by two islands, and
    half the islands with no texel of their own - a suit's triangles came out
    the colour of the skin or hair packed beside them."""
    import numpy as np
    uvs = np.asarray(uvs, np.float64)
    island_of_face = _chart_ids(faces, len(uvs))
    island = np.zeros(len(uvs), np.int64)
    island[faces] = island_of_face[:, None]
    labels, island = np.unique(island[np.unique(faces)], return_inverse=True)
    members = np.unique(faces)
    count = len(labels)
    low = np.full((count, 2), np.inf)
    high = np.full((count, 2), -np.inf)
    np.minimum.at(low, island, uvs[members])
    np.maximum.at(high, island, uvs[members])
    extent = high - low
    turned = extent[:, 1] > extent[:, 0]                 # the long side across
    extent[turned] = extent[turned][:, ::-1]

    def blocks(scale: float):
        size = np.maximum(np.ceil(extent * scale - 1e-9), 1).astype(np.int64)
        return np.minimum(size, [width, height])

    def place(size):
        """Rows, tallest blocks first; a block goes in the first row with room."""
        order = np.lexsort((-size[:, 0], -size[:, 1]))
        at = np.zeros((count, 2), np.int64)
        rows = []                                        # [y, height, used width]
        top = 0
        for index in order:
            wide, tall = int(size[index, 0]), int(size[index, 1])
            for row in rows:
                if row[1] >= tall and row[2] + wide <= width:
                    at[index] = (row[2], row[0])
                    row[2] += wide
                    break
            else:
                if top + tall > height:
                    return None
                rows.append([top, tall, wide])
                at[index] = (0, top)
                top += tall
        return at

    longest = max(float(extent.max()), 1e-12)
    fits, too_big = 0.0, width / longest * 2.0
    best = None
    for _ in range(TEXEL_PACK_STEPS):
        scale = 0.5 * (fits + too_big)
        size = blocks(scale)
        at = place(size) if (extent * scale <= [width + 1e-9, height + 1e-9]).all() else None
        if at is None:
            too_big = scale
        else:
            fits, best = scale, (at, size)
    if best is None:
        size = np.ones((count, 2), np.int64)             # a texel each ...
        at = place(size)
        if at is None:                                   # ... or, with more islands than texels, shared
            index = np.arange(count) % (width * height)
            at = np.stack([index % width, index // width], axis=1)
        best = (at, size)
    at, size = best
    local = uvs[members] - low[island]
    local[turned[island]] = np.stack([local[turned[island], 1],
                                      extent[island[turned[island]], 1] - local[turned[island], 0]],
                                     axis=1)
    share = np.where(extent[island] > 1e-12, local / np.maximum(extent[island], 1e-12), 0.5)
    drawn = np.zeros_like(uvs)
    baked = np.zeros_like(uvs)
    drawn[members] = (at[island] + 0.5 + share * (size[island] - 1)) / [width, height]
    baked[members] = (at[island] + share * size[island]) / max(width, height)
    return drawn, baked


def _unwrap(xatlas, positions, faces, size: int, padding: int, priority=None,
            boost: float = 1.0, shape=None, packed=None):
    """xatlas charts packed into ``size`` x ``size`` with ``padding`` texels between
    islands. Returns (vertex mapping, faces, uvs, order): ``order`` is the input
    triangle each output triangle came from.

    ``priority`` (one flag per triangle) is unwrapped as a mesh of its own,
    ``boost`` times larger, so its islands get boost^2 times the texels: the
    face and other fine colour detail. xatlas' first guess at the scale usually
    overshoots the size (and the padding then shrinks when the UVs are fitted
    back), so it is re-packed at the scale that fits.

    A small texture (to NEAREST_TEXTURE) is packed by the texel instead
    (_pack_texels), into ``shape`` (width, height) if given: the UVs that come
    back are across that shape, and ``packed`` (a dict) is given "bake_uvs",
    the ones to bake with."""
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
    small = size <= NEAREST_TEXTURE          # only the islands are wanted: one pass
    for attempt in range(1 if small else 4):
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
    if small:
        wide, tall = shape if shape is not None else (size, size)
        uvs, bake_uvs = _pack_texels(uvs, np.concatenate(all_faces), int(wide), int(tall))
        if packed is not None:
            packed["bake_uvs"] = bake_uvs
        return (np.concatenate(mappings), np.concatenate(all_faces), uvs,
                np.concatenate(groups))
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


def _front_view(positions, faces, region, centre, direction=None):
    """The face seen straight from the front, the way N64 faces were drawn:
    returns (kept triangles, right, up) - the triangles of ``region`` that
    can be mapped from the front and the two directions the front view's x
    and y run along - or None when the region has no front to map from.

    xatlas cut the test sculpt's face into dozens of islands, the hair fringe took much
    of the 64 px texture and her eyes came out two or three texels wide (12 Nov
    2026). From the front, eyes, brows and lips span most of the texture.
    Kept: triangles facing within FACE_FRONT_DEGREES of the face's direction
    and not hidden from the front behind another one (they would share its
    texels); the others go back to the main texture. ``direction``: the way
    the face looks, when it is known already."""
    import numpy as np
    chosen = np.nonzero(region)[0]
    if len(chosen) < 8:
        return None
    corners = positions[faces[chosen]]
    normal = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    doubled = np.linalg.norm(normal, axis=1)
    known = direction
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
    if known is not None:
        direction = np.asarray(known, np.float64) / np.linalg.norm(known)
    elif length < 0.25 * float(doubled[nearest].sum()):  # no one way it looks
        return None
    else:
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
    return keep, right, up


def _mirror_side(positions, faces, ids, right, axis: float) -> float:
    """Which half of the face, seen from the front, a mirrored face texture
    keeps: +1 for the half right of ``axis`` (along ``right``), -1 for the
    left. The half with more of its triangles in view (_front_view has
    dropped the hidden ones) - the other is the one behind hair."""
    import numpy as np
    corners = positions[faces[ids]]
    area = 0.5 * np.linalg.norm(np.cross(corners[:, 1] - corners[:, 0],
                                         corners[:, 2] - corners[:, 0]), axis=1)
    x = corners.mean(axis=1) @ right - axis
    return 1.0 if float(area[x >= 0].sum()) >= float(area[x < 0].sum()) else -1.0


#: A mirrored face texture mirrors about the plane the sculpt's face colours
#: are most nearly symmetric in (_symmetry_plane): searched up to
#: MIRROR_TURN_DEGREES either way from the front view's own, and
#: MIRROR_SHIFT_REACH times the facial detail's reach to either side. A sculpt
#: point matches its mirror image when a colour within MIRROR_COLOUR_MATCH
#: (0-1 RGB distance) is among the MIRROR_NEIGHBOURS points nearest it.
MIRROR_TURN_DEGREES = 8.0
MIRROR_SHIFT_REACH = 0.3
MIRROR_COLOUR_MATCH = 0.1
MIRROR_NEIGHBOURS = 8
#: A face whose best mirror plane still leaves more than this share of its
#: points unmatched is not mirrored (the test sculpt: under 1%; a game rip
#: with a fringe swept across one eye: 27%).
MIRROR_MAX_MISMATCH = 0.15
#: ... and neither is one where more than this share of the half that would
#: be mirrored (by area) is made of triangles that differ from their mirror
#: image (MIRROR_MISMATCH).
MIRROR_MAX_MISFITS = 0.25
#: A triangle on the mirrored half with more than this share of its sculpt
#: points unmatched (a lock of hair across one eye, a scar) is not mirrored:
#: it keeps texels of its own on the body texture.
MIRROR_MISMATCH = 0.5
#: Sculpt points used to search for the mirror plane, and sculpt points round
#: the face they are matched against, at most.
MIRROR_SAMPLES = 3000
MIRROR_TREE_POINTS = 100_000


def _mirror_mismatch(points, colours, tree, all_colours, normal, offset: float):
    """For each point, whether no sculpt point near its mirror image (in the
    plane ``normal`` . x = ``offset``) has its colour."""
    import numpy as np
    image = points - 2.0 * ((points @ normal) - offset)[:, None] * normal
    _, around = tree.query(image, k=MIRROR_NEIGHBOURS, workers=-1)
    distance = np.linalg.norm(all_colours[around] - colours[:, None, :], axis=2).min(axis=1)
    return distance > MIRROR_COLOUR_MATCH


def _symmetry_plane(points, colours, sculpt_points, sculpt_colours, right, up, reach: float,
                    spatial):
    """(normal, offset, unmatched share) of the plane the face is most nearly
    mirror-symmetric in, near the front view's: ``points`` (sculpt points on the face) and
    their ``colours`` against the sculpt's (``sculpt_points``,
    ``sculpt_colours``). The face centre (the detail's middle) is rarely on
    the nose line - a fringe over one eye pulls it over - and a head is often
    turned a little; mirrored about the wrong line the second eye lands a
    texel or two off."""
    import numpy as np
    points = np.asarray(points, np.float64)
    # Only the sculpt round the face is searched (a tree of the whole sculpt
    # made the search 20 s on the test sculpt).
    low = points.min(axis=0) - MIRROR_SHIFT_REACH * 2 * reach
    high = points.max(axis=0) + MIRROR_SHIFT_REACH * 2 * reach
    near = np.nonzero(np.all((sculpt_points >= low) & (sculpt_points <= high), axis=1))[0]
    # A dense sculpt has far more points than a 64 px face has texels.
    near = near[::max(1, len(near) // MIRROR_TREE_POINTS)]
    tree = spatial.cKDTree(sculpt_points[near])
    all_colours = sculpt_colours[near]
    if len(points) > MIRROR_SAMPLES:
        pick = np.linspace(0, len(points) - 1, MIRROR_SAMPLES).astype(np.int64)
        points, colours = points[pick], colours[pick]
    forward = np.cross(right, up)
    middle = points.mean(axis=0)

    def plane(degrees: float):
        turn = np.radians(degrees)
        return np.cos(turn) * right + np.sin(turn) * forward

    def cost(degrees: float, shift: float) -> float:
        normal = plane(degrees)
        return float(_mirror_mismatch(points, colours, tree, all_colours, normal,
                                      float(middle @ normal) + shift).mean())

    # Every turn with every shift, then finer round the best: the match is
    # sharp (on the test sculpt 5% of points unmatched at the best plane, 24%
    # a fifth of an edge to one side), and turn and shift trade off.
    turns = np.linspace(-MIRROR_TURN_DEGREES, MIRROR_TURN_DEGREES, 9)
    shifts = np.linspace(-MIRROR_SHIFT_REACH, MIRROR_SHIFT_REACH, 13) * reach
    degrees, shift = min(((t, v) for t in turns for v in shifts), key=lambda tv: cost(*tv))
    turn_step, shift_step = turns[1] - turns[0], shifts[1] - shifts[0]
    degrees, shift = min(((t, v) for t in np.linspace(degrees - turn_step / 2,
                                                      degrees + turn_step / 2, 5)
                          for v in np.linspace(shift - shift_step, shift + shift_step, 9)),
                         key=lambda tv: cost(*tv))
    normal = plane(degrees)
    return normal, float(middle @ normal) + shift, cost(degrees, shift)


def _front_image(sculpt, sculpt_vertices, positions, faces, nearest, ids, right, up,
                 texture: int, padding: int, xatlas) -> dict:
    """Triangles ``ids`` baked as seen from the front (along ``right`` and
    ``up``), stretched to fill a ``texture`` x ``texture`` image, unmirrored:
    what the face looks like, for laying out its textures. Returns colour
    (rows from v = 0 up, 0-1), inside (texels covered) and the projection
    (used, local, uvs)."""
    import numpy as np
    used, local, uvs = _projected_uvs(positions, faces, ids, right, up, (texture, texture),
                                      padding, fill=True)
    check = {"ids": ids, "uvs": (used, local, uvs)}
    built = _assemble([check], positions, faces, nearest, xatlas, texture, padding)
    image, _, inside = _bake_part(sculpt, sculpt_vertices, built, 0, (texture, texture), False,
                                  view=(right, up, np.cross(right, up)))
    return {"colour": np.flipud(np.asarray(image.convert("RGB"), np.float32) / 255.0),
            "inside": np.flipud(inside), "used": used, "local": local, "uvs": uvs}


def _mirror_misfits(front: dict, positions, faces, ids, across, axis: float, side: float,
                    texture: int, padding: int):
    """The triangles of ``ids`` on the mirrored half (the ``side`` of the plane
    ``across`` . x = ``axis`` not kept) that would look wrong mirrored, from
    the face baked from the front, unmirrored (_front_image, along ``across``):
    a triangle whose texels mostly differ from their mirror image's
    (MIRROR_MISMATCH, colours more than MIRROR_COLOUR_MATCH apart; the texel
    either side counts too) is one - a lock of hair across one side of the
    forehead, mirrored, became skin."""
    import numpy as np
    colour, inside = front["colour"], front["inside"]
    used, local, uvs = front["used"], front["local"], front["uvs"]
    owner, _ = _rasterise(uvs * texture, local, texture)
    owner = owner.reshape(texture, texture)
    # The mirror line in texels: x = axis, mapped as _projected_uvs maps x.
    x = positions[used] @ across
    low, extent = float(x.min()), max(float(x.max() - x.min()), 1e-12)
    line = ((axis - low) / extent * (1.0 - 2 * padding / texture) + padding / texture) * texture
    rows, cols = np.nonzero(owner >= 0)
    tri = owner[rows, cols]
    middles = positions[faces[ids]].mean(axis=1)
    other = side * (middles @ across - axis) < 0
    pick = other[tri]
    rows, cols, tri = rows[pick], cols[pick], tri[pick]
    if not len(tri):
        return ids[:0]
    mirror = np.floor(2.0 * line - (cols + 0.5)).astype(np.int64)
    best = np.full(len(tri), np.inf)
    for shift in (-1, 0, 1):
        col = mirror + shift
        ok = (col >= 0) & (col < texture)
        safe = np.clip(col, 0, texture - 1)
        ok &= inside[rows, safe]
        distance = np.linalg.norm(colour[rows, safe] - colour[rows, cols], axis=1)
        best = np.where(ok, np.minimum(best, distance), best)
    differs = best > MIRROR_COLOUR_MATCH
    share = (np.bincount(tri, differs, minlength=len(ids))
             / np.maximum(np.bincount(tri, minlength=len(ids)), 1))
    return ids[other & (share > MIRROR_MISMATCH)]


def _projected_uvs(positions, faces, ids, right, up, shape, padding: int,
                   fill: bool = False, axis: Optional[float] = None, side: float = 1.0):
    """UVs for triangles ``ids`` as seen from the front (along ``right`` and
    ``up``), for a texture ``shape`` = (width, height) texels with ``padding``
    texels round the island. Returns (vertex mapping, faces, uvs).

    By default the island is centred with square texels (a face texture of
    its own). ``fill`` stretches it to fill both ways (a strip: the texture's
    shape was chosen to keep its texels near square). With ``axis``, the face
    is mirrored about the line x = ``axis``: the ``side`` half (+1 right of
    the line) spans u 0..1 and the other half gets u below 0, which a
    mirroring sampler (glTF MIRRORED_REPEAT, the N64's mirror bit) draws
    with the same texels flipped - twice the texels across for the face."""
    import numpy as np
    used, local = np.unique(faces[ids], return_inverse=True)
    points = positions[used]
    xy = np.stack([points @ right, points @ up], axis=1)
    size = np.asarray(shape, np.float64)
    pad = padding / size
    low = xy.min(axis=0)
    extent = np.maximum(xy.max(axis=0) - low, 1e-12)
    if axis is not None:
        across = side * (xy[:, 0] - axis)
        # The wider half sets the scale, so neither half reads past the
        # texture's far edge (mirrored again there).
        reach = float(np.abs(across).max()) or 1.0
        uvs = np.stack([across / reach * (1.0 - pad[0]),
                        (xy[:, 1] - low[1]) / extent[1] * (1.0 - 2 * pad[1]) + pad[1]], axis=1)
    elif fill:
        uvs = (xy - low) / extent * (1.0 - 2 * pad) + pad
    else:
        # Square texels, centred: texels per unit set by the tighter axis.
        scale = float(np.min((size - 2 * padding) / extent))
        texels = (xy - low) * scale + (size - extent * scale) / 2.0
        uvs = texels / size
    return used, local.reshape(-1, 3), uvs


def _front_projection(positions, faces, region, centre, size: int, padding: int):
    """The face mapped straight from the front (_front_view): one island,
    upright, filling a ``size`` x ``size`` texture. Returns (region, mapping,
    uv faces, uvs) for the triangles kept (``region`` narrowed to them), or
    None when the region has no front to map from."""
    import numpy as np
    view = _front_view(positions, faces, region, centre)
    if view is None:
        return None
    keep, right, up = view
    used, local, uvs = _projected_uvs(positions, faces, keep, right, up, (size, size), padding)
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
    each a dict with faces, base_png, name and optionally normal_png,
    image_names (the face texture's own material) and mirror (its texture is
    mirrored across: half a face drawn twice)."""
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

    samplers: list[dict] = []

    def sampler(mirror: bool) -> int:
        # MIRRORED_REPEAT across (33648) for a texture that holds half a face.
        wanted = {"magFilter": 9728 if nearest else 9729, "minFilter": 9984 if nearest else 9987,
                  "wrapS": 33648 if mirror else 10497, "wrapT": 10497}
        if wanted not in samplers:
            samplers.append(wanted)
        return samplers.index(wanted)

    def add_texture(data: bytes, label: str, mirror: bool = False) -> int:
        image = {"bufferView": add_view(data), "mimeType": "image/png"}
        if label:
            image["name"] = label
        images.append(image)
        textures.append({"source": len(images) - 1, "sampler": sampler(mirror)})
        return len(textures) - 1

    for part in parts:
        labels = tuple(part.get("image_names") or ("", ""))
        mirror = bool(part.get("mirror"))
        indices = add_accessor(np.ascontiguousarray(part["faces"],
                                                    np.uint32 if big else np.uint16).ravel(),
                               "SCALAR", 5125 if big else 5123, 34963)
        material = {"name": part["name"], "pbrMetallicRoughness": {
            "baseColorTexture": {"index": add_texture(part["base_png"], labels[0], mirror)},
            "metallicFactor": 0.0, "roughnessFactor": 0.8}}
        if part.get("normal_png") is not None:
            material["normalTexture"] = {"index": add_texture(part["normal_png"], labels[1],
                                                              mirror)}
        materials.append(material)
        primitives.append({"attributes": attributes, "indices": indices,
                           "material": len(materials) - 1, "mode": 4})
    document = {
        "asset": {"version": "2.0", "generator": "AssetGatekeeper polypaint bake"},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": name}],
        "meshes": [{"name": name, "primitives": primitives}],
        "materials": materials,
        "samplers": samplers,
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
#: Colours are told apart by brightness and, this many times as much, by hue.
HUE_WEIGHT = 1.5
#: The eyes' texture keeps up to EYE_PALETTE_SHARED of its colours from the
#: face's palette: those nearest its outer EYE_PALETTE_RING texels.
#: The body's and the head's textures when 16 colours are asked for: this many,
#: at half the texels.
SMOOTH_COLOURS = 256
EYE_PALETTE_SHARED = 0.5
EYE_PALETTE_RING = 3


def _reduce_colours(image, colours: int, inside=None, fixed=None):
    """The texture in ``colours`` colours, no dithering: a palette texture like the
    N64's 4-bit (16) and 8-bit (256) colour-index formats.

    Most of the palette is a median cut of the colours on the UV islands
    (``inside``), each counted by the square root of how often it appears: a
    plain median cut spent 13 of 16 colours on the blue suit and turned the
    eyebrows grey. The last DETAIL_COLOUR_SHARE are then added one at a time,
    each the colour that most lowers the error left (counted the same way):
    small, distinct details - the test sculpt's teal irises, its lip colour - that a
    median cut merges into their dark or skin-toned neighbours (12 Nov 2026).

    ``fixed`` (n x 3, 0-255) starts the palette with colours it must hold -
    those of the texture this one meets - and all the rest are added that way."""
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
    if fixed is not None and len(fixed) and len(distinct) <= DETAIL_COLOUR_LIMIT:
        palette = np.asarray(fixed, np.float64).reshape(-1, 3)[:colours]
        extra = colours - len(palette)
        base = None
    else:
        weighted = np.repeat(distinct, np.ceil(weight).astype(np.int64), axis=0)
        sample = Image.fromarray(weighted.reshape(1, -1, 3).astype(np.uint8))
        base = sample.quantize(colors=colours - extra, method=Image.Quantize.MEDIANCUT,
                               dither=Image.Dither.NONE)
    def apart(colour):
        """Colours as told apart here: brightness, and hue counted for more
        (HUE_WEIGHT) - by plain RGB a lip's red was nearest a dark hair's olive,
        and a face with much hair in its texture had its lips that colour."""
        colour = np.asarray(colour, np.float64)
        light = colour @ np.array([0.299, 0.587, 0.114])
        return np.stack([light, HUE_WEIGHT * (colour[..., 2] - light),
                         HUE_WEIGHT * (colour[..., 0] - light)], axis=-1)

    if base is not None:
        found = len(base.getcolors(1 << 16) or ()) or colours - extra
        palette = np.asarray(base.getpalette()[:3 * found], np.float64).reshape(-1, 3)
    if extra:
        points = distinct.astype(np.float64)
        spread = apart(points)
        error = ((spread[:, None, :] - apart(palette)[None]) ** 2).sum(axis=2).min(axis=1)
        for _ in range(extra):
            best_gain, best = 0.0, -1
            for first in range(0, len(points), 256):
                candidates = spread[first:first + 256]
                after = ((candidates[:, None, :] - spread[None]) ** 2).sum(axis=2)
                gain = (np.maximum(error[None] - after, 0.0) * weight[None]).sum(axis=1)
                index = int(np.argmax(gain))
                if gain[index] > best_gain:
                    best_gain, best = float(gain[index]), first + index
            if best < 0:                    # every colour is already in the palette
                break
            palette = np.vstack([palette, points[best]])
            error = np.minimum(error, ((spread - spread[best]) ** 2).sum(axis=1))
    palette = palette.round().clip(0, 255).astype(np.uint8)
    # Each texel to the palette's nearest, told apart the same way.
    wanted = apart(palette)
    flat = pixels.reshape(-1, 3)
    index = np.empty(len(flat), np.uint8)
    for first in range(0, len(flat), 1 << 14):
        chunk = apart(flat[first:first + (1 << 14)])
        index[first:first + (1 << 14)] = ((chunk[:, None, :] - wanted[None]) ** 2).sum(axis=2) \
            .argmin(axis=1)
    reduced = Image.fromarray(index.reshape(pixels.shape[:2]), "P")
    # Exactly these colours: a 16-colour texture keeps a 16-entry palette.
    reduced.putpalette(palette.ravel().tolist())
    return reduced


#: With a head budget, the head is everything above the neck: below the face's
#: centre by this many times the reach of its detail (eyes, brows, lips) - on
#: the test sculpt, through the neck under the chin, clear of the shoulders.
NECK_REACH = 2.0
#: With the face framed from its eyes, the head's texture ends this much of the
#: eyes' distance apart below the frame (under the jaw), if that is lower.
NECK_BELOW_CHIN = 0.6
#: The reduction to the head budget starts from this many times the budget,
#: reduced evenly: few enough triangles that the cut round the neck crosses a
#: few dozen vertices (32 on the test sculpt), each kept on both sides.
HEAD_SPLIT_FACTOR = 4


def _reduce(simplify, positions, faces, target: int, preserve_border: bool = False):
    import numpy as np
    if len(faces) <= target:
        return np.asarray(positions, np.float64), np.asarray(faces, np.int64)
    low_positions, low_faces = simplify.simplify(
        np.asarray(positions, np.float32), np.asarray(faces, np.int32), target_count=int(target),
        preserve_border=preserve_border)
    return np.asarray(low_positions, np.float64), np.asarray(low_faces, np.int64)


def _open_vertices(faces) -> int:
    """How many vertices lie on an open edge (an edge of one triangle only)."""
    import numpy as np
    edges = np.sort(np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1)
    unique, counts = np.unique(edges, axis=0, return_counts=True)
    return int(len(np.unique(unique[counts == 1])))


def _reduce_with_head(simplify, positions, faces, triangles: int, neck_height: float,
                      share: float):
    """``faces`` reduced to ``triangles`` with ``share`` of them for the head
    (everything above ``neck_height`` on the up axis, Y).

    The reducer keeps what changes the shape least overall, and at N64 sizes
    that starves a detailed head: the test sculpt's head kept about a fifth of
    1,200 triangles. Here the whole model is first reduced evenly to
    HEAD_SPLIT_FACTOR times the budget, cut at the neck, and the head and the
    body are reduced to their own budgets with the cut's vertices kept on both
    sides, so the two halves still meet exactly; then they are joined.
    Returns (positions, faces, note): a note says why the head budget was not
    used (then the reduction is even)."""
    import numpy as np
    middle, middle_faces = _reduce(simplify, positions, faces, triangles * HEAD_SPLIT_FACTOR)
    above = middle[middle_faces].mean(axis=1)[:, 1] > neck_height
    if above.all() or not above.any():
        return (*_reduce(simplify, middle, middle_faces, triangles),
                "no head budget: nothing above the neck to give it to")
    pieces = []
    for mask in (above, ~above):
        used, local = np.unique(middle_faces[mask], return_inverse=True)
        pieces.append((middle[used], local.reshape(-1, 3)))
    # Every open edge of a piece is kept (the cut's must be), and a game rip
    # has many more - decals, holsters, an outline shell. With too few
    # triangles for the rest, the reducer collapsed a rip's legs to nothing
    # (12 Nov 2026): give each piece at least two triangles per open-edge
    # vertex, the head giving way first.
    floors = [2 * _open_vertices(piece_faces) for _, piece_faces in pieces]
    head_target = min(int(round(triangles * share)), len(pieces[0][1]))
    head_target = max(min(head_target, triangles - floors[1]), min(floors[0], head_target))
    parts = []
    for (piece, piece_faces), target in zip(pieces, (head_target, triangles - head_target)):
        reduced = _reduce(simplify, piece, piece_faces, max(target, 1), preserve_border=True)
        # A piece that lost its shape (its size shrank) means the budget could
        # not hold it: reduce the whole model evenly instead.
        if np.any(np.ptp(reduced[0], axis=0) < 0.9 * np.ptp(piece, axis=0)):
            return (*_reduce(simplify, middle, middle_faces, triangles),
                    "no head budget: the body could not keep its shape with what was left "
                    "(it has many open edges - an outline shell or decals? leave them out)")
        parts.append(reduced)
    joined = np.concatenate([parts[0][0], parts[1][0]])
    joined_faces = np.concatenate([parts[0][1], parts[1][1] + len(parts[0][0])])
    joined, joined_faces = _weld(joined, joined_faces)    # the cut, closed again
    # Kept cut vertices can leave a few over: a budget is a ceiling.
    return (*_reduce(simplify, joined, joined_faces, triangles), "")


def _assemble(parts, low_positions, low_faces, nearest, xatlas, texture: int, padding: int,
              priority=None, boost: float = 1.0) -> dict:
    """The reduced mesh laid out for baking, one part (material) after another:
    each part's UVs (its own, or xatlas'), shading normals split at hard folds,
    tangents. ``nearest`` (the reduced triangle each sculpt vertex is nearest)
    comes back numbered by the new triangle order, -1 for a triangle in no part."""
    import numpy as np
    mappings, part_faces, part_uvs, orders = [], [], [], []
    offset = 0
    for part in parts:
        group = part["ids"]
        if part.get("uvs") is not None:
            mapping, uv_faces, uvs = part["uvs"]
            order = np.arange(len(group))
        else:
            got = {}
            mapping, uv_faces, uvs, order = _unwrap(
                xatlas, low_positions, low_faces[group], texture, part.get("padding", padding),
                priority[group] if priority is not None else None, boost,
                shape=part.get("shape"), packed=got)
            if "bake_uvs" in got:                     # packed by the texel: UVs to bake with
                part["packed"] = True
                uvs = np.concatenate([uvs, got["bake_uvs"]], axis=1)
        if uvs.shape[1] == 2:
            uvs = np.concatenate([uvs, uvs], axis=1)
        mappings.append(mapping)
        part_faces.append(uv_faces + offset)
        part_uvs.append(uvs)
        orders.append(group[order])
        offset += len(mapping)
    mapping = np.concatenate(mappings)
    uvs = np.concatenate(part_uvs)
    all_faces = np.concatenate(part_faces)
    order = np.concatenate(orders)
    renumber = np.full(len(low_faces), -1, np.int64)
    renumber[order] = np.arange(len(order))
    # Shading normals per corner, hard across sharp folds; corners that disagree
    # get a vertex each. Neighbours are found through the reduced mesh's own
    # vertices, so a UV seam does not cut the shading.
    soft = np.concatenate([np.full(len(faces), part.get("view") is not None)
                           for part, faces in zip(parts, part_faces)])
    corner_normals = _corner_normals(low_positions, mapping[all_faces], soft=soft)
    positions, uvs, normals, all_faces = _split_by_normal(
        low_positions[mapping], uvs, all_faces, corner_normals)
    uvs, bake_uvs = np.ascontiguousarray(uvs[:, :2]), np.ascontiguousarray(uvs[:, 2:])
    sizes = np.cumsum([len(part) for part in part_faces])[:-1]
    # A part seen from the front (a face) is shaded as facing the viewer at
    # least a little everywhere: the inside of a lip, a socket's wall, turned
    # away and shaded dark, showed as dark triangles in the mouth and eyes.
    for part, faces_of in zip(parts, np.split(all_faces, sizes)):
        if part.get("view") is None or not len(faces_of):
            continue
        towards = np.asarray(part["view"][2], np.float64)
        used = np.unique(faces_of)
        facing = normals[used] @ towards
        turned = facing < SOFT_FACING
        if turned.any():
            ids = used[turned]
            before = normals[ids].copy()
            push = (SOFT_FACING - facing[turned])[:, None] * towards
            corners = positions[faces_of]
            flat = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
            # ... as far as it goes without shading one of its triangles from behind.
            todo = np.ones(len(ids), bool)
            for share in (1.0, 0.75, 0.5, 0.25, 0.0):
                fixed = before[todo] + share * push[todo]
                normals[ids[todo]] = fixed / np.maximum(
                    np.linalg.norm(fixed, axis=1, keepdims=True), 1e-30)
                behind = _dot(normals[faces_of], flat[:, None, :]) <= 0.0
                todo &= np.isin(ids, np.unique(faces_of[behind]))
                if not todo.any():
                    break
    return {"positions": positions, "uvs": uvs, "bake_uvs": bake_uvs, "normals": normals,
            "faces": all_faces, "packed": [bool(part.get("packed")) for part in parts],
            "part_faces": np.split(all_faces, sizes),
            "tangents": _tangents(positions, uvs, all_faces, normals),
            "nearest": renumber[nearest]}


def _bake_part(sculpt, sculpt_vertices, built: dict, index: int, shape, want_normal: bool,
               colours: int = 0, view=None):
    """One part's textures (_assemble): (BaseColor image, normal image or None,
    covered texels (h, w) bool, rows top first). A strip is baked square at its
    longer side, then averaged down to its ``shape`` (width, height): the same
    samples per texel along both. ``view``: the part is the model seen along
    it (_bake_maps)."""
    import numpy as np
    from PIL import Image
    part_faces = built["part_faces"]
    first = sum(len(faces) for faces in part_faces[:index])
    last = first + len(part_faces[index])
    nearest = built["nearest"]
    local = np.where((nearest >= first) & (nearest < last), nearest - first, -1)
    edge = max(shape)
    colour, normal_pixels, inside = _bake_maps(sculpt, sculpt_vertices, local, built["positions"],
                                                 built["normals"], built["tangents"],
                                                 built.get("bake_uvs", built["uvs"]),
                                                 part_faces[index], edge, want_normal,
                                                 samples_per_texel(edge), view=view)
    if built.get("packed", [False] * (index + 1))[index] and tuple(shape) != (edge, edge):
        # Packed by the texel into its shape, baked across a square of the longer
        # side: the rows (v upward) and columns it has are the texture.
        colour, inside = colour[:shape[1], :shape[0]], inside[:shape[1], :shape[0]]
        if normal_pixels is not None:
            normal_pixels = normal_pixels[:shape[1], :shape[0]]
        edge_done = True
    else:
        edge_done = False
    narrow = (not edge_done and tuple(shape) != (edge, edge) and edge % shape[0] == 0
              and edge % shape[1] == 0)
    if narrow:
        # Averaged down to the strip's shape over the texels the islands cover
        # only: the gaps hold their neighbours' colours (filled in), and
        # averaged in plainly they tinted the last row of each island - skin
        # on the edge of the hair.
        across, down = edge // shape[0], edge // shape[1]
        weight = inside.reshape(shape[1], down, shape[0], across).astype(np.float32)
        total = (colour.reshape(shape[1], down, shape[0], across, 3)
                 * weight[..., None]).sum(axis=(1, 3))
        count = weight.sum(axis=(1, 3))
        covered = count > 0
        total[covered] /= count[covered, None]
        colour, inside = _fill_background(total, covered), covered
    # Rows were filled with v going up; an image's first row is its top.
    image = Image.fromarray((np.flipud(colour) * 255).clip(0, 255).round().astype(np.uint8))
    inside = np.flipud(inside)
    normal_image = None
    if normal_pixels is not None:
        normal_image = Image.fromarray(
            (np.flipud(normal_pixels) * 255).clip(0, 255).round().astype(np.uint8))
    if tuple(shape) != (edge, edge) and not edge_done:
        if not narrow:
            image = image.resize(tuple(shape), Image.BOX)
            inside = np.asarray(Image.fromarray(inside.astype(np.uint8) * 255).resize(
                tuple(shape), Image.BOX)) > 0
        if normal_image is not None:
            normal_image = normal_image.resize(tuple(shape), Image.BOX)
    if colours:
        image = _reduce_colours(image, colours, inside)
    return image, normal_image, inside


#: A cut no nearer a corner than this share of the region's typical edge: a
#: nearer one would shave a sliver off, so the corner counts as on the line.
CUT_SNAP = 0.04
#: Triangles kept back from the budget for the cuts: at most this many, and
#: this share of the budget.
CUT_RESERVE = (240, 0.08)


def _cut_mesh(positions, faces, direction, value: float, region):
    """The mesh cut along the plane ``direction`` . x = ``value`` where it
    crosses the triangles of ``region`` (bool per triangle): each edge of
    theirs that crosses gets a vertex there, and every triangle using such an
    edge (in the region or beside it) is split, so no edge is left with a
    vertex in its middle. Returns (positions, faces, parent of each triangle).

    So a texture can end on a straight line: a box round an eye, the middle
    of a face that is mirrored."""
    import numpy as np
    parent = np.arange(len(faces))
    if not region.any():
        return positions, faces, parent
    height = positions @ direction - value
    edges = positions[faces[region][:, [1, 2, 0]]] - positions[faces[region]]
    snap = CUT_SNAP * float(np.median(np.linalg.norm(edges, axis=2)))
    # A corner of the region that close to the line is moved onto it.
    close = np.zeros(len(positions), bool)
    close[np.unique(faces[region])] = True
    close &= (np.abs(height) < snap) & (height != 0.0)
    if close.any():
        positions = positions.copy()
        positions[close] -= height[close, None] * np.asarray(direction, np.float64)
    height = np.where(np.abs(height) < snap, 0.0, height)
    count = len(positions)
    pairs = np.stack([faces, faces[:, [1, 2, 0]]], axis=2)              # (f, 3, 2): edge k
    crossing = height[pairs[..., 0]] * height[pairs[..., 1]] < 0
    keys = np.minimum(pairs[..., 0], pairs[..., 1]) * count + np.maximum(pairs[..., 0], pairs[..., 1])
    wanted = np.unique(keys[region][crossing[region]])
    if not len(wanted):
        return positions, faces, parent
    first, second = wanted // count, wanted % count
    along = height[first] / (height[first] - height[second])
    made = positions[first] + along[:, None] * (positions[second] - positions[first])
    where = np.searchsorted(wanted, keys)
    where = np.minimum(where, len(wanted) - 1)
    split = np.where(crossing & (wanted[where] == keys), count + where, -1)   # (f, 3)
    cuts = (split >= 0).sum(axis=1)
    out_faces, out_parent = [faces[cuts == 0]], [parent[cuts == 0]]
    for lone in range(3):                                   # the corner both cut edges share
        after, before = lone, (lone + 2) % 3                # edges lone->next, previous->lone
        rows = np.nonzero((cuts == 2) & (split[:, after] >= 0) & (split[:, before] >= 0))[0]
        if len(rows):
            a, b, c = (faces[rows, (lone + k) % 3] for k in range(3))
            p, q = split[rows, after], split[rows, before]
            out_faces += [np.stack([a, p, q], axis=1), np.stack([p, b, c], axis=1),
                          np.stack([p, c, q], axis=1)]
            out_parent += [parent[rows]] * 3
    for edge in range(3):                                   # one cut edge: its far corner is on the line
        rows = np.nonzero((cuts == 1) & (split[:, edge] >= 0))[0]
        if len(rows):
            a, b, c = (faces[rows, (edge + k) % 3] for k in range(3))
            p = split[rows, edge]
            out_faces += [np.stack([a, p, c], axis=1), np.stack([p, b, c], axis=1)]
            out_parent += [parent[rows]] * 2
    return (np.concatenate([positions, made]), np.concatenate(out_faces),
            np.concatenate(out_parent))


def _folded_uvs(positions, faces, ids, right, up, shape, padding: int, axis: float):
    """UVs for triangles ``ids`` seen from the front (_projected_uvs), folded
    about the line x = ``axis``: both halves of the face on the same texels,
    by distance from the line. No triangle may cross the line (_cut_mesh). The
    part fills its texture both ways. Returns (vertex mapping, faces, uvs).

    A texture mirrored by its sampler (MIRRORED_REPEAT) draws the same, but
    the setting has to survive every exporter and editor on the way to the
    console; folded UVs are plain UVs."""
    import numpy as np
    used, local = np.unique(faces[ids], return_inverse=True)
    points = positions[used]
    xy = np.stack([np.abs(points @ right - axis), points @ up], axis=1)
    size = np.asarray(shape, np.float64)
    pad = padding / size
    low = xy.min(axis=0)
    extent = np.maximum(xy.max(axis=0) - low, 1e-12)
    return used, local.reshape(-1, 3), (xy - low) / extent * (1.0 - 2 * pad) + pad


def _close_gap(x, middle: float, inner: float):
    """Distances across (``x``) with the gap between the two eye boxes - within
    ``inner`` of ``middle`` - closed: the two boxes side by side."""
    import numpy as np
    x = np.asarray(x, np.float64) - middle
    return np.sign(x) * np.maximum(np.abs(x) - inner, 0.0)


def _paired_uvs(positions, faces, ids, right, up, shape, padding: int, middle: float,
                inner: float):
    """UVs for the two boxes round the eyes of a face that is not mirrored
    (triangles ``ids``, seen from the front): side by side in one texture, the
    stretch of face between them (within ``inner`` of ``middle``) left out, so
    each eye has half the texture rather than the fifth of a strip from ear to
    ear. Returns (vertex mapping, faces, uvs)."""
    import numpy as np
    used, local = np.unique(faces[ids], return_inverse=True)
    points = positions[used]
    xy = np.stack([_close_gap(points @ right, middle, inner), points @ up], axis=1)
    size = np.asarray(shape, np.float64)
    pad = padding / size
    low = xy.min(axis=0)
    extent = np.maximum(xy.max(axis=0) - low, 1e-12)
    return used, local.reshape(-1, 3), (xy - low) / extent * (1.0 - 2 * pad) + pad


#: Framing the face from its eyes (_find_eyes): the face is looked for within
#: FACE_SEARCH_REACH times the detail's reach; the eyes are the best pair of
#: dark patches there (EYE_DARK_SHARE of the texels) - alike in size (within
#: EYE_PAIR_SIZE times), level (no more than EYE_PAIR_TILT of their distance
#: apart in height) and EYE_PAIR_APART of the view's width apart. The frame
#: is FACE_FRAME times their distance apart: to each side of their middle,
#: below them (the chin) and above (the brow).
FACE_SEARCH_REACH = 2.6
EYE_PAIR_SIZE = 3.0
EYE_PAIR_TILT = 0.35
EYE_PAIR_APART = (0.12, 0.7)
EYE_PAIR_SPECK = 0.3
#: A pair's cost: its distance from the detail's middle (in texture widths)
#: plus these times its tilt and how unlike mirror images the two patches'
#: surroundings (EYE_PAIR_WINDOW of their distance apart each way) are.
EYE_PAIR_WEIGHTS = (2.0, 2.0)
EYE_PAIR_WINDOW = 0.3
#: The box round an eye is its dark patch (lashes, pupil) and this much of the
#: eyes' distance apart more, across and up-and-down.
EYE_BOX_PAD = (0.1, 0.1)
FACE_FRAME = (1.1, 1.45, 0.75)
#: With the chin found, the frame reaches at least this much of the chin's
#: distance above the eyes.
FACE_BROW_OF_CHIN = 0.6
#: A busy brow - colour stepping by more than FACE_BUSY_STEP from texel to
#: texel, between FACE_BUSY_BROW of the eyes' distance above them (a fringe's
#: strands across the forehead) - is framed with the face, up to the higher.
FACE_BUSY_BROW = (0.75, 1.25)
FACE_BUSY_STEP = 0.04
FACE_CHIN_SLACK = 0.35
FACE_EYE_REACH = 3.0
FACE_LOOSE_BELOW = 0.9


#: Triangles the front view left out (they face sideways: an eye socket's
#: walls) join the face when they sit inside its outline and no further behind
#: it than FACE_HOLLOW times the eyes' distance apart.
FACE_HOLLOW = 0.5
FACE_HOLLOW_GRID = 128


def _face_hollows(positions, faces, keep, right, up, behind: float):
    """The triangles not in ``keep`` (the face, seen along right and up) that
    lie in its hollows: every corner inside the face's outline as the view
    shows it, and at most ``behind`` deeper than the face there. With the
    eyeballs painted on, a socket's walls were left to the body's texture,
    and the eye with them (13 Nov 2026)."""
    import numpy as np
    from scipy import ndimage
    towards = np.cross(right, up)
    used, local = np.unique(faces[keep], return_inverse=True)
    points = positions[used]
    xy = np.stack([points @ right, points @ up], axis=1)
    low = xy.min(axis=0)
    extent = max(float((xy.max(axis=0) - low).max()), 1e-12)
    grid = FACE_HOLLOW_GRID

    def texels(where):
        return (np.stack([where @ right, where @ up], axis=1) - low) / extent * (grid - 1) + 0.5

    depth = (points @ towards).astype(np.float32)
    local = local.reshape(-1, 3)
    owner, bary = _rasterise(texels(points), local, grid, depth=depth)
    owner, bary = owner.reshape(grid, grid), bary.reshape(grid, grid, 3)
    covered = owner >= 0
    if not covered.any():
        return keep[:0]
    surface = np.where(covered, (depth[local[np.maximum(owner, 0)]] * bary).sum(axis=2), 0.0)
    outline = ndimage.binary_fill_holes(covered)
    # The face's depth inside a hollow: that of the nearest texel it covers.
    _, nearest = ndimage.distance_transform_edt(~covered, return_indices=True)
    surface = surface[nearest[0], nearest[1]]
    others = np.setdiff1d(np.arange(len(faces)), keep)
    corners = positions[faces[others]]                                  # (n, 3, 3)
    at = np.floor(texels(corners.reshape(-1, 3))).astype(np.int64).reshape(-1, 3, 2)
    inside = ((at >= 0) & (at < grid)).all(axis=2)
    col, row = np.clip(at[..., 0], 0, grid - 1), np.clip(at[..., 1], 0, grid - 1)
    inside &= outline[row, col]
    inside &= (corners @ towards) >= surface[row, col] - behind
    return others[inside.all(axis=1)]


#: The chin (_find_chin): going down the middle of the face from the mouth, the
#: surface stays about as near the viewer as the lips (CHIN_LIPS below the eyes) until the chin ends and
#: it falls back to the neck - by more than CHIN_DROP of the eyes' distance
#: apart. Looked for between CHIN_RANGE of that distance below the eyes; the
#: frame ends CHIN_PAD below it.
CHIN_DROP = 0.4
CHIN_RANGE = (1.2, 2.2)
CHIN_PAD = 0.05
CHIN_LIPS = (1.0, 1.3)
CHIN_GRID = 160


def _find_chin(positions, faces, keep, right, up, middle: float, height: float, apart: float):
    """How far below the eyes the chin ends, in eyes' distances apart, from the
    triangles ``keep`` seen from the front; None when it cannot be told. A
    frame of fixed proportions (FACE_FRAME) cut a longer face off above its
    chin, and the chin's triangles went to the body's texture (13 Nov 2026)."""
    import numpy as np
    towards = np.cross(right, up)
    used, local = np.unique(faces[keep], return_inverse=True)
    points = positions[used]
    x = (points @ right - middle) / apart
    y = (height - points @ up) / apart                # down from the eyes
    depth = (points @ towards / apart).astype(np.float32)
    grid = CHIN_GRID
    span_x, span_y = 0.3, 2.6
    texels = np.stack([(x + span_x) / (2 * span_x) * grid, y / span_y * grid], axis=1)
    local = local.reshape(-1, 3)
    near = (np.abs(x[local]) < 1.0).any(axis=1) & (y[local] > -0.5).all(axis=1) \
        & (y[local] < span_y + 0.5).all(axis=1)
    if near.sum() < 4:
        return None
    owner, bary = _rasterise(texels, local[near], grid, depth=depth)
    owner, bary = owner.reshape(grid, grid), bary.reshape(grid, grid, 3)
    surface = np.where(owner >= 0, (depth[local[near][np.maximum(owner, 0)]] * bary).sum(axis=2),
                       -np.inf)
    front = surface.max(axis=1)                       # per row: the nearest point
    rows = (np.arange(grid) + 0.5) / grid * span_y
    lips = front[(rows > CHIN_LIPS[0]) & (rows < CHIN_LIPS[1])]       # below the nose's tip
    lips = lips[np.isfinite(lips)]
    if not len(lips):
        return None
    level = float(lips.max())
    for row in np.nonzero(rows >= CHIN_RANGE[0])[0]:
        if front[row] < level - CHIN_DROP:
            return float(min(max(rows[row], CHIN_RANGE[0]), CHIN_RANGE[1]))
    return None


#: An edge of the box round an eye is moved, by up to EYE_EDGE_REACH of the
#: eyes' distance apart (in, out), to where the face is plainest along it: a
#: brow cut through the middle showed the box's outline, sharp inside it and
#: coarse outside.
EYE_EDGE_REACH = (0.04, 0.14)
EYE_EDGE_STEPS = 19
EYE_EDGE_FAR = 0.25
EYE_EDGE_TEXTURE = 256


def _calm_edges(front: dict, positions, right, up, texture: int, padding: int, eyes: dict) -> dict:
    """``eyes`` (_find_eyes) with each edge of the boxes moved to the line
    nearby that the face (``front``: _front_image, finer than the one the eyes
    were found in) varies least along, both eyes together."""
    import numpy as np
    colour, inside = front["colour"], front["inside"]
    used = front["used"]
    scale = 1.0 - 2 * padding / texture
    spans = []
    for axis in (right, up):
        spread = positions[used] @ axis
        spans.append((float(spread.min()), max(float(spread.max() - spread.min()), 1e-12)))

    def texel_of(k: int, value):
        return ((np.asarray(value, np.float64) - spans[k][0]) / spans[k][1] * scale
                + padding / texture) * texture - 0.5

    def sample(xs, ys):
        cols = np.clip(np.rint(texel_of(0, xs)).astype(np.int64), 0, texture - 1)
        rows = np.clip(np.rint(texel_of(1, ys)).astype(np.int64), 0, texture - 1)
        return colour[rows, cols], inside[rows, cols]

    middle, apart = eyes["middle"], eyes["apart"]
    out = dict(eyes)

    def unrest(name: str, value: float) -> float:
        box = dict(out)
        box[name] = value
        total, count = 0.0, 0
        for sign in (-1.0, 1.0):
            if name in ("low", "high"):
                xs = middle + sign * np.linspace(box["inner"], box["outer"], 48)
                ys = np.full(48, value)
            else:
                xs = np.full(48, middle + sign * value)
                ys = np.linspace(box["low"], box["high"], 48)
            got, ok = sample(xs, ys)
            if ok.sum() >= 8:
                total += float(got[ok].std(axis=0).sum())
                count += 1
        return total / count if count else np.inf

    for name, outward in (("high", 1.0), ("low", -1.0), ("outer", 1.0), ("inner", -1.0)):
        if name == "inner" and eyes["inner"] <= 0.0:
            continue
        offsets = np.linspace(-EYE_EDGE_REACH[0], EYE_EDGE_REACH[1], EYE_EDGE_STEPS) * apart
        choices = []
        for offset in offsets:
            value = eyes[name] + outward * offset
            if name == "inner" and value < 0.02 * apart:
                continue
            # A little for the distance moved, so a plain face keeps its box.
            choices.append((unrest(name, value) + EYE_EDGE_FAR * abs(offset) / apart, value))
        if choices:
            out[name] = min(choices)[1]
    return out


def _find_eyes(front: dict, positions, right, up, texture: int, padding: int, centre):
    """The two eyes in a face seen from the front (_front_image): their middle
    along ``right``, height along ``up`` and distance apart, and the box round
    each (inner and outer edge from the middle, low and high), or None if no
    pair of dark patches there looks like eyes. Of the pairs that do, the one that
    costs least (EYE_PAIR_WEIGHTS): near ``centre`` (the facial detail's middle),
    level, and alike - a dark collar's two halves made a larger pair than the eyes."""
    import numpy as np
    from scipy import ndimage
    colour, inside = front["colour"], front["inside"]
    if inside.sum() < 16:
        return None
    light = colour @ np.array([0.299, 0.587, 0.114], np.float32)
    dark = inside & (light <= np.quantile(light[inside], EYE_DARK_SHARE))
    patches, count = ndimage.label(dark, structure=np.ones((3, 3), bool))
    if count < 2:
        return None
    ids = np.arange(1, count + 1)
    size = ndimage.sum(dark, patches, ids)
    where = np.asarray(ndimage.center_of_mass(dark, patches, ids))      # (row, col)
    # Texels and the model, as _projected_uvs (fill) laid it out.
    used = front["used"]
    scale = 1.0 - 2 * padding / texture
    spans = []
    for axis in (right, up):
        spread = positions[used] @ axis
        spans.append((float(spread.min()), max(float(spread.max() - spread.min()), 1e-12)))

    def along(k: int, texel: float) -> float:
        return spans[k][0] + ((texel + 0.5) / texture - padding / texture) / scale * spans[k][1]

    def texel_of(k: int, value: float) -> float:
        return ((value - spans[k][0]) / spans[k][1] * scale + padding / texture) * texture - 0.5

    middle = np.array([texel_of(1, float(np.asarray(centre) @ up)),
                       texel_of(0, float(np.asarray(centre) @ right))])   # (row, col)
    def window(at, half: int):
        rows = np.clip(np.arange(int(round(at[0])) - half, int(round(at[0])) + half + 1),
                       0, colour.shape[0] - 1)
        cols = np.clip(np.arange(int(round(at[1])) - half, int(round(at[1])) + half + 1),
                       0, colour.shape[1] - 1)
        return colour[np.ix_(rows, cols)]

    pairs = []
    for a in range(count):
        for b in range(a + 1, count):
            apart = abs(where[a, 1] - where[b, 1])
            if not EYE_PAIR_APART[0] * texture <= apart <= EYE_PAIR_APART[1] * texture:
                continue
            if abs(where[a, 0] - where[b, 0]) > EYE_PAIR_TILT * apart:
                continue
            if max(size[a], size[b]) > EYE_PAIR_SIZE * min(size[a], size[b]):
                continue
            # How far from the detail's middle, how tilted, and how unlike its
            # mirror image one patch's surroundings are to the other's: an eye
            # and the shadow under a lock of hair were level and alike in size.
            half = max(2, int(round(EYE_PAIR_WINDOW * apart)))
            first, second = (a, b) if where[a, 1] < where[b, 1] else (b, a)
            unlike = float(np.abs(window(where[first], half)
                                  - window(where[second], half)[:, ::-1]).mean())
            cost = (float(np.linalg.norm((where[a] + where[b]) / 2.0 - middle)) / texture
                    + EYE_PAIR_WEIGHTS[0] * abs(where[a, 0] - where[b, 0]) / apart
                    + EYE_PAIR_WEIGHTS[1] * unlike)
            pairs.append((float(min(size[a], size[b])), cost, a, b))
    if not pairs:
        return None
    # Not specks (a pair of nostrils): at least EYE_PAIR_SPECK of the largest pair.
    largest = max(pair[0] for pair in pairs)
    _, _, a, b = min((pair for pair in pairs if pair[0] >= EYE_PAIR_SPECK * largest),
                     key=lambda pair: pair[1])
    left, right_eye = along(0, where[a, 1]), along(0, where[b, 1])
    height = along(1, (where[a, 0] + where[b, 0]) / 2.0)
    middle_x, apart = (left + right_eye) / 2.0, abs(right_eye - left)
    # The box round each eye: its patch's edges, from the middle outwards.
    boxes = ndimage.find_objects(patches)
    near, far, low, high = [], [], [], []
    for index in (a, b):
        rows, cols = boxes[index]
        edges = sorted(abs(along(0, edge) - middle_x) for edge in (cols.start - 0.5, cols.stop - 0.5))
        near.append(edges[0])
        far.append(edges[1])
        low.append(along(1, rows.start - 0.5))
        high.append(along(1, rows.stop - 0.5))
    # How busy the brow above the eyes is (hair combed across it): the mean
    # step in colour from texel to texel, FACE_BUSY_BROW above the eyes.
    eye_row, mid_col = (where[a, 0] + where[b, 0]) / 2.0, (where[a, 1] + where[b, 1]) / 2.0
    wide = abs(where[a, 1] - where[b, 1])
    tall = wide * spans[0][1] / spans[1][1]              # the eyes' distance, in rows
    rows = slice(int(np.clip(eye_row + FACE_BUSY_BROW[0] * tall, 0, texture - 2)),
                 int(np.clip(eye_row + FACE_BUSY_BROW[1] * tall, 1, texture - 1)) + 1)
    cols = slice(int(np.clip(mid_col - wide, 0, texture - 2)),
                 int(np.clip(mid_col + wide, 1, texture - 1)) + 1)
    band, covered = colour[rows, cols], inside[rows, cols]
    busy = 0.0
    if band.shape[0] > 1 and band.shape[1] > 1 and covered.mean() > 0.5:
        busy = float(np.abs(np.diff(band, axis=1)).sum(axis=2).mean()
                     + np.abs(np.diff(band, axis=0)).sum(axis=2).mean()) / 2.0
    return {"middle": middle_x, "height": height, "apart": apart, "brow": busy,
            "inner": max(0.0, min(near) - EYE_BOX_PAD[0] * apart),
            "outer": max(far) + EYE_BOX_PAD[0] * apart,
            "low": min(low) - EYE_BOX_PAD[1] * apart, "high": max(high) + EYE_BOX_PAD[1] * apart}


#: The eye strip (face layouts eyes, eyes-mirror) runs across the face where
#: its darkest texels are, seen from the front: pupils, lashes and brows
#: (EYE_DARK_SHARE of the face's texels; the rows holding the middle
#: EYE_DARK_ROWS of them), EYE_BAND_PAD of the face's height wider at the top
#: and bottom. Tried first and dropped: the densest colour detail above the
#: face's centre - on a game rip the fringe swept over the brow is busier than
#: the eyes, and the strip came out all hair.
EYE_DARK_SHARE = 0.04
EYE_DARK_ROWS = (0.1, 0.9)
EYE_BAND_PAD = 0.04


def _eye_band(front: dict, positions, up, texture: int, padding: int):
    """(low, high) along ``up``: the strip across the eyes and brows, from the
    face seen from the front (_front_image), or None if it shows no face."""
    import numpy as np
    colour, inside = front["colour"], front["inside"]
    if inside.sum() < 16:
        return None
    light = colour @ np.array([0.299, 0.587, 0.114], np.float32)
    cut = np.quantile(light[inside], EYE_DARK_SHARE)
    rows, _ = np.nonzero(inside & (light <= cut))
    if not len(rows):
        return None
    low_row, high_row = np.quantile(rows, EYE_DARK_ROWS)
    low_row -= EYE_BAND_PAD * texture
    high_row += EYE_BAND_PAD * texture + 1.0
    # Texel rows back to heights, as _projected_uvs (fill) put heights in rows.
    y = positions[front["used"]] @ up
    bottom, extent = float(y.min()), max(float(y.max() - y.min()), 1e-12)

    def height_of(row: float) -> float:
        return bottom + (row / texture - padding / texture) / (1.0 - 2 * padding / texture) * extent

    return height_of(low_row), height_of(high_row)


def _strip_shape(width: float, height: float, texture: int, max_edge: int = 0) -> tuple:
    """(width, height) in texels of a strip ``width`` x ``height`` (model units)
    holding the texels of one ``texture`` x ``texture`` texture (so it fits the
    same texture memory), whichever of 4:1, 1:1 and 1:4 leaves its texels
    nearest square - and no edge over ``max_edge`` (a folder's rules), if given."""
    import math
    choices = [(texture * 2, texture // 2), (texture, texture), (texture // 2, texture * 2)]
    if max_edge:
        choices = [shape for shape in choices if max(shape) <= max_edge] or [(texture, texture)]
    ratio = max(width, 1e-12) / max(height, 1e-12)
    return min(choices, key=lambda shape: abs(math.log((shape[0] / shape[1]) / ratio)))


#: Small parts painted on (``painted_parts``): a sculpt's eyeballs, lashes,
#: teeth and tongue, a game model's lash and brow cards and hair strands, are
#: pieces of their own lying in or on the head. Reduced with it they turn to
#: spikes (four 113,000-triangle eyeballs to a dozen triangles each) and take
#: the triangles the face needs. An N64 face is one surface with the eyes
#: drawn on it, so those pieces are left out of the reduced mesh and their
#: colours baked onto the surface behind them. A piece is painted on when it
#: is above the neck, has under PAINT_MAJOR_SHARE of the model's surface, is
#: no larger across than PAINT_MAX_SIZE of the model's largest extent (hair
#: cards are larger: painted on the scalp they left it in patches, skin
#: showing between the strands), and at least PAINT_HUG_SHARE of its points
#: lie inside a larger piece or within PAINT_HUG_REACH (of the model's largest
#: extent) of its surface.
PAINT_MAJOR_SHARE = 0.01
PAINT_MAX_SIZE = 0.05
PAINT_HUG_SHARE = 0.7
PAINT_HUG_REACH = 0.0075
#: Points per piece tested, and triangles of the larger pieces sampled, at most.
PAINT_POINTS = 4000
PAINT_SAMPLE_FACES = 200_000


def _pieces(vertex_count: int, faces):
    """(count, piece of each face): the connected pieces of a welded mesh."""
    import numpy as np
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    a = np.concatenate([faces[:, 0], faces[:, 1], faces[:, 2]])
    b = np.concatenate([faces[:, 1], faces[:, 2], faces[:, 0]])
    graph = coo_matrix((np.ones(len(a), np.int8), (a, b)), shape=(vertex_count, vertex_count))
    count, label = connected_components(graph, directed=False)
    return count, label[faces[:, 0]]


#: Hidden triangles (dropped with ``painted_parts``): the head, above the
#: neck, is looked at from HIDDEN_VIEWS directions all round, HIDDEN_GRID
#: pixels across (a triangle of an N64 head is several of them wide); a triangle that is in front at no pixel of any view is
#: hidden. Only the head: it does not bend, so what is hidden stays hidden
#: (an armpit shut in the modelled pose opens when the arm lifts).
HIDDEN_VIEWS = 32
HIDDEN_GRID = 192
#: ... with what stands below it, down to this share of the head's height.
HIDDEN_BELOW = 1.0
#: In front at fewer pixels than this, all views together, counts as not seen.
#: (4 was tried, for what is glimpsed between closed lips: it took the lips'
#: own small triangles too, and left holes.)
HIDDEN_PIXELS = 1


#: A hole in the head is closed (_cap_holes) when its rim has no more edges
#: than HOLE_EDGES and is no wider than HOLE_SIZE of the model's size.
HOLE_EDGES = 40
HOLE_SIZE = 0.04


def _cap_holes(positions, faces, above: float):
    """(positions, faces, holes closed): small holes in the head (above height
    ``above``) closed with a fan of triangles to the middle of their rim.

    Dropping what nothing can see (_hidden_faces) takes the back of the mouth
    with it - seen through the slit between the lips at no more than a pixel
    in any of its views - and through the lips, parted a hair, the collar
    behind the head then showed (14 Nov 2026). The fan is painted like any
    other triangle: the dark of the mouth, seen from the front."""
    import numpy as np
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    directed = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]).astype(np.int64)
    count = int(positions.shape[0])
    key = directed[:, 0] * count + directed[:, 1]
    back = directed[:, 1] * count + directed[:, 0]
    rim = directed[~np.isin(key, back)]                  # an edge no triangle runs back along
    rim = rim[(positions[rim][..., 1] > above).all(axis=1)]
    if not len(rim):
        return positions, faces, 0
    graph = coo_matrix((np.ones(len(rim), np.int8), (rim[:, 0], rim[:, 1])), shape=(count, count))
    _, label = connected_components(graph, directed=False)
    reach = HOLE_SIZE * float(np.ptp(positions, axis=0).max())
    added_points, added_faces = [], []
    for loop in np.unique(label[rim[:, 0]]):
        edges = rim[label[rim[:, 0]] == loop]
        points = np.unique(edges)
        if not 3 <= len(edges) <= HOLE_EDGES:
            continue
        # A closed rim: every point on it left once and reached once.
        if not (np.bincount(edges[:, 0], minlength=count)[points] == 1).all() \
                or not (np.bincount(edges[:, 1], minlength=count)[points] == 1).all():
            continue
        if float(np.linalg.norm(np.ptp(positions[points], axis=0))) > reach:
            continue
        middle = count + len(added_points)
        added_points.append(positions[points].mean(axis=0))
        added_faces.append(np.stack([edges[:, 1], edges[:, 0], np.full(len(edges), middle)], axis=1))
    if not added_points:
        return positions, faces, 0
    return (np.concatenate([positions, np.asarray(added_points, positions.dtype)]),
            np.concatenate([faces, np.concatenate(added_faces).astype(faces.dtype)]),
            len(added_points))


def _hidden_faces(positions, faces, above: float):
    """Which triangles above the height ``above`` nothing can see from outside
    (bool per triangle): the inside of a shell of hair, the back of a mouth."""
    import numpy as np
    hidden = np.zeros(len(faces), bool)
    head = np.nonzero(positions[faces].mean(axis=1)[:, 1] > above)[0]
    if len(head) < 8:
        return hidden
    # What stands just below (shoulders, a collar) is drawn too and hides what
    # it hides: through the head's open neck every view from below saw up
    # into the mouth, and the reducer spent the chin's triangles there.
    middle = positions[faces].mean(axis=1)[:, 1]
    tall = float(positions[faces[head]][..., 1].max()) - above
    below = np.nonzero((middle <= above) & (middle > above - HIDDEN_BELOW * tall))[0]
    drawn = np.concatenate([head, below])
    used, local = np.unique(faces[drawn], return_inverse=True)
    local = local.reshape(-1, 3)
    points = positions[used]
    framed = np.unique(local[:len(head)])                # the views are framed on the head
    seen = np.zeros(len(drawn), np.int64)                # pixels in front, over all the views
    # Directions spread evenly over the sphere (a Fibonacci spiral).
    index = np.arange(HIDDEN_VIEWS) + 0.5
    polar = np.arccos(1.0 - 2.0 * index / HIDDEN_VIEWS)
    turn = np.pi * (1.0 + 5.0 ** 0.5) * index
    directions = np.stack([np.sin(polar) * np.cos(turn), np.cos(polar),
                           np.sin(polar) * np.sin(turn)], axis=1)
    for towards in directions:
        aside = np.cross([0.0, 1.0, 0.0] if abs(towards[1]) < 0.9 else [1.0, 0.0, 0.0], towards)
        aside /= np.linalg.norm(aside)
        upward = np.cross(towards, aside)
        flat = np.stack([points @ aside, points @ upward], axis=1)
        low = flat[framed].min(axis=0)
        span = max(float((flat[framed].max(axis=0) - low).max()), 1e-12)
        owner, _ = _rasterise((flat - low) / span * (HIDDEN_GRID - 1) + 0.5, local, HIDDEN_GRID,
                              depth=(points @ towards).astype(np.float32))
        seen += np.bincount(owner[owner >= 0], minlength=len(drawn))
    hidden[head[seen[:len(head)] < HIDDEN_PIXELS]] = True
    return hidden


def _painted_parts(positions, faces, neck: float, spatial):
    """Which faces of a welded mesh belong to small parts to paint on (see
    PAINT_MAJOR_SHARE): (mask over faces, number of parts)."""
    import numpy as np
    none = np.zeros(len(faces), bool), 0
    if not len(faces):
        return none
    count, piece = _pieces(len(positions), faces)
    if count < 2:
        return none
    corners = positions[faces]
    normal = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    doubled = np.linalg.norm(normal, axis=1)
    normal = normal / np.maximum(doubled, 1e-30)[:, None]
    piece_area = np.bincount(piece, doubled, minlength=count)
    major = piece_area >= PAINT_MAJOR_SHARE * piece_area.sum()
    if major.all() or not major.any():
        return none
    # Which way is out: a piece modelled inside out (its triangles wound the
    # other way) has its normals turned, so "behind its surface" is inside it.
    middles = corners.mean(axis=1)
    piece_middle = np.stack([np.bincount(piece, middles[:, k] * doubled, minlength=count)
                             for k in range(3)], axis=1) / np.maximum(piece_area, 1e-30)[:, None]
    outwards = np.bincount(piece, ((middles - piece_middle[piece]) * normal).sum(axis=1) * doubled,
                           minlength=count)
    normal = np.where((outwards < 0)[piece][:, None], -normal, normal)
    # The larger pieces as points with normals: four per triangle (a game
    # model's triangles are large), a dense sculpt's thinned out.
    chosen = np.nonzero(major[piece])[0]
    if len(chosen) > PAINT_SAMPLE_FACES:
        chosen = chosen[::len(chosen) // PAINT_SAMPLE_FACES]
    weights = np.array([[1, 1, 1], [3, 1, 1], [1, 3, 1], [1, 1, 3]], np.float64)
    weights /= weights.sum(axis=1, keepdims=True)
    samples = np.einsum("sk,fkd->fsd", weights, corners[chosen]).reshape(-1, 3)
    sample_normals = np.repeat(normal[chosen], len(weights), axis=0)
    tree = spatial.cKDTree(samples)
    extent = float(np.ptp(positions, axis=0).max())
    reach = PAINT_HUG_REACH * extent
    middle_height = np.bincount(piece, corners[:, :, 1].mean(axis=1) * doubled, minlength=count) \
        / np.maximum(piece_area, 1e-30)
    painted = np.zeros(count, bool)
    order = np.argsort(piece, kind="stable")
    starts = np.searchsorted(piece[order], np.arange(count + 1))
    for index in np.nonzero(~major & (middle_height > neck))[0]:
        points = positions[np.unique(faces[order[starts[index]:starts[index + 1]]])]
        if float(np.linalg.norm(np.ptp(points, axis=0))) > PAINT_MAX_SIZE * extent:
            continue
        if len(points) > PAINT_POINTS:
            points = points[::len(points) // PAINT_POINTS]
        distance, nearest = tree.query(points)
        height = ((points - samples[nearest]) * sample_normals[nearest]).sum(axis=1)
        # Inside the larger piece (behind its surface, close by), or lying on it.
        hugging = ((height <= reach) & (distance <= 4 * reach)) | ((height < 0) & (distance <= 4 * reach))
        painted[index] = hugging.mean() >= PAINT_HUG_SHARE
    return painted[piece], int(painted.sum())


def bake(source: Path, out_dir: Optional[Path] = None, triangles: int = DEFAULT_TRIANGLES,
         texture: int = DEFAULT_TEXTURE,
         progress: Optional[Callable[[str, int, int], None]] = None,
         normal_map: bool = True, colours: int = 0, preset: str = "",
         detail_boost: float = DETAIL_BOOST, face_texture: bool = False,
         head_share: float = 0.0, skip_materials=(), face_layout: str = "single",
         body_textures: int = 1, painted_parts: bool = False, max_edge: int = 0) -> dict:
    """Reduce, unwrap and bake ``source`` into a game asset. Returns what it wrote.

    ``normal_map`` adds T_<name>_Normal.png (and puts it in the .glb);
    ``colours`` reduces the BaseColor to that many colours (0 keeps them all).
    ``preset`` only labels the result here - resolve_settings() turns a preset
    into these values. ``detail_boost`` (1 turns it off) gives the face and
    other fine colour detail more of the texture. ``face_texture`` gives the
    face a texture of its own, the same size, as a second material
    (T_<name>_Face_BaseColor.png): the region around the densest colour detail,
    about a tenth of the surface. A sculpt with no such detail keeps one.
    ``head_share`` (0-0.9; 0 is off) gives the head - above the neck, found
    from the face - that share of the triangles, and the body the rest
    (_reduce_with_head): the higher it is, the more of the head's shape is
    kept, at the body's expense. ``face_layout`` (FACE_LAYOUTS) lays the face
    texture out mirrored and/or with a strip of its own for the eyes;
    ``body_textures`` 2 gives the lower body a texture of its own. Each is
    another material (T_<name>_Face/_Eyes/_Lower_BaseColor.png)."""
    import numpy as np
    if texture < 16:
        raise PolypaintError(f"the texture size must be at least 16 px (got {texture})")
    if texture & (texture - 1):
        raise PolypaintError(f"the texture size must be a power of two (got {texture})")
    if triangles < 100:
        raise PolypaintError(f"a triangle budget of {triangles} is too small to bake into")
    if colours and not 2 <= colours <= 256:
        raise PolypaintError(f"a texture can be reduced to 2-256 colours (got {colours})")
    if face_layout not in FACE_LAYOUTS:
        raise PolypaintError(f"unknown face layout '{face_layout}' (choose: {', '.join(FACE_LAYOUTS)})")
    if body_textures not in (1, 2):
        raise PolypaintError(f"the body can have 1 or 2 textures (got {body_textures})")
    simplify, xatlas, spatial = _require_bake_libraries()
    steps = 4

    def step(name: str, index: int) -> None:
        if progress is not None:
            progress(name, index, steps)

    started = time.time()
    step("reading the sculpt", 0)
    textured = bake_source(Path(source)) == "texture"
    obj = (read_textured(Path(source), tuple(skip_materials)) if textured
           else read_obj(Path(source)))
    step("reducing the mesh", 1)
    # Reduce a welded copy: vertices that sit on top of each other (a UV seam,
    # a pole fan, pieces exported separately) are open edges to the reducer,
    # which keeps them, and a 1,200-triangle bake came out at 4,628.
    weld_positions, weld_faces = _weld(obj.vertices, obj.faces)
    # The sculpt's pieces, a label per vertex (_closest_by_piece).
    sculpt_piece = None
    if len(obj.faces):
        _, piece_of_face = _pieces(len(obj.vertices), obj.faces)
        sculpt_piece = np.zeros(len(obj.vertices), np.int64)
        sculpt_piece[obj.faces] = piece_of_face[:, None]
    neck = None
    head_note = ""
    painted_note = ""
    head_floor = None
    if head_share and not 0.0 < head_share <= 0.9:
        raise PolypaintError(f"the head's share of the triangles must be 0-0.9 (got {head_share})")
    if head_share or painted_parts:
        found = _face_centre(obj.vertices, obj.faces, obj.colours, with_radius=True)
        if found is None:
            if head_share:
                head_note = "no head budget: the model has too little colour detail to find a face"
        else:
            neck = float(found[0][1] - NECK_REACH * found[1])
            if painted_parts:
                head_floor = neck
                # Eyeballs, lashes, teeth: off the mesh, painted on what is behind them.
                small, parts = _painted_parts(weld_positions, weld_faces, neck, spatial)
                if parts:
                    painted_note = (f"{parts} small parts in the head (eyes, lashes, teeth: "
                                    f"{int(small.sum()):,} triangles) are painted on "
                                    "the surface behind them")
                    weld_faces = weld_faces[~small]
            if not head_share:
                neck = None
    # Cutting the face where its textures end (_cut_mesh) adds triangles: room
    # is left for them, so the budget still holds.
    target = triangles
    if face_texture and face_layout != "single":
        target -= min(CUT_RESERVE[0], int(triangles * CUT_RESERVE[1]))
    sculpt = None
    first_neck = neck
    # ``target`` is what the mesh may have before the cuts; ``ask`` what the
    # reducer is asked for, more than that when part of what it makes is
    # dropped again (the head's hidden triangles). A few rounds at most: the
    # first shows how much is hidden, and one more follows if the cuts took
    # more triangles than were kept back for them.
    ask, ask_share = target, head_share
    hidden_dropped = 0
    holes_closed = 0
    for attempt in range(4):
        neck = first_neck
        if neck is not None and len(weld_faces) > ask:
            # A body that cannot keep its shape on what the head leaves it:
            # the head's share is eased before it is given up.
            for eased in (1.0, 0.85, 0.7):
                low_positions, low_faces, head_note = _reduce_with_head(
                    simplify, weld_positions, weld_faces, ask, neck, ask_share * eased)
                if not head_note:
                    break
            if head_note:
                neck = None
        elif len(weld_faces) > ask:
            low_positions, low_faces = simplify.simplify(
                weld_positions.astype(np.float32), weld_faces.astype(np.int32),
                target_reduction=1.0 - ask / len(weld_faces))
        else:
            low_positions, low_faces = weld_positions.astype(np.float32), weld_faces.astype(np.int32)
        if len(low_faces) > ask:                # a few over: a budget is a ceiling
            low_positions, low_faces = simplify.simplify(
                np.asarray(low_positions, np.float32), np.asarray(low_faces, np.int32),
                target_reduction=1.0 - (ask - 2) / len(low_faces))
        low_positions = np.asarray(low_positions, dtype=np.float64)
        low_faces = np.asarray(low_faces, dtype=np.int64)
        # Reduction keeps flat or collapsed triangles it is given (a ZBrush pole, a
        # fan of slivers); the gate fails a mesh for them, so the bake drops them.
        corners = low_positions[low_faces]
        flat = agk.degenerate_mask(corners[:, 0], corners[:, 1], corners[:, 2])
        if flat.any():
            low_faces = low_faces[~flat]
        low_faces = _drop_stacked(low_positions, low_faces)
        if head_floor is not None:
            # The head's triangles nothing can see (the inside of a shell of
            # hair, a mouth's back): gone, and the budget spent on what shows.
            hidden = _hidden_faces(low_positions, low_faces, head_floor)
            hidden_dropped = int(hidden.sum())
            if hidden_dropped:
                heights = low_positions[low_faces].mean(axis=1)[:, 1]
                head_before = int((heights > head_floor).sum())
                low_faces = low_faces[~hidden]
                if attempt == 0 and hidden_dropped > 0.03 * target and len(weld_faces) > ask:
                    head_shown = head_before - hidden_dropped
                    if neck is not None and head_shown > 0:
                        # So that the head still shows its share of the budget.
                        head_ask = head_share * target * head_before / head_shown
                        ask = int(head_ask + (1.0 - head_share) * target)
                        ask_share = min(0.9, head_ask / ask)
                    else:
                        ask = int(target * (len(low_faces) + hidden_dropped) / len(low_faces))
                    continue
            if len(low_faces) > target:
                low_positions, low_faces = simplify.simplify(
                    np.asarray(low_positions, np.float32), np.asarray(low_faces, np.int32),
                    target_reduction=1.0 - (target - 2) / len(low_faces))
                low_positions = np.asarray(low_positions, dtype=np.float64)
                low_faces = np.asarray(low_faces, dtype=np.int64)
                corners = low_positions[low_faces]
                flat = agk.degenerate_mask(corners[:, 0], corners[:, 1], corners[:, 2])
                low_faces = _drop_stacked(low_positions, low_faces[~flat])
            # Small holes that leaves in the head (between the lips) are closed.
            low_positions, low_faces, holes_closed = _cap_holes(low_positions, low_faces, head_floor)
            corners = low_positions[low_faces]
            flat = agk.degenerate_mask(corners[:, 0], corners[:, 1], corners[:, 2])
            if flat.any():
                low_faces = low_faces[~flat]
        if sculpt is None:
            step("unwrapping UVs", 2)
        if sculpt is None:
            sculpt = (TexturedSculpt(obj, spatial) if textured
                      else Sculpt(obj.vertices, obj.faces, obj.colours, spatial))
        low_origin = None
        if sculpt_piece is not None:
            low_origin = piece_of_face[sculpt.closest(np.asarray(low_positions, np.float64))[0]]
        nearest = _closest_by_piece(obj.vertices, sculpt_piece, low_positions, low_faces, spatial,
                                    low_origin)
        corners = low_positions[low_faces]
        area = 0.5 * np.linalg.norm(np.cross(corners[:, 1] - corners[:, 0],
                                             corners[:, 2] - corners[:, 0]), axis=1)
        face_region = None
        face_note = ""
        split_neck = None
        split_skin = None           # lower: down to here, what is the face's colour is the head's
        if face_texture:
            found = _face_centre(obj.vertices, obj.faces, obj.colours, with_radius=True)
            if found is None:
                face_note = "no separate face texture: the sculpt has too little colour detail to find one"
            else:
                centre, reach = found
                split_neck = float(centre[1] - NECK_REACH * reach)
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
        # One part per material, each a texture of its own: the body (one or two:
        # upper, lower), then (if any) the face and the eye strip. A part holds its
        # reduced triangles, its UVs (None: xatlas unwraps it) and its texture's
        # shape. The face is seen from the front, one island; xatlas unwraps it
        # only when the region has no front to map from.
        view = None
        framed = None
        layout_note = ""
        mirror_own = 0
        if face_region is not None:
            # Hairline to chin: FACE_REACH times the reach of the eyes, brows and
            # lips from the centre (on the test sculpt, 3% of the surface rather than the 10%
            # an unwrapped face takes, where hair and collar filled the texture).
            middle = low_positions[low_faces].mean(axis=1)
            distance = np.linalg.norm(middle - centre, axis=1)
            # Looked for wider than that first: with the two eyes found, the face
            # is framed from them (brow to chin, cheek to cheek) and not from where
            # the colour detail happens to be - on a game model the fringe's
            # strands pulled the frame up over the hair and cut the mouth off.
            view = _front_view(low_positions, low_faces, distance <= FACE_SEARCH_REACH * reach, centre)
            framed = None
            if view is not None:
                keep, right, up = view
                looked = _front_image(sculpt, obj.vertices, low_positions, low_faces, nearest, keep,
                                      right, up, texture, padding, xatlas)
                framed = _find_eyes(looked, low_positions, right, up, texture, padding, centre)
                if framed is not None:
                    across_eyes, eye_height, apart = framed["middle"], framed["height"], framed["apart"]
                    # Looked at again from between the eyes, FACE_EYE_REACH of
                    # their distance apart all round: the detail's middle sits
                    # high on a face with a fringe, and its reach left the
                    # chin out.
                    middle_c = np.asarray(centre, np.float64)
                    eye_point = (middle_c + (across_eyes - float(middle_c @ right)) * right
                                 + (eye_height - float(middle_c @ up)) * up)
                    again = _front_view(low_positions, low_faces,
                                        np.linalg.norm(middle - eye_point, axis=1)
                                        <= FACE_EYE_REACH * apart, eye_point,
                                        direction=np.cross(right, up))
                    if again is not None and np.allclose(again[1], right, atol=1e-6):
                        keep = again[0]
                    # Every corner inside: a long triangle of a hair lock that only
                    # starts in the frame is not where the view shows the lock.
                    corners = low_positions[low_faces[keep]]
                    x, y = corners @ right, corners @ up
                    # Down to the chin, where the face shows one; FACE_FRAME's
                    # proportion otherwise.
                    chin = _find_chin(low_positions, low_faces, keep, right, up, across_eyes,
                                      eye_height, apart)
                    below = FACE_FRAME[1] if chin is None else chin + CHIN_PAD
                    # The head's texture reaches under the jaw: the neck's skin is
                    # the head's, not the body's (16 colours, most of them the suit's).
                    eye_y = float(centre[1] + (eye_height - float(np.asarray(centre) @ up)) * up[1])
                    split_skin = min(split_neck, eye_y - (below + NECK_BELOW_CHIN) * apart)
                    # A longer face has a higher brow too.
                    above = max(FACE_FRAME[2], FACE_BROW_OF_CHIN * below)
                    if framed["brow"] > FACE_BUSY_STEP:
                        # Hair combed across the brow: drawn as the view shows it,
                        # not a texel or two a triangle on the head's texture.
                        above = max(above, FACE_BUSY_BROW[1])
                    # At the chin a triangle may hang FACE_CHIN_SLACK below the
                    # frame if its middle is inside: left to the head's texture,
                    # a triangle from the chin's tip to under the jaw is one
                    # texel, the colour of the shadow under the chin.
                    inside_frame = ((np.abs(x - across_eyes) <= FACE_FRAME[0] * apart)
                                    & (y >= eye_height - (below + FACE_CHIN_SLACK) * apart)
                                    & (y <= eye_height + above * apart)).all(axis=1)
                    inside_frame &= y.mean(axis=1) >= eye_height - below * apart
                    if inside_frame.sum() >= 8:
                        keep = keep[inside_frame]
                        # Pieces of the frame apart from the face itself (the one
                        # nearest the eyes) stay only above the mouth - a fringe,
                        # brows; below it they are a collar's rim, a hanging lock.
                        _, piece = _pieces(len(low_positions), low_faces[keep])
                        at = low_positions[low_faces[keep]].mean(axis=1)
                        main = piece[int(np.argmin(np.linalg.norm(at - eye_point, axis=1)))]
                        largest = int(np.argmax(np.bincount(piece, area[keep])))
                        loose = ((piece != main) & (piece != largest)
                                 & (at @ up < eye_height - FACE_LOOSE_BELOW * apart))
                        if (~loose).sum() >= 8:
                            keep = keep[~loose]
                        keep = np.union1d(keep, _face_hollows(low_positions, low_faces, keep, right, up,
                                                              FACE_HOLLOW * apart))
                        if face_layout.startswith("eyes"):
                            # The boxes' edges where the face is plainest, from a
                            # finer look at the face than the eyes were found in.
                            fine = _front_image(sculpt, obj.vertices, low_positions, low_faces,
                                                nearest, keep, right, up, EYE_EDGE_TEXTURE, padding,
                                                xatlas)
                            framed = _calm_edges(fine, low_positions, right, up, EYE_EDGE_TEXTURE,
                                                 padding, framed)
                            del fine
                        if face_layout != "single":
                            # Cut where its textures will end: down the middle (a
                            # mirrored face) and round the eyes (their own texture).
                            in_face = np.zeros(len(low_faces), bool)
                            in_face[keep] = True
                            before = len(low_faces)
                            lines = []
                            if face_layout in ("mirror", "eyes-mirror"):
                                lines.append((right, across_eyes, None))
                            if face_layout.startswith("eyes"):
                                band = (framed["low"], framed["high"])
                                lines += [(up, framed["low"], None), (up, framed["high"], None)]
                                offsets = [framed["outer"]] + ([framed["inner"]] if framed["inner"] > 0
                                                               else [])
                                lines += [(right, across_eyes + sign * offset, band)
                                          for offset in offsets for sign in (-1.0, 1.0)]
                            for direction, value, between in lines:
                                region = in_face
                                if between is not None:      # only across the eyes' band
                                    heights = low_positions[low_faces].mean(axis=1) @ up
                                    region = in_face & (heights > between[0]) & (heights < between[1])
                                low_positions, low_faces, parent = _cut_mesh(
                                    low_positions, low_faces, direction, value, region)
                                in_face = in_face[parent]
                            if len(low_faces) != before:
                                keep = np.nonzero(in_face)[0]
                                if sculpt_piece is not None:
                                    low_origin = piece_of_face[sculpt.closest(
                                        np.asarray(low_positions, np.float64))[0]]
                                nearest = _closest_by_piece(obj.vertices, sculpt_piece, low_positions,
                                                            low_faces, spatial, low_origin)
                                corners = low_positions[low_faces]
                                area = 0.5 * np.linalg.norm(np.cross(corners[:, 1] - corners[:, 0],
                                                                     corners[:, 2] - corners[:, 0]), axis=1)
                                middle = corners.mean(axis=1)
                                distance = np.linalg.norm(middle - centre, axis=1)
                                if face_region is not None:
                                    face_region = np.zeros(len(low_faces), bool)
                        view = (keep, right, up)
                    else:
                        framed = None
            if view is not None and framed is None:
                view = _front_view(low_positions, low_faces, distance <= FACE_REACH * reach, centre)
            if view is not None:
                face_region = np.zeros(len(low_faces), bool)
                face_region[view[0]] = True
            elif face_layout != "single":
                layout_note = f"face layout {face_layout} not used: the face has no front to map from"
        if len(low_faces) <= triangles or attempt == 3 or target < 100:
            break
        over = len(low_faces) - triangles + 8 + int(0.02 * target)
        ask = max(100, int(ask * (target - over) / target))
        target -= over
    del weld_faces, weld_positions
    face_parts = []
    if face_region is not None and view is None:
        face_parts.append({"ids": np.nonzero(face_region)[0], "suffix": PART_SUFFIXES[1]})
    elif view is not None:
        keep, right, up = view
        mirrored = face_layout in ("mirror", "eyes-mirror")
        across, axis, side = right, None, 1.0
        front = None
        if mirrored:
            # Mirror about the face's own symmetry, found from the sculpt's
            # colours; the half more in view is kept, and on the other half a
            # triangle that does not match its mirror image (hair across one
            # eye) keeps texels of its own on the body texture.
            if framed is not None:
                # Between the eyes; what does not match across it is found
                # triangle by triangle below.
                across, axis, unmatched = right, framed["middle"], 0.0
            else:
                in_face = np.zeros(len(low_faces), bool)
                in_face[keep] = True
                on_face = np.nonzero(in_face[nearest])[0]
                tint = np.asarray(obj.colours, np.float32)
                if tint.max(initial=0.0) > 1.0:
                    tint = tint / 255.0
                across, axis, unmatched = _symmetry_plane(
                    obj.vertices[on_face], tint[on_face], obj.vertices, tint, right, up, reach,
                    spatial)
            if unmatched > MIRROR_MAX_MISMATCH:
                layout_note = (layout_note + "; " if layout_note else "") + (
                    f"face not mirrored: {unmatched:.0%} of it differs from its mirror image "
                    "(hair across one side?)")
                mirrored, across, axis = False, right, None
            else:
                side = _mirror_side(low_positions, low_faces, keep, across, axis)
                front = _front_image(sculpt, obj.vertices, low_positions, low_faces, nearest, keep,
                                     across, up, texture, padding, xatlas)
                own = _mirror_misfits(front, low_positions, low_faces, keep, across, axis, side,
                                      texture, padding)
                other_half = keep[side * (middle[keep] @ across - axis) < 0]
                share = float(area[own].sum() / max(float(area[other_half].sum()), 1e-30))
                if share > MIRROR_MAX_MISFITS:
                    layout_note = (layout_note + "; " if layout_note else "") + (
                        f"face not mirrored: {share:.0%} of the half that would be mirrored "
                        "differs from the other (hair across one side?)")
                    mirrored, across, axis, side, front = False, right, None, 1.0, None
                else:
                    if len(own) and framed is not None and face_layout.startswith("eyes"):
                        # Not in the boxes round the eyes: an eye is drawn once,
                        # for both sides, whatever the look in it.
                        x = np.abs(middle[own] @ across - framed["middle"])
                        y = middle[own] @ up
                        own = own[~((y > framed["low"]) & (y < framed["high"])
                                    & (x < framed["outer"]) & (x > framed["inner"]))]
                    if len(own):
                        keep = np.setdiff1d(keep, own)
                        mirror_own = len(own)
        eyes = None
        folded = mirrored and framed is not None        # cut down the middle: plain UVs
        # Not mirrored: a box round each eye all the same, side by side.
        paired = (not mirrored and framed is not None and framed["inner"] > 0
                  and face_layout.startswith("eyes"))
        if face_layout.startswith("eyes"):
            if framed is not None:
                # The triangles in the boxes cut round the eyes (one box across
                # both when the face is not mirrored).
                x = np.abs(middle[keep] @ across - framed["middle"])
                y = middle[keep] @ up
                inside_band = ((y > framed["low"]) & (y < framed["high"]) & (x < framed["outer"])
                               & ((x > framed["inner"]) | (framed["inner"] <= 0)))
            else:
                if front is None:
                    front = _front_image(sculpt, obj.vertices, low_positions, low_faces, nearest,
                                         keep, across, up, texture, padding, xatlas)
                band = _eye_band(front, low_positions, up, texture, padding)
                height = middle[keep] @ up
                inside_band = ((height >= band[0]) & (height <= band[1]) if band is not None
                               else np.zeros(len(keep), bool))
            if inside_band.sum() >= 4 and (~inside_band).sum() >= 8:
                eyes = keep[inside_band]
                keep = keep[~inside_band]
            else:
                layout_note = (layout_note + "; " if layout_note else "") + \
                    "no eye strip: too few triangles across the eyes"
        seen = (across, up, np.cross(across, up), (axis, side) if folded else None)
        face_parts.append({"ids": keep, "suffix": PART_SUFFIXES[1], "mirror": mirrored,
                           "fold": folded, "view": seen,
                           "uvs": (_folded_uvs(low_positions, low_faces, keep, across, up,
                                               (texture, texture), padding, axis) if folded
                                   else _projected_uvs(low_positions, low_faces, keep, across, up,
                                                       (texture, texture), padding, axis=axis,
                                                       side=side))})
        if eyes is not None:
            # The strip's shape (same texels as a square texture) keeps its
            # texels nearest square: 128 x 32 at 64 px for a long, low strip.
            corners = low_positions[low_faces[eyes]].reshape(-1, 3)
            gap = (framed["middle"], framed["inner"]) if paired else None
            if folded:
                wide = float(np.ptp(np.abs(corners @ across - axis)))
            elif paired:
                wide = float(np.ptp(_close_gap(corners @ across, *gap)))
            elif mirrored:
                wide = float(np.abs(corners @ across - axis).max())
            else:
                wide = float(np.ptp(corners @ across))
            shape = _strip_shape(wide, float(np.ptp(corners @ up)), texture, max_edge)
            face_parts.append({"ids": eyes, "suffix": PART_SUFFIXES[2], "mirror": mirrored,
                               "fold": folded, "shape": shape,
                               "view": seen + (gap,) if paired else seen,
                               "uvs": (_folded_uvs(low_positions, low_faces, eyes, across, up,
                                                   shape, padding, axis) if folded
                                       else _paired_uvs(low_positions, low_faces, eyes, across, up,
                                                        shape, padding, *gap) if paired
                                       else _projected_uvs(low_positions, low_faces, eyes, across,
                                                           up, shape, padding, fill=not mirrored,
                                                           axis=axis, side=side))})
    face_region = None
    if face_parts:
        face_region = np.zeros(len(low_faces), bool)
        for part in face_parts:
            face_region[part["ids"]] = True
    body = np.nonzero(~face_region)[0] if face_region is not None else np.arange(len(low_faces))
    parts = []
    if body_textures == 2:
        height = low_positions[low_faces[body]].mean(axis=1)[:, 1]
        if split_neck is not None:
            # The head (hair, ears, the back of it) and the body: the head has
            # most of the triangles, and in one texture with the body its
            # islands are a texel or two each.
            upper, suffix = height > split_neck, PART_SUFFIXES[3]
            if split_skin is not None and split_skin < split_neck and face_parts:
                # Under the jaw the neck's skin is the head's too, but not a
                # collar that stands as high: between the two heights a
                # triangle goes by its colour - nearer the face's or the body's.
                tint = np.asarray(obj.colours, np.float64)
                on = nearest >= 0
                count = np.bincount(nearest[on], minlength=len(low_faces))
                shade = np.stack([np.bincount(nearest[on], tint[on, channel],
                                              minlength=len(low_faces))
                                  for channel in range(3)], axis=1) / np.maximum(count, 1)[:, None]
                face_ids = np.concatenate([part["ids"] for part in face_parts])
                below_ids = body[height <= split_skin]
                face_ids, below_ids = face_ids[count[face_ids] > 0], below_ids[count[below_ids] > 0]
                band = (height > split_skin) & (height <= split_neck)
                bare = body[band & (count[body] == 0)]
                if len(bare):
                    # No sculpt point is nearest it (a coarse game model): the
                    # colour of the point nearest its middle.
                    _, at = spatial.cKDTree(obj.vertices).query(
                        low_positions[low_faces[bare]].mean(axis=1))
                    shade[bare] = tint[at]
                if len(face_ids) and len(below_ids) and band.any():
                    skin = np.median(shade[face_ids], axis=0)
                    cloth = np.median(shade[below_ids], axis=0)
                    like_skin = (np.linalg.norm(shade[body] - skin, axis=1)
                                 < np.linalg.norm(shade[body] - cloth, axis=1))
                    upper = upper | (band & like_skin)
        else:
            # No head found: upper and lower, at the height that halves the area.
            order = np.argsort(height, kind="stable")
            halfway = height[order[min(np.searchsorted(np.cumsum(area[body][order]),
                                                       0.5 * float(area[body].sum())),
                                       len(order) - 1)]]
            upper, suffix = height >= halfway, PART_SUFFIXES[0]
        if upper.all() or not upper.any():
            layout_note = (layout_note + "; " if layout_note else "") + \
                "one body texture: the body is too small to split"
            parts.append({"ids": body, "suffix": ""})
        elif suffix == PART_SUFFIXES[3]:
            parts.append({"ids": body[~upper], "suffix": ""})
            parts.append({"ids": body[upper], "suffix": suffix})
        else:
            parts.append({"ids": body[upper], "suffix": ""})
            parts.append({"ids": body[~upper], "suffix": suffix})
    else:
        parts.append({"ids": body, "suffix": ""})
    if str(preset).startswith("n64") and colours == 16 and 32 <= texture <= NEAREST_TEXTURE:
        # The N64 presets' body textures (and the head's): 16 colours left a suit's or a
        # head of hair's shading in blocks - a few tones for a smooth gradient,
        # one to an island. 256 colours at half the texels take the same
        # texture memory on an N64 (CI8 for CI4), and these islands are a texel
        # or two a triangle either way. The face and eyes, one island each,
        # keep their texels and 16 colours.
        for part in parts:
            part.update(shape=(texture, texture // 2), colours=SMOOTH_COLOURS,
                        padding=2 * padding)
    parts.extend(face_parts)
    built = _assemble(parts, low_positions, low_faces, nearest, xatlas, texture, padding,
                      priority, boost)
    positions, uvs, vertex_normals = built["positions"], built["uvs"], built["normals"]
    all_faces, part_faces, tangents = built["faces"], built["part_faces"], built["tangents"]
    step("baking the textures", 3)
    baked = []
    mirror_normal = False
    # The face and its eye strip meet on the face: reduced to colours each on
    # its own, the same skin came out two tones and the strip showed as a band
    # across the eyes. They are reduced together, to one palette.
    together = [index for index, part in enumerate(parts)
                if part["suffix"] in PART_SUFFIXES[1:3]] if colours else []
    together = together if len(together) > 1 else []
    for index, part in enumerate(parts):
        # A normal map mirrored would point the other half's bumps the wrong
        # way across (its sideways component flips); a mirrored part has none.
        mirror_normal |= normal_map and bool(part.get("mirror"))
        baked.append(_bake_part(sculpt, obj.vertices, built, index,
                                part.get("shape", (texture, texture)),
                                normal_map and not part.get("mirror"),
                                0 if index in together else part.get("colours", colours),
                                view=part.get("view")))
    if together:
        # The face first, in colours of its own; the eyes then keep the face's
        # colours their edge is made of (the skin meets in one tone) and take
        # the rest for themselves - iris, lash, the white of the eye. In one
        # palette of 16 the eyes' colours left the skin too few, and it banded.
        face_index, eye_index = together[0], together[1]
        face_image = _reduce_colours(baked[face_index][0].convert("RGB"), colours,
                                     baked[face_index][2])
        baked[face_index] = (face_image,) + baked[face_index][1:]
        used = sorted(set(np.asarray(face_image).ravel().tolist()))
        face_palette = np.asarray(face_image.getpalette(), np.float64).reshape(-1, 3)[used]
        eye_pixels = np.asarray(baked[eye_index][0].convert("RGB"), np.float64)
        covered = baked[eye_index][2]
        edge = np.zeros(covered.shape, bool)
        ring = EYE_PALETTE_RING
        edge[:ring], edge[-ring:], edge[:, :ring], edge[:, -ring:] = True, True, True, True
        edge &= covered
        fixed = None
        if edge.any():
            nearest_colour = ((eye_pixels[edge][:, None, :] - face_palette[None]) ** 2).sum(axis=2) \
                .argmin(axis=1)
            counts = np.bincount(nearest_colour, minlength=len(face_palette))
            order = np.argsort(-counts, kind="stable")[:max(1, int(colours * EYE_PALETTE_SHARED))]
            fixed = face_palette[order[counts[order] > 0]]
        baked[eye_index] = (_reduce_colours(baked[eye_index][0].convert("RGB"), colours, covered,
                                            fixed=fixed),) + baked[eye_index][1:]
    del sculpt, nearest

    out_dir = Path(out_dir) if out_dir is not None else Path(source).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    mesh_name, texture_name = output_names(Path(source))
    written = []
    for part, (image, normal_image, _) in zip(parts, baked):
        base_name, normal_name = part_texture_names(Path(source), part["suffix"])
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
              "mirror": bool(part.get("mirror")) and not part.get("fold"),
              "image_names": (base_name[:-4], normal_name[:-4] if normal_name else "")}
             for part, faces, (base_name, base_png, normal_name, normal_png)
             in zip(parts[1:], part_faces[1:], written[1:])]
    base_name, base_png, normal_name, normal_png = written[0]
    write_glb(mesh_path, positions, vertex_normals, uvs, part_faces[0], base_png, material,
              tangents=tangents if any(item[3] is not None for item in written) else None,
              normal_png=normal_png,
              image_names=(base_name[:-4], normal_name[:-4] if normal_name else ""),
              nearest=texture <= NEAREST_TEXTURE, extra_parts=tuple(extra))
    if normal_png is not None:
        note = "BaseColor and a tangent-space normal map (glTF/OpenGL, green up)"
    else:
        note = "colour only (no normal map)"
    if colours and parts[0].get("colours"):
        wide, tall = baked[0][0].size
        note += (f"; BaseColor {wide}x{tall} in {parts[0]['colours']} colours (the texture memory of "
                 f"{texture}x{texture} in {colours}: smoother shading); a face's textures in {colours}")
    elif colours:
        note += f"; BaseColor reduced to {colours} colours"
    if len(all_faces) > triangles:
        note += (f"; WARNING: reduced to {len(all_faces):,} triangles, not {triangles:,} "
                 "(the sculpt has open edges the reducer keeps)")
    face_share = 0.0
    suffixes = [part["suffix"] for part in parts]
    described = []
    for part, (image, _, _) in zip(parts, baked):
        share = float(area[part["ids"]].sum() / max(area.sum(), 1e-30))
        part["share"] = share
        part["size"] = list(image.size)
        if part["suffix"] in PART_SUFFIXES[1:3]:
            face_share += share
        if part["suffix"]:
            what = {"_Lower": "the lower body", "_Face": "the face",
                    "_Eyes": "the eyes and brows",
                    "_Head": "the rest of the head"}[part["suffix"]]
            described.append(f"{what} {image.size[0]}x{image.size[1]}"
                             + (f" in {part['colours']} colours" if part.get("colours") else "")
                             + (" mirrored" if part.get("mirror") else "")
                             + f" ({share:.0%} of the surface)")
    if described:
        note += f"; textures of their own: {', '.join(described)}"
    if painted_note:
        note += f"; {painted_note}"
    if hidden_dropped:
        note += (f"; {hidden_dropped:,} triangles of the head that nothing can see were dropped "
                 "and the budget spent on the rest")
    if holes_closed:
        note += (f"; {holes_closed} small hole{'s' if holes_closed != 1 else ''} in the head closed "
                 "(between the lips)")
    if face_note:
        note += f"; {face_note}"
    if layout_note:
        note += f"; {layout_note}"
    if mirror_own:
        note += (f"; {mirror_own} triangles on the mirrored half of the face differ from "
                 "the other half (hair across it?) and keep texels of their own on the body texture")
    if mirror_normal:
        note += "; no normal map on the mirrored face textures (it would point the wrong way on the mirrored half)"
    kept_share = 0.0
    if neck is not None:
        kept_share = float((positions[all_faces].mean(axis=1)[:, 1] > neck).mean())
        note += f"; the head (above the neck) has {kept_share:.0%} of the triangles"
    elif head_note:
        note += f"; {head_note}"
    inside = baked[0][2]
    face_index = suffixes.index("_Face") if "_Face" in suffixes else -1
    return {
        "source": str(source), "colour": obj.source,
        "mesh": str(mesh_path), "texture": str(out_dir / written[0][0]),
        "normal": str(out_dir / written[0][2]) if written[0][2] else "",
        "face_texture": str(out_dir / written[face_index][0]) if face_index >= 0 else "",
        "face_normal": (str(out_dir / written[face_index][2])
                        if face_index >= 0 and written[face_index][2] else ""),
        "face_share": round(face_share, 3), "materials": len(written),
        "textures": [str(out_dir / name) for item in written for name in (item[0], item[2]) if name],
        "parts": [{"texture": str(out_dir / item[0]), "suffix": part["suffix"],
                   "size": part["size"], "mirror": bool(part.get("mirror")),
                   "share": round(part["share"], 3)} for part, item in zip(parts, written)],
        "face_layout": face_layout if face_index >= 0 else "",
        "body_textures": 2 if "_Lower" in suffixes or "_Head" in suffixes else 1,
        "source_triangles": int(len(obj.faces)), "triangles": int(len(all_faces)),
        "texture_size": texture, "colours": colours, "preset": preset,
        "samples_per_texel": samples_per_texel(texture) ** 2,
        "coverage": round(float(inside.mean()), 3),
        "detail_boost": round(boost, 2),
        "head_share": round(kept_share, 3),
        "seconds": round(time.time() - started, 1), "note": note,
        # The same, one point a line: what was made, left out or fell back, and why.
        "notes": [part.strip() for part in note.split("; ") if part.strip()],
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
