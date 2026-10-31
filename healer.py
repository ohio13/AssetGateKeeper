#!/usr/bin/env python3
r"""Opt-in healing for AssetGatekeeper. Nothing here runs unless it is asked for.

The design rule this file exists to obey: **an asset is only ever modified if the
modified version is verified to pass**. Healing therefore happens in a staging
folder next to the original; the gate re-inspects the staged file with the same
budget; only then is it swapped in, atomically. If the healed file still fails,
the staged copy is deleted and the artist's file is untouched — byte for byte.

What can be healed, and why only that:

``.png``  name suffix (rename) and non-power-of-two (pad or resize). Both are
          reversible in an afternoon and neither changes what the image means.
``.obj``  zero-area faces are deleted. Deleting a face that contributes no pixels
          is the one mesh edit that cannot cost an artist anything.

What is deliberately *not* healed: triangle budgets (decimation destroys intent),
material merging (changes shading), texture resolution (throwing away detail), and
any ``.gltf``/``.glb`` geometry surgery (rewriting accessors and buffers blind is
how you ship a corrupt file). Those keep failing, which is the safe answer.

Third-party: Pillow, for textures only. The gate itself still imports no healer
unless healing is switched on.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment dependent
    Image = None  # type: ignore[assignment]

LOG = logging.getLogger("assetgatekeeper.healer")

# Staging folders live inside the watch directory (same filesystem => atomic
# rename), so the watcher must ignore them. Keep in sync with IGNORED_PREFIXES.
STAGING_PREFIX = ".agk-heal-"

TEXTURE_MODES = ("pad", "resize")
HEALABLE_MESH_SUFFIXES = {".obj"}


class HealError(Exception):
    """Healing cannot run at all (missing Pillow, bad setting). Not a heal failure."""


@dataclass(frozen=True)
class HealResult:
    """Where the asset now lives, and what was done to it."""

    path: Path
    modifications: tuple[str, ...]

    @property
    def summary(self) -> str:
        return "; ".join(self.modifications)


def next_power_of_two(value: int) -> int:
    """Smallest power of two >= value. 1 stays 1; 0 is not a texture size."""
    if value <= 1:
        return 1
    return 1 << (value - 1).bit_length()


def looks_power_of_two(value: int) -> bool:
    return value > 0 and (value & (value - 1)) == 0


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #

# Hints are matched against the whole stem, longest/most specific first. Order
# matters: "norm" contains "nrm"? No, but "basecolor" must beat "color".
_SUFFIX_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("normal", "norm", "nrm", "_n"), "_Normal"),
    (("rough", "rgh", "roughness"), "_Roughness"),
    (("albedo", "diffuse", "basecolor", "base_color", "colour", "color", "_bc", "_d", "_alb"),
     "_Albedo"),
)


def choose_suffix(stem: str, suffixes: Sequence[str]) -> Optional[str]:
    """Pick the suffix this filename is *asking* for, or None if it already has one."""
    lowered = stem.lower()
    for suffix in suffixes:
        if lowered.endswith(suffix.lower()):
            return None  # already compliant: never double-suffix
    for hints, suffix in _SUFFIX_HINTS:
        if suffix in suffixes and any(hint in lowered for hint in hints):
            return suffix
    # No hint and no suffix: fall back to the first configured suffix, which is
    # the studio's own declared convention (_Albedo by default). Deterministic
    # beats clever here — a wrong-but-obvious guess is easy to spot in review.
    return suffixes[0] if suffixes else None


# --------------------------------------------------------------------------- #
# Textures
# --------------------------------------------------------------------------- #

def _pad_to(img, width: int, height: int):
    """Pad to the target size by replicating the last row/column.

    Replication, not black or transparent: a tiled texture padded with black
    shows a seam the artist did not author, and transparent padding silently
    changes alpha at the edges. Repeating the border is the least surprising.
    """
    canvas = Image.new(img.mode, (width, height))
    canvas.paste(img, (0, 0))
    if width > img.width:
        strip = img.crop((img.width - 1, 0, img.width, img.height))
        canvas.paste(strip.resize((width - img.width, img.height), Image.NEAREST),
                     (img.width, 0))
    if height > img.height:
        strip = img.crop((0, img.height - 1, img.width, img.height))
        canvas.paste(strip.resize((img.width, height - img.height), Image.NEAREST),
                     (0, img.height))
    if width > img.width and height > img.height:
        corner = img.crop((img.width - 1, img.height - 1, img.width, img.height))
        canvas.paste(corner.resize((width - img.width, height - img.height), Image.NEAREST),
                     (img.width, img.height))
    return canvas


def heal_texture_file(path: Path, mode: str = "pad") -> Optional[str]:
    """Make a texture's edges a power of two. Returns a change note, or None."""
    if Image is None:  # pragma: no cover - checked at startup by the gate
        raise HealError("Pillow is not installed")
    with Image.open(path) as img:
        img.load()
        source_size = img.size
        if looks_power_of_two(img.width) and looks_power_of_two(img.height):
            return None
        target = (next_power_of_two(img.width), next_power_of_two(img.height))
        working = img
        if working.mode in ("P", "1", "LA"):   # resize/pad need a real channel model
            working = working.convert("RGBA")
        if mode == "resize":
            healed = working.resize(target, Image.LANCZOS)
        else:
            healed = _pad_to(working, *target)
        save_kwargs = {}
        if path.suffix.lower() in (".png",):
            save_kwargs["optimize"] = True
        healed.save(path, **save_kwargs)
    return (f"resized {source_size[0]}x{source_size[1]} -> {target[0]}x{target[1]}"
            if mode == "resize" else
            f"padded {source_size[0]}x{source_size[1]} -> {target[0]}x{target[1]}")


# --------------------------------------------------------------------------- #
# OBJ degenerate faces
# --------------------------------------------------------------------------- #

def _obj_positions_and_faces(text: str):
    """Return (lines, positions, face_line_indices, face_vertex_indices)."""
    lines = text.splitlines(keepends=True)
    positions: list[tuple[float, float, float]] = []
    faces: list[tuple[int, list[int]]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("v "):
            parts = stripped.split()
            if len(parts) >= 4:
                try:
                    positions.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    positions.append((0.0, 0.0, 0.0))
        elif stripped.startswith("f "):
            refs = []
            for token in stripped.split()[1:]:
                head = token.split("/")[0]
                try:
                    raw = int(head)
                except ValueError:
                    refs = []
                    break
                refs.append(raw - 1 if raw > 0 else len(positions) + raw)
            faces.append((index, refs))
    return lines, positions, faces


def _face_area(vertices: list[tuple[float, float, float]]) -> float:
    """Area of a planar polygon via the fan around vertex 0 (absolute)."""
    if len(vertices) < 3:
        return 0.0
    ox, oy, oz = vertices[0]
    total = 0.0
    for i in range(1, len(vertices) - 1):
        ax, ay, az = vertices[i]
        bx, by, bz = vertices[i + 1]
        ux, uy, uz = ax - ox, ay - oy, az - oz
        vx, vy, vz = bx - ox, by - oy, bz - oz
        cx = uy * vz - uz * vy
        cy = uz * vx - ux * vz
        cz = ux * vy - uy * vx
        total += 0.5 * (cx * cx + cy * cy + cz * cz) ** 0.5
    return total


def strip_degenerate_obj(path: Path, epsilon: float = 1e-8) -> Optional[str]:
    """Delete zero-area faces from an OBJ in place. Returns a note, or None."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines, positions, faces = _obj_positions_and_faces(text)
    doomed: set[int] = set()
    for line_index, refs in faces:
        if len(refs) < 3:            # a 2-vertex "face" draws nothing at all
            doomed.add(line_index)
            continue
        try:
            vertices = [positions[ref] for ref in refs]
        except IndexError:
            continue                 # bad index: not ours to fix
        if _face_area(vertices) <= epsilon:
            doomed.add(line_index)
    if not doomed:
        return None
    kept = [line for index, line in enumerate(lines) if index not in doomed]
    path.write_text("".join(kept), encoding="utf-8")
    return f"Stripped {len(doomed)} degenerate face{'s' if len(doomed) != 1 else ''}"


# --------------------------------------------------------------------------- #
# The healer
# --------------------------------------------------------------------------- #

class Healer:
    """Turns a failing asset into a passing one, or changes nothing at all.

    ``checker`` is the gate's own inspection function: the healer never decides
    on its own whether the result is acceptable.
    """

    def __init__(self, budget, *, textures: bool = True, meshes: bool = True,
                 texture_mode: str = "pad") -> None:
        if texture_mode not in TEXTURE_MODES:
            raise HealError(f"texture_mode must be one of {', '.join(TEXTURE_MODES)}, "
                            f"got {texture_mode!r}")
        if textures and Image is None:
            raise HealError("texture healing needs Pillow: python -m pip install pillow")
        self.budget = budget
        self.textures = textures
        self.meshes = meshes
        self.texture_mode = texture_mode

    # -- public ------------------------------------------------------------- #

    def heal(self, path: Path, checker: Callable[[Path], object]) -> Optional[HealResult]:
        """Try to heal ``path``. Returns None when nothing could be safely done.

        The original file is only replaced when the staged copy verifies as
        passing; every other outcome leaves the artist's bytes untouched.
        """
        staging_root = Path(tempfile.mkdtemp(dir=str(path.parent), prefix=STAGING_PREFIX))
        try:
            return self._heal_into(staging_root, path, checker)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    # -- internals ---------------------------------------------------------- #

    def _heal_into(self, staging_root: Path, path: Path, checker) -> Optional[HealResult]:
        staged = staging_root / path.name
        try:
            shutil.copy2(path, staged)
        except OSError as exc:
            LOG.debug("healing: cannot stage %s: %s", path.name, exc)
            return None

        modifications: list[str] = []
        suffix = path.suffix.lower()

        if suffix == ".png" and self.textures:
            # Name first: the rename decides the final filename, and every later
            # step has to work on the renamed copy (a stale handle was a real bug).
            renamed = self._rename_for_suffix(staged)
            if renamed is not None:
                staged, note = renamed
                modifications.append(note)
            try:
                note = heal_texture_file(staged, self.texture_mode)
            except (OSError, ValueError) as exc:   # corrupt/unreadable: not healable
                LOG.debug("healing: texture failed (%s): %s", staged.name, exc)
                return None
            if note:
                modifications.append(note)
        elif suffix in HEALABLE_MESH_SUFFIXES and self.meshes:
            try:
                note = strip_degenerate_obj(staged, self.budget.degenerate_epsilon)
            except (OSError, ValueError) as exc:
                LOG.debug("healing: cannot parse %s: %s", path.name, exc)
                return None
            if note:
                modifications.append(note)

        if not modifications:
            return None

        # Renaming is only safe when we are not about to clobber another asset.
        target = path.parent / staged.name
        if target.exists() and target.name != path.name:
            LOG.warning("healing: refusing to overwrite existing %s", target.name)
            return None

        verdict = checker(staged)
        if getattr(verdict, "problems", ()):  # did not actually fix it: keep the original
            LOG.debug("healing: %s still fails after healing, discarding (%s)",
                      path.name, "; ".join(verdict.problems))
            return None

        try:
            return self._adopt(path, staged, modifications)
        except OSError as exc:
            LOG.warning("healing: could not replace %s: %s", path.name, exc)
            return None

    def _rename_for_suffix(self, staged: Path):
        """Give the staged copy the suffix its stem is asking for, if any."""
        suffix = choose_suffix(staged.stem, self.budget.texture_suffixes)
        if suffix is None:
            return None
        healed = staged.with_name(f"{staged.stem}{suffix}{staged.suffix}")
        try:
            os.replace(staged, healed)
        except OSError as exc:
            LOG.debug("healing: rename failed: %s", exc)
            return None
        return healed, f"renamed {staged.name} -> {healed.name}"

    def _adopt(self, original: Path, staged: Path, modifications: list[str]) -> HealResult:
        target = original.parent / staged.name
        # Atomic swap on the same filesystem; the original is replaced only now,
        # after the staged copy passed inspection.
        os.replace(staged, target)
        if target.name != original.name and original.exists():
            original.unlink()   # the rename case: old name must not survive
        for note in modifications:
            LOG.info("healed  %s: %s", target.name, note)
        return HealResult(target, tuple(modifications))
