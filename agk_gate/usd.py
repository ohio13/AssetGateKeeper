"""OpenUSD (.usd, .usda, .usdc, .usdz) read through Pixar's open-source reader.

The reader is the ``usd-core`` package (Apache 2.0): optional, installed by
Install_Extras.bat. Without it a USD file fails with that install hint rather
than being skipped, so nobody believes a USD asset was checked when it was not.

What is measured matches the other formats: triangles (a face of n corners is
n - 2), material slots (distinct bound materials, GeomSubsets included, plus one
for any mesh with none), UV sets (texture-coordinate primvars), the worst
per-vertex skin influence count, flat or collapsed triangles, the files the
stage needs that are not there, and the images packed in a .usdz.
"""

from __future__ import annotations

import os
import sys
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from agk_gate.common import np

USD_SUFFIXES = frozenset({".usd", ".usda", ".usdc", ".usdz"})

#: What a missing reader tells the user.
INSTALL_HINT = ("reading USD needs the free OpenUSD reader: run Install_Extras.bat, "
                "or python -m pip install usd-core")

#: Image types a .usdz may carry (the USDZ spec allows PNG and JPEG; EXR and
#: AVIF are seen too).
_PACKED_IMAGES = (".png", ".jpg", ".jpeg", ".exr", ".avif")


class UsdUnavailable(RuntimeError):
    """usd-core is not installed."""


@dataclass(frozen=True)
class UsdSummary:
    triangles: int
    material_slots: int
    uv_channels: int
    max_influences: int
    degenerate: int
    #: Asset paths the stage names that resolve to nothing, as written.
    unresolved: tuple = ()
    #: Asset paths that resolve, as (written, resolved path).
    resolved: tuple = ()
    #: (name, width, height) of images inside a .usdz.
    packed: tuple = ()
    #: (points (n, 3) world space, triangles (m, 3)) per mesh, for the scene loader.
    meshes: tuple = field(default=(), compare=False)


def available() -> bool:
    try:
        import pxr  # noqa: F401
    except ImportError:
        return False
    return True


def read_usd(path: Path, epsilon: float = 0.0, ratio: float = 1e-6) -> UsdSummary:
    """Everything the gate measures, read once per (path, modification time)."""
    path = Path(path)
    return _read_cached(str(path), path.stat().st_mtime_ns, float(epsilon), float(ratio))


@lru_cache(maxsize=8)
def _read_cached(path_string: str, modified_ns: int, epsilon: float, ratio: float) -> UsdSummary:
    try:
        from pxr import Usd, UsdGeom, UsdShade, UsdSkel, UsdUtils
    except ImportError as exc:
        raise UsdUnavailable(INSTALL_HINT) from exc
    with _trap():
        stage = Usd.Stage.Open(path_string)
    if stage is None:
        raise ValueError("the stage did not open (damaged or not USD)")
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    triangles = degenerate = worst = 0
    materials: set = set()
    unbound = 0
    uv_names: set = set()
    meshes = []
    predicate = Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)
    for prim in stage.Traverse(predicate):
        if not prim.IsA(UsdGeom.Mesh):
            continue
        imageable = UsdGeom.Imageable(prim)
        if imageable.ComputeVisibility() == UsdGeom.Tokens.invisible:
            continue
        if imageable.ComputePurpose() in (UsdGeom.Tokens.guide, UsdGeom.Tokens.proxy):
            continue                     # never drawn in a final render
        mesh = UsdGeom.Mesh(prim)
        counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get() or [], np.int64)
        indices = np.asarray(mesh.GetFaceVertexIndicesAttr().Get() or [], np.int64)
        points = np.asarray(mesh.GetPointsAttr().Get() or [], np.float64).reshape(-1, 3)
        if counts.size == 0 or points.size == 0:
            continue
        if int(counts.sum()) != len(indices) or (indices.size and (
                int(indices.max()) >= len(points) or int(indices.min()) < 0)):
            raise ValueError(f"mesh {prim.GetPath()} is damaged (its face lists do not "
                             "match its points)")
        triangles += int(np.maximum(counts - 2, 0).sum())
        # World space, so a scaled-down prop is measured as placed.
        matrix = np.asarray(cache.GetLocalToWorldTransform(prim), np.float64)
        world = points @ matrix[:3, :3] + matrix[3, :3]
        faces = _fan(counts, indices)
        meshes.append((world, faces))
        if faces.size:
            from agk_gate.readers import degenerate_mask
            corners = world[faces]
            degenerate += int(np.count_nonzero(degenerate_mask(
                corners[:, 0], corners[:, 1], corners[:, 2], ratio, epsilon)))
        # Materials: the mesh's own binding and any per-face-set binding.
        bound = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
        subsets = UsdShade.MaterialBindingAPI(prim).GetMaterialBindSubsets()
        found_any = bool(bound)
        if bound:
            materials.add(str(bound.GetPath()))
        for subset in subsets:
            material = UsdShade.MaterialBindingAPI(subset.GetPrim()).ComputeBoundMaterial()[0]
            if material:
                materials.add(str(material.GetPath()))
                found_any = True
        if not found_any:
            unbound = 1
        for primvar in UsdGeom.PrimvarsAPI(prim).GetPrimvars():
            role = primvar.GetTypeName().role
            if role == "TextureCoordinate" or primvar.GetPrimvarName() in ("st", "uv", "UVMap"):
                uv_names.add(str(primvar.GetPrimvarName()))
        worst = max(worst, _influences(UsdSkel.BindingAPI(prim)))
    with _trap():
        # Each missing file is a warning USD would print; the gate reports them itself.
        layers, assets, unresolved = UsdUtils.ComputeAllDependencies(path_string)
    resolved = tuple((Path(str(asset)).name, str(asset)) for asset in assets)
    packed = _packed_images(Path(path_string)) if path_string.lower().endswith(".usdz") else ()
    return UsdSummary(triangles=triangles, material_slots=len(materials) + unbound,
                      uv_channels=len(uv_names), max_influences=worst, degenerate=degenerate,
                      unresolved=tuple(str(item) for item in unresolved), resolved=resolved,
                      packed=packed, meshes=tuple(meshes))


_QUIET = threading.Lock()


@contextmanager
def _trap():
    """USD prints a warning for every file it cannot find, straight to the
    process's error stream; the gate reports them itself, once and readably.
    The stream is borrowed for the call only (one thread at a time)."""
    with _QUIET:
        try:
            sys.stderr.flush()
            saved = os.dup(2)
        except (OSError, ValueError):         # no real stream (a service, a test runner)
            yield
            return
        try:
            with open(os.devnull, "wb") as sink:
                os.dup2(sink.fileno(), 2)
                yield
        finally:
            os.dup2(saved, 2)
            os.close(saved)


def _fan(counts, indices):
    """Triangles of polygons given as corner counts and a flat index list."""
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    pieces = []
    for size in np.unique(counts):
        if size < 3:
            continue
        first = starts[counts == size]
        for k in range(1, int(size) - 1):
            pieces.append(np.stack([indices[first], indices[first + k], indices[first + k + 1]],
                                   axis=1))
    return np.concatenate(pieces) if pieces else np.zeros((0, 3), np.int64)


def _influences(binding) -> int:
    """The most non-zero joint weights on one point."""
    weights = binding.GetJointWeightsPrimvar()
    if not weights or not weights.HasValue():
        return 0
    size = max(int(weights.GetElementSize() or 1), 1)
    values = np.asarray(weights.Get() or [], np.float64)
    if values.size == 0:
        return 0
    if weights.GetInterpolation() == "constant":
        return int(np.count_nonzero(values[:size] > 1e-4))
    return int((values.reshape(-1, size) > 1e-4).sum(axis=1).max())


def _packed_images(path: Path) -> tuple:
    from agk_gate.readers import image_size_from_bytes
    found = []
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.filename.lower().endswith(_PACKED_IMAGES):
                    continue
                with archive.open(info) as handle:
                    head = handle.read(1 << 16)
                size = image_size_from_bytes(head)
                if size:
                    found.append((info.filename, int(size[0]), int(size[1])))
    except (OSError, zipfile.BadZipFile):
        return ()
    return tuple(found)
