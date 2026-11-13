"""AssetGatekeeper, part 4 of 7: the Gatekeeper: references, PBR values, boundaries, LODs, collision and every check.

Split out of asset_gatekeeper.py on 11 Nov 2026 with no change in behaviour;
asset_gatekeeper.py still exposes every name. See agk_gate/__init__.py.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import unquote

from agk_gate.common import (
    engine_module, GLTF_SUFFIXES, HEADER_ONLY_TEXTURES, Image, LOG, MESH_SUFFIXES,
    TEXTURE_SUFFIXES, walk_files,
)
from agk_gate.config import (
    AssetError, Budget, BudgetException, DEFAULT_TAG_SIMILARITY, EXCEPTION_WARN_DAYS,
    is_normal_name, _mb, mesh_memory_bytes, resolve_tag, TAG_MODES, texture_memory_bytes,
    Verdict,
)
from agk_gate.readers import (
    _bare_name, build_naming_pattern, count_degenerate_triangles, count_material_slots,
    count_triangles, count_uv_channels, _describe_unexpected, embedded_texture_sizes,
    FBX_COLLISION_PREFIXES, _fbx_reference_target, FBX_SUFFIXES, gltf_triangle_counts,
    gltf_undecodable_compression, gltf_uses_draco, HULL_CONCAVITY_TOLERANCE, hull_shape,
    is_power_of_two, lightmap_facts, lod_chains_by_name, max_bone_influences, measure_texture,
    mesh_facts, mesh_name_problem, _name_list, obj_colour_source, _ordinal, _percent,
    _primitive_triangles, read_accessor, read_fbx, read_gltf_document, skeleton_facts,
    texture_name_problem,
)


# --- #
# The gate itself
# --- #

# --- #
# Reference & hierarchy auditing
# --- #

# What counts as a texture on disk when hunting orphans. Wider than the gated
# suffixes on purpose: an orphaned .tga is still an orphan.
TEXTURE_DISK_SUFFIXES = (".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".exr", ".webp")

# Material lines in an .mtl that point at a file (map_Kd foo.png, bump foo.png ...)
_MTL_MAP_KEYS = ("map_kd", "map_ka", "map_ks", "map_bump", "bump", "map_ns", "map_d",
                 "map_pr", "norm", "refl")


def _is_external_uri(uri: object) -> bool:
    """True for a path we must resolve on disk (not a data URI, not a URL)."""
    if not isinstance(uri, str) or not uri.strip():
        return False
    lowered = uri.lower()
    return not lowered.startswith(("data:", "http://", "https://", "blob:", "//"))


def _gltf_external_uris(document: dict) -> list[str]:
    """Buffer and image URIs - the two places glTF stores a sibling file."""
    uris: list[str] = []
    for section in ("buffers", "images"):
        for entry in document.get(section, []) or []:
            uri = entry.get("uri") if isinstance(entry, dict) else None
            if _is_external_uri(uri):
                uris.append(unquote(str(uri).split("?")[0]))
    return uris


def _mtl_texture_uris(mtl_path: Path) -> list[str]:
    """Texture files named inside a .mtl. Missing .mtl files are reported by the caller."""
    uris: list[str] = []
    try:
        text = mtl_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return uris
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        if parts[0].lower() in _MTL_MAP_KEYS:
            # `map_Kd -s 1 1 1 foo.png`: the filename is the last token.
            uris.append(parts[-1].strip('"'))
    return uris


def collect_references(path: Path) -> list[str]:
    """Every external file this asset points at, exactly as written in the file.

    glTF: ``buffers[].uri`` and ``images[].uri``. OBJ: ``mtllib`` plus the maps
    referenced *by* those .mtl files (one level, which is how OBJ works).
    """
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        try:
            return [written for written, _ in read_fbx(path).references]
        except AssetError as exc:
            LOG.debug("cannot read references from %s: %s", path.name, exc)
            return []
    if suffix in (".gltf", ".glb"):
        try:
            return _gltf_external_uris(read_gltf_document(path))
        except Exception as exc:
            LOG.debug("cannot read references from %s: %s", path.name, exc)
            return []
    if suffix != ".obj":
        return []

    references: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return references
    for line in text.splitlines():
        if not line.lower().startswith("mtllib "):
            continue
        for name in line.split()[1:]:
            name = name.strip('"')
            references.append(name)
            material_path = (path.parent / name)
            if material_path.is_file():
                references.extend(_mtl_texture_uris(material_path))
    return references


def resolve_references(path: Path) -> list[tuple[str, Path]]:
    """[(as written, resolved absolute path)] for every external reference."""
    base = path.parent
    if path.suffix.lower() in FBX_SUFFIXES:
        try:
            summary = read_fbx(path)
        except AssetError:
            return []
        return [(written, _fbx_reference_target(path, candidates))
                for written, candidates in summary.references]
    resolved = []
    for written in collect_references(path):
        # Normalise Windows-style separators coming out of a DCC.
        normalised = written.replace("\\", "/")
        resolved.append((written, (base / normalised)))
    return resolved


def missing_references(path: Path) -> list[tuple[str, Path]]:
    """References that point at a file which is not there."""
    return [(written, target) for written, target in resolve_references(path)
            if not target.is_file()]


def escaping_references(path: Path, root: Optional[Path]) -> list[tuple[str, Path]]:
    """References that resolve outside the project folder.

    ``../../shared/tex.png`` or a hardcoded ``C:/Users/.../wood.png`` is either a
    mistaken export path or an attempt to read a file the project never shipped.
    Either way the build machine will not have it, and everything that follows a
    reference on trust - the dashboard bridge, the workstation importer - walks
    out of the repository on the asset's say-so.

    ``root is None`` (single-file mode) means there is no boundary to escape, so
    nothing is reported.
    """
    if root is None:
        return []
    try:
        base = Path(root).resolve()
    except OSError:                       # pragma: no cover - defensive
        return []
    escaping: list[tuple[str, Path]] = []
    for written, target in resolve_references(path):
        try:
            resolved = Path(target).resolve()
        except OSError:
            escaping.append((written, target))
            continue
        if resolved != base and base not in resolved.parents:
            escaping.append((written, target))
    return escaping


def orphaned_files(root: Path) -> list[Path]:
    """Texture files in the repository that no mesh references.

    Repository-level, not per-asset: an orphan is only visible when you compare
    every asset against every texture on disk.
    """
    root = Path(root)
    on_disk = walk_files(root)
    referenced: set[Path] = set()
    for candidate in on_disk:
        if candidate.suffix.lower() not in MESH_SUFFIXES:
            continue
        for _written, target in resolve_references(candidate):
            try:
                referenced.add(target.resolve())
            except OSError:      # pragma: no cover - unreadable path
                continue
    orphans = []
    for candidate in on_disk:
        if candidate.suffix.lower() not in TEXTURE_DISK_SUFFIXES:
            continue
        try:
            if candidate.resolve() not in referenced:
                orphans.append(candidate)
        except OSError:          # pragma: no cover
            continue
    return sorted(orphans)


# --- #
# PBR value sanitisation
# --- #

def _luma(pixel: tuple[int, int, int]) -> float:
    """Rec. 709 luminance on the 0..1 scale the shader will see."""
    return (0.2126 * pixel[0] + 0.7152 * pixel[1] + 0.0722 * pixel[2]) / 255.0


def pbr_violation(path: Path, budget: Budget) -> Optional[str]:
    """Is this albedo inside the range a physically based shader can use?

    Sampled at 64x64: a 4k texture costs the same as a 512 to inspect, and an
    albedo that is uniformly black or white is uniform at any resolution.
    """
    if Image is None:            # pragma: no cover - Pillow is optional
        return None
    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((64, 64), Image.BILINEAR)
            # Pillow 12+ renamed it (getdata goes in Pillow 14, 2027-10).
            flatten = getattr(image, "get_flattened_data", None) or image.getdata
            pixels = list(flatten())
    except Exception as exc:     # unreadable: the texture check already fails it
        LOG.debug("pbr: cannot sample %s: %s", path.name, exc)
        return None
    if not pixels:
        return None

    luminances = [_luma(pixel) for pixel in pixels]
    mean = sum(luminances) / len(luminances)
    if mean < budget.pbr_floor:
        return (f"albedo mean luminance {mean:.3f} is below the PBR floor "
                f"{budget.pbr_floor:g} (near-black absorbs all light)")
    if mean > budget.pbr_ceiling:
        return (f"albedo mean luminance {mean:.3f} is above the PBR ceiling "
                f"{budget.pbr_ceiling:g} (near-white blinds the lighting model)")

    # A texture can average mid-grey and still be two clipped extremes.
    extreme = sum(1 for value in luminances if value <= 0.01 or value >= 0.99)
    if extreme / len(luminances) > 0.9:
        return (f"{extreme * 100 // len(luminances)}% of albedo pixels are clipped to "
                f"black or white (mean {mean:.3f}); the surface has no usable variance")
    return None


# --- #
# Geometric boundary profiling
# --- #

def _node_translation(node: dict) -> tuple[float, float, float]:
    """Translation from a TRS node or from a 4x4 matrix, in metres."""
    translation = node.get("translation")
    if isinstance(translation, list) and len(translation) == 3:
        try:
            return (float(translation[0]), float(translation[1]), float(translation[2]))
        except (TypeError, ValueError):
            return (0.0, 0.0, 0.0)
    matrix = node.get("matrix")
    if isinstance(matrix, list) and len(matrix) == 16:
        try:
            return (float(matrix[12]), float(matrix[13]), float(matrix[14]))
        except (TypeError, ValueError):
            return (0.0, 0.0, 0.0)
    return (0.0, 0.0, 0.0)


def transform_audit(path: Path, budget: Budget) -> list[str]:
    """Unbaked node transforms, floating/off-centre pivots, unindexed geometry.

    Only called when ``budget.audit_transforms`` (or ``require_indexed``) is on:
    an offset pivot is often authored on purpose, so this is a review prompt,
    not a hard violation.
    """
    problems: list[str] = []
    suffix = path.suffix.lower()

    if suffix in (".gltf", ".glb"):
        try:
            document = read_gltf_document(path)
        except Exception:
            return problems

        for node in document.get("nodes", []) or []:
            if not isinstance(node, dict):
                continue
            x, y, z = _node_translation(node)
            distance = (x * x + y * y + z * z) ** 0.5
            if distance > budget.transform_epsilon:
                name = node.get("name") or f"node {node.get('mesh', '?')}"
                problems.append(
                    f"'{name}' has an unbaked translation ({x:.3f}, {y:.3f}, {z:.3f}); "
                    f"the pivot is floating, so engine placement will double the offset")
            rotation = node.get("rotation")
            if isinstance(rotation, list) and len(rotation) == 4:
                try:
                    w = float(rotation[3])
                except (TypeError, ValueError):
                    w = 1.0
                if abs(abs(w) - 1.0) > 1e-3:
                    name = node.get("name") or f"node {node.get('mesh', '?')}"
                    problems.append(f"'{name}' carries an unbaked rotation; bake it into the mesh")

        if budget.audit_transforms:
            accessors = document.get("accessors", []) or []
            for mesh in document.get("meshes", []) or []:
                for primitive in mesh.get("primitives", []) or []:
                    position = (primitive.get("attributes") or {}).get("POSITION")
                    if not isinstance(position, int) or position >= len(accessors):
                        continue
                    accessor = accessors[position]
                    low, high = accessor.get("min"), accessor.get("max")
                    if not (isinstance(low, list) and isinstance(high, list) and len(low) == 3):
                        continue
                    center = [(low[i] + high[i]) / 2.0 for i in range(3)]
                    size = [(high[i] - low[i]) for i in range(3)]
                    diagonal = (size[0] ** 2 + size[1] ** 2 + size[2] ** 2) ** 0.5 or 1.0
                    offset = (center[0] ** 2 + center[1] ** 2 + center[2] ** 2) ** 0.5
                    if offset / diagonal > budget.pivot_tolerance:
                        problems.append(
                            f"geometry is centred {offset:.3f}m from its own origin "
                            f"({offset / diagonal * 100:.0f}% of its size); the pivot is uncentred")

        if budget.require_indexed:
            for mesh in document.get("meshes", []) or []:
                for primitive in mesh.get("primitives", []) or []:
                    if "indices" not in primitive:
                        name = mesh.get("name") or "unnamed mesh"
                        problems.append(f"'{name}' has unindexed geometry (no index buffer)")
        return problems

    if suffix != ".obj":
        return problems

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return problems
    vertices = 0
    triangles = 0
    low = [float("inf")] * 3
    high = [float("-inf")] * 3
    for line in text.splitlines():
        if line.startswith("v "):
            parts = line.split()
            if len(parts) >= 4:
                vertices += 1
                for axis in range(3):
                    try:
                        value = float(parts[axis + 1])
                    except ValueError:
                        continue
                    low[axis] = min(low[axis], value)
                    high[axis] = max(high[axis], value)
        elif line.startswith("f "):
            triangles += max(0, len(line.split()) - 3)

    if triangles and vertices:
        if budget.audit_transforms and low[0] != float("inf"):
            center = [(low[i] + high[i]) / 2.0 for i in range(3)]
            size = [(high[i] - low[i]) for i in range(3)]
            diagonal = (size[0] ** 2 + size[1] ** 2 + size[2] ** 2) ** 0.5 or 1.0
            offset = (center[0] ** 2 + center[1] ** 2 + center[2] ** 2) ** 0.5
            if offset / diagonal > budget.pivot_tolerance:
                problems.append(
                    f"geometry is centred {offset:.3f}m from its own origin "
                    f"({offset / diagonal * 100:.0f}% of its size); the pivot is uncentred")
        if budget.require_indexed:
            # An OBJ is indexed by definition; the useful question is whether the
            # exporter welded its vertices or emitted a triangle soup (3 per tri).
            ratio = vertices / (3.0 * triangles)
            if ratio > 0.95:
                problems.append(
                    f"unindexed geometry: {vertices:,} vertices for {triangles:,} triangles "
                    f"(a welded mesh needs about a third of that)")
    return problems


# --- #
# LOD levels and collision ([lod], [collision]): what Unreal and Unity import
# --- #


def _gltf_points_and_triangles(path: Path, document: dict, mesh: dict):
    """POSITION points and triangle indices of one glTF mesh; triangles None if unreadable."""
    accessors = document.get("accessors") or []
    points: list = []
    triangles: Optional[list] = []
    for primitive in mesh.get("primitives") or []:
        position = (primitive.get("attributes") or {}).get("POSITION")
        if not isinstance(position, int) or not 0 <= position < len(accessors):
            continue
        accessor = accessors[position]
        compressed = (accessor.get("bufferView") is None
                      or "KHR_draco_mesh_compression" in (primitive.get("extensions") or {}))
        if compressed:
            # No readable positions: count them, but they cannot be tested.
            points.extend([(float(len(points) + i), 0.0, 0.0)
                           for i in range(int(accessor.get("count", 0) or 0))])
            triangles = None
            continue
        offset = len(points)
        points.extend(read_accessor(path, document, position))
        mode = primitive.get("mode", 4)
        if triangles is None or mode != 4:
            triangles = None
            continue
        index = primitive.get("indices")
        flat = ([item[0] for item in read_accessor(path, document, index)]
                if isinstance(index, int) else list(range(len(points) - offset)))
        triangles.extend((offset + flat[i], offset + flat[i + 1], offset + flat[i + 2])
                         for i in range(0, len(flat) - 2, 3))
    return points, triangles


def gltf_structure(path: Path, document: dict) -> dict:
    """LOD chains, render-mesh names, collision shapes and skinning of one glTF.

    LOD chains come from the MSFT_lod node extension (its ``ids`` are the
    coarser levels, which sit outside the scene); without it, from node names
    ending _LOD0, _LOD1 ... Collision meshes are nodes (or, for an unnamed node,
    meshes) named UCX_/UBX_/USP_/UCP_, as Unreal's glTF import reads them.
    """
    nodes = document.get("nodes") or []
    meshes = document.get("meshes") or []
    per_mesh = [sum(_primitive_triangles(document, primitive)
                    for primitive in (mesh.get("primitives") or []))
                if isinstance(mesh, dict) else 0 for mesh in meshes]

    def node(index):
        return nodes[index] if isinstance(index, int) and 0 <= index < len(nodes) \
            and isinstance(nodes[index], dict) else None

    def mesh_of(index):
        item = node(index)
        mesh = item.get("mesh") if item else None
        return mesh if isinstance(mesh, int) and 0 <= mesh < len(meshes) else None

    def name_of(index):
        item = node(index) or {}
        mesh = mesh_of(index)
        mesh_name = meshes[mesh].get("name", "") if mesh is not None and isinstance(meshes[mesh], dict) else ""
        return _bare_name(item.get("name") or mesh_name or "")

    def walk(roots):
        order, seen, stack = [], set(), list(reversed(list(roots)))
        while stack:
            index = stack.pop()
            if node(index) is None or index in seen:
                continue                             # malformed or cyclic: never loop
            seen.add(index)
            order.append(index)
            stack.extend(reversed([child for child in node(index).get("children", []) or []]))
        return order

    scenes = document.get("scenes") or []
    scene_index = document.get("scene", 0)
    if scenes:
        scene = scenes[scene_index] if isinstance(scene_index, int) and 0 <= scene_index < len(scenes) \
            else scenes[0]
        reachable = walk((scene or {}).get("nodes", []) if isinstance(scene, dict) else [])
    else:
        reachable = walk(range(len(nodes)))

    def is_collision(index):
        return name_of(index).lower().startswith(FBX_COLLISION_PREFIXES)

    def subtree_triangles(index):
        return sum(per_mesh[mesh_of(item)] for item in walk([index])
                   if mesh_of(item) is not None and not is_collision(item))

    lods = []
    for index in reachable:
        extension = ((node(index) or {}).get("extensions") or {}).get("MSFT_lod")
        ids = extension.get("ids") if isinstance(extension, dict) else None
        if isinstance(ids, list) and ids:
            levels = [index] + [item for item in ids if node(item) is not None]
            lods.append((name_of(index) or f"node {index}",
                         tuple(subtree_triangles(item) for item in levels), "MSFT_lod"))
    if not lods:
        named = [(name_of(index), per_mesh[mesh_of(index)]) for index in reachable
                 if mesh_of(index) is not None and not is_collision(index)]
        lods = [(base, levels, "names") for base, levels in lod_chains_by_name(named)]

    render_names, shapes, skinned = set(), [], False
    for index in reachable:
        mesh = mesh_of(index)
        if "skin" in (node(index) or {}):
            skinned = True
        if mesh is None:
            continue
        if any("JOINTS_0" in (primitive.get("attributes") or {})
               for primitive in (meshes[mesh].get("primitives") or [])):
            skinned = True
        if is_collision(index):
            points, triangles = _gltf_points_and_triangles(path, document, meshes[mesh])
            if triangles is None:
                shapes.append((name_of(index), len(points), None, False))
            else:
                shapes.append((name_of(index), *hull_shape(points, triangles)))
            continue
        render_names.update(name for name in (_bare_name((node(index) or {}).get("name", "")),
                                               _bare_name(meshes[mesh].get("name", ""))) if name)
    return {"lod_groups": tuple(lods), "render_names": tuple(sorted(render_names)),
            "collision_shapes": tuple(shapes), "skinned": skinned}


def asset_structure(path: Path) -> Optional[dict]:
    """LOD chains and collision of a mesh file, or None for a format with neither (.obj)."""
    suffix = path.suffix.lower()
    if suffix in FBX_SUFFIXES:
        summary = read_fbx(path)
        return {"lod_groups": summary.lod_groups, "render_names": summary.render_names,
                "collision_shapes": summary.collision_shapes, "skinned": summary.skinned}
    if suffix in GLTF_SUFFIXES:
        try:
            return gltf_structure(path, read_gltf_document(path))
        except AssetError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OSError) as exc:
            raise AssetError(f"damaged glTF ({_describe_unexpected(exc)}); re-export it") from exc
    return None


def collision_target(name: str, render_names: Sequence[str]) -> bool:
    """Whether Unreal attaches collision mesh *name* to one of *render_names*.

    Unreal's FBX import reads UCX_<RenderMeshName> and UCX_<RenderMeshName>_<suffix>
    (its documentation: UCX_Tree_01_00 and UCX_Tree_01_01 both belong to Tree_01).
    Names compare exactly, case included, as Unreal compares them.
    """
    _prefix, _sep, rest = _bare_name(name).partition("_")
    candidates = {rest}
    if "_" in rest:
        candidates.add(rest.rsplit("_", 1)[0])
    candidates.discard("")
    return bool(candidates & set(render_names))


def _share(fraction: float) -> str:
    """12%, 3.4%: a fraction (0-1) as a percentage, for the LOD and collision lines."""
    return f"{fraction:.0%}" if fraction >= 0.1 else f"{fraction:.1%}"


def lod_collision_problems(path: Path, structure: Optional[dict], budget: "Budget",
                           ) -> tuple[list[str], list[str], list[str], list[tuple[str, int]]]:
    """(problems, warnings, metrics, stats) of the [lod] and [collision] rules."""
    problems: list[str] = []
    warnings: list[str] = []
    metrics: list[str] = []
    stats: list[tuple[str, int]] = []
    suffix = path.suffix.lower()
    lod_on = budget.lod_min_levels > 0 or budget.lod_max_ratio > 0
    collision_on = (budget.collision_require or budget.collision_name_match
                    or budget.collision_convex or budget.collision_max_hulls > 0
                    or budget.collision_max_hull_vertices > 0)

    if lod_on:
        chains = tuple((structure or {}).get("lod_groups", ()))
        levels = min((len(chain[1]) for chain in chains), default=1)
        shortest = min(chains, key=lambda chain: len(chain[1]), default=None)
        stats.append(("lod_levels", levels))
        if shortest is not None:
            stats.extend((f"lod{level}_triangles", count) for level, count in enumerate(shortest[1]))
        metrics.append(f"{levels} LODs" if levels > 1 else "no LODs")
        if any(chain[2] == "names" for chain in chains) and suffix in FBX_SUFFIXES:
            warnings.append("LODs found by name only (_LOD1, ...): Unity builds an LOD group from "
                            "these, but Unreal reads an FBX's LODs only from an LOD group")
        need = budget.lod_min_levels
        if need and levels < need:
            missing = f"LOD{levels}" + (f"-LOD{need - 1}" if need - 1 > levels else "")
            if not chains:
                how = {".obj": ".obj cannot carry LODs: export FBX with an LOD group"}.get(
                    suffix, "export an LOD group" if suffix in FBX_SUFFIXES
                    else "add them as MSFT_lod levels or as nodes named <mesh>_LOD1, ...")
                problems.append(f"no LODs (LOD0 only); at least {need} levels are required: "
                                f"add {missing} ({how})")
            else:
                problems.append(f"{levels} LOD levels in '{shortest[0]}'; at least {need} are "
                                f"required: add {missing}")
        limit = budget.lod_max_ratio
        if limit:
            for name, counts, _source in chains:
                for level in range(1, len(counts)):
                    above, here = counts[level - 1], counts[level]
                    if here == 0:
                        problems.append(f"LOD{level} of '{name}' has no triangles: re-export it "
                                        "or remove the level")
                    elif above and here / above > limit:
                        # The largest count that passes: int(0.29 * 100) is 28 in
                        # floating point, yet 29 / 100 passes a 0.29 limit.
                        allowed = int(limit * above)
                        while (allowed + 1) / above <= limit:
                            allowed += 1
                        problems.append(
                            f"LOD{level} of '{name}' keeps {here / above:.2f} of LOD{level - 1}'s "
                            f"triangles ({here:,} of {above:,}); at most {limit:g} is allowed: "
                            f"reduce it to {allowed:,} triangles or fewer")

    if collision_on:
        if structure is None:
            if budget.collision_require:
                warnings.append(f"collision not checked: {suffix or 'this format'} carries no "
                                "collision Unreal reads; export FBX for UCX_ collision")
            return problems, warnings, metrics, stats
        shapes = tuple(structure.get("collision_shapes", ()))
        render_names = tuple(structure.get("render_names", ()))
        hulls = [shape for shape in shapes if shape[0].upper().startswith("UCX_")]
        stats.append(("collision_shapes", len(shapes)))
        if hulls:
            stats.append(("collision_hulls", len(hulls)))
            stats.append(("max_hull_vertices", max(shape[1] for shape in hulls)))
        metrics.append(f"{len(shapes)} collision")
        if budget.collision_require and not shapes and not structure.get("skinned"):
            example = render_names[0] if render_names else path.stem
            problems.append(f"no collision: a static mesh needs a UCX_ (or UBX_/USP_/UCP_) mesh "
                            f"named after its render mesh, such as UCX_{example}_00 (a skinned mesh "
                            "is exempt: it uses a physics asset)")
        if budget.collision_name_match and render_names:
            orphans = [shape[0] for shape in shapes if not collision_target(shape[0], render_names)]
            if orphans:
                prefix = orphans[0].split("_", 1)[0].upper()
                problems.append(
                    f"collision mesh{'es' if len(orphans) > 1 else ''} {_name_list(orphans)} "
                    f"match{'' if len(orphans) > 1 else 'es'} no render mesh, so Unreal ignores "
                    f"{'them' if len(orphans) > 1 else 'it'} on import: name "
                    f"{'each' if len(orphans) > 1 else 'it'} {prefix}_<mesh> or {prefix}_<mesh>_00 "
                    f"after {'one of ' if len(render_names) > 1 else ''}{_name_list(render_names)} "
                    "(case matters)")
        if budget.collision_max_hulls and len(hulls) > budget.collision_max_hulls:
            problems.append(f"{len(hulls)} convex hulls (UCX_) exceed the {budget.collision_max_hulls} "
                            f"hull budget: merge or remove {len(hulls) - budget.collision_max_hulls}")
        cap = budget.collision_max_hull_vertices
        if cap:
            heavy = [shape for shape in hulls if shape[1] > cap]
            if heavy:
                named = ", ".join(f"'{shape[0]}' ({shape[1]:,} vertices)" for shape in heavy[:3])
                if len(heavy) > 3:
                    named += f" and {len(heavy) - 3} more"
                problems.append(f"collision hull{'s' if len(heavy) > 1 else ''} {named} "
                                f"{'are' if len(heavy) > 1 else 'is'} over the {cap}-vertex hull "
                                f"limit: simplify {'them' if len(heavy) > 1 else 'it'}")
        if budget.collision_convex:
            flat = [shape[0] for shape in hulls if shape[3]]
            dented = [shape for shape in hulls if not shape[3] and shape[2] is not None
                      and shape[2] > HULL_CONCAVITY_TOLERANCE]
            untested = [shape[0] for shape in hulls if not shape[3] and shape[2] is None]
            if flat:
                problems.append(f"collision hull{'s' if len(flat) > 1 else ''} {_name_list(flat)} "
                                f"{'are' if len(flat) > 1 else 'is'} flat (no volume): Unreal "
                                "cannot build a convex hull from a plane; give it thickness")
            for name, _vertices, concavity, _flat in dented:
                problems.append(f"collision hull '{name}' is not convex (a dent {_share(concavity)} "
                                "of its size): Unreal replaces it with its convex hull on import, "
                                "filling the dent; split it into convex pieces "
                                f"({name.split('_', 1)[0]}_<mesh>_00, _01, ...)")
            if untested:
                warnings.append(f"collision hull{'s' if len(untested) > 1 else ''} "
                                f"{_name_list(untested)} not tested for convexity (compressed, "
                                "not triangles, or too large to compare)")
    return problems, warnings, metrics, stats


def skeleton_problems(skeleton: dict, budget: "Budget", problems: list, warnings: list,
                      metrics: list, stats: list) -> None:
    """[skeleton] findings from skeleton facts ({"bones", "roots", "depth",
    "deepest", "sections": [(material, bones)], "unread_sections"}): what
    skeleton_facts() reads from a file and the DCC panels from the open scene,
    so both say the same words for the same rig."""
    bones, roots, depth = skeleton["bones"], skeleton["roots"], skeleton["depth"]
    metrics.append(f"{bones} bones")
    stats.extend((("bones", bones), ("root_bones", len(roots)), ("bone_depth", depth)))
    if budget.max_bones and bones > budget.max_bones:
        problems.append(f"{bones:,} bones in the skeleton exceeds the {budget.max_bones:,} "
                        f"bone budget (+{bones - budget.max_bones:,}): remove helper or "
                        "unused bones before export, or split the character")
    sections = skeleton["sections"]
    if skeleton["unread_sections"]:
        sections = []      # not counted, rather than counted as 0 bones
        warnings.append("bones per material section not counted: the skin weights are "
                        "compressed, and this check reads plain vertex data only")
    if sections:
        stats.append(("max_bones_per_section", max(count for _label, count in sections)))
    limit = budget.max_bones_per_section
    over = sorted(((count, label) for label, count in sections if limit and count > limit),
                  reverse=True)
    if over:
        count, label = over[0]
        where = (f"material section '{label}' is skinned to {count} bones" if len(over) == 1
                 else f"{len(over)} material sections are skinned to more than {limit} bones "
                      f"(worst '{label}': {count})")
        problems.append(f"{where}, over the {limit} bones-per-section limit: the engine "
                        "splits such a section into extra draw calls (Unreal on mobile "
                        "allows 75); weight it to fewer bones or give some of its faces "
                        "another material")
    if budget.single_root_bone and len(roots) != 1:
        problems.append(f"skeleton has {len(roots)} root bones ({_name_list(roots)}): Unreal "
                        "imports one root only (\"Multiple roots are found in the bone "
                        "hierarchy\"); parent every bone under a single root bone")
    if budget.max_bone_depth and depth > budget.max_bone_depth:
        problems.append(f"bone chain is {depth} bones deep (root to "
                        f"'{skeleton['deepest']}'), over the {budget.max_bone_depth} depth "
                        "limit: shorten the chain or remove in-between bones")


def lightmap_problems(facts: dict, budget: "Budget", problems: list, warnings: list,
                      metrics: list, stats: list) -> None:
    """[lightmap] findings from lightmap_facts() / lightmap_facts_of(), for the
    gate and the DCC panels alike."""
    channel = budget.lightmap_uv_channel
    wanted_overlap = budget.lightmap_max_overlap >= 0
    label = f"UV channel {channel}, the {_ordinal(channel + 1)} set"
    if facts["unread"]:
        warnings.append(f"lightmap UVs not fully checked: {facts['unread']}")
    if budget.lightmap_require:
        if facts["obj_single_set"]:
            problems.append(f"no lightmap UV set: an OBJ file holds one UV set, so {label} "
                            "cannot exist; export FBX or glTF with a lightmap UV set, or set "
                            "[lightmap] uv_channel = 0 if the first set is the lightmap")
            stats.append(("lightmap_missing", 1))
        elif facts["missing"]:
            missing = facts["missing"]
            which = (f"static mesh '{missing[0]}'" if len(missing) == 1
                     else f"{len(missing)} static meshes ({_name_list(missing)})")
            problems.append(f"no lightmap UV set ({label}) on {which}: add a UV set unwrapped "
                            "for lightmaps (no overlaps, inside 0-1), or switch off "
                            "[lightmap] require to let the engine generate one")
            stats.append(("lightmap_missing", len(missing)))
    if budget.lightmap_inside_unit_square and facts["uvs"]:
        stats.append(("lightmap_uvs_outside", facts["outside"]))
        if facts["outside"]:
            u, v = facts["worst"]
            problems.append(f"{facts['outside']:,} of {facts['uvs']:,} lightmap UVs ({label}) "
                            f"are outside 0-1 (worst at u {u:.3f}, v {v:.3f}): a lightmap does "
                            "not tile, so those texels get another surface's light; move "
                            "every lightmap island inside the 0-1 square")
    if wanted_overlap:
        collapsed = facts["collapsed"]
        if collapsed:
            which = (f"'{collapsed[0]}'" if len(collapsed) == 1
                     else f"{len(collapsed)} meshes ({_name_list(collapsed)})")
            problems.append(f"lightmap UVs ({label}) of {which} cover no texel of a "
                            f"{budget.lightmap_resolution}x{budget.lightmap_resolution} grid "
                            "(collapsed or far too small): unwrap the lightmap UVs again")
        if facts["overlap"] is not None:
            percent, overlapped, covered, name, grid = facts["overlap"]
            stats.extend((("lightmap_overlap_texels", overlapped), ("lightmap_texels", covered)))
            metrics.append(f"lightmap {_percent(percent)}% overlap")
            if grid < budget.lightmap_resolution:
                warnings.append(f"lightmap overlap measured on a {grid}x{grid} grid, not "
                                f"{budget.lightmap_resolution}x{budget.lightmap_resolution}: "
                                "its triangles cover the square many times over")
            if overlapped and percent > budget.lightmap_max_overlap:
                problems.append(f"lightmap UVs ({label}) overlap on {_percent(percent)}% of "
                                f"their texels in '{name}' ({overlapped:,} of {covered:,} on a "
                                f"{grid}x{grid} grid), over the "
                                f"{budget.lightmap_max_overlap:g}% limit: overlapping islands "
                                "share baked light and bleed into each other; repack the "
                                "lightmap UVs so no islands overlap")


#: Texels of one texture the Nintendo 64's 4 KB texture memory (TMEM) holds,
#: by the format its colours allow: 16 colours or fewer load as CI4 (4 bits a
#: texel; the palette takes half of TMEM), 256 or fewer as CI8 (8 bits; the
#: palette again half), more as RGBA16 (16 bits, the whole 4 KB).
N64_TMEM_TEXELS = {"CI4": 4096, "CI8": 2048, "RGBA16": 2048}


def n64_tmem_check(path: Path, width: int, height: int) -> tuple:
    """(format, problem): the N64 format a texture's colours allow, and why it
    does not fit the 4 KB texture memory ("" if it does)."""
    if Image is None or path.suffix.lower() in HEADER_ONLY_TEXTURES:
        return "", ""
    try:
        with Image.open(path) as image:
            colours = image.convert("RGBA").getcolors(maxcolors=257)
    except (OSError, ValueError):
        return "", ""
    count = len(colours) if colours is not None else 257
    kind = "CI4" if count <= 16 else "CI8" if count <= 256 else "RGBA16"
    limit = N64_TMEM_TEXELS[kind]
    texels = width * height
    if texels <= limit:
        return kind, ""
    many = "over 256" if colours is None else f"{count}"
    if kind == "CI4":
        advice = "the most any N64 texture can have; make it smaller (64x64, 128x32)"
    else:
        smaller = "reduce it to 16 colours (CI4: 4,096 texels)" if texels <= 4096 else \
            "make it smaller"
        advice = f"{smaller} or keep it to {limit:,} texels (64x32)"
    return kind, (f"{width}x{height} ({texels:,} texels, {many} colours: {kind}) does not fit "
                  f"the N64's 4 KB texture memory - {kind} holds {limit:,} texels; {advice}")


class Gatekeeper:
    """Applies the budget rules and returns a Verdict. No I/O side effects.

    The single exception is healing, and it is opt-in: pass a ``Healer`` and a
    failing asset is repaired *only if* the repaired copy then verifies as
    passing. With ``healer=None`` (the default) this class never writes a byte.
    """

    def __init__(self, budget: Budget, healer: Optional[object] = None,
                 overrides: Optional[dict] = None,
                 tag_budgets: Optional[dict[str, Budget]] = None,
                 tag_mode: str = "suggest",
                 tag_similarity: float = DEFAULT_TAG_SIMILARITY,
                 tag_root: Optional[Path] = None,
                 root: Optional[Path] = None,
                 exceptions: Sequence["BudgetException"] = (),
                 today: Optional[date] = None) -> None:
        self.budget = budget
        self.exceptions = tuple(exceptions)
        self.today = today
        # file -> the path [[exceptions]] see it under (the P4 trigger's temp copy
        # stands for a depot file: --exception-path)
        self.exception_paths: dict = {}
        # Measure mesh memory even without a per-asset budget: [[totals]] add it up.
        self.want_memory = False
        self.healer = healer
        self.tag_budgets = tag_budgets or {}
        self.tag_mode = tag_mode if tag_mode in TAG_MODES else "suggest"
        # CLI flags, re-applied on top of any tag budget: the documented order
        # is flags last, and a tag must not silently outrank an explicit flag.
        self.overrides = dict(overrides or {})
        self.tag_similarity = tag_similarity
        self.tag_root = tag_root
        # The project boundary, for the reference-escape check. Separate from
        # tag_root only so a caller can point tagging at a category root while
        # the boundary stays the watch folder.
        self.root = root if root is not None else tag_root
        self._naming = build_naming_pattern(budget.texture_suffixes) if budget.enforce_texture_naming else None

    def _exceptions_for(self, path: Path, budget: Budget) -> tuple[Budget, list[str], list[str]]:
        """Apply the active exceptions for this path; describe the expired ones."""
        if not self.exceptions:
            return budget, [], []
        today = self.today or date.today()
        notes: list[str] = []
        expired: list[str] = []
        changes: dict[str, object] = {}
        seen_as = self.exception_paths.get(path, path)
        for exception in self.exceptions:
            if not exception.matches(seen_as):
                continue
            if exception.active(today):
                changes[exception.rule] = exception.allow
                left = (exception.expires - today).days
                soon = (f" - ends in {left} day{'s' if left != 1 else ''}"
                        if left <= EXCEPTION_WARN_DAYS else "")
                notes.append(f"exception: {exception.label()} until {exception.expires.isoformat()} "
                             f"({exception.owner}: {exception.reason}){soon}")
            else:
                expired.append(f"the exception {exception.label()} ({exception.owner}) expired on "
                               f"{exception.expires.isoformat()}")
        if changes:
            budget = replace(budget, **changes)
        return budget, notes, expired

    def check(self, path: Path) -> Verdict:
        budget, tag, warning = self._budget_for(path)
        budget, exception_notes, expired = self._exceptions_for(path, budget)
        previous = self.budget
        if budget is not previous:
            # One asset is inspected at a time (single worker thread), so a
            # scoped swap is safe and avoids threading a budget through every
            # analyzer signature.
            self.budget = budget
            self._naming = (build_naming_pattern(budget.texture_suffixes)
                            if budget.enforce_texture_naming else None)
        try:
            try:
                verdict = self._inspect(path)
            except (KeyboardInterrupt, SystemExit, MemoryError):
                raise
            except Exception as exc:
                # One damaged file must never stop a folder scan, a watch, a
                # trigger or a report: it becomes an ERROR verdict like any
                # other unreadable file, and the run carries on (6 Nov corpus:
                # an out-of-range glTF index used to abort --once outright).
                LOG.debug("unexpected error inspecting %s", path, exc_info=True)
                verdict = Verdict(path, (f"could not be checked ({_describe_unexpected(exc)})",),
                                  unreadable=True)
            if verdict.problems and self.healer is not None:
                verdict = self._heal(path, verdict)
        finally:
            self.budget = previous
            self._naming = (build_naming_pattern(previous.texture_suffixes)
                            if previous.enforce_texture_naming else None)

        notes = list(verdict.warnings) + exception_notes
        if expired and verdict.problems:
            # Only when it fails: an asset that now fits the normal budget does
            # not need its old waiver, expired or not.
            verdict = Verdict(verdict.path, verdict.problems + tuple(expired), verdict.metric,
                              verdict.stats, verdict.heal_notes, verdict.warnings,
                              verdict.unreadable)
        if warning:
            if self.tag_mode == "strict":
                # Strict mode turns a near miss into a failure, with the fix in
                # the message: that is the whole point of not guessing politely.
                problems = (warning + " (strict tag mode: rename the folder to match)",) + verdict.problems
                return Verdict(verdict.path, problems, verdict.metric, verdict.stats,
                               verdict.heal_notes, tuple(notes), verdict.unreadable)
            notes.append(warning)
        if notes:
            # unreadable rides along: an ERROR with a note attached is still an
            # ERROR, not a FAILED budget.
            return Verdict(verdict.path, verdict.problems, verdict.metric, verdict.stats,
                           verdict.heal_notes, tuple(notes), verdict.unreadable)
        return verdict

    def _budget_for(self, path: Path) -> tuple[Budget, Optional[str], Optional[str]]:
        if not self.tag_budgets:
            return self.budget, None, None
        tag, warning = resolve_tag(path, self.tag_budgets, self.tag_root, self.tag_similarity)
        if tag is None:
            return self.budget, None, None
        if self.tag_mode == "strict" and warning:
            # Do not apply a guess in strict mode; let the caller fail it.
            return self.budget, tag, warning
        # CLI flags win over a tag budget (documented order: flags last).
        if self.overrides:
            try:
                return replace(self.tag_budgets[tag], **self.overrides), tag, warning
            except TypeError:
                return self.tag_budgets[tag], tag, warning
        return self.tag_budgets[tag], tag, warning

    def _heal(self, path: Path, verdict: Verdict) -> Verdict:
        """Attempt a repair, then re-inspect with the same budget.

        ``_inspect`` (not ``check``) is the checker on purpose: the healer's
        output must be judged by the rules, never by the healer itself.
        """
        try:
            result = self.healer.heal(path, self._inspect)  # type: ignore[attr-defined]
        except Exception as exc:  # healing must never turn a FAILED into a crash
            LOG.warning("healing %s failed: %s", path.name, exc)
            return verdict
        if result is None:
            return verdict          # nothing safe to do; the original is untouched
        if getattr(result, "dry_run", False):
            # Verified in staging, nothing written: still FAILED on disk, and
            # the notes say what a real heal would do.
            return Verdict(verdict.path, verdict.problems, verdict.metric, verdict.stats,
                           (), verdict.warnings + tuple(
                               f"--heal would: {note}" for note in result.modifications))
        healed = self._inspect(result.path)
        if healed.problems:         # belt and braces: never report a lie
            LOG.warning("healing %s did not satisfy the budget: %s",
                        result.path.name, healed.reason)
            return verdict
        return Verdict(result.path, (), healed.metric, healed.stats, result.modifications)

    def _inspect(self, path: Path) -> Verdict:
        suffix = path.suffix.lower()
        if suffix in MESH_SUFFIXES:
            return self._check_mesh(path)
        if suffix in TEXTURE_SUFFIXES:
            return self._check_texture(path)
        return Verdict(path)

    # - meshes - #

    def _check_mesh(self, path: Path) -> Verdict:
        try:
            triangles = count_triangles(path)
        except AssetError as exc:
            return Verdict(path, (str(exc),))

        problems: list[str] = []
        warnings: list[str] = []
        metrics = [f"{triangles:,} tris"]
        stats: list[tuple[str, int]] = [("triangles", triangles)]
        if path.suffix.lower() in GLTF_SUFFIXES:
            # Repeated parts (one wheel placed twice) count every time - Unreal
            # bakes them into the mesh - and the unique count is kept for the
            # dashboard and --json.
            try:
                unique = gltf_triangle_counts(read_gltf_document(path))[0]
            except Exception:
                unique = triangles
            if unique != triangles:
                stats.append(("unique_triangles", unique))
        naming = mesh_name_problem(path.stem, self.budget)
        if naming:
            problems.append(naming)

        if triangles == 0:
            # A zero-triangle mesh is never a legitimate export: it means a
            # truncated write, an empty collection or a broken DCC round-trip.
            problems.append("mesh contains no triangles (empty or corrupt export)")
        else:
            limit = self.budget.max_triangles
            if triangles > limit:
                problems.append(
                    f"{triangles:,} triangles exceeds the {limit:,} budget (+{triangles - limit:,})"
                )

        try:
            slots = count_material_slots(path)
        except AssetError as exc:
            problems.append(str(exc))
        else:
            metrics.append(f"{slots} mat")
            stats.append(("material_slots", slots))
            limit = self.budget.max_material_slots
            if slots > limit:
                problems.append(
                    f"{slots:,} material slots exceeds the {limit:,} slot budget (draw calls)"
                )

        undecodable = gltf_undecodable_compression(path)
        if undecodable:
            warnings.append(undecodable)
        if gltf_uses_draco(path):
            # Measured 6 Nov: Unreal Engine 5.3's glTF importer created nothing
            # from a Draco-compressed .glb (duck.glb) and reported no error.
            warnings.append("compressed with Draco: Unreal Engine 5.3 imports nothing from such a "
                            "file; re-export it without Draco compression for Unreal")
        if path.suffix.lower() == ".obj":
            colour = obj_colour_source(path)
            if colour:
                what = "ZBrush Polypaint" if colour == "polypaint" else "vertex colours"
                warnings.append(
                    f"its colour is {what} (one colour per vertex), not a texture: Maya, 3ds Max "
                    "and most engines show it grey, and reducing the mesh loses it. Bake it into "
                    f"a texture with: asset-gatekeeper \"{path.name}\" --bake-polypaint")

        try:
            influences = 0 if undecodable else max_bone_influences(path)
        except AssetError as exc:
            problems.append(str(exc))
        else:
            if influences:
                metrics.append(f"{influences} inf")
                stats.append(("max_bone_influences", influences))
                limit = self.budget.max_bone_influences
                if influences > limit:
                    problems.append(
                        f"{influences} bone weights on one vertex exceeds the {limit} influence limit"
                    )

        try:
            uv_channels = count_uv_channels(path)
        except AssetError as exc:
            problems.append(str(exc))
        else:
            if uv_channels:
                metrics.append(f"{uv_channels} uv")
                stats.append(("uv_channels", uv_channels))
                limit = self.budget.max_uv_channels
                if uv_channels > limit:
                    problems.append(f"{uv_channels} UV channels exceeds the {limit} channel limit")

        # [skeleton] and [lightmap]: off by default, read only when a rule is on.
        if self.budget.skeleton_checked:
            self._check_skeleton(path, problems, warnings, metrics, stats)
        if self.budget.lightmap_checked:
            self._check_lightmap(path, problems, warnings, metrics, stats)

        if (not self.budget.allow_degenerate and self.budget.degenerate_mode != "allow"
                and not undecodable):
            try:
                degenerate = count_degenerate_triangles(path, self.budget.degenerate_epsilon,
                                                        self.budget.degenerate_ratio)
            except AssetError as exc:
                problems.append(str(exc))
            else:
                stats.append(("degenerate_triangles", degenerate))
                if degenerate:
                    message = (f"{degenerate:,} degenerate triangles (flat or zero-area) - Nanite "
                               f"and ray tracing compute garbage from these")
                    (warnings if self.budget.degenerate_mode == "warn" else problems).append(message)

        try:
            packed = embedded_texture_sizes(path)
        except AssetError:
            packed = []
        if packed:
            stats.append(("embedded_textures", len(packed)))
            edge = self.budget.max_texture_edge
            too_big = [(label, width, height) for label, width, height in packed
                       if max(width, height) > edge]
            not_pot = [(label, width, height) for label, width, height in packed
                       if self.budget.require_power_of_two
                       and not (is_power_of_two(width) and is_power_of_two(height))]
            # Up to three are named one per line; more become one line, so a
            # file with 100 packed textures (NodePerformanceTest.glb) is not a
            # hundred-line verdict.
            # The N64's texture memory holds 4,096 texels at most (16 colours);
            # packed textures are not decoded here, so only that ceiling is checked.
            over_tmem = [(label, width, height) for label, width, height in packed
                         if self.budget.n64_tmem and width * height > N64_TMEM_TEXELS["CI4"]]
            for found, wording in ((too_big, f"exceeds the {edge}x{edge} maximum resolution"),
                                   (not_pot, "is not a power of two"),
                                   (over_tmem, "has more texels than the N64's 4 KB texture "
                                               "memory holds at any colour depth (4,096)")):
                if len(found) <= 3:
                    problems.extend(f"embedded texture '{label}' {width}x{height} {wording}"
                                    for label, width, height in found)
                else:
                    named = ", ".join(f"'{label}' {width}x{height}" for label, width, height in found[:3])
                    verb = (wording.replace("exceeds", "exceed").replace("is not a power", "are not powers")
                            .replace("has more", "have more"))
                    problems.append(f"{len(found)} embedded textures {verb} ({named} and "
                                    f"{len(found) - 3} more)")

        # Memory (G2) and texel density (G8): measured when budgeted, or when a
        # [[totals]] line needs the number.
        want_memory = self.budget.max_mesh_mb > 0 or self.want_memory
        want_texel = self.budget.texel_density_max > 0
        if want_memory or want_texel:
            try:
                if path.suffix.lower() in FBX_SUFFIXES:
                    facts = engine_module("fbx_preview").measure(path, self.budget.texel_texture)
                else:
                    facts = mesh_facts(path, self.budget.texel_texture)
            except Exception as exc:          # a measurement must never break the verdict
                warnings.append(f"mesh memory / texel density not measured ({exc})")
            else:
                known = dict(stats)
                if want_memory:
                    size = mesh_memory_bytes(facts["vertices"], triangles,
                                             int(known.get("uv_channels", 0)),
                                             int(known.get("max_bone_influences", 0)) > 0,
                                             facts["colours"],
                                             self.budget.mesh_lightmap_uv,
                                             self.budget.mesh_depth_index_buffer)
                    stats.append(("memory_kb", size // 1024))
                    stats.append(("vertices", facts["vertices"]))
                    if self.budget.max_mesh_mb:
                        metrics.append(f"~{_mb(size)}")
                        if size > self.budget.max_mesh_mb * 1024 * 1024:
                            problems.append(
                                f"~{_mb(size)} estimated GPU memory exceeds the "
                                f"{self.budget.max_mesh_mb:g} MB mesh budget "
                                f"({facts['vertices']:,} vertices)")
                if want_texel:
                    density = facts["texel_density"]
                    if density is None:
                        warnings.append("no UVs: texel density not checked")
                    else:
                        stats.append(("texel_density", int(round(density))))
                        metrics.append(f"{density:,.0f} px/m")
                        assumed = (f" (assuming a {self.budget.texel_texture}px texture)"
                                   if facts["assumed_texture"] else "")
                        low, high = self.budget.texel_density_min, self.budget.texel_density_max
                        if density < low:
                            problems.append(f"texel density {density:,.0f} px/m is below the "
                                            f"{low:,.0f} px/m minimum{assumed}")
                        elif density > high:
                            problems.append(f"texel density {density:,.0f} px/m is above the "
                                            f"{high:,.0f} px/m maximum{assumed}")
                        if facts["without_uv"]:
                            warnings.append(f"{facts['without_uv']} mesh(es) without UVs are "
                                            "not in the texel density")

        # Broken references are unconditional when enabled: a .gltf pointing at a
        # texture nobody shipped fails to load in every engine, on every platform.
        if self.budget.check_references:
            # One line per missing file: an FBX that names ALPHA.png from two
            # materials used to report it twice (Head_69.fbx, 6 Nov).
            seen_written: set = set()
            broken = [item for item in missing_references(path)
                      if not (item[0] in seen_written or seen_written.add(item[0]))]
            broken_names = {written for written, _target in broken}
            for written, _target in broken:
                problems.append(
                    f"missing reference '{written}' (broken link: the engine cannot load it)")
            if broken:
                stats.append(("broken_references", len(broken)))
            # A reference that leaves the watch folder is a build-machine failure
            # waiting to happen, and a path-traversal hazard for anything that
            # follows it on trust. Studios with a shared library opt out in rules.
            escaped = [item for item in escaping_references(path, self.root)
                       if item[0] not in broken_names]
            if escaped:
                if self.budget.allow_outside_references:
                    warnings.extend(f"reference '{written}' points outside the project "
                                    "folder ([mesh] allow_outside_references is on)"
                                    for written, _target in escaped)
                else:
                    problems.extend(
                        f"reference '{written}' points outside the project folder "
                        "(the build machine will not have it; set "
                        "allow_outside_references in [mesh] if it is a shared library)"
                        for written, _target in escaped)
                stats.append(("outside_references", len(escaped)))

        # Transform/pivot/index audits are opt-in (see Budget.audit_transforms).
        if self.budget.audit_transforms or self.budget.require_indexed:
            try:
                problems.extend(transform_audit(path, self.budget))
            except Exception as exc:      # an audit must never break the verdict
                LOG.debug("transform audit failed for %s: %s", path.name, exc)

        # LODs and collision ([lod], [collision]): opt-in, every key off by default.
        budget = self.budget
        if triangles and (budget.lod_min_levels or budget.lod_max_ratio or budget.collision_require
                          or budget.collision_name_match or budget.collision_convex
                          or budget.collision_max_hulls or budget.collision_max_hull_vertices):
            try:
                found = lod_collision_problems(path, asset_structure(path), budget)
            except AssetError as exc:
                problems.append(f"LODs / collision not read ({exc})")
            else:
                problems.extend(found[0])
                warnings.extend(found[1])
                metrics.extend(found[2])
                stats.extend(found[3])

        return Verdict(path, tuple(problems), " / ".join(metrics), tuple(stats),
                       warnings=tuple(warnings))

    def _check_skeleton(self, path: Path, problems: list, warnings: list, metrics: list,
                        stats: list) -> None:
        """[skeleton]: bones, bones per material section, roots and chain depth."""
        budget = self.budget
        try:
            skeleton = skeleton_facts(path)
        except AssetError as exc:
            problems.append(str(exc))
            return
        except Exception as exc:              # a skeleton read must never break the verdict
            warnings.append(f"skeleton not checked ({_describe_unexpected(exc)})")
            return
        if not skeleton or not skeleton["bones"]:
            return                            # nothing skinned: the rules do not apply
        skeleton_problems(skeleton, budget, problems, warnings, metrics, stats)

    def _check_lightmap(self, path: Path, problems: list, warnings: list, metrics: list,
                        stats: list) -> None:
        """[lightmap]: an authored lightmap UV set on static meshes, inside 0-1, no overlap."""
        budget = self.budget
        channel = budget.lightmap_uv_channel
        wanted_overlap = budget.lightmap_max_overlap >= 0
        try:
            facts = lightmap_facts(path, channel, budget.lightmap_resolution, wanted_overlap)
        except AssetError as exc:
            problems.append(str(exc))
            return
        except Exception as exc:              # a lightmap read must never break the verdict
            warnings.append(f"lightmap UVs not checked ({_describe_unexpected(exc)})")
            return
        lightmap_problems(facts, budget, problems, warnings, metrics, stats)

    # - textures - #

    def _check_texture(self, path: Path) -> Verdict:
        try:
            width, height = measure_texture(path)
        except AssetError as exc:
            return Verdict(path, (str(exc),))

        problems: list[str] = []

        limit = self.budget.max_texture_edge
        if max(width, height) > limit:
            problems.append(f"{width}x{height} exceeds the {limit}x{limit} maximum resolution")

        if self.budget.require_power_of_two and not (is_power_of_two(width) and is_power_of_two(height)):
            problems.append(f"{width}x{height} is not a power of two")

        n64_format = ""
        if self.budget.n64_tmem:
            n64_format, tmem_problem = n64_tmem_check(path, width, height)
            if tmem_problem:
                problems.append(tmem_problem)

        # GPU memory in the target's compressed format, mips included (G2).
        texture_format = (self.budget.normal_format if is_normal_name(path)
                          else self.budget.texture_format)
        memory = texture_memory_bytes(width, height, texture_format)
        memory_metric = ""
        if self.budget.max_texture_mb:
            memory_metric = f" ~{_mb(memory)} {texture_format.upper()}"
            if memory > self.budget.max_texture_mb * 1024 * 1024:
                problems.append(f"~{_mb(memory)} as {texture_format.upper()} with mips exceeds the "
                                f"{self.budget.max_texture_mb:g} MB texture budget")

        if self._naming is not None:
            naming_problem = texture_name_problem(path.stem, self.budget)
            if naming_problem:
                problems.append(naming_problem)

        # PBR sanitisation applies to albedo/diffuse only: a normal map is
        # allowed to be flat violet-blue, and a roughness map is allowed to be
        # pure white, because neither is a reflectance colour.
        if self.budget.pbr_check and self._is_pbr(path):
            violation = pbr_violation(path, self.budget)
            if violation:
                problems.append(violation)

        return Verdict(path, tuple(problems),
                       f"{width}x{height}{memory_metric}" + (f" {n64_format}" if n64_format else ""),
                       (("width", width), ("height", height), ("memory_kb", memory // 1024)))

    def _is_pbr(self, path: Path) -> bool:
        if path.suffix.lower() in HEADER_ONLY_TEXTURES:
            return False              # sizes come from the header; pixels are not decoded
        stem = path.stem.lower()
        return any(stem.endswith(suffix.lower()) for suffix in self.budget.pbr_suffixes)
