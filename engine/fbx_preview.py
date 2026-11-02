#!/usr/bin/env python3
"""FBX -> .glb conversion for the Art Workstation's 3D preview.

The browser cannot draw FBX, and the gate already parses it (binary and ASCII,
no Autodesk SDK). This module turns that same node tree into a small GLB so a
reviewer can orbit the mesh. It is a *preview*, and the viewer says so:

* positions, triangles, UVs (first set) and the file's own normals, per corner,
  so hard edges and UV seams survive;
* each material slot's base-colour (diffuse) texture, referenced by file name -
  the server stores the asset's textures beside the preview, so they load when
  they were uploaded with the FBX; a slot without one gets a flat colour;
* no animation, skinning, blend shapes, cameras or lights;
* object transforms are applied (translation, pre-rotation, rotation with its
  rotation order, scaling, geometric offsets) but rotation/scaling pivots and
  inherit modes are not, so an unusual rig can sit differently than in the DCC;
* the file's axis system and unit scale are converted to glTF's (Y up, metres).

What is drawn matches what the gate counts: Unreal collision meshes (UCX_, UBX_,
USP_, UCP_) and LOD1+ levels of an LOD group are left out, and polygons are fan
triangulated the same way, so the preview's triangle count equals the verdict's.

    python engine/fbx_preview.py model.fbx out.glb
"""

from __future__ import annotations

import json
import math
import struct
from urllib.parse import quote
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import asset_gatekeeper as agk  # noqa: E402 - the validated FBX parser

try:
    import numpy as np
except ImportError:                                   # pragma: no cover
    np = None

#: What the viewer prints over every converted preview. One place, so the API,
#: the report and the page cannot drift apart.
PREVIEW_WARNING = (
    "You are viewing a .glb converted from this FBX, not the FBX itself. "
    "Geometry, UVs, normals and base-colour textures (those uploaded with it); "
    "a slot with no texture gets a flat colour. No animation, skinning or other "
    "material inputs; pivots are not applied, so placement can differ from your "
    "DCC or engine. The numbers on the right are measured from the FBX."
)

#: Material properties a base-colour texture is connected to, by DCC.
BASE_COLOUR_PROPERTIES = ("diffusecolor", "basecolor", "base_color", "maya|basecolor",
                          "maya|base_color", "3dsmax|parameters|base_color_map", "diffuse")

#: Distinguishable, mid-value slot colours (sRGB 0..1), cycled.
SLOT_COLOURS = (
    (0.62, 0.64, 0.68), (0.85, 0.55, 0.35), (0.40, 0.65, 0.85), (0.55, 0.78, 0.45),
    (0.80, 0.45, 0.65), (0.90, 0.78, 0.40), (0.50, 0.50, 0.85), (0.45, 0.80, 0.78),
)


class FbxPreviewError(Exception):
    """The FBX can be measured but not converted (or not read at all)."""


def _props70(node) -> dict:
    """``Properties70`` as {name: values}: ``P: "Lcl Rotation", ..., x, y, z``."""
    table = {}
    block = node.child("Properties70") if node is not None else None
    for entry in (block.all("P") if block is not None else []):
        if entry.props and isinstance(entry.props[0], str):
            table[entry.props[0]] = entry.props[4:]
    return table


def _vec3(table: dict, name: str, default: tuple) -> tuple:
    values = table.get(name)
    if not values or len(values) < 3:
        return default
    try:
        return tuple(float(v) for v in values[:3])
    except (TypeError, ValueError):
        return default


def _matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _translate(t):
    return [[1, 0, 0, t[0]], [0, 1, 0, t[1]], [0, 0, 1, t[2]], [0, 0, 0, 1]]


def _scale(s):
    return [[s[0], 0, 0, 0], [0, s[1], 0, 0], [0, 0, s[2], 0], [0, 0, 0, 1]]


def _rotate(degrees, order: int = 0):
    """Euler rotation. FBX order 0 = eEulerXYZ: X applied first, so R = Rz Ry Rx."""
    x, y, z = (math.radians(v) for v in degrees)
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    rx = [[1, 0, 0, 0], [0, cx, -sx, 0], [0, sx, cx, 0], [0, 0, 0, 1]]
    ry = [[cy, 0, sy, 0], [0, 1, 0, 0], [-sy, 0, cy, 0], [0, 0, 0, 1]]
    rz = [[cz, -sz, 0, 0], [sz, cz, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    axes = {"X": rx, "Y": ry, "Z": rz}
    sequence = ("XYZ", "XZY", "YZX", "YXZ", "ZXY", "ZYX")[order] if 0 <= order <= 5 else "XYZ"
    result = [[1 if i == j else 0 for j in range(4)] for i in range(4)]
    for axis in sequence:                    # first-applied axis ends up rightmost
        result = _matmul(axes[axis], result)
    return result


def _local_matrix(model) -> list:
    table = _props70(model)
    order = table.get("RotationOrder")
    try:
        order = int(order[0]) if order else 0
    except (TypeError, ValueError):
        order = 0
    translation = _vec3(table, "Lcl Translation", (0.0, 0.0, 0.0))
    rotation = _vec3(table, "Lcl Rotation", (0.0, 0.0, 0.0))
    scaling = _vec3(table, "Lcl Scaling", (1.0, 1.0, 1.0))
    pre = _vec3(table, "PreRotation", (0.0, 0.0, 0.0))
    post = _vec3(table, "PostRotation", (0.0, 0.0, 0.0))
    matrix = _translate(translation)
    matrix = _matmul(matrix, _rotate(pre, 0))
    matrix = _matmul(matrix, _rotate(rotation, order))
    if any(post):
        inverse_post = _rotate(tuple(-v for v in post), 5)   # (Rz Ry Rx)^-1 = Rx^-1 Ry^-1 Rz^-1
        matrix = _matmul(matrix, inverse_post)
    return _matmul(matrix, _scale(scaling))


def _geometric_matrix(model) -> list:
    table = _props70(model)
    matrix = _translate(_vec3(table, "GeometricTranslation", (0.0, 0.0, 0.0)))
    matrix = _matmul(matrix, _rotate(_vec3(table, "GeometricRotation", (0.0, 0.0, 0.0)), 0))
    return _matmul(matrix, _scale(_vec3(table, "GeometricScaling", (1.0, 1.0, 1.0))))


def _axis_matrix(top: dict) -> tuple[list, float, str]:
    """File axes and units -> glTF (right +X, up +Y, front +Z, metres)."""
    table = _props70(top.get("GlobalSettings"))

    def setting(name: str, default: int) -> int:
        values = table.get(name)
        try:
            return int(values[0]) if values else default
        except (TypeError, ValueError):
            return default

    up, up_sign = setting("UpAxis", 1), setting("UpAxisSign", 1)
    front, front_sign = setting("FrontAxis", 2), setting("FrontAxisSign", 1)
    coord, coord_sign = setting("CoordAxis", 0), setting("CoordAxisSign", 1)
    if sorted((up, front, coord)) != [0, 1, 2]:
        up, up_sign, front, front_sign, coord, coord_sign = 1, 1, 2, 1, 0, 1
    unit = table.get("UnitScaleFactor")
    try:
        centimetres = float(unit[0]) if unit else 1.0
    except (TypeError, ValueError):
        centimetres = 1.0
    if not math.isfinite(centimetres) or centimetres <= 0:
        centimetres = 1.0
    scale = centimetres / 100.0
    rows = [[0.0] * 4 for _ in range(4)]
    rows[0][coord] = (1 if coord_sign >= 0 else -1) * scale
    rows[1][up] = (1 if up_sign >= 0 else -1) * scale
    rows[2][front] = (1 if front_sign >= 0 else -1) * scale
    rows[3][3] = 1.0
    names = "XYZ"
    described = (f"{'+' if up_sign >= 0 else '-'}{names[up]} up, "
                 f"1 unit = {centimetres:g} cm")
    return rows, scale, described


def _scene(path: Path):
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FbxPreviewError(f"could not read FBX ({exc})") from exc
    try:
        nodes = agk.parse_fbx_binary(data)
    except Exception as exc:
        raise FbxPreviewError(f"could not parse FBX ({type(exc).__name__}: {exc})") from exc
    return {node.name: node for node in nodes}


def _layer_values(geometry, layer_name: str, values_name: str, index_name: str, width: int,
                  corners, control_count: int):
    """Per-corner values (UVs, normals) from an FBX layer element, or None.

    Handles the mappings exporters actually write: ByPolygonVertex and
    ByControlPoint/ByVertice, each Direct or IndexToDirect.
    """
    layer = geometry.child(layer_name)
    if layer is None:
        return None
    values_node = layer.child(values_name)
    if values_node is None or not values_node.props:
        return None
    values = np.asarray(values_node.props[0], dtype=np.float64)
    if values.size == 0 or values.size % width:
        return None
    values = values.reshape(-1, width)
    mapping = agk._fbx_string(layer.child("MappingInformationType"))
    reference = agk._fbx_string(layer.child("ReferenceInformationType"))
    index_node = layer.child(index_name)
    indices = (np.asarray(index_node.props[0], dtype=np.int64)
               if index_node is not None and index_node.props else None)
    if mapping == "ByPolygonVertex":
        lookup = np.arange(len(corners))
    elif mapping in ("ByControlPoint", "ByVertice", "ByVertex"):
        lookup = corners
    else:
        return None
    if reference == "IndexToDirect" and indices is not None:
        if lookup.size and int(lookup.max()) >= len(indices):
            return None
        lookup = indices[lookup]
    if lookup.size and (int(lookup.max()) >= len(values) or int(lookup.min()) < 0):
        return None
    return values[lookup]


def _textures_by_material(objects, connections) -> dict:
    """material id -> the base-colour texture's path as the FBX names it."""
    textures = {}
    for node in objects.children:
        if node.name != "Texture":
            continue
        relative = agk._fbx_string(node.child("RelativeFilename")).replace("\\", "/")
        absolute = agk._fbx_string(node.child("FileName")).replace("\\", "/")
        # Where the workstation keeps it: the relative path as written, or the
        # bare file name for an absolute one (that is how sidecars are stored).
        chosen = relative if relative and ":" not in relative and not relative.startswith("/") \
            else (relative or absolute).rsplit("/", 1)[-1]
        chosen = chosen.removeprefix("./")
        if chosen:
            textures[agk._fbx_id(node)] = chosen
    found = {}
    for link in (connections.all("C") if connections else []):
        if len(link.props) < 4 or link.props[0] != "OP":
            continue
        texture, material, slot = link.props[1], link.props[2], str(link.props[3]).lower()
        if texture in textures and slot in BASE_COLOUR_PROPERTIES and material not in found:
            found[material] = textures[texture]
    return found


def convert(path: Path, out_path: Path) -> dict:
    """Write a preview GLB for ``path``; return what was converted.

    {"triangles", "vertices", "materials": [names], "meshes", "bounds": (min, max),
     "axes": "+Z up, 1 unit = 1 cm", "skipped": {"collision": n, "lower_lod": n}}
    """
    if np is None:                                   # pragma: no cover
        raise FbxPreviewError("numpy is not installed; FBX cannot be converted")
    top = _scene(Path(path))
    objects = top.get("Objects")
    if objects is None:
        raise FbxPreviewError("the FBX has no Objects section")

    geometries, models, materials, model_names, lod_groups = {}, {}, {}, {}, []
    for node in objects.children:
        node_id = agk._fbx_id(node)
        kind = agk._fbx_class(node)
        if node.name == "Geometry" and kind == "Mesh":
            geometries[node_id] = node
        elif node.name == "Model":
            models[node_id] = node
            model_names[node_id] = agk._fbx_object_name(node)
            if kind == "LodGroup":
                lod_groups.append(node_id)
        elif node.name == "Material":
            materials[node_id] = node

    parents, children = {}, {}
    connections = top.get("Connections")
    for link in (connections.all("C") if connections else []):
        if len(link.props) < 3 or link.props[0] != "OO":
            continue
        child, parent = link.props[1], link.props[2]
        parents.setdefault(child, []).append(parent)
        children.setdefault(parent, []).append(child)

    material_textures = _textures_by_material(objects, connections)

    # Same selection as the gate: no collision hulls, no LOD1+ levels.
    collision = {m for m, name in model_names.items()
                 if name.lower().startswith(agk.FBX_COLLISION_PREFIXES)}
    lower_lod: set = set()

    def descendants(start) -> set:
        found, stack = set(), [start]
        while stack:
            for item in children.get(stack.pop(), ()):
                if item not in found:
                    found.add(item)
                    stack.append(item)
        return found

    for group in lod_groups:
        levels = [c for c in children.get(group, []) if c in models and c not in collision]
        for level in levels[1:]:
            lower_lod.add(level)
            lower_lod |= descendants(level)

    world_cache: dict = {}

    def world(model_id, depth: int = 0) -> list:
        if model_id in world_cache:
            return world_cache[model_id]
        local = _local_matrix(models[model_id])
        parent = next((p for p in parents.get(model_id, []) if p in models), None)
        matrix = local if parent is None or depth > 64 else _matmul(world(parent, depth + 1), local)
        world_cache[model_id] = matrix
        return matrix

    axis, unit_scale, axes_text = _axis_matrix(top)
    all_positions, all_uvs, all_normals, primitives = [], [], [], {}
    material_names: list = []
    material_texture: dict = {}      # material name -> texture path
    has_uv = has_normals = False
    corner_total = 0
    vertex_total = triangle_total = 0
    skipped = {"collision": 0, "lower_lod": 0}
    meshes = 0

    for geometry_id, geometry in geometries.items():
        owners = [p for p in parents.get(geometry_id, []) if p in models]
        if owners and all(o in collision for o in owners):
            skipped["collision"] += 1
            continue
        if owners and all(o in lower_lod for o in owners):
            skipped["lower_lod"] += 1
            continue
        index_node, vertex_node = geometry.child("PolygonVertexIndex"), geometry.child("Vertices")
        if index_node is None or vertex_node is None or not index_node.props or not vertex_node.props:
            continue
        positions = np.asarray(vertex_node.props[0], dtype=np.float64)
        if positions.size == 0 or positions.size % 3:
            raise FbxPreviewError("corrupt FBX vertex array (not a multiple of 3)")
        positions = positions.reshape(-1, 3)
        corners, starts, sizes = agk._fbx_polygons(index_node.props[0])
        if corners.size and int(corners.max()) >= len(positions):
            raise FbxPreviewError("corrupt FBX polygon list (index past the last vertex)")
        if not np.all(np.isfinite(positions)):
            raise FbxPreviewError("the FBX has non-finite vertex positions")

        # Per-polygon material index -> this model's material slot names.
        model = owners[0] if owners else None
        slot_ids = [c for c in children.get(model, []) if c in materials] if model else []
        slot_names = [agk._fbx_object_name(materials[m]) or f"material {i}"
                      for i, m in enumerate(slot_ids)] or ["default"]
        for material_id, slot_name in zip(slot_ids, slot_names):
            if material_id in material_textures:
                material_texture.setdefault(slot_name, material_textures[material_id])
        per_polygon = np.zeros(len(sizes), dtype=np.int64)
        layer = geometry.child("LayerElementMaterial")
        if layer is not None:
            mapping = agk._fbx_string(layer.child("MappingInformationType"))
            values = layer.child("Materials")
            if values is not None and values.props:
                raw = np.asarray(values.props[0], dtype=np.int64)
                if mapping == "ByPolygon" and raw.size == len(sizes):
                    per_polygon = raw
                elif raw.size:
                    per_polygon[:] = raw[0]
        per_polygon = np.clip(per_polygon, 0, len(slot_names) - 1)

        transform = np.eye(4)
        if model is not None:
            transform = np.asarray(world(model), dtype=np.float64) @ np.asarray(
                _geometric_matrix(models[model]), dtype=np.float64)
        transform = np.asarray(axis, dtype=np.float64) @ transform
        # Unit scale is in the axis matrix; Lcl values are in file units.
        homogeneous = np.c_[positions, np.ones(len(positions))]
        placed = (homogeneous @ transform.T)[:, :3]
        flip = np.linalg.det(transform[:3, :3]) < 0

        # One preview vertex per polygon corner, so per-corner UVs and normals
        # (seams, hard edges) survive. The reported vertex count stays the
        # FBX's own control points.
        uv = _layer_values(geometry, "LayerElementUV", "UV", "UVIndex", 2, corners,
                           len(positions))
        normals = _layer_values(geometry, "LayerElementNormal", "Normals", "NormalsIndex", 3,
                                corners, len(positions))
        corner_positions = placed[corners]
        if uv is None:
            uv = np.zeros((len(corners), 2))
        else:
            has_uv = True
            uv = np.c_[uv[:, 0], 1.0 - uv[:, 1]]          # FBX v is up, glTF v is down
        if normals is None:
            normals = np.zeros((len(corners), 3))
        else:
            has_normals = True
            inverse_transpose = np.linalg.inv(transform[:3, :3]).T
            normals = normals @ inverse_transpose.T
            length = np.linalg.norm(normals, axis=1, keepdims=True)
            normals = np.divide(normals, length, out=np.zeros_like(normals), where=length > 1e-12)

        base = corner_total
        all_positions.append(corner_positions)
        all_uvs.append(uv)
        all_normals.append(normals)
        corner_total += len(corners)
        vertex_total += len(placed)
        meshes += 1

        fan = np.clip(sizes - 2, 0, None)
        count = int(fan.sum())
        if not count:
            continue
        owner = np.repeat(np.arange(len(sizes)), fan)
        first = np.repeat(np.cumsum(fan) - fan, fan)
        step = np.arange(count) - first + 1
        a = starts[owner] + base
        b = starts[owner] + step + base
        c = starts[owner] + step + 1 + base
        triangles = np.stack([a, c, b] if flip else [a, b, c], axis=1)
        triangle_total += count
        for slot in np.unique(per_polygon[owner]):
            name = slot_names[int(slot)]
            if name not in material_names:
                material_names.append(name)
            primitives.setdefault(material_names.index(name), []).append(
                triangles[per_polygon[owner] == slot])

    if not all_positions or not triangle_total:
        raise FbxPreviewError("the FBX has no mesh geometry to preview")
    positions = np.vstack(all_positions).astype(np.float32)
    uvs = np.vstack(all_uvs).astype(np.float32) if has_uv else None
    normals_out = np.vstack(all_normals).astype(np.float32) if has_normals else None
    groups = [(slot, np.vstack(parts).astype(np.uint32)) for slot, parts in sorted(primitives.items())]
    textures = [material_texture.get(name, "") if has_uv else "" for name in material_names]
    _write_glb(Path(out_path), positions, groups, material_names, Path(path).stem,
               uvs=uvs, normals=normals_out, textures=textures)
    return {
        "uvs": has_uv,
        "normals": has_normals,
        "textures": sorted({item for item in textures if item}),
        "triangles": triangle_total,
        "vertices": vertex_total,
        "materials": material_names,
        "meshes": meshes,
        "bounds": (positions.min(axis=0).astype(float).tolist(),
                   positions.max(axis=0).astype(float).tolist()),
        "axes": axes_text,
        "unit_scale": unit_scale,
        "skipped": skipped,
    }


def _pad(blob: bytes, filler: bytes) -> bytes:
    return blob + filler * ((4 - len(blob) % 4) % 4)


def _write_glb(path: Path, positions, groups, material_names: list, name: str,
               uvs=None, normals=None, textures: list | None = None) -> None:
    """One mesh, one primitive per material slot, vertex arrays shared. GLB 2.0.

    ``textures`` names each material's base-colour image by the path the
    workstation stores it under; the viewer resolves it against the asset's
    files, so a texture uploaded with the FBX shows, and one that was not
    falls back to the slot colour."""
    position_bytes = positions.tobytes()
    buffer = bytearray(_pad(position_bytes, b"\x00"))
    views = [{"buffer": 0, "byteOffset": 0, "byteLength": len(position_bytes), "target": 34962}]
    accessors = [{
        "bufferView": 0, "componentType": 5126, "count": int(len(positions)), "type": "VEC3",
        # float32 values, so min/max match the data exactly (validator-clean).
        "min": [float(v) for v in positions.min(axis=0)],
        "max": [float(v) for v in positions.max(axis=0)],
    }]
    attributes = {"POSITION": 0}
    for key, array, kind in (("NORMAL", normals, "VEC3"), ("TEXCOORD_0", uvs, "VEC2")):
        if array is None:
            continue
        blob = array.tobytes()
        offset = len(buffer)
        buffer += _pad(blob, b"\x00")
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(blob), "target": 34962})
        accessors.append({"bufferView": len(views) - 1, "componentType": 5126,
                          "count": int(len(array)), "type": kind})
        attributes[key] = len(accessors) - 1
    mesh_primitives = []
    for slot, triangles in groups:
        index_bytes = triangles.reshape(-1).tobytes()
        offset = len(buffer)
        buffer += _pad(index_bytes, b"\x00")
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(index_bytes),
                      "target": 34963})
        accessors.append({"bufferView": len(views) - 1, "componentType": 5125,
                          "count": int(triangles.size), "type": "SCALAR"})
        mesh_primitives.append({"attributes": dict(attributes), "indices": len(accessors) - 1,
                                "material": int(slot), "mode": 4})
    textures = textures or [""] * len(material_names)
    images = sorted({item for item in textures if item})
    material_list = []
    for i, material in enumerate(material_names):
        image = textures[i] if i < len(textures) else ""
        pbr = {"metallicFactor": 0.0, "roughnessFactor": 0.8}
        if image:
            pbr["baseColorFactor"] = [1.0, 1.0, 1.0, 1.0]
            pbr["baseColorTexture"] = {"index": images.index(image)}
        else:
            pbr["baseColorFactor"] = list(SLOT_COLOURS[i % len(SLOT_COLOURS)]) + [1.0]
        material_list.append({"name": material, "doubleSided": True, "pbrMetallicRoughness": pbr})
    document = {
        "asset": {"version": "2.0", "generator": "AssetGatekeeper fbx_preview (preview only)"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": name}],
        "meshes": [{"name": name, "primitives": mesh_primitives}],
        "materials": material_list,
        "buffers": [{"byteLength": len(buffer)}],
        "bufferViews": views,
        "accessors": accessors,
    }
    if images:
        document["images"] = [{"uri": "/".join(quote(part) for part in image.split("/"))}
                              for image in images]
        document["samplers"] = [{"wrapS": 10497, "wrapT": 10497}]
        document["textures"] = [{"source": i, "sampler": 0} for i in range(len(images))]
    json_blob = _pad(json.dumps(document, separators=(",", ":")).encode("utf-8"), b" ")
    bin_blob = bytes(buffer)
    total = 12 + 8 + len(json_blob) + 8 + len(bin_blob)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(struct.pack("<III", 0x46546C67, 2, total))
        handle.write(struct.pack("<II", len(json_blob), 0x4E4F534A) + json_blob)
        handle.write(struct.pack("<II", len(bin_blob), 0x004E4942) + bin_blob)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: fbx_preview.py model.fbx out.glb", file=sys.stderr)
        return 2
    try:
        info = convert(Path(args[0]), Path(args[1]))
    except FbxPreviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{args[1]}: {info['triangles']:,} triangles, {len(info['materials'])} material slot(s), "
          f"{info['axes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
