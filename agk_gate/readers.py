"""AssetGatekeeper, part 3 of 7: reading assets: glTF, OBJ, textures, the FBX parser, skin weights, UV sets, skeleton and lightmap facts.

Split out of asset_gatekeeper.py on 11 Nov 2026 with no change in behaviour;
asset_gatekeeper.py still exposes every name. See agk_gate/__init__.py.
"""

from __future__ import annotations

import array
import base64
import importlib.util
import json
import math
import re
import struct
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import unquote

from agk_gate.common import (
    GLB_JSON_CHUNK, GLB_MAGIC, GLTF_SUFFIXES, HEADER_ONLY_TEXTURES, Image, LOG, np,
    TEXTURE_FORMATS, trimesh,
)
from agk_gate.config import (
    AssetError, LIGHTMAP_UV_TOLERANCE,
)


# --- #
# Analyzers - one concern each, no logging, no watching.
# --- #

def _describe_unexpected(exc: BaseException) -> str:
    """``TypeName: message``, or just ``TypeName`` when the message is empty.

    An empty message once surfaced as ``could not read mesh (AssertionError: )``,
    which tells an artist nothing. The type alone at least reads as a word."""
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _mesh_read_error(exc: BaseException) -> str:
    """The FAILED reason for a mesh trimesh could not load, with the fix if there is one."""
    if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") == "charset_normalizer":
        return ("could not read mesh: it is not UTF-8 text, and reading it needs "
                "charset-normalizer (python -m pip install charset-normalizer)")
    if isinstance(exc, (IndexError, AssertionError)):
        return ("could not read mesh: it is damaged (an index points past the end of its "
                f"data - {_describe_unexpected(exc)}); re-export it")
    return f"could not read mesh ({_describe_unexpected(exc)})"


def obj_colour_source(path: Path) -> str:
    """How an .obj carries colour without a texture: 'polypaint', 'vertex colours' or ''.

    ZBrush writes Polypaint as ``#MRGB`` comment lines, which Houdini and Blender
    read and Maya, 3ds Max and three.js skip, so a sculpt looked blue in one and
    grey in the next (9 Nov 2026). Other exporters put r g b on each ``v`` line.
    Read in chunks: a sculpt can be 90 MB.
    """
    try:
        with open(path, "rb") as handle:
            carry = b"\n"
            first_vertex_checked = False
            while True:
                chunk = handle.read(16 * 1024 * 1024)
                if not chunk:
                    return ""
                data = carry + chunk
                if b"\n#MRGB " in data:
                    return "polypaint"
                if not first_vertex_checked:
                    start = data.find(b"\nv ")
                    end = data.find(b"\n", start + 1) if start >= 0 else -1
                    if end > 0:
                        first_vertex_checked = True
                        if len(data[start + 1:end].split()) >= 7:
                            return "vertex colours"
                carry = data[-8:]
    except OSError:
        return ""


def load_trimesh_scene(source, **kwargs) -> object:
    """trimesh's scene loader, without the call trimesh marks as a compatibility
    wrapper: ``trimesh.load(force='scene')`` (9 Nov 2026 audit, B7)."""
    loader = getattr(trimesh, "load_scene", None)
    if loader is not None:
        return loader(source, **kwargs)
    return trimesh.load(source, force="scene", **kwargs)    # trimesh before load_scene


@lru_cache(maxsize=8)
def _load_scene_cached(path_string: str, modified_ns: int) -> object:
    """One load per (path, mtime). The mtime keeps the cache honest on re-export."""
    return load_trimesh_scene(path_string, process=False)


def load_scene(path: Path) -> object:
    """Load a mesh once and reuse it across the triangle/material/geometry checks."""
    if trimesh is None:
        raise AssetError("trimesh is not installed; mesh data cannot be analysed")
    try:
        return _load_scene_cached(str(path), path.stat().st_mtime_ns)
    except Exception as exc:
        raise AssetError(_mesh_read_error(exc)) from exc


def _geometries(scene: object) -> list:
    if trimesh is not None and isinstance(scene, trimesh.Scene):
        return list(scene.geometry.values())
    return [scene]


def _collision_geometry_names(scene: object) -> set:
    """Geometry of a loaded glTF scene that only UCX_/UBX_/USP_/UCP_ nodes place."""
    if trimesh is None or not isinstance(scene, trimesh.Scene):
        return set()
    graph = getattr(scene, "graph", None)
    users: dict = {}
    try:
        for node_name in graph.nodes_geometry:
            geometry_name = graph[node_name][1]
            users.setdefault(geometry_name, []).append(str(node_name))
    except Exception:                    # an unusual graph: count everything, as before
        return set()

    def collision(name):
        return _bare_name(str(name)).lower().startswith(FBX_COLLISION_PREFIXES)

    return {geometry for geometry, nodes in users.items()
            if all(collision(node) or collision(geometry) for node in nodes)}


def render_scene(path: Path, scene: object) -> object:
    """The scene as rendered: a glTF's collision shapes removed (as an FBX's
    are), so they add nothing to degenerate, memory or texel-density counts."""
    if path.suffix.lower() not in GLTF_SUFFIXES:
        return scene
    hulls = _collision_geometry_names(scene)
    if not hulls:
        return scene
    trimmed = scene.copy()
    trimmed.delete_geometry(hulls)
    return trimmed


# --- #
# FBX (binary, 7.1+): a small read-only parser, standard library + numpy only
# --- #
#
# Why not an SDK: Autodesk's FBX SDK is a native install per platform and per
# Python version, which is exactly what a gate that has to run on every artist
# machine and every CI agent cannot depend on. The binary format is a plain tree
# of records with typed (optionally zlib-compressed) properties; the handful of
# facts the gate needs - polygon indices, vertex positions, UV layers, material
# and skin connections, texture paths - are all directly in it.
#
# ASCII FBX (7.1+) is read into the same record tree as binary, so every
# measurement below is shared. What is refused rather than guessed: files older
# than 7.1 (FBX 2011), whose geometry layout is different; they get a message
# that says how to re-export.
#
# Hostile input is bounded: record offsets are checked against the file size,
# nesting depth is capped, and every array is decompressed with an explicit
# maximum output size, so a 1 KB file cannot ask for gigabytes.

FBX_SUFFIXES = frozenset({".fbx"})
_FBX_MAGIC = b"Kaydara FBX Binary  \x00"
FBX_MAX_DEPTH = 64
FBX_MAX_ARRAY_BYTES = 512 * 1024 * 1024      # one array
FBX_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # all arrays in one file
# 'c' is a byte array some exporters write where others write 'b' (assimp's
# boxWithCompressedCTypeArray.FBX); it used to fail as "unknown property type 99".
_FBX_ARRAY_TYPES = {ord("f"): ("f", 4), ord("d"): ("d", 8), ord("l"): ("q", 8),
                    ord("i"): ("i", 4), ord("b"): ("b", 1), ord("c"): ("b", 1)}


class _FbxNode:
    __slots__ = ("name", "props", "children")

    def __init__(self, name: str, props: list, children: list) -> None:
        self.name = name
        self.props = props
        self.children = children

    def child(self, name: str) -> "Optional[_FbxNode]":
        for node in self.children:
            if node.name == name:
                return node
        return None

    def all(self, name: str) -> "list[_FbxNode]":
        return [node for node in self.children if node.name == name]


def _fbx_array(raw: bytes, code: str):
    if np is not None:
        return np.frombuffer(raw, dtype="<" + {"f": "f4", "d": "f8", "q": "i8",
                                               "i": "i4", "b": "i1"}[code])
    values = array.array(code)
    values.frombytes(raw)
    if sys.byteorder != "little":                     # pragma: no cover
        values.byteswap()
    return values


def parse_fbx_binary(data: bytes) -> "list[_FbxNode]":
    """The top-level records of a binary FBX file. Raises AssetError when malformed."""
    import zlib

    if not data.startswith(_FBX_MAGIC):
        if _looks_like_ascii_fbx(data):
            return parse_fbx_ascii(data)
        raise AssetError("not an FBX file (missing the binary FBX header)")
    if len(data) < 27:
        raise AssetError("truncated FBX header")
    version = struct.unpack_from("<I", data, 23)[0]
    if version < 7100:
        raise AssetError(f"FBX version {version} is too old to read (7.1 / FBX 2011 or "
                         f"newer is needed); re-export with a current exporter")
    wide = version >= 7500
    header = struct.Struct("<QQQB" if wide else "<IIIB")
    size = len(data)
    budget = [FBX_MAX_TOTAL_BYTES]

    def read_property(pos: int):
        if pos >= size:
            raise AssetError("truncated FBX property")
        kind = data[pos]
        pos += 1
        try:
            if kind == ord("Y"):
                return struct.unpack_from("<h", data, pos)[0], pos + 2
            if kind == ord("C"):
                return bool(data[pos]), pos + 1
            if kind == ord("I"):
                return struct.unpack_from("<i", data, pos)[0], pos + 4
            if kind == ord("F"):
                return struct.unpack_from("<f", data, pos)[0], pos + 4
            if kind == ord("D"):
                return struct.unpack_from("<d", data, pos)[0], pos + 8
            if kind == ord("L"):
                return struct.unpack_from("<q", data, pos)[0], pos + 8
            if kind in (ord("S"), ord("R")):
                length = struct.unpack_from("<I", data, pos)[0]
                pos += 4
                if pos + length > size:
                    raise AssetError("truncated FBX string")
                raw = data[pos:pos + length]
                return (raw.decode("utf-8", "replace") if kind == ord("S") else raw), pos + length
            if kind in _FBX_ARRAY_TYPES:
                code, width = _FBX_ARRAY_TYPES[kind]
                count, encoding, stored = struct.unpack_from("<III", data, pos)
                pos += 12
                expected = count * width
                if expected > FBX_MAX_ARRAY_BYTES or expected > budget[0]:
                    raise AssetError(f"FBX array of {count:,} values is too large to inspect safely")
                if pos + stored > size:
                    raise AssetError("truncated FBX array")
                raw = data[pos:pos + stored]
                if encoding == 1:
                    inflater = zlib.decompressobj()
                    raw = inflater.decompress(raw, expected)
                elif encoding != 0:
                    raise AssetError(f"unknown FBX array encoding {encoding}")
                if len(raw) != expected:
                    raise AssetError("corrupt FBX array (wrong decoded length)")
                budget[0] -= expected
                return _fbx_array(raw, code), pos + stored
        except struct.error as exc:
            raise AssetError(f"truncated FBX property ({exc})") from exc
        except zlib.error as exc:
            raise AssetError(f"corrupt compressed FBX array ({exc})") from exc
        raise AssetError(f"corrupt FBX (unknown property type {kind!r})")

    def read_node(pos: int, depth: int):
        if depth > FBX_MAX_DEPTH:
            raise AssetError("FBX nesting is too deep to be a real export")
        if pos + header.size > size:
            raise AssetError("truncated FBX record")
        end, count, _length, name_length = header.unpack_from(data, pos)
        if end == 0:
            return None, pos + header.size              # end-of-list sentinel
        if end <= pos or end > size:
            raise AssetError("corrupt FBX (record points outside the file)")
        pos += header.size
        name = data[pos:pos + name_length].decode("ascii", "replace")
        pos += name_length
        props = []
        for _ in range(count):
            value, pos = read_property(pos)
            props.append(value)
        children = []
        while pos < end:
            node, pos = read_node(pos, depth + 1)
            if node is None:
                break
            children.append(node)
        return _FbxNode(name, props, children), end

    nodes: list[_FbxNode] = []
    pos = 27
    while pos + header.size <= size:
        node, pos = read_node(pos, 0)
        if node is None:
            break
        nodes.append(node)
    return nodes


def _looks_like_ascii_fbx(data: bytes) -> bool:
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith(b";") or head.startswith(b"FBXHeaderExtension")


# One token of ASCII FBX: a key ("Vertices:"), a string, a number, an array
# length ("*24"), a brace, a comma, a newline, or a bare word (T, Y, W, ...).
_FBX_ASCII_TOKEN = re.compile(
    rb"""(?P<key>[A-Za-z_][A-Za-z0-9_|]*)[ \t]*:(?!:)"""
    # No backslash escapes in ASCII FBX (a quote is written &quot;): Maya writes
    # paths such as "D:\\" whose final backslash once swallowed the closing quote.
    rb"""|(?P<string>"[^"]*")"""
    rb"""|(?P<count>\*[0-9]+)"""
    rb"""|(?P<number>[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)"""
    rb"""|(?P<open>\{)|(?P<close>\})|(?P<comma>,)|(?P<newline>\n)"""
    rb"""|(?P<comment>;[^\n]*)|(?P<space>[ \t\r]+)"""
    rb"""|(?P<word>[A-Za-z_][A-Za-z0-9_]*)""")


def parse_fbx_ascii(data: bytes) -> "list[_FbxNode]":
    """The top-level records of an ASCII FBX file, as the binary parser returns them.

    Arrays (``Vertices: *24 { a: ... }``) become the record's first property, as
    in binary; their numbers are parsed in one pass (a 240k-triangle file has
    millions of them). Same bounds as binary: depth, per-array and total size.
    """
    import base64

    if np is None:                                       # pragma: no cover
        raise AssetError("numpy is not installed; FBX cannot be analysed")
    size = len(data)
    budget = [FBX_MAX_TOTAL_BYTES]
    position = [0]

    def token():
        while position[0] < size:
            match = _FBX_ASCII_TOKEN.match(data, position[0])
            if match is None:
                snippet = data[position[0]:position[0] + 20]
                raise AssetError(f"corrupt ASCII FBX near byte {position[0]:,} ({snippet!r})")
            position[0] = match.end()
            kind = match.lastgroup
            if kind in ("space", "comment"):
                continue
            return kind, match.group(kind)
        return "end", b""

    def peek():
        saved = position[0]
        result = token()
        position[0] = saved
        return result

    def value(kind, raw):
        if kind == "string":
            return raw[1:-1].decode("utf-8", "replace").replace("&quot;", '"')
        if kind == "number":
            text = raw.decode("ascii")
            if any(mark in text for mark in ".eE"):
                return float(text)
            return int(text)
        return raw.decode("ascii", "replace")          # bare word: T, Y, W, ...

    def read_array(count: int):
        # "{" "a:" numbers "}" - the numbers are read in one go.
        kind, _ = token()
        if kind != "open":
            raise AssetError("corrupt ASCII FBX array (missing '{')")
        kind, raw = token()
        while kind == "newline":
            kind, raw = token()
        if kind == "close":
            return np.zeros(0)
        if kind != "key" or raw.split(b":")[0].strip() != b"a":
            raise AssetError("corrupt ASCII FBX array (missing 'a:')")
        end = data.find(b"}", position[0])
        if end < 0:
            raise AssetError("truncated ASCII FBX array")
        text = data[position[0]:end]
        position[0] = end + 1
        width = 8
        if count * width > FBX_MAX_ARRAY_BYTES or count * width > budget[0]:
            raise AssetError(f"FBX array of {count:,} values is too large to inspect safely")
        budget[0] -= count * width
        integral = not any(mark in text for mark in (b".", b"e", b"E"))
        try:
            values = np.array(text.replace(b"\n", b"").replace(b"\r", b"").split(b","),
                              dtype=np.int64 if integral else np.float64) \
                if text.strip() else np.zeros(0, dtype=np.int64)
        except ValueError as exc:
            raise AssetError(f"corrupt ASCII FBX array ({exc})") from exc
        if values.size != count:
            raise AssetError(f"corrupt ASCII FBX array ({values.size:,} values, header says {count:,})")
        return values

    def read_node(name: str, depth: int) -> _FbxNode:
        if depth > FBX_MAX_DEPTH:
            raise AssetError("FBX nesting is too deep to be a real export")
        props: list = []
        children: list = []
        expect_value = True
        while True:
            kind, raw = peek()
            if kind == "count" and expect_value:
                token()
                props.append(read_array(int(raw[1:])))
                break
            if kind in ("string", "number", "word") and expect_value:
                token()
                props.append(value(kind, raw))
                expect_value = False
                continue
            if kind == "comma":
                token()
                expect_value = True
                while peek()[0] == "newline":           # a list may continue on the next line
                    token()
                continue
            break
        if peek()[0] == "open":
            token()
            while True:
                kind, raw = token()
                if kind == "close":
                    break
                if kind == "end":
                    raise AssetError("truncated ASCII FBX (unclosed '{')")
                if kind in ("newline", "comma"):
                    continue
                if kind != "key":
                    raise AssetError(f"corrupt ASCII FBX (unexpected {raw[:20]!r})")
                children.append(read_node(raw.split(b":")[0].strip().decode("ascii"), depth + 1))
        if name == "Content" and props and all(isinstance(item, str) for item in props):
            # Embedded media: base64 text in ASCII, raw bytes in binary.
            joined = "".join(props)
            try:
                props = [base64.b64decode(joined)] if joined else [b""]
            except ValueError:
                props = [joined.encode("utf-8", "replace")]
        return _FbxNode(name, props, children)

    nodes: list = []
    while True:
        kind, raw = token()
        if kind == "end":
            break
        if kind in ("newline", "comma"):
            continue
        if kind != "key":
            raise AssetError(f"corrupt ASCII FBX (unexpected {raw[:20]!r} at top level)")
        nodes.append(read_node(raw.split(b":")[0].strip().decode("ascii"), 0))

    header = next((node for node in nodes if node.name == "FBXHeaderExtension"), None)
    version_node = header.child("FBXVersion") if header is not None else None
    version = version_node.props[0] if version_node is not None and version_node.props else None
    if not isinstance(version, int):
        raise AssetError("ASCII FBX without a version (FBXHeaderExtension/FBXVersion); "
                         "re-export it")
    if version < 7100:
        raise AssetError(f"FBX version {version} is too old to read (7.1 / FBX 2011 or "
                         f"newer is needed); re-export with a current exporter")
    return nodes


@dataclass(frozen=True)
class FbxSummary:
    """The facts the gate reads from an FBX file, computed once per (path, mtime)."""

    triangles: int
    material_slots: int
    uv_channels: int
    max_influences: int
    degenerate: int
    #: (as written, candidate paths in preference order) per external texture
    references: tuple
    #: UCX_/UBX_/USP_/UCP_ meshes: Unreal's collision convention, not rendered
    collision_meshes: int = 0
    #: meshes below LOD0 in an LOD group: not part of the LOD0 budget
    lower_lod_meshes: int = 0
    #: (name, width, height) of each texture packed inside the FBX
    embedded_textures: tuple = ()
    #: LOD chains: (name, (LOD0 triangles, LOD1 triangles, ...), "group" | "names")
    lod_groups: tuple = ()
    #: names of the LOD0 render meshes, which a collision mesh must be named after
    render_names: tuple = ()
    #: (name, distinct vertices, concavity or None, flat) per collision mesh
    collision_shapes: tuple = ()
    #: a rendered mesh is skinned (it uses a physics asset, not UCX_ collision)
    skinned: bool = False


def _fbx_id(node: _FbxNode) -> Optional[int]:
    return node.props[0] if node.props and isinstance(node.props[0], int) else None


def _fbx_class(node: _FbxNode) -> str:
    return node.props[2] if len(node.props) > 2 and isinstance(node.props[2], str) else ""


def _fbx_string(node: Optional[_FbxNode]) -> str:
    if node is None or not node.props or not isinstance(node.props[0], str):
        return ""
    return node.props[0]


#: Unreal's collision-mesh prefixes (UCX convex, UBX box, USP sphere, UCP capsule).
#: Engines import these as collision, never as render geometry.
FBX_COLLISION_PREFIXES = ("ucx_", "ubx_", "usp_", "ucp_")


def _fbx_object_name(node: _FbxNode) -> str:
    """The object's own name: binary writes 'Name\\x00\\x01Class', ASCII 'Class::Name'."""
    raw = node.props[1] if len(node.props) > 1 and isinstance(node.props[1], str) else ""
    if "\x00\x01" in raw:
        return raw.split("\x00\x01", 1)[0]
    if "::" in raw:
        return raw.split("::", 1)[1]
    return raw


def _fbx_polygons(indices):
    """(corner vertex indices, polygon start offsets, polygon sizes) from PolygonVertexIndex.

    FBX ends each polygon by storing its last index as ``-(index + 1)``.
    """
    raw = np.asarray(indices, dtype=np.int64)
    if raw.size == 0:
        return raw, raw, raw
    ends = np.flatnonzero(raw < 0)
    if ends.size == 0 or ends[-1] != raw.size - 1:
        raise AssetError("corrupt FBX polygon list (last polygon is not terminated)")
    corners = np.where(raw < 0, -raw - 1, raw)
    starts = np.concatenate(([0], ends[:-1] + 1))
    return corners, starts, ends - starts + 1


@lru_cache(maxsize=8)
def _read_fbx_cached(path_string: str, modified_ns: int, epsilon: float,
                     ratio: float = 1e-6) -> FbxSummary:
    if np is None:                                       # pragma: no cover
        raise AssetError("numpy is not installed; FBX cannot be analysed")
    try:
        # One parse per file and modification time, shared with the skeleton,
        # lightmap and preview/measure readers (_fbx_tree).
        top = _fbx_tree_cached(path_string, modified_ns)
    except OSError as exc:
        raise AssetError(f"could not read FBX ({exc})") from exc
    objects = top.get("Objects")
    if objects is None:
        return FbxSummary(0, 0, 0, 0, 0, ())

    geometries: dict = {}
    models: set = set()
    model_names: dict = {}
    lod_groups: list = []
    materials: set = set()
    textures: dict = {}
    videos: dict = {}
    skins: set = set()
    clusters: dict = {}
    for node in objects.children:
        node_id = _fbx_id(node)
        kind = _fbx_class(node)
        if node.name == "Geometry" and kind == "Mesh":
            geometries[node_id] = node
        elif node.name == "Model":
            models.add(node_id)
            model_names[node_id] = _fbx_object_name(node)
            if kind == "LodGroup":
                lod_groups.append(node_id)
        elif node.name == "Material":
            materials.add(node_id)
        elif node.name == "Texture":
            textures[node_id] = node
        elif node.name == "Video":
            videos[node_id] = node
        elif node.name == "Deformer" and kind == "Skin":
            skins.add(node_id)
        elif node.name == "Deformer" and kind == "Cluster":
            clusters[node_id] = node

    # Connections: "OO" child -> parent object, "OP" child -> parent property.
    parents: dict = {}
    children_of: dict = {}
    ordered_children: dict = {}
    connections = top.get("Connections")
    for link in (connections.all("C") if connections else []):
        if len(link.props) < 3:
            continue
        child, parent = link.props[1], link.props[2]
        parents.setdefault(child, set()).add(parent)
        children_of.setdefault(parent, set()).add(child)
        ordered_children.setdefault(parent, []).append(child)

    # What an engine imports as LOD0 render geometry: not collision meshes, and
    # not the LOD1+ children of an LOD group (exporters write them in LOD order).
    def descendants(start) -> set:
        found, stack = set(), [start]
        while stack:
            for item in children_of.get(stack.pop(), ()):
                if item not in found:
                    found.add(item)
                    stack.append(item)
        return found

    collision_models = {model for model, name in model_names.items()
                        if name.lower().startswith(FBX_COLLISION_PREFIXES)}
    lower_lod: set = set()
    group_levels: dict = {}
    for group in lod_groups:
        # Unreal parents the UCX_ hull to the LOD group too; it is not a level.
        levels = [item for item in ordered_children.get(group, [])
                  if item in models and item not in collision_models]
        group_levels[group] = levels
        for level in levels[1:]:
            lower_lod.add(level)
            lower_lod |= descendants(level)
    collision_meshes = lower_lod_meshes = 0
    rendered: dict = {}
    for geometry_id, geometry in geometries.items():
        owners = parents.get(geometry_id, set()) & models
        if owners and owners <= collision_models:
            collision_meshes += 1
        elif owners and owners <= lower_lod:
            lower_lod_meshes += 1
        else:
            rendered[geometry_id] = geometry

    triangles = degenerate = uv_channels = max_influences = 0
    triangles_of: dict = {}
    for geometry_id, geometry in rendered.items():
        index_node = geometry.child("PolygonVertexIndex")
        vertex_node = geometry.child("Vertices")
        if index_node is None or not index_node.props:
            continue
        corners, starts, sizes = _fbx_polygons(index_node.props[0])
        fan = np.clip(sizes - 2, 0, None)
        count = int(fan.sum())
        triangles_of[geometry_id] = count
        triangles += count
        uv_channels = max(uv_channels, len(geometry.all("LayerElementUV")))
        if count and vertex_node is not None and vertex_node.props:
            positions = np.asarray(vertex_node.props[0], dtype=np.float64)
            if positions.size % 3:
                raise AssetError("corrupt FBX vertex array (not a multiple of 3)")
            positions = positions.reshape(-1, 3)
            if corners.size and int(corners.max()) >= len(positions):
                raise AssetError("corrupt FBX polygon list (index past the last vertex)")
            # Fan triangulation, as engines do on import: (c0, ck, ck+1).
            owner = np.repeat(np.arange(len(sizes)), fan)
            first = np.repeat(np.cumsum(fan) - fan, fan)
            step = np.arange(count) - first + 1
            a = corners[starts[owner]]
            b = corners[starts[owner] + step]
            c = corners[starts[owner] + step + 1]
            degenerate += int(np.count_nonzero(degenerate_mask(
                positions[a], positions[b], positions[c], ratio, epsilon)))
        # Skin: Skin deformer -> this geometry; clusters -> that skin.
        for skin_id in children_of.get(geometry_id, ()):
            if skin_id not in skins:
                continue
            per_vertex: dict = {}
            for cluster_id in children_of.get(skin_id, ()):
                cluster = clusters.get(cluster_id)
                if cluster is None:
                    continue
                indexes, weights = cluster.child("Indexes"), cluster.child("Weights")
                if indexes is None or weights is None:
                    continue
                for index, weight in zip(np.asarray(indexes.props[0]).tolist(),
                                         np.asarray(weights.props[0]).tolist()):
                    if weight > 1e-4:
                        per_vertex[index] = per_vertex.get(index, 0) + 1
            if per_vertex:
                max_influences = max(max_influences, max(per_vertex.values()))

    # Materials that a mesh-bearing model actually uses.
    mesh_models = {parent for geometry_id in rendered
                   for parent in parents.get(geometry_id, ()) if parent in models}
    used = {material for material in materials
            if parents.get(material, set()) & mesh_models}
    slots = len(used) if used else (1 if triangles else 0)
    lods, render_names, shapes, skinned = _fbx_structure(
        geometries, models, model_names, parents, children_of, descendants, collision_models,
        group_levels, rendered, mesh_models, triangles_of, skins)

    references = []
    packed: dict = {}
    for texture_id, texture in textures.items():
        relative = _fbx_string(texture.child("RelativeFilename"))
        absolute = _fbx_string(texture.child("FileName"))
        embedded = False
        for video_id in children_of.get(texture_id, ()):
            video = videos.get(video_id)
            content = video.child("Content") if video is not None else None
            if content is not None and content.props and isinstance(content.props[0], bytes) \
                    and len(content.props[0]) > 0:
                embedded = True
                size = image_size_from_bytes(content.props[0])
                if size is not None and video_id not in packed:
                    label = (relative or absolute).replace("\\", "/").rsplit("/", 1)[-1] \
                        or f"texture {len(packed) + 1}"
                    packed[video_id] = (label, size[0], size[1])
        if embedded or not (relative or absolute):
            continue
        written = relative or absolute
        references.append((written, tuple(item for item in (relative, absolute) if item)))
    return FbxSummary(triangles, slots, uv_channels, max_influences, degenerate,
                      tuple(references), collision_meshes, lower_lod_meshes,
                      tuple(packed.values()), lods, render_names, shapes, skinned)


def _fbx_structure(geometries, models, model_names, parents, children_of, descendants,
                   collision_models, group_levels, rendered, mesh_models, triangles_of, skins):
    """LOD chains, render-mesh names, collision shapes and skinning of one FBX.

    LOD chains come from LOD groups (what Unreal and Unity import); without one,
    from _LOD0/_LOD1 model names (Unity builds an LOD group from those).
    """
    def geometry_triangles(geometry_id) -> int:
        if geometry_id not in triangles_of:
            index_node = geometries[geometry_id].child("PolygonVertexIndex")
            count = 0
            if index_node is not None and index_node.props:
                _corners, _starts, sizes = _fbx_polygons(index_node.props[0])
                count = int(np.clip(sizes - 2, 0, None).sum())
            triangles_of[geometry_id] = count
        return triangles_of[geometry_id]

    owned: dict = {}
    for geometry_id in geometries:
        for owner in parents.get(geometry_id, set()) & models:
            owned.setdefault(owner, []).append(geometry_id)

    def subtree_triangles(model) -> int:
        members = ({model} | (descendants(model) & models)) - collision_models
        found = {item for member in members for item in owned.get(member, ())}
        return sum(geometry_triangles(item) for item in found)

    lods = [(_bare_name(model_names.get(group, "")), tuple(subtree_triangles(level) for level in levels),
             "group") for group, levels in group_levels.items() if levels]
    if not lods:
        named = [(model_names.get(model, ""), sum(geometry_triangles(item) for item in owned[model]))
                 for model in owned if model not in collision_models]
        lods = [(base, levels, "names") for base, levels in lod_chains_by_name(named)]
    render_names = tuple(sorted({_bare_name(model_names.get(model, "")) for model in mesh_models}
                                - {""}))
    shapes = []
    for model in sorted(collision_models, key=lambda item: model_names.get(item, "")):
        points, triangles, offset = [], [], 0
        for geometry_id in owned.get(model, ()):
            geometry = geometries[geometry_id]
            vertex_node, index_node = geometry.child("Vertices"), geometry.child("PolygonVertexIndex")
            if vertex_node is None or not vertex_node.props:
                continue
            positions = np.asarray(vertex_node.props[0], dtype=np.float64)
            if positions.size % 3:
                raise AssetError("corrupt FBX vertex array (not a multiple of 3)")
            positions = positions.reshape(-1, 3)
            if index_node is not None and index_node.props:
                corners, starts, sizes = _fbx_polygons(index_node.props[0])
                fan = np.clip(sizes - 2, 0, None)
                owner = np.repeat(np.arange(len(sizes)), fan)
                first = np.repeat(np.cumsum(fan) - fan, fan)
                step = np.arange(int(fan.sum())) - first + 1
                triangles.append(np.stack([corners[starts[owner]], corners[starts[owner] + step],
                                           corners[starts[owner] + step + 1]], axis=1) + offset)
            points.append(positions)
            offset += len(positions)
        if not points:
            continue                          # a UCX_ null with no mesh: nothing to import
        shapes.append((_bare_name(model_names.get(model, "")),
                       *hull_shape(np.concatenate(points),
                                   np.concatenate(triangles) if triangles else np.zeros((0, 3), int))))
    skinned = any(item in skins for geometry_id in rendered
                  for item in children_of.get(geometry_id, ()))
    return tuple(lods), render_names, tuple(shapes), skinned


def read_fbx(path: Path, epsilon: float = 0.0, ratio: float = 1e-6) -> FbxSummary:
    try:
        modified = path.stat().st_mtime_ns
    except OSError as exc:
        raise AssetError(f"could not read FBX ({exc})") from exc
    try:
        return _read_fbx_cached(str(path), modified, float(epsilon), float(ratio))
    except AssetError:
        raise
    except (ValueError, IndexError, TypeError, MemoryError) as exc:
        raise AssetError(f"could not read FBX ({type(exc).__name__}: {exc})") from exc


def _fbx_reference_target(path: Path, candidates: tuple) -> Path:
    """Where an engine will look for an FBX texture: relative path, absolute path,
    then the bare filename next to the FBX (Unreal and Unity both fall back to it)."""
    base = path.parent
    options = []
    for candidate in candidates:
        normalised = candidate.replace("\\", "/")
        options.append(base / normalised)
        options.append(base / normalised.rsplit("/", 1)[-1])
    for option in options:
        try:
            if option.is_file():
                return option
        except OSError:
            continue
    return options[0] if options else base


def _count_triangles_trimesh(path: Path) -> int:
    """Triangle count via trimesh. Handles single meshes and multi-mesh scenes."""
    total = 0
    for geometry in _geometries(load_scene(path)):
        faces = getattr(geometry, "faces", None)
        if faces is not None:
            total += len(faces)
    return total


def read_gltf_document(path: Path) -> dict:
    """Return the glTF JSON document for a .gltf (text) or .glb (binary) file."""
    try:
        if path.suffix.lower() == ".gltf":
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        else:
            document = _glb_json(_glb_json_prefix(path))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Corrupt text used to escape as an uncaught UnicodeDecodeError (6 Nov
        # fuzzing) and stop a folder scan; it is a damaged file like any other.
        raise AssetError(f"damaged glTF: its JSON is not valid ({type(exc).__name__}); "
                         "re-export it") from exc
    if not isinstance(document, dict):
        raise AssetError("damaged glTF: its JSON is not an object; re-export it")
    asset = document.get("asset")
    version = str(asset.get("version", "")) if isinstance(asset, dict) else ""
    # glTF 1.0 kept meshes/accessors as name-keyed objects, not lists.
    if version.startswith("1") or isinstance(document.get("meshes"), dict):
        raise AssetError("glTF 1.0 file: this format was replaced by glTF 2.0 in 2017; "
                         "re-export it as glTF 2.0")
    return document


def _glb_json_prefix(path: Path) -> bytes:
    """The bytes of a .glb up to the end of its JSON chunk, not the whole file.

    One check reads the document a dozen times (triangles, materials, UV sets,
    compression, ...): reading all of a 500 MB .glb for each, binary chunk
    included, was a dozen full reads of the file for a few kilobytes of JSON.
    """
    with path.open("rb") as handle:
        blob = handle.read(20)                 # header (12) + first chunk header (8)
        if blob[:4] != GLB_MAGIC or len(blob) < 20:
            return blob
        chunk_length, chunk_type = struct.unpack_from("<II", blob, 12)
        if chunk_type == GLB_JSON_CHUNK:       # the glTF spec puts it first
            return blob + handle.read(chunk_length)
        return blob + handle.read()            # out of order: read it all, as before


def _glb_json(blob: bytes) -> dict:
    if blob[:4] != GLB_MAGIC:
        raise AssetError("not a GLB container (bad magic header)")
    offset = 12  # magic(4) + version(4) + total length(4)
    while offset + 8 <= len(blob):
        chunk_length, chunk_type = struct.unpack_from("<II", blob, offset)
        offset += 8
        if chunk_type == GLB_JSON_CHUNK:
            return json.loads(blob[offset:offset + chunk_length].decode("utf-8"))
        offset += chunk_length
    raise AssetError("GLB contains no JSON chunk")


#: glTF primitive modes: 4 TRIANGLES, 5 TRIANGLE_STRIP, 6 TRIANGLE_FAN. 0-3 are
#: points and lines, which an engine does not import as mesh triangles.
_GLTF_TRIANGLE_MODES = (4, 5, 6)


def _primitive_triangles(document: dict, primitive: dict) -> int:
    accessors = document.get("accessors", [])
    mode = primitive.get("mode", 4)
    if mode not in _GLTF_TRIANGLE_MODES:
        return 0
    index_accessor = primitive.get("indices")
    if index_accessor is None:
        index_accessor = (primitive.get("attributes") or {}).get("POSITION")
    if not isinstance(index_accessor, int) or not 0 <= index_accessor < len(accessors):
        return 0
    count = int(accessors[index_accessor].get("count", 0) or 0)
    if mode == 4:
        return count // 3
    return max(count - 2, 0)        # a strip or a fan: one triangle per index after two


def gltf_triangle_counts(document: dict) -> tuple[int, int]:
    """(unique, drawn) triangles of a glTF document, read from its accessors.

    *unique* counts each mesh once. *drawn* counts a mesh once per node that
    places it - what the scene renders, and what Unreal Engine 5.3 imports: its
    glTF importer bakes repeated parts into the static mesh (CesiumMilkTruck:
    2,856 unique, 3,624 drawn, and Unreal's two meshes hold 2,088 + 1,536 =
    3,624; measured 6 Nov). The gate budgets the drawn count. Both come straight from the
    accessor counts, so they need no decoding: Draco- or meshopt-compressed
    files count the same as plain ones, and strips and fans are not dropped (a
    fan-only mesh used to read as 0 triangles).
    """
    meshes = document.get("meshes", [])
    per_mesh = [sum(_primitive_triangles(document, primitive)
                    for primitive in (mesh.get("primitives") or []))
                for mesh in meshes]
    nodes = document.get("nodes", [])
    roots = _gltf_scene_roots(document) or []
    if not roots:
        # UCX_ & co are collision, not render geometry, as in an FBX (11 Nov 2026).
        collision = _gltf_collision_meshes(document)
        total = sum(count for index, count in enumerate(per_mesh) if index not in collision)
        return total, total

    used: set[int] = set()
    drawn = 0
    stack = [(root, frozenset()) for root in roots]
    while stack:
        node_index, seen = stack.pop()
        if not isinstance(node_index, int) or not 0 <= node_index < len(nodes) or node_index in seen:
            continue                     # malformed or cyclic graph: skip, never loop
        node = nodes[node_index]
        mesh_index = node.get("mesh")
        if isinstance(mesh_index, int) and 0 <= mesh_index < len(per_mesh) \
                and not _gltf_is_collision_node(document, node):
            used.add(mesh_index)
            copies = 1
            instancing = (node.get("extensions") or {}).get("EXT_mesh_gpu_instancing")
            if isinstance(instancing, dict):
                attributes = instancing.get("attributes") or {}
                accessor = next(iter(attributes.values()), None)
                accessors = document.get("accessors", [])
                if isinstance(accessor, int) and 0 <= accessor < len(accessors):
                    copies = max(int(accessors[accessor].get("count", 1) or 1), 1)
            drawn += per_mesh[mesh_index] * copies
        for child in node.get("children", []):
            stack.append((child, seen | {node_index}))
    return sum(per_mesh[index] for index in used), drawn


def _count_triangles_from_document(document: dict) -> int:
    """Drawn triangles of a glTF document (see gltf_triangle_counts)."""
    return gltf_triangle_counts(document)[1]


def count_triangles(path: Path) -> int:
    """Triangle count for .obj / .gltf / .glb and .fbx.

    glTF is counted from its accessors (one counter, no decoding); OBJ through
    trimesh; FBX through the built-in reader."""
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        return read_fbx(path).triangles
    if suffix in GLTF_SUFFIXES:
        document = read_gltf_document(path)
        try:
            return _count_triangles_from_document(document)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AssetError(f"damaged glTF ({_describe_unexpected(exc)}); re-export it") from exc
    if trimesh is not None:
        try:
            return _count_triangles_trimesh(path)
        except AssetError:
            raise
        except Exception as exc:  # trimesh raises a wide variety of errors
            raise AssetError(_mesh_read_error(exc)) from exc
    raise AssetError("trimesh is not installed; .obj cannot be analysed")


def _count_material_slots_trimesh(path: Path) -> int:
    """Fallback: distinct material objects across a loaded scene."""
    if trimesh is None:
        raise AssetError("trimesh is not installed; material slots cannot be counted")
    loaded = load_trimesh_scene(str(path), process=False)
    geometries = loaded.geometry.values() if isinstance(loaded, trimesh.Scene) else [loaded]

    unused_default = 0
    named: set[object] = set()
    for geometry in geometries:
        material = getattr(getattr(geometry, "visual", None), "material", None)
        if material is None:
            unused_default += 1
            continue
        key = getattr(material, "name", None) or id(material)
        named.add(key)
    return len(named) + unused_default


def count_material_slots(path: Path) -> int:
    """Number of distinct materials referenced by a mesh (approx. draw calls).

    Counted from the source data rather than from the loaded object graph, so two
    primitives that point at the same glTF material index count as one slot.
    """
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        return read_fbx(path).material_slots

    if suffix in GLTF_SUFFIXES:
        try:
            document = read_gltf_document(path)
        except (AssetError, ValueError, OSError, struct.error):
            return _count_material_slots_trimesh(path)

        used_indices: set[int] = set()
        has_default_material = False
        render = _gltf_render_meshes(document)
        for index, mesh in enumerate(document.get("meshes", [])):
            if index not in render:
                continue          # collision, a coarser LOD, or never placed
            for primitive in mesh.get("primitives", []):
                material_index = primitive.get("material")
                if material_index is None:
                    has_default_material = True
                else:
                    used_indices.add(material_index)
        return len(used_indices) + (1 if has_default_material else 0)

    if suffix == ".obj":
        names: set[str] = set()
        has_default_material = False
        current: Optional[str] = None
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.lstrip()
                if stripped.startswith("usemtl"):
                    current = stripped[6:].strip() or None
                    if current is not None:
                        names.add(current)
                elif stripped.startswith("f ") or stripped.startswith("f\t"):
                    if current is None:
                        has_default_material = True
        return len(names) + (1 if has_default_material else 0)

    return _count_material_slots_trimesh(path)


# --- #
# Deep data profiling: skin weights, UV sets, degenerate geometry
# --- #

_COMPONENT_TYPES = {
    5120: ("b", 1),   # byte
    5121: ("B", 1),   # unsigned byte
    5122: ("h", 2),   # short
    5123: ("H", 2),   # unsigned short
    5125: ("I", 4),   # unsigned int
    5126: ("f", 4),   # float
}
_TYPE_COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def _glb_binary_chunk(path: Path) -> bytes:
    """The BIN chunk of a .glb, where embedded accessor data lives."""
    blob = path.read_bytes()
    offset = 12  # magic(4) + version(4) + length(4)
    while offset + 8 <= len(blob):
        chunk_length, chunk_type = struct.unpack_from("<II", blob, offset)
        offset += 8
        if chunk_type == 0x004E4942:  # 'BIN'
            return blob[offset:offset + chunk_length]
        offset += chunk_length
    raise AssetError("GLB contains no binary chunk")


def buffer_bytes(path: Path, document: dict, index: int) -> bytes:
    """Resolve a glTF buffer: GLB chunk, base64 data URI, or sibling file."""
    buffers = document.get("buffers", [])
    if index >= len(buffers):
        raise AssetError(f"glTF references missing buffer {index}")
    uri = buffers[index].get("uri")
    if uri is None:
        return _glb_binary_chunk(path)
    if uri.startswith("data:"):
        _, _, payload = uri.partition("base64,")
        try:
            return base64.b64decode(payload)
        except ValueError as exc:
            raise AssetError(f"glTF data URI is not valid base64 ({exc})") from exc
    return (path.parent / unquote(uri)).read_bytes()


def read_accessor(path: Path, document: dict, index: int) -> list[tuple]:
    """Decode one glTF accessor into a list of tuples. Standard library only.

    Honours ``byteStride`` so interleaved buffers (the common Unity/UE export
    layout) decode correctly instead of producing garbage weights.
    """
    accessors = document.get("accessors", [])
    if index >= len(accessors):
        raise AssetError(f"glTF references missing accessor {index}")
    accessor = accessors[index]

    component_type = accessor.get("componentType")
    if component_type not in _COMPONENT_TYPES:
        raise AssetError(f"unsupported glTF componentType {component_type}")
    code, size = _COMPONENT_TYPES[component_type]

    components = _TYPE_COMPONENTS.get(accessor.get("type", "SCALAR"))
    if components is None:
        raise AssetError(f"unsupported glTF accessor type {accessor.get('type')}")
    count = int(accessor.get("count", 0))

    view_index = accessor.get("bufferView")
    if view_index is None:  # valid: accessor with no data (all zeros)
        return [tuple([0] * components) for _ in range(count)]

    view = document.get("bufferViews", [])[view_index]
    raw = buffer_bytes(path, document, int(view.get("buffer", 0)))
    start = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    tight = components * size
    stride = int(view.get("byteStride") or 0) or tight

    if start + stride * (count - 1) + tight > len(raw):
        raise AssetError("glTF accessor runs past the end of its buffer")

    def unpack(base: int) -> tuple:
        values = array.array(code)
        values.frombytes(raw[base:base + tight])
        return tuple(values)

    if stride == tight:
        return [unpack(start + i * tight) for i in range(count)]
    return [unpack(start + i * stride) for i in range(count)]


def _texture_edge_of(geometry) -> int:
    """The base-colour texture's size (sqrt of its area) a mesh's material names, or 0."""
    material = getattr(getattr(geometry, "visual", None), "material", None)
    for attribute in ("baseColorTexture", "image"):
        image = getattr(material, attribute, None)
        size = getattr(image, "size", None)
        # trimesh gives a material-less mesh a tiny placeholder image; anything
        # under 16 px is that, not an artist's texture.
        if size and len(size) == 2 and min(size) >= 16:
            return int(round(math.sqrt(size[0] * size[1])))
    return 0


def mesh_facts(path: Path, assumed_texture: int = 1024) -> dict:
    """Vertex count, vertex colours and texel density, from the placed scene.

    Texel density is pixels per metre of surface, area-weighted over every mesh
    with UVs: sqrt(UV area x texture pixels / world area). The texture is the
    one the material names when it can be read, else ``assumed_texture``.
    Units are taken as metres (glTF's unit; OBJ has none).
    """
    import numpy as np
    scene = render_scene(path, load_scene(path))
    meshes = scene.dump(concatenate=False) if hasattr(scene, "dump") else [scene]
    vertices = 0
    colours = False
    weighted = world_total = 0.0
    with_uv = without_uv = 0
    textures = set()
    for mesh in meshes:
        points = getattr(mesh, "vertices", None)
        faces = getattr(mesh, "faces", None)
        if points is None or faces is None or not len(faces):
            continue
        vertices += len(points)
        visual = getattr(mesh, "visual", None)
        kind = getattr(visual, "kind", None)
        colours = colours or kind == "vertex"
        uv = getattr(visual, "uv", None)
        if uv is None or len(uv) != len(points):
            without_uv += 1
            continue
        with_uv += 1
        uv = np.asarray(uv, dtype=np.float64)[:, :2]
        tri = uv[np.asarray(faces)]
        uv_area = 0.5 * np.abs((tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1])
                               - (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1])).sum()
        world_area = float(mesh.area)
        edge = _texture_edge_of(mesh) or assumed_texture
        textures.add(edge if _texture_edge_of(mesh) else 0)
        weighted += float(uv_area) * edge * edge
        world_total += world_area
    density = math.sqrt(weighted / world_total) if world_total > 0 and weighted > 0 else None
    return {"vertices": int(vertices), "colours": colours, "texel_density": density,
            "with_uv": with_uv, "without_uv": without_uv,
            "assumed_texture": 0 in textures}


def count_uv_channels(path: Path) -> int:
    """Number of distinct texture-coordinate sets (TEXCOORD_n / OBJ vt / FBX UV layers)."""
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        return read_fbx(path).uv_channels

    if suffix in GLTF_SUFFIXES:
        try:
            document = read_gltf_document(path)
        except (AssetError, ValueError, OSError, struct.error):
            document = None
        if document is not None:
            channels = set()
            render = _gltf_render_meshes(document)
            for index, mesh in enumerate(document.get("meshes", [])):
                if index not in render:
                    continue      # collision, a coarser LOD, or never placed
                for primitive in mesh.get("primitives", []):
                    for attribute in primitive.get("attributes", {}):
                        if attribute.startswith("TEXCOORD_"):
                            channels.add(attribute)
            return len(channels)

    if suffix == ".obj":
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.lstrip().startswith("vt "):
                    return 1  # the OBJ format carries a single UV set
        return 0

    for geometry in _geometries(load_scene(path)):
        if getattr(getattr(geometry, "visual", None), "uv", None) is not None:
            return 1
    return 0


def max_bone_influences(path: Path, epsilon: float = 1e-4) -> int:
    """Worst per-vertex bone-weight count across every JOINTS_n/WEIGHTS_n set.

    glTF encodes four influences per set, so a vertex bound to six bones carries a
    second set (JOINTS_1/WEIGHTS_1). We sum non-zero weights across all sets -
    which is exactly what a mobile GPU cannot afford above four.
    """
    if path.suffix.lower() in FBX_SUFFIXES:
        return read_fbx(path).max_influences
    if path.suffix.lower() not in GLTF_SUFFIXES:
        return 0  # .obj has no skin data

    document = read_gltf_document(path)
    worst = 0
    for mesh in document.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            attributes = primitive.get("attributes", {})
            sets = sorted(
                int(name.rsplit("_", 1)[1])
                for name in attributes
                if name.startswith("WEIGHTS_") and name.rsplit("_", 1)[1].isdigit()
            )
            if not sets:
                continue

            per_vertex: Optional[list[int]] = None
            for set_index in sets:
                weights = read_accessor(path, document, attributes[f"WEIGHTS_{set_index}"])
                counts = [sum(1 for w in row if abs(w) > epsilon) for row in weights]
                if per_vertex is None:
                    per_vertex = counts
                else:
                    per_vertex = [a + b for a, b in zip(per_vertex, counts)]

            if per_vertex:
                worst = max(worst, max(per_vertex))
    return worst


#: glTF extensions that compress vertex data trimesh cannot read without help.
_GLTF_DRACO = "KHR_draco_mesh_compression"
_GLTF_MESHOPT = ("EXT_meshopt_compression", "KHR_meshopt_compression")


def _dracopy_available() -> bool:
    # trimesh decodes Draco through DracoPy; only its presence matters here.
    return importlib.util.find_spec("DracoPy") is not None


def gltf_uses_draco(path: Path) -> bool:
    if path.suffix.lower() not in GLTF_SUFFIXES:
        return False
    try:
        return _GLTF_DRACO in set(read_gltf_document(path).get("extensionsUsed") or [])
    except AssetError:
        return False


def gltf_undecodable_compression(path: Path) -> str:
    """A warning when a glTF's vertex data is compressed in a way this machine
    cannot decode, else ''. Geometry checks are then skipped rather than run on
    placeholder zeros: without DracoPy every Draco vertex reads as (0, 0, 0) and
    every triangle looked degenerate (duck.glb: 4,212 of 4,212)."""
    if path.suffix.lower() not in GLTF_SUFFIXES:
        return ""
    try:
        document = read_gltf_document(path)
    except AssetError:
        return ""
    used = set(document.get("extensionsUsed") or [])
    required = set(document.get("extensionsRequired") or [])
    if _GLTF_DRACO in used and not _dracopy_available():
        return ("compressed with Draco and DracoPy is not installed, so the shape checks "
                "(degenerate triangles, bone weights) were skipped - "
                "python -m pip install DracoPy")
    if required & set(_GLTF_MESHOPT):
        return ("compressed with meshopt, which this version cannot decode, so the shape "
                "checks (degenerate triangles, bone weights) were skipped; counts are exact")
    return ""


# --- #
# Skeleton and lightmap UVs ([skeleton], [lightmap]): read only when a rule is on
# --- #

#: FBX Model classes that are skeleton joints (Maya, Max and Blender write LimbNode).
FBX_BONE_CLASSES = frozenset({"LimbNode", "Limb", "Root"})
#: A weight at or below this does not bind a vertex to a bone (as max_bone_influences).
BONE_WEIGHT_EPSILON = 1e-4
#: The overlap grid is sampled in chunks of this many texel centres; above
#: LIGHTMAP_MAX_SAMPLES centres in one mesh the grid is halved until it fits.
LIGHTMAP_CHUNK = 1 << 20
LIGHTMAP_MAX_SAMPLES = 1 << 23


def _name_list(names: Sequence[str], shown: int = 3) -> str:
    """'a', 'b', 'c' and 2 more: names in a one-line verdict."""
    quoted = ", ".join(f"'{name}'" for name in list(names)[:shown])
    return quoted + (f" and {len(names) - shown} more" if len(names) > shown else "")


def _ordinal(number: int) -> str:
    words = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth",
             7: "seventh", 8: "eighth"}
    return words.get(number, f"{number}th")


def _percent(value: float) -> str:
    """12, 3.4, 0.05: enough digits to tell a small overlap from none."""
    if value == 0:
        return "0"
    if value >= 10:
        return f"{value:.0f}"
    return f"{value:.1f}" if value >= 1 else f"{value:.2f}"


@lru_cache(maxsize=1)
def _fbx_tree_cached(path_string: str, modified_ns: int) -> dict:
    return {node.name: node for node in parse_fbx_binary(Path(path_string).read_bytes())}


def _fbx_tree(path: Path) -> dict:
    """The parsed FBX, shared by the skeleton and lightmap readers of one check."""
    try:
        return _fbx_tree_cached(str(path), path.stat().st_mtime_ns)
    except AssetError:
        raise
    except OSError as exc:
        raise AssetError(f"could not read FBX ({exc})") from exc
    except (ValueError, IndexError, TypeError, MemoryError) as exc:
        raise AssetError(f"could not read FBX ({type(exc).__name__}: {exc})") from exc


def _fbx_graph(top: dict) -> dict:
    """Objects and connections of a parsed FBX, and its render geometry.

    The render geometry is chosen as in _read_fbx_cached: no UCX_/UBX_/USP_/UCP_
    collision meshes and no LOD1+ levels of an LOD group.
    """
    graph: dict = {"geometries": {}, "models": {}, "classes": {}, "names": {}, "materials": {},
                   "skins": set(), "clusters": {}, "parents": {}, "children": {}, "rendered": []}
    objects = top.get("Objects")
    if objects is None:
        return graph
    lod_groups = []
    for node in objects.children:
        node_id, kind = _fbx_id(node), _fbx_class(node)
        if node.name == "Geometry" and kind == "Mesh":
            graph["geometries"][node_id] = node
        elif node.name == "Model":
            graph["models"][node_id] = node
            graph["classes"][node_id] = kind
            graph["names"][node_id] = _fbx_object_name(node)
            if kind == "LodGroup":
                lod_groups.append(node_id)
        elif node.name == "Material":
            graph["materials"][node_id] = node
        elif node.name == "Deformer" and kind == "Skin":
            graph["skins"].add(node_id)
        elif node.name == "Deformer" and kind == "Cluster":
            graph["clusters"][node_id] = node
    parents, children = graph["parents"], graph["children"]
    connections = top.get("Connections")
    for link in (connections.all("C") if connections else []):
        if len(link.props) < 3:
            continue
        child, parent = link.props[1], link.props[2]
        if parent not in parents.setdefault(child, []):
            parents[child].append(parent)
        if child not in children.setdefault(parent, []):
            children[parent].append(child)
    models = graph["models"]
    collision = {model for model, name in graph["names"].items()
                 if name.lower().startswith(FBX_COLLISION_PREFIXES)}
    lower_lod: set = set()
    for group in lod_groups:
        levels = [item for item in children.get(group, []) if item in models and item not in collision]
        stack = list(levels[1:])
        while stack:
            item = stack.pop()
            if item not in lower_lod:
                lower_lod.add(item)
                stack.extend(children.get(item, ()))
    for geometry_id in graph["geometries"]:
        owners = {parent for parent in parents.get(geometry_id, ()) if parent in models}
        if owners and (owners <= collision or owners <= lower_lod):
            continue
        graph["rendered"].append(geometry_id)
    return graph


def _fbx_owner(graph: dict, geometry_id) -> object:
    return next((parent for parent in graph["parents"].get(geometry_id, ())
                 if parent in graph["models"]), None)


def _fbx_mesh_label(graph: dict, geometry_id) -> str:
    owner = _fbx_owner(graph, geometry_id)
    name = graph["names"].get(owner, "") if owner is not None else ""
    return name or _fbx_object_name(graph["geometries"][geometry_id]) or "mesh"


def _fbx_skin_binds(graph: dict, geometry_id) -> list:
    """(cluster node, bone model id) for every skin cluster deforming this geometry."""
    binds = []
    for skin_id in graph["children"].get(geometry_id, ()):
        if skin_id not in graph["skins"]:
            continue
        for cluster_id in graph["children"].get(skin_id, ()):
            cluster = graph["clusters"].get(cluster_id)
            bone = next((item for item in graph["children"].get(cluster_id, ())
                         if item in graph["models"]), None)
            if cluster is not None and bone is not None:
                binds.append((cluster, bone))
    return binds


def _fbx_polygon_materials(geometry, polygons: int):
    """Material slot index of each polygon (LayerElementMaterial; 0 when absent)."""
    per_polygon = np.zeros(polygons, dtype=np.int64)
    layer = geometry.child("LayerElementMaterial")
    values = layer.child("Materials") if layer is not None else None
    if values is not None and values.props:
        raw = np.asarray(values.props[0], dtype=np.int64).reshape(-1)
        mapping = _fbx_string(layer.child("MappingInformationType"))
        if mapping == "ByPolygon" and raw.size == polygons:
            per_polygon = raw.copy()
        elif raw.size:
            per_polygon[:] = raw[0]
    return np.clip(per_polygon, 0, None)


def _fbx_layer_values(layer, values_name: str, index_name: str, width: int, corners):
    """Per-corner values of one FBX layer element (a UV set), or None.

    ByPolygonVertex or ByControlPoint mapping, Direct or IndexToDirect reference,
    as engine/fbx_preview reads the first UV set.
    """
    values_node = layer.child(values_name)
    if values_node is None or not values_node.props:
        return None
    values = np.asarray(values_node.props[0], dtype=np.float64).reshape(-1)
    if values.size == 0 or values.size % width:
        return None
    values = values.reshape(-1, width)
    mapping = _fbx_string(layer.child("MappingInformationType"))
    reference = _fbx_string(layer.child("ReferenceInformationType"))
    index_node = layer.child(index_name)
    if mapping == "ByPolygonVertex":
        lookup = np.arange(len(corners))
    elif mapping in ("ByControlPoint", "ByVertice", "ByVertex"):
        lookup = corners
    else:
        return None
    if reference == "IndexToDirect" and index_node is not None and index_node.props:
        indices = np.asarray(index_node.props[0], dtype=np.int64).reshape(-1)
        if lookup.size and int(lookup.max()) >= len(indices):
            return None
        lookup = indices[lookup]
    if lookup.size and (int(lookup.max()) >= len(values) or int(lookup.min()) < 0):
        return None
    return values[lookup]


def _fbx_fan(starts, sizes):
    """Fan triangulation as engines import it: corner indices (n, 3) of (c0, ck, ck+1)."""
    fan = np.clip(sizes - 2, 0, None)
    count = int(fan.sum())
    owner = np.repeat(np.arange(len(sizes)), fan)
    first = np.repeat(np.cumsum(fan) - fan, fan)
    step = np.arange(count) - first + 1
    return np.stack([starts[owner], starts[owner] + step, starts[owner] + step + 1], axis=1), owner


_NUMPY_COMPONENTS = {5120: "<i1", 5121: "<u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}
_NORMALISED_SCALE = {5120: 127.0, 5121: 255.0, 5122: 32767.0, 5123: 65535.0, 5125: 4294967295.0}


def _accessor_numpy(path: Path, document: dict, index, buffers: dict):
    """One glTF accessor as a float64 (count, components) array (read_accessor, in numpy).

    ``buffers`` caches each buffer's bytes for the caller. Normalised integers
    (KHR_mesh_quantization UVs and weights) are scaled to 0-1.
    """
    accessors = document.get("accessors", [])
    if not isinstance(index, int) or not 0 <= index < len(accessors):
        raise AssetError(f"glTF references missing accessor {index}")
    accessor = accessors[index]
    component = accessor.get("componentType")
    dtype = _NUMPY_COMPONENTS.get(component)
    width = _TYPE_COMPONENTS.get(accessor.get("type", "SCALAR"))
    if dtype is None or width is None:
        raise AssetError(f"unsupported glTF accessor {index} ({component}, {accessor.get('type')})")
    count = int(accessor.get("count", 0) or 0)
    view_index = accessor.get("bufferView")
    if view_index is None or count == 0:
        return np.zeros((count, width))
    views = document.get("bufferViews", [])
    if not isinstance(view_index, int) or not 0 <= view_index < len(views):
        raise AssetError(f"glTF references missing bufferView {view_index}")
    view = views[view_index]
    buffer_index = int(view.get("buffer", 0))
    if buffer_index not in buffers:
        buffers[buffer_index] = buffer_bytes(path, document, buffer_index)
    raw = buffers[buffer_index]
    item = np.dtype(dtype).itemsize
    start = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    stride = int(view.get("byteStride") or 0) or item * width
    if start < 0 or start + stride * (count - 1) + item * width > len(raw):
        raise AssetError("glTF accessor runs past the end of its buffer")
    values = np.ndarray((count, width), dtype=dtype, buffer=raw, offset=start,
                        strides=(stride, item)).astype(np.float64)
    if accessor.get("normalized") and component in _NORMALISED_SCALE:
        values = np.maximum(values / _NORMALISED_SCALE[component], -1.0)
    return values


def _gltf_triangles(path: Path, document: dict, primitive: dict, buffers: dict):
    """Vertex indices (n, 3) of a triangle primitive (lists, strips and fans), or None."""
    mode = primitive.get("mode", 4)
    if mode not in _GLTF_TRIANGLE_MODES:
        return None
    if primitive.get("indices") is not None:
        indices = _accessor_numpy(path, document, primitive["indices"], buffers)[:, 0]
        indices = indices.astype(np.int64)
    else:
        position = (primitive.get("attributes") or {}).get("POSITION")
        accessors = document.get("accessors", [])
        if not isinstance(position, int) or not 0 <= position < len(accessors):
            return None
        indices = np.arange(int(accessors[position].get("count", 0) or 0), dtype=np.int64)
    if mode == 4:
        return indices[:len(indices) // 3 * 3].reshape(-1, 3)
    if len(indices) < 3:
        return np.zeros((0, 3), dtype=np.int64)
    first = indices[:-2] if mode == 5 else np.full(len(indices) - 2, indices[0])
    return np.stack([first, indices[1:-1], indices[2:]], axis=1)


def _gltf_draco(primitive: dict) -> bool:
    """This primitive's vertex data is in a Draco blob, not in its accessors."""
    extensions = primitive.get("extensions") if isinstance(primitive, dict) else None
    return isinstance(extensions, dict) and _GLTF_DRACO in extensions


def _bone_tree(bones: set, parent_of, children_of) -> tuple:
    """(roots, depth per bone): roots are bones whose parent is not a bone."""
    roots = []
    for bone in sorted(bones, key=str):
        up = parent_of(bone)
        if up is None or up not in bones:
            roots.append(bone)
    depth: dict = {}
    stack = [(root, 1) for root in roots]
    while stack:
        bone, level = stack.pop()
        if bone in depth:
            continue
        depth[bone] = level
        stack.extend((child, level + 1) for child in children_of(bone)
                     if child in bones and child not in depth)
    return roots, depth


def _fbx_skeleton(path: Path) -> Optional[dict]:
    graph = _fbx_graph(_fbx_tree(path))
    models, children, names = graph["models"], graph["children"], graph["names"]
    skinned = [(geometry_id, binds) for geometry_id in graph["rendered"]
               for binds in [_fbx_skin_binds(graph, geometry_id)] if binds]
    if not skinned:
        return None

    def parent_of(model):
        return next((item for item in graph["parents"].get(model, ()) if item in models), None)

    def child_models(model):
        return [item for item in children.get(model, ()) if item in models]

    # A bone is a joint node (LimbNode) or any node a skin cluster binds to. The
    # skeleton is every bone connected to a bound one: up to its root, and down
    # through unweighted bones (IK targets, twist helpers, tips) below that root.
    linked = {bone for _geometry, binds in skinned for _cluster, bone in binds}

    def is_bone(model) -> bool:
        return model in linked or graph["classes"].get(model) in FBX_BONE_CLASSES

    tops = set()
    for bone in linked:
        node, seen = bone, {bone}
        while True:
            up = parent_of(node)
            if up is None or up in seen or not is_bone(up):
                break
            seen.add(up)
            node = up
        tops.add(node)
    bones: set = set()
    stack = list(tops)
    while stack:
        model = stack.pop()
        if model in bones:
            continue
        bones.add(model)
        stack.extend(item for item in child_models(model) if is_bone(item))
    roots, depth = _bone_tree(bones, parent_of, child_models)

    # Bones per material section: the clusters with weight on a vertex of a
    # polygon of that material. Sections of several meshes that share a
    # material are one section, as Unreal builds them.
    sections: dict = {}
    for geometry_id, binds in skinned:
        geometry = graph["geometries"][geometry_id]
        index_node = geometry.child("PolygonVertexIndex")
        if index_node is None or not index_node.props:
            continue
        corners, starts, sizes = _fbx_polygons(index_node.props[0])
        if not corners.size:
            continue
        vertex_count = int(corners.max()) + 1
        per_polygon = _fbx_polygon_materials(geometry, len(sizes))
        per_corner = np.repeat(per_polygon, sizes)
        owner = _fbx_owner(graph, geometry_id)
        slots = [item for item in children.get(owner, []) if item in graph["materials"]]
        weighted = []
        for cluster, bone in binds:
            indexes, weights = cluster.child("Indexes"), cluster.child("Weights")
            if indexes is None or weights is None or not indexes.props or not weights.props:
                continue
            index = np.asarray(indexes.props[0], dtype=np.int64).reshape(-1)
            weight = np.asarray(weights.props[0], dtype=np.float64).reshape(-1)
            size = min(index.size, weight.size)
            index = index[:size][weight[:size] > BONE_WEIGHT_EPSILON]
            weighted.append((bone, index[(index >= 0) & (index < vertex_count)]))
        for slot in np.unique(per_polygon).tolist():
            used = np.zeros(vertex_count, dtype=bool)
            used[corners[per_corner == slot]] = True
            if slot < len(slots):
                key = slots[slot]
                label = _fbx_object_name(graph["materials"][key]) or f"material {slot}"
            elif not slots:
                key, label = "default", "default material"
            else:
                key, label = (owner, slot), f"material {slot}"
            section = sections.setdefault(key, (label, set()))[1]
            section.update(bone for bone, index in weighted if index.size and used[index].any())
    deepest = max(depth, key=lambda bone: (depth[bone], str(names.get(bone)))) if depth else None
    return {"bones": len(bones), "roots": sorted(names.get(root, "") or "bone" for root in roots),
            "depth": max(depth.values()) if depth else 0,
            "deepest": names.get(deepest, "") if deepest is not None else "",
            "sections": [(label, len(found)) for label, found in sections.values()],
            "unread_sections": 0}


def _gltf_skeleton(path: Path) -> Optional[dict]:
    document = read_gltf_document(path)
    nodes = document.get("nodes") or []
    skins = document.get("skins") or []
    meshes = document.get("meshes") or []
    uses = []
    render = _gltf_render_meshes(document)
    for node in nodes:
        mesh, skin = node.get("mesh"), node.get("skin")
        if isinstance(mesh, int) and isinstance(skin, int) and 0 <= mesh < len(meshes) \
                and 0 <= skin < len(skins) and mesh in render:
            uses.append((mesh, skin))       # LOD0 only, as for an FBX
    if not uses:
        return None
    joints_of = {}
    for skin in {skin for _mesh, skin in uses}:
        # Kept whole: JOINTS_n values index this list. Invalid entries are not bones.
        joints_of[skin] = [item if isinstance(item, int) and 0 <= item < len(nodes) else None
                           for item in (skins[skin].get("joints") or [])]
    bones = {item for joints in joints_of.values() for item in joints if item is not None}
    parent: dict = {}
    for index, node in enumerate(nodes):
        for child in node.get("children") or []:
            if isinstance(child, int):
                parent.setdefault(child, index)

    def child_nodes(index):
        return [item for item in (nodes[index].get("children") or []) if isinstance(item, int)]

    roots, depth = _bone_tree(bones, parent.get, child_nodes)
    materials = document.get("materials") or []
    sections: dict = {}
    unread = 0
    buffers: dict = {}
    # meshopt-compressed buffers hold no plain weights to read
    compressed = bool(gltf_undecodable_compression(path))
    for mesh, skin in uses:
        joints = joints_of[skin]
        for primitive in meshes[mesh].get("primitives") or []:
            attributes = primitive.get("attributes") or {}
            material = primitive.get("material")
            if isinstance(material, int) and 0 <= material < len(materials):
                label = materials[material].get("name") or f"material {material}"
            else:
                material, label = "default", "default material"
            section = sections.setdefault(material, (label, set()))[1]
            if compressed or _gltf_draco(primitive):
                unread += 1
                continue
            for name in attributes:
                if not (name.startswith("JOINTS_") and name[7:].isdigit()):
                    continue
                weights_name = "WEIGHTS_" + name[7:]
                if weights_name not in attributes:
                    continue
                local = _accessor_numpy(path, document, attributes[name], buffers)
                weight = _accessor_numpy(path, document, attributes[weights_name], buffers)
                size = min(len(local), len(weight))
                used = np.unique(local[:size][weight[:size] > BONE_WEIGHT_EPSILON]).astype(np.int64)
                section.update(joints[item] for item in used.tolist()
                               if 0 <= item < len(joints) and joints[item] is not None)
    names = {index: (nodes[index].get("name") or f"node {index}") for index in bones}
    deepest = max(depth, key=lambda bone: (depth[bone], str(names.get(bone)))) if depth else None
    return {"bones": len(bones), "roots": sorted(names[root] for root in roots),
            "depth": max(depth.values()) if depth else 0,
            "deepest": names.get(deepest, "") if deepest is not None else "",
            "sections": [(label, len(found)) for label, found in sections.values()],
            "unread_sections": unread}


def skeleton_facts(path: Path) -> Optional[dict]:
    """The skeleton the asset's skinned meshes use, or None when nothing is skinned.

    {"bones", "roots" (names), "depth" (longest root-to-tip chain, root = 1),
    "deepest" (its last bone), "sections" [(material, bones weighting it)],
    "unread_sections" (compressed primitives whose weights were not read)}.
    FBX: LimbNode joints and every node a skin cluster binds, connected to the
    bound ones. glTF: the joints of every skin a mesh node uses. OBJ: no skins.
    """
    if np is None:                                         # pragma: no cover
        raise AssetError("numpy is not installed; the skeleton cannot be read")
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        return _fbx_skeleton(path)
    if suffix in GLTF_SUFFIXES:
        return _gltf_skeleton(path)
    return None


def lightmap_coverage(triangles, resolution: int = 256) -> tuple[int, int, int]:
    """(texels covered, texels covered more than once, grid used) of UV triangles.

    ``triangles`` is an (n, 3, 2) array of UVs, rasterised into a resolution x
    resolution grid over 0-1. A texel belongs to a triangle when its centre is
    inside it. A centre exactly on an edge goes to one side only (the top-left
    rule GPUs use), and the edge test is computed from the same end of the edge
    for both triangles that share it, so it gives exactly opposite answers: the
    triangles of one island, and islands that only touch, never overlap.

    Cost: one test per texel centre inside each triangle's bounding box, in
    chunks of LIGHTMAP_CHUNK. A clean layout tests about two centres per covered
    texel whatever the triangle count (100,000 triangles at 256: ~0.05 s). A layout
    whose triangles each cover the square many times over would test far more,
    so above LIGHTMAP_MAX_SAMPLES the grid is halved until it fits (down to 4).
    """
    tri = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 2)
    tri = tri[np.all(np.isfinite(tri), axis=(1, 2))]
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    twice_area = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    keep = twice_area != 0                  # zero-area triangles cover nothing
    a, b, c, twice_area = a[keep], b[keep], c[keep], twice_area[keep]
    mirrored = (twice_area < 0)[:, None]    # one winding for all: mirrored islands too
    b, c = np.where(mirrored, c, b), np.where(mirrored, b, c)
    res = max(4, int(resolution))
    while True:
        low = np.minimum(np.minimum(a, b), c) * res
        high = np.maximum(np.maximum(a, b), c) * res
        x0 = np.clip(np.ceil(low[:, 0] - 0.5), 0, res - 1).astype(np.int64)
        y0 = np.clip(np.ceil(low[:, 1] - 0.5), 0, res - 1).astype(np.int64)
        x1 = np.clip(np.floor(high[:, 0] - 0.5), -1, res - 1).astype(np.int64)
        y1 = np.clip(np.floor(high[:, 1] - 0.5), -1, res - 1).astype(np.int64)
        width = np.clip(x1 - x0 + 1, 0, None)
        samples = width * np.clip(y1 - y0 + 1, 0, None)
        total = int(samples.sum())
        if total <= LIGHTMAP_MAX_SAMPLES or res <= 4:
            break
        res = max(4, res // 2)
    if total == 0:
        return 0, 0, res
    pa, pb, pc = a * res, b * res, c * res
    edges = []
    for p, q in ((pa, pb), (pb, pc), (pc, pa)):
        dx, dy = q[:, 0] - p[:, 0], q[:, 1] - p[:, 1]
        # Counter-clockwise, interior on the left: a centre on a left or top edge
        # is inside. The neighbour sees the edge reversed, so not its left or top.
        own = (dy < 0) | ((dy == 0) & (dx < 0))
        # Measure from the lexicographically smaller end: the neighbour's value
        # is then the exact negative of this one, bit for bit.
        swap = (p[:, 0] > q[:, 0]) | ((p[:, 0] == q[:, 0]) & (p[:, 1] > q[:, 1]))
        origin = np.where(swap[:, None], q, p)
        edges.append((origin[:, 0].copy(), origin[:, 1].copy(), dx, dy, own))
    counts = np.zeros(res * res, dtype=np.int64)
    ends = np.cumsum(samples)
    begins = ends - samples
    for start in range(0, total, LIGHTMAP_CHUNK):
        k = np.arange(start, min(start + LIGHTMAP_CHUNK, total), dtype=np.int64)
        t = np.searchsorted(ends, k, side="right")
        iy, ix = np.divmod(k - begins[t], width[t])
        ix += x0[t]
        iy += y0[t]
        px, py = ix + 0.5, iy + 0.5
        inside = np.ones(len(k), dtype=bool)
        for ox, oy, dx, dy, own in edges:
            value = dx[t] * (py - oy[t]) - dy[t] * (px - ox[t])
            inside &= (value > 0) | ((value == 0) & own[t])
        counts += np.bincount(iy[inside] * res + ix[inside], minlength=res * res)
    return int(np.count_nonzero(counts)), int(np.count_nonzero(counts > 1)), res


def _gltf_is_collision_node(document: dict, node: dict) -> bool:
    """Is this glTF node an Unreal collision shape (UCX_/UBX_/USP_/UCP_)? Its own
    name decides; an unnamed node goes by its mesh's name, as gltf_structure."""
    meshes = document.get("meshes") or []
    mesh = node.get("mesh")
    mesh_name = (meshes[mesh].get("name") or "") if isinstance(mesh, int) \
        and 0 <= mesh < len(meshes) and isinstance(meshes[mesh], dict) else ""
    return _bare_name(node.get("name") or mesh_name or "").lower().startswith(FBX_COLLISION_PREFIXES)


def _gltf_scene_roots(document: dict) -> Optional[list]:
    """Root nodes of the glTF's scene, or None when it has no scene at all."""
    scenes = document.get("scenes") or []
    if not scenes:
        return None
    scene_index = document.get("scene", 0)
    scene = scenes[scene_index] if isinstance(scene_index, int) and 0 <= scene_index < len(scenes) \
        else scenes[0]
    return list(scene.get("nodes", []) or []) if isinstance(scene, dict) else []


def _gltf_render_meshes(document: dict) -> set:
    """Meshes the scene draws (LOD0, as an engine imports it): placed by a node
    the scene reaches, not a collision shape. MSFT_lod's coarser levels sit
    outside the scene, and a mesh no node places is never imported, so neither
    counts - the FBX reader's rule (11 Nov 2026). A document without scenes is
    read whole."""
    meshes = document.get("meshes") or []
    nodes = document.get("nodes") or []
    roots = _gltf_scene_roots(document)
    if roots is None:
        placed = {node.get("mesh") for node in nodes if isinstance(node, dict)
                  and not _gltf_is_collision_node(document, node)}
        unplaced = set(range(len(meshes))) - {node.get("mesh") for node in nodes
                                              if isinstance(node, dict)}
        found = {index for index in placed | unplaced
                 if isinstance(index, int) and 0 <= index < len(meshes)}
        return found - _gltf_collision_meshes(document)
    found, seen, stack = set(), set(), list(roots)
    while stack:
        index = stack.pop()
        if not isinstance(index, int) or not 0 <= index < len(nodes) or index in seen \
                or not isinstance(nodes[index], dict):
            continue                                   # malformed or cyclic: never loop
        seen.add(index)
        node = nodes[index]
        mesh = node.get("mesh")
        if isinstance(mesh, int) and 0 <= mesh < len(meshes) \
                and not _gltf_is_collision_node(document, node):
            found.add(mesh)
        stack.extend(node.get("children", []) or [])
    return found


def _gltf_collision_meshes(document: dict) -> set:
    """Indices of glTF meshes that are collision only (UCX_/UBX_/USP_/UCP_).

    A mesh is collision when every node placing it is named so (or, for an
    unnamed node, the mesh is), as gltf_structure reads them; a mesh no node
    places is judged by its own name.
    """
    meshes = document.get("meshes") or []
    users: dict = {}
    for node in document.get("nodes") or []:
        if isinstance(node, dict) and isinstance(node.get("mesh"), int) \
                and 0 <= node["mesh"] < len(meshes):
            users.setdefault(node["mesh"], []).append(node.get("name") or "")
    found = set()
    for index, mesh in enumerate(meshes):
        own = (mesh.get("name") or "") if isinstance(mesh, dict) else ""
        names = [_bare_name(name or own).lower() for name in users.get(index, [own])]
        if names and all(name.startswith(FBX_COLLISION_PREFIXES) for name in names):
            found.add(index)
    return found


def _lightmap_meshes(path: Path, channel: int) -> dict:
    """The static meshes of an asset and their lightmap UV set.

    {"meshes": [(name, uv points (n, 2), uv triangles (m, 3, 2))], "missing":
    [names of static meshes without the set], "unread": why some were not read}.
    Skinned meshes are left out: a skeletal mesh is not lightmapped. So are
    UCX_/UBX_/USP_/UCP_ collision meshes, which are never rendered.
    """
    found: dict = {"meshes": [], "missing": [], "unread": ""}
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        graph = _fbx_graph(_fbx_tree(path))
        for geometry_id in graph["rendered"]:
            if _fbx_skin_binds(graph, geometry_id):
                continue
            geometry = graph["geometries"][geometry_id]
            index_node = geometry.child("PolygonVertexIndex")
            if index_node is None or not index_node.props:
                continue
            corners, starts, sizes = _fbx_polygons(index_node.props[0])
            if not int(np.clip(sizes - 2, 0, None).sum()):
                continue
            name = _fbx_mesh_label(graph, geometry_id)
            # A UV layer element carries its set index; files without one are
            # read in order, as the sets appear.
            layers = geometry.all("LayerElementUV")
            indexed = [item for item in layers if item.props and isinstance(item.props[0], int)]
            if indexed:
                layer = next((item for item in indexed if item.props[0] == channel), None)
            else:
                layer = layers[channel] if len(layers) > channel else None
            if layer is None:
                found["missing"].append(name)
                continue
            uv = _fbx_layer_values(layer, "UV", "UVIndex", 2, corners)
            if uv is None:
                found["unread"] = (f"'{name}' has a UV set {channel} this version cannot read "
                                   "(unusual mapping)")
                continue
            triangles, _owner = _fbx_fan(starts, sizes)
            found["meshes"].append((name, uv, uv[triangles]))
        return found
    if suffix in GLTF_SUFFIXES:
        document = read_gltf_document(path)
        undecodable = gltf_undecodable_compression(path)
        if undecodable:
            found["unread"] = "the vertex data is compressed in a way this machine cannot decode"
            return found
        buffers: dict = {}
        attribute = f"TEXCOORD_{channel}"
        render = _gltf_render_meshes(document)
        for index, mesh in enumerate(document.get("meshes") or []):
            if index not in render:
                continue          # UCX_ & co, coarser LODs, unplaced: not lightmapped
            name = mesh.get("name") or f"mesh {index}"
            points, triangles, missing = [], [], False
            for primitive in mesh.get("primitives") or []:
                attributes = primitive.get("attributes") or {}
                if any(key.startswith("JOINTS_") for key in attributes):
                    continue                              # skinned: not lightmapped
                if primitive.get("mode", 4) not in _GLTF_TRIANGLE_MODES:
                    continue
                if attribute not in attributes:
                    missing = True
                    continue
                if _gltf_draco(primitive):
                    found["unread"] = f"'{name}' is Draco-compressed"
                    continue
                uv = _accessor_numpy(path, document, attributes[attribute], buffers)[:, :2]
                corners = _gltf_triangles(path, document, primitive, buffers)
                if corners is None or not len(corners):
                    continue
                if int(corners.max()) >= len(uv) or int(corners.min()) < 0:
                    raise AssetError(f"damaged glTF: '{name}' indexes past its {attribute}; "
                                     "re-export it")
                points.append(uv[np.unique(corners)])
                triangles.append(uv[corners])
            if missing:
                found["missing"].append(name)
            if triangles:
                found["meshes"].append((name, np.concatenate(points), np.concatenate(triangles)))
        return found
    if suffix == ".obj":
        if channel > 0:
            found["obj_single_set"] = True
            return found
        scene = load_scene(path)
        items = scene.geometry.items() if hasattr(scene, "geometry") else [(path.stem, scene)]
        for name, geometry in items:
            faces = getattr(geometry, "faces", None)
            if faces is None or not len(faces):
                continue
            uv = getattr(getattr(geometry, "visual", None), "uv", None)
            if uv is None or len(uv) != len(geometry.vertices):
                found["missing"].append(str(name))
                continue
            uv = np.asarray(uv, dtype=np.float64)[:, :2]
            faces = np.asarray(faces, dtype=np.int64)
            found["meshes"].append((str(name), uv[np.unique(faces)], uv[faces]))
    return found


def lightmap_facts(path: Path, channel: int = 1, resolution: int = 256,
                   overlap: bool = True) -> dict:
    """What the [lightmap] rules need about an asset's lightmap UV set.

    {"static": static meshes found, "missing": [names without the set],
    "obj_single_set": an OBJ asked for a set past its one, "uvs": lightmap UVs
    read, "outside": how many lie outside 0-1, "worst": (u, v) farthest outside,
    "overlap": (percent, texels overlapped, texels covered, mesh, grid) of the
    worst mesh or None, "collapsed": [meshes whose set covers no texel],
    "unread": why part of the asset was not read, or ""}.
    """
    if np is None:                                         # pragma: no cover
        raise AssetError("numpy is not installed; lightmap UVs cannot be read")
    return lightmap_facts_of(_lightmap_meshes(path, channel), resolution, overlap)


def lightmap_facts_of(found: dict, resolution: int = 256, overlap: bool = True) -> dict:
    """lightmap_facts() from meshes already read: {"meshes": [(name, uv points,
    uv triangles)], "missing": [names], "unread": "", "obj_single_set": bool}.
    The DCC panels pass what the open scene holds (plugins/preflight_core.py)."""
    facts: dict = {"static": len(found["meshes"]) + len(found["missing"]),
                   "missing": found["missing"], "obj_single_set": found.get("obj_single_set", False),
                   "uvs": 0, "outside": 0, "worst": None, "overlap": None, "collapsed": [],
                   "unread": found["unread"]}
    farthest = 0.0
    for name, points, triangles in found["meshes"]:
        points = points[np.all(np.isfinite(points), axis=1)]
        beyond = np.max(np.stack([-points[:, 0], points[:, 0] - 1.0,
                                  -points[:, 1], points[:, 1] - 1.0], axis=1), axis=1) \
            if len(points) else np.zeros(0)
        outside = beyond > LIGHTMAP_UV_TOLERANCE
        facts["uvs"] += len(points)
        facts["outside"] += int(np.count_nonzero(outside))
        if outside.any():
            worst = int(np.argmax(beyond))
            if beyond[worst] > farthest:
                farthest = float(beyond[worst])
                facts["worst"] = (float(points[worst, 0]), float(points[worst, 1]))
        if overlap and len(triangles):
            covered, overlapped, grid = lightmap_coverage(triangles, resolution)
            if not covered:
                facts["collapsed"].append(name)
                continue
            percent = 100.0 * overlapped / covered
            if facts["overlap"] is None or percent > facts["overlap"][0]:
                facts["overlap"] = (percent, overlapped, covered, name, grid)
    return facts


#: Corners closer than this (in file units) are the same point, whatever the scale.
_COINCIDENT_EDGE_SQUARED = 1e-14


def degenerate_mask(a, b, c, ratio: float = 1e-6, epsilon: float = 0.0):
    """Which triangles (corner arrays a, b, c of shape (n, 3)) are degenerate.

    Flat (area <= ratio x longest edge squared), collapsed (all corners within
    1e-7 units), or - when epsilon is set - no larger than epsilon in area.
    Shape, not size: a well-made 0.1 mm triangle on a real-scale prop is fine.
    """
    ab, bc, ca = b - a, c - b, a - c
    areas = 0.5 * np.linalg.norm(np.cross(ab, -ca), axis=1)
    longest = np.max(np.stack([(ab * ab).sum(axis=1), (bc * bc).sum(axis=1),
                               (ca * ca).sum(axis=1)]), axis=0)
    mask = (areas <= ratio * longest) | (longest <= _COINCIDENT_EDGE_SQUARED)
    if epsilon > 0:
        mask |= areas <= epsilon
    return mask


def count_degenerate_triangles(path: Path, epsilon: float = 0.0, ratio: float = 1e-6) -> int:
    """Triangles that are flat or collapsed (see degenerate_mask)."""
    if path.suffix.lower() in FBX_SUFFIXES:
        return read_fbx(path, epsilon, ratio).degenerate
    if np is None:  # pragma: no cover - numpy ships with trimesh
        LOG.debug("numpy is unavailable; skipping degenerate geometry check")
        return 0

    total = 0
    for geometry in _geometries(render_scene(path, load_scene(path))):
        faces = getattr(geometry, "faces", None)
        vertices = getattr(geometry, "vertices", None)
        if faces is None or vertices is None or len(faces) == 0:
            continue
        vertices = np.asarray(vertices, dtype=np.float64)
        faces = np.asarray(faces)
        if faces.size and (int(faces.max()) >= len(vertices) or int(faces.min()) < 0):
            raise AssetError("could not read mesh: it is damaged (a triangle points past the "
                             f"last of its {len(vertices):,} vertices); re-export it")
        corners = vertices[faces]
        total += int(np.count_nonzero(degenerate_mask(corners[:, 0], corners[:, 1],
                                                      corners[:, 2], ratio, epsilon)))
    return total


def image_size_from_bytes(data: bytes) -> Optional[tuple[int, int]]:
    """(width, height) of an image held in memory, or None if unrecognised.

    Pillow reads PNG/JPEG/TGA/WebP/DDS headers; KTX2 (the GPU format glTF
    uses) is read from its fixed header so no extra library is needed."""
    if data[:12] == b"\xabKTX 20\xbb\r\n\x1a\n" and len(data) >= 28:
        width, height = struct.unpack_from("<II", data, 20)
        return int(width), int(max(height, 1))
    if Image is None:
        return None
    try:
        import io
        with Image.open(io.BytesIO(data)) as image:
            return int(image.size[0]), int(image.size[1])
    except Exception:
        return None


def embedded_texture_sizes(path: Path) -> list[tuple[str, int, int]]:
    """(label, width, height) of every texture packed inside a mesh file.

    glTF: images stored in the .glb binary chunk or as data: URIs. FBX: embedded
    media. External textures are not listed - they are checked as files of
    their own. Packed ones used to be skipped entirely, so a .glb carrying
    4096x4096 maps passed a 2048 budget (6 Nov corpus test)."""
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        return [tuple(item) for item in read_fbx(path).embedded_textures]
    if suffix not in GLTF_SUFFIXES:
        return []
    document = read_gltf_document(path)
    images = document.get("images") or []
    if not images:
        return []
    views = document.get("bufferViews") or []
    binary: Optional[bytes] = None
    found: list[tuple[str, int, int]] = []
    for number, image in enumerate(images, start=1):
        if not isinstance(image, dict):
            continue
        label = str(image.get("name") or "") or f"image {number} of {len(images)}"
        data: Optional[bytes] = None
        uri = image.get("uri")
        if isinstance(uri, str) and uri.startswith("data:") and "," in uri:
            try:
                data = base64.b64decode(uri.split(",", 1)[1])
            except ValueError:
                data = None
        elif isinstance(image.get("bufferView"), int) and suffix == ".glb":
            view_index = image["bufferView"]
            if 0 <= view_index < len(views):
                if binary is None:
                    try:
                        binary = _glb_binary_chunk(path)
                    except (AssetError, OSError, struct.error):
                        binary = b""
                view = views[view_index]
                start = int(view.get("byteOffset", 0) or 0)
                data = binary[start:start + int(view.get("byteLength", 0) or 0)]
        if not data:
            continue
        size = image_size_from_bytes(data)
        if size is not None:
            found.append((label, size[0], size[1]))
    return found


def _exr_size(data: bytes) -> Optional[tuple[int, int]]:
    """Width and height from an OpenEXR header's dataWindow attribute."""
    if data[:4] != b"\x76\x2f\x31\x01":
        return None
    position = 8
    while position < len(data):
        end = data.find(b"\0", position)
        if end <= position:
            return None
        name = data[position:end]
        type_end = data.find(b"\0", end + 1)
        if type_end < 0 or type_end + 5 > len(data):
            return None
        size = struct.unpack_from("<i", data, type_end + 1)[0]
        value = type_end + 5
        if name == b"dataWindow" and size == 16 and value + 16 <= len(data):
            x_min, y_min, x_max, y_max = struct.unpack_from("<4i", data, value)
            return x_max - x_min + 1, y_max - y_min + 1
        position = value + max(size, 0)
    return None


def measure_texture(path: Path) -> tuple[int, int]:
    """A texture's resolution, after proving the whole file decodes."""
    suffix = path.suffix.lower()
    if suffix in HEADER_ONLY_TEXTURES:
        try:
            with path.open("rb") as handle:
                head = handle.read(1 << 16)
        except OSError as exc:
            raise AssetError(f"could not read image ({exc})") from exc
        size = _exr_size(head) if suffix == ".exr" else image_size_from_bytes(head[:64])
        if size is None or min(size) <= 0:
            raise AssetError(f"could not read the {suffix} header (damaged or not really {suffix})")
        return size
    if Image is None:
        raise AssetError("Pillow is not installed; textures cannot be analysed")
    try:
        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format
            # Decode the pixels too. The header alone said a half-written PNG/JPG/TGA
            # was a perfectly good 1024x1024 texture; decoding is what notices the
            # missing second half ("image file is truncated").
            image.load()
    except Image.DecompressionBombError as exc:  # pragma: no cover - extreme input
        raise AssetError(f"image dimensions are unsafe to inspect ({exc})") from exc
    except OSError as exc:
        raise AssetError(f"could not read image ({exc})") from exc

    suffix = path.suffix.lower()
    expected = TEXTURE_FORMATS.get(suffix, "PNG")
    if image_format != expected:
        raise AssetError(f"file has a {suffix or '.png'} extension but is actually {image_format}")
    return width, height


def is_power_of_two(value: int) -> bool:
    """True for 1, 2, 4, ... 1024, 2048, 4096."""
    return value > 0 and value & (value - 1) == 0


def _suffix_token(suffix: str) -> str:
    """``_BaseColor`` -> a pattern that also accepts base_color, base-color, basecolor."""
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", suffix.strip("_- ."))
    if not words:
        words = [suffix.strip("_- .")]
    return r"[_\-.]?".join(re.escape(word) for word in words)


def build_naming_pattern(suffixes: Sequence[str]) -> Optional[re.Pattern[str]]:
    """Case-insensitive ``_Albedo``-style suffix matcher.

    Separators inside a suffix are optional (``_BaseColor`` matches
    ``_base_color``), the suffix itself may follow ``_`` ``-`` or ``.``, and a
    trailing index is tolerated (``_Albedo_02``) because exported atlases and
    variant sets legitimately number their maps.
    """
    tokens = [_suffix_token(suffix) for suffix in suffixes if suffix.strip("_- .")]
    if not tokens:
        return None
    return re.compile(rf"[_\-.](?:{'|'.join(tokens)})(?:[_\-]?\d+)?$", re.IGNORECASE)


def _starts_with_any(name: str, prefixes: Sequence[str]) -> bool:
    return not prefixes or any(name.lower().startswith(prefix.lower()) for prefix in prefixes)


def _full_match(pattern: str, name: str) -> bool:
    return not pattern or re.fullmatch(pattern, name) is not None


def mesh_name_problem(name: str, rules) -> str:
    """The FAILED reason when a mesh name breaks the naming rules, else ''.

    ``rules`` is a Budget (its [naming] conventions, or mesh_name_prefixes) or a
    plain list of prefixes. One rule for every caller: the gate passes the file
    name, the DCC panels pass the object name. Case-insensitive prefixes."""
    conventions = tuple(getattr(rules, "naming_conventions", ()) or ())
    if conventions:
        relevant = [c for c in conventions if c[4] or c[5]]
        if not relevant or any(_starts_with_any(name, c[4]) and _full_match(c[5], name)
                               for c in relevant):
            return ""
        wanted = sorted({prefix for c in relevant for prefix in c[4]})
        shown = " or ".join(wanted) if wanted else "the naming pattern"
        return (f"mesh name '{name}' does not follow the naming rules (start with {shown}"
                f"; conventions: {', '.join(c[0] for c in relevant)})")
    prefixes = getattr(rules, "mesh_name_prefixes", rules) or ()
    if not prefixes:
        return ""
    if _starts_with_any(name, prefixes):
        return ""
    return f"mesh name '{name}' does not start with {' or '.join(prefixes)}"


def texture_name_problem(stem: str, budget) -> str:
    """The FAILED reason when a texture name breaks the naming rules, else ''."""
    if not getattr(budget, "enforce_texture_naming", True):
        return ""
    conventions = tuple(getattr(budget, "naming_conventions", ()) or ())
    relevant = [c for c in conventions if c[1] or c[2] or c[3]]
    if relevant:
        for label, prefixes, suffixes, pattern, _mesh_prefixes, _mesh_pattern in relevant:
            suffix_pattern = build_naming_pattern(suffixes) if suffixes else None
            if (_starts_with_any(stem, prefixes)
                    and (suffix_pattern is None or suffix_pattern.search(stem))
                    and _full_match(pattern, stem)):
                return ""
        return ("name follows none of the naming conventions ("
                + "; ".join(_describe_convention(c) for c in relevant) + ")")
    pattern = build_naming_pattern(budget.texture_suffixes)
    if pattern is not None and not pattern.search(stem):
        return f"name does not end with a standard suffix ({describe_suffixes(budget.texture_suffixes)})"
    return ""


def _describe_convention(convention) -> str:
    label, prefixes, suffixes, pattern = convention[:4]
    parts = []
    if prefixes:
        parts.append("starts " + "/".join(prefixes))
    if suffixes:
        parts.append("ends " + describe_suffixes(suffixes, shown=4))
    if pattern:
        parts.append(f"matches {pattern}")
    return f"{label}: " + ", ".join(parts)


def describe_suffixes(suffixes: Sequence[str], shown: int = 6) -> str:
    """The suffix list for a FAILED line: the first few, then how many more."""
    listed = ", ".join(suffixes[:shown])
    return listed + (f" and {len(suffixes) - shown} more" if len(suffixes) > shown else "")


#: Unity's LOD naming (Rock_LOD0, Rock_LOD1, ...): case-insensitive, "_LOD<n>" at the end.
LOD_NAME = re.compile(r"^(?P<base>.+?)_LOD(?P<level>\d+)$", re.IGNORECASE)


#: A UCX_ hull with a vertex further than this fraction of its bounding-box
#: diagonal in front of one of its own faces is not convex. 1%: a dent of 1.7 cm
#: in a 1 m crate. Smaller dents (a slightly bent quad) change nothing in play.
HULL_CONCAVITY_TOLERANCE = 0.01


#: A hull thinner than this fraction of its diagonal is flat: no volume to build.
HULL_FLAT_TOLERANCE = 1e-4


#: The convexity test compares every vertex with every face; above this many
#: comparisons (a hull far too big to be a hull) it is not run.
HULL_TEST_LIMIT = 20_000_000


def _bare_name(name: str) -> str:
    """An object name without its DCC namespace ("props:Rock" -> "Rock"), as Unreal reads it."""
    return str(name or "").rsplit(":", 1)[-1]


def lod_chains_by_name(items) -> list[tuple[str, tuple[int, ...]]]:
    """LOD chains from (object name, triangles) pairs named <base>_LOD<n>.

    A chain is LOD0, LOD1, ... up to the first missing number; an object named
    just <base> stands in for a missing _LOD0. Only chains of two levels or more
    are returned: a lone Rock_LOD0 is a mesh without LODs.
    """
    plain: dict[str, int] = {}
    chains: dict[str, dict[int, int]] = {}
    labels: dict[str, str] = {}
    for name, triangles in items:
        name = _bare_name(name)
        match = LOD_NAME.match(name)
        if match is None:
            plain[name.lower()] = plain.get(name.lower(), 0) + int(triangles)
            continue
        key = match.group("base").lower()
        labels.setdefault(key, match.group("base"))
        level = int(match.group("level"))
        levels = chains.setdefault(key, {})
        levels[level] = levels.get(level, 0) + int(triangles)
    found = []
    for key, levels in chains.items():
        if 0 not in levels and key in plain:
            levels[0] = plain[key]
        counts = []
        while len(counts) in levels:
            counts.append(levels[len(counts)])
        if len(counts) >= 2:
            found.append((labels[key], tuple(counts)))
    return sorted(found)


def hull_shape(points, triangles) -> tuple[int, Optional[float], bool]:
    """(distinct vertices, concavity, flat) of one collision mesh.

    Concavity is how far the worst vertex lies in front of one of the mesh's own
    faces, as a fraction of its bounding-box diagonal: 0 for a convex hull (every
    vertex on or behind every face plane). The test does not care which way a
    face is wound: it asks whether a face plane has vertices on both sides. None
    when it could not be run (no faces, or too big to compare).
    """
    if np is None:                                       # pragma: no cover
        return len(points), None, False
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if not len(points):
        return 0, None, True
    unique, inverse = np.unique(points, axis=0, return_inverse=True)
    inverse = np.asarray(inverse).reshape(-1)
    count = len(unique)
    diagonal = float(np.linalg.norm(unique.max(axis=0) - unique.min(axis=0)))
    if count < 4 or diagonal <= 0:
        return count, None, True
    centred = unique - unique.mean(axis=0)
    thinnest = np.linalg.svd(centred, full_matrices=False)[2][-1]
    spread = centred @ thinnest
    if float(spread.max() - spread.min()) <= HULL_FLAT_TOLERANCE * diagonal:
        return count, None, True
    triangles = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    if not len(triangles) or int(triangles.max()) >= len(points) or int(triangles.min()) < 0:
        return count, None, False
    corners = unique[inverse[triangles]]
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    keep = lengths > 1e-12 * diagonal * diagonal
    if not keep.any() or count * int(keep.sum()) > HULL_TEST_LIMIT:
        return count, None, False
    normals = normals[keep] / lengths[keep, None]
    offsets = np.einsum("ij,ij->i", corners[keep, 0], normals)
    worst = 0.0
    block = max(1, 1_000_000 // count)
    for start in range(0, len(normals), block):
        distances = unique @ normals[start:start + block].T - offsets[start:start + block]
        both_sides = np.minimum(distances.max(axis=0), -distances.min(axis=0))
        worst = max(worst, float(both_sides.max()))
    return count, round(max(worst, 0.0) / diagonal, 6), False
