"""Generate throwaway assets to exercise asset_gatekeeper.py."""
from __future__ import annotations   # X | None annotations on Python 3.9

import base64
import json
import struct
from pathlib import Path

import numpy as np
from PIL import Image

OUT = Path("dropbox")
OUT.mkdir(exist_ok=True)

JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942


# --------------------------------------------------------------------------- #
# OBJ
# --------------------------------------------------------------------------- #

def obj_with_materials(path: Path, triangles: int, material_names: list[str]) -> None:
    """Valid OBJ + MTL: triangles are spread across the given material slots."""
    if material_names:
        mtl = path.with_suffix(".mtl")
        mtl.write_text(
            "".join(f"newmtl {name}\nKd 0.8 0.8 0.8\nKs 0 0 0\nillum 1\n\n" for name in material_names),
            encoding="utf-8",
        )
        header = f"mtllib {mtl.name}\n"
    else:
        header = ""

    verts = []
    body = []
    for material_index, name in enumerate(material_names or [None]):
        if name:
            body.append(f"usemtl {name}\n")
        share = triangles // max(1, len(material_names))
        for triangle in range(share):
            base = len(verts) + 1
            verts.extend([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
            body.append(f"f {base} {base + 1} {base + 2}\n")

    with path.open("w", encoding="utf-8") as fh:
        fh.write(header)
        for x, y, z in verts:
            fh.write(f"v {x} {y} {z}\n")
        fh.writelines(body)


# --------------------------------------------------------------------------- #
# glTF / GLB
# --------------------------------------------------------------------------- #

def _document(triangles_per_primitive: int, material_count: int) -> tuple[dict, bytes]:
    rng = np.random.default_rng(0)
    positions = rng.random((triangles_per_primitive * 3, 3), dtype=np.float32)
    indices = np.arange(triangles_per_primitive * 3, dtype=np.uint32)
    pos_bytes, idx_bytes = positions.tobytes(), indices.tobytes()

    primitives = []
    for material in range(material_count):
        primitive = {"attributes": {"POSITION": 0}, "indices": 1}
        if material_count:
            primitive["material"] = material
        primitives.append(primitive)

    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": primitives}],
        "materials": [{"name": f"mat_{i}"} for i in range(material_count)],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": triangles_per_primitive * 3, "type": "VEC3",
             "min": positions.min(axis=0).tolist(), "max": positions.max(axis=0).tolist()},
            {"bufferView": 1, "componentType": 5125, "count": triangles_per_primitive * 3, "type": "SCALAR"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bytes)},
            {"buffer": 0, "byteOffset": len(pos_bytes), "byteLength": len(idx_bytes)},
        ],
    }
    return document, pos_bytes + idx_bytes


def write_gltf(path: Path, triangles_per_primitive: int, material_count: int) -> None:
    document, payload = _document(triangles_per_primitive, material_count)
    document["buffers"] = [{
        "byteLength": len(payload),
        "uri": "data:application/octet-stream;base64," + base64.b64encode(payload).decode(),
    }]
    path.write_text(json.dumps(document), encoding="utf-8")


def write_glb(path: Path, triangles_per_primitive: int, material_count: int) -> None:
    document, payload = _document(triangles_per_primitive, material_count)
    document["buffers"] = [{"byteLength": len(payload)}]

    json_bytes = json.dumps(document).encode("utf-8")
    json_bytes += b" " * ((4 - len(json_bytes) % 4) % 4)  # spec: pad JSON with spaces
    bin_bytes = payload + b"\x00" * ((4 - len(payload) % 4) % 4)

    total = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)
    blob = bytearray(b"glTF")
    blob += struct.pack("<II", 2, total)
    blob += struct.pack("<II", len(json_bytes), JSON_CHUNK) + json_bytes
    blob += struct.pack("<II", len(bin_bytes), BIN_CHUNK) + bin_bytes
    path.write_bytes(bytes(blob))


# --------------------------------------------------------------------------- #
# Build the corpus
# --------------------------------------------------------------------------- #

obj_with_materials(OUT / "crate_1mtl.obj", 12, ["crate"])
obj_with_materials(OUT / "crate_4mtl.obj", 4_000, ["crate", "planks", "metal", "glass"])
obj_with_materials(OUT / "crate_nomtl.obj", 12, [])

write_gltf(OUT / "lantern.gltf", 1_200, 1)          # 1,200 tris, 1 slot
write_gltf(OUT / "boss_statue.gltf", 18_125, 4)     # 72,500 tris, 4 slots
write_glb(OUT / "prop_crate.glb", 900, 2)           # 1,800 tris, 2 slots
write_glb(OUT / "heavy_prop.glb", 40_000, 2)        # 80,000 tris, 2 slots

def write_gltf_profiled(path: Path, triangles: int = 120, influences: int = 4,
                         uv_channels: int = 1, degenerate: bool = False) -> None:
    """glTF with skin weights across JOINTS_n sets and N UV channels.

    ``influences`` > 4 forces a second WEIGHTS_1/JOINTS_1 set, exactly like an
    artist who forgot to prune weights after a merge.
    """
    vertices = triangles * 3
    positions = np.random.default_rng(1).random((vertices, 3), dtype=np.float32)
    if degenerate:
        positions[0:3] = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]
    indices = np.arange(vertices, dtype=np.uint32)

    accessors: list[dict] = []
    views: list[dict] = []
    chunks: list[bytes] = []
    offset = 0

    def add(payload: bytes, component_type: int, count: int, type_str: str,
            bounds: dict | None = None) -> int:
        nonlocal offset
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(payload)})
        accessor = {"bufferView": len(views) - 1, "componentType": component_type,
                    "count": count, "type": type_str}
        if bounds:
            accessor.update(bounds)
        accessors.append(accessor)
        chunks.append(payload)
        offset += len(payload)
        return len(accessors) - 1

    attributes = {
        "POSITION": add(positions.tobytes(), 5126, vertices, "VEC3",
                        {"min": positions.min(axis=0).tolist(),
                         "max": positions.max(axis=0).tolist()})
    }
    attributes_indices = add(indices.tobytes(), 5125, vertices, "SCALAR")

    remaining, set_index = influences, 0
    while remaining > 0:
        per_set = min(4, remaining)
        weights = np.zeros((vertices, 4), dtype=np.float32)
        weights[:, :per_set] = 1.0 / per_set
        joints = np.zeros((vertices, 4), dtype=np.uint8)
        joints[:, :per_set] = np.arange(per_set)
        attributes[f"WEIGHTS_{set_index}"] = add(weights.tobytes(), 5126, vertices, "VEC4")
        attributes[f"JOINTS_{set_index}"] = add(joints.tobytes(), 5121, vertices, "VEC4")
        remaining -= per_set
        set_index += 1

    for channel in range(uv_channels):
        attributes[f"TEXCOORD_{channel}"] = add(
            np.zeros((vertices, 2), dtype=np.float32).tobytes(), 5126, vertices, "VEC2")

    payload = b"".join(chunks)
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": attributes, "indices": attributes_indices,
                                     "material": 0}]}],
        "materials": [{"name": "profiled"}],
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(payload),
                     "uri": "data:application/octet-stream;base64," +
                            base64.b64encode(payload).decode()}],
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def obj_with_degenerate(path: Path, normal_triangles: int = 12) -> None:
    """OBJ where exactly one triangle is collinear (zero area)."""
    lines = ["v 0 0 0", "v 1 0 0", "v 2 0 0", "f 1 2 3"]  # degenerate
    base = 4
    for _ in range(normal_triangles):
        lines += [f"v {base} 0 0", f"v {base} 1 0", f"v {base} 0 1",
                  f"f {base} {base + 1} {base + 2}"]
        base += 3
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def texture(name: str, size: tuple[int, int]) -> None:
    Image.new("RGB", size, (90, 120, 200)).save(OUT / name, format="PNG")

texture("bark_Albedo.png", (512, 512))        # PASS: named, POT, in budget
texture("bark_Normal_02.png", (512, 256))     # PASS: numbered variant
texture("bark_Roughness.png", (256, 256))     # PASS: lowercase-safe suffix
texture("atlas_max.png", (2048, 2048))        # FAIL: naming only
texture("bark_diffuse.png", (512, 512))       # FAIL: naming only
texture("ui_icon.png", (300, 300))            # FAIL: naming + not power of two
texture("hull_Albedo.png", (4096, 1024))      # FAIL: resolution only (both edges POT)

# Deep profiling fixtures
write_gltf_profiled(OUT / "rig_ok.gltf", influences=4, uv_channels=2)
write_gltf_profiled(OUT / "rig_heavy.gltf", influences=6, uv_channels=2)   # FAIL: 6 influences
write_gltf_profiled(OUT / "uv_heavy.gltf", influences=1, uv_channels=5)    # FAIL: 5 UV channels
write_gltf_profiled(OUT / "shattered.gltf", influences=1, uv_channels=1, degenerate=True)
obj_with_degenerate(OUT / "broken_glass.obj", normal_triangles=6)          # FAIL: 1 degenerate

# Real-world noise: these must be ignored, not crashed on.
(OUT / "export.tmp").write_bytes(b"partial")
(OUT / "notes.txt").write_text("ignore me", encoding="utf-8")
Image.new("RGB", (64, 64)).save(OUT / "fake_Albedo.png", format="JPEG")

print("generated:", len(list(OUT.iterdir())), "files")
