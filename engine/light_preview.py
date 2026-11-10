"""A lighter copy of a big model for the review page's first look.

The developer's test sculpt (1.08 million triangles, 90 MB) took about 70 seconds to
appear in the 3D view, with nothing on screen meanwhile (9 Nov 2026 audit).
Above LIGHT_TRIANGLES the Art Workstation now also stores
``.preview/preview_light.glb``: the same model reduced to about
LIGHT_TARGET triangles, with its vertex colours (ZBrush Polypaint) carried
over. The viewer shows that first, says so, and offers "Show full detail".

The reduction is quadric (fast-simplification, one of the bake extras), which
keeps a face recognisable; the vertex-clustering decimator the LOD preview uses
does not. Without fast-simplification there is no light copy: the full model
loads as before, now with a progress bar. Materials and UVs are not kept - a
light copy is shape and colour, which is what a first look needs.

Standard library + numpy + trimesh (the gate's own dependencies) +
fast-simplification when installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

#: Models with more triangles than this get a light copy.
LIGHT_TRIANGLES = 500_000
#: About how many triangles the light copy has.
LIGHT_TARGET = 200_000
LIGHT_NAME = "preview_light.glb"


def available() -> bool:
    """Is the reducer installed (and loadable on this Python)?"""
    try:
        __import__("fast_simplification")
    except Exception:
        return False
    return True


def _load(path: Path):
    """(positions, faces, colours or None) of a .glb/.gltf/.obj, as numpy arrays."""
    import numpy as np
    suffix = path.suffix.lower()
    if suffix == ".obj":
        import polypaint                          # engine/ is on sys.path, as for the server
        try:
            obj = polypaint.read_obj(path)
            return obj.vertices, obj.faces, obj.colours
        except polypaint.PolypaintError:
            pass                                  # no colours: trimesh reads it plain
    import trimesh
    loaded = trimesh.load(str(path), force="mesh", process=False)
    positions = np.asarray(loaded.vertices, np.float64)
    faces = np.asarray(loaded.faces, np.int64)
    colours = None
    visual = getattr(loaded, "visual", None)
    if visual is not None and getattr(visual, "kind", None) == "vertex":
        rgba = np.asarray(visual.vertex_colors)
        if len(rgba) == len(positions):
            colours = rgba[:, :3].astype(np.float32) / 255.0
    return positions, faces, colours


def make(source: Path, target: Path, target_triangles: Optional[int] = None) -> Optional[dict]:
    """Write the light copy of ``source`` to ``target``; returns {"triangles",
    "source_triangles"}, or None when there is nothing to gain (already small,
    or the reducer is not installed)."""
    import numpy as np
    if not available():
        return None
    import fast_simplification
    import trimesh
    if target_triangles is None:
        target_triangles = LIGHT_TARGET
    positions, faces, colours = _load(Path(source))
    if len(faces) <= max(target_triangles, 1) * 1.2:
        return None
    reduction = 1.0 - target_triangles / len(faces)
    if colours is None:
        low_positions, low_faces = fast_simplification.simplify(
            positions.astype(np.float32), faces.astype(np.int32), target_reduction=reduction)
        low_colours = None
    else:
        _, _, collapses = fast_simplification.simplify(
            positions.astype(np.float32), faces.astype(np.int32), target_reduction=reduction,
            return_collapses=True)
        low_positions, low_faces, mapping = fast_simplification.replay_simplification(
            positions.astype(np.float32), faces.astype(np.int32), collapses)
        mapping = np.asarray(mapping, np.int64)
        # Each kept vertex wears the average colour of the sculpt vertices it
        # absorbed: eyes and brows blur a little but keep their place.
        total = np.zeros((len(low_positions), 3), np.float64)
        np.add.at(total, mapping, colours.astype(np.float64))
        count = np.bincount(mapping, minlength=len(low_positions)).astype(np.float64)
        low_colours = (total / np.maximum(count, 1.0)[:, None]).clip(0.0, 1.0)
    low_positions = np.asarray(low_positions, np.float64)
    low_faces = np.asarray(low_faces, np.int64)
    if not len(low_faces):
        return None
    kwargs = {}
    if low_colours is not None:
        rgba = np.concatenate([(low_colours * 255).round().astype(np.uint8),
                               np.full((len(low_colours), 1), 255, np.uint8)], axis=1)
        kwargs["vertex_colors"] = rgba
    mesh = trimesh.Trimesh(vertices=low_positions, faces=low_faces, process=False, **kwargs)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_name(target.name + ".part")
    mesh.export(scratch, file_type="glb")
    scratch.replace(target)
    return {"triangles": int(len(low_faces)), "source_triangles": int(len(faces))}
