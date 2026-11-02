#!/usr/bin/env python3
r"""Opt-in healing for AssetGatekeeper. Nothing here runs unless it is asked for.

The design rule this file exists to obey: **an asset is only ever modified if the
modified version is verified to pass**. Healing therefore happens in a staging
folder next to the original; the gate re-inspects the staged file with the same
budget; only then is it swapped in, atomically. If the healed file still fails,
the staged copy is deleted and the artist's file is untouched — byte for byte.

What can be healed, and why only that:

``.png``  name suffix (rename) and non-power-of-two (resize by default; pad is
          opt-in). Neither changes what the image means, and every heal keeps
          the original (below).
          A rename also rewrites the ``.mtl``/``.gltf`` files that named the old
          filename, and only at the moment the rename is adopted.
``.obj``  zero-area faces are deleted. Deleting a face that contributes no pixels
          is the one mesh edit that cannot cost an artist anything.

Every adopted heal keeps the files it replaced - the asset and any ``.mtl`` or
``.gltf`` it repointed - in ``.agk-backups/<id>/`` with a ``heal.json`` that
says what changed. ``undo_heal`` (``asset-gatekeeper <file> --undo-heal``) puts
them back, and refuses if the healed file was edited since.

What is deliberately *not* healed: triangle budgets (decimation destroys intent),
material merging (changes shading), texture resolution (throwing away detail), and
any ``.gltf``/``.glb`` geometry surgery (rewriting accessors and buffers blind is
how you ship a corrupt file). Those keep failing, which is the safe answer.

Third-party: Pillow, for textures only. The gate itself still imports no healer
unless healing is switched on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence
from urllib.parse import quote, unquote

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment dependent
    Image = None  # type: ignore[assignment]

LOG = logging.getLogger("assetgatekeeper.healer")

# Staging folders live inside the watch directory (same filesystem => atomic
# rename), so the watcher must ignore them. Keep in sync with IGNORED_PREFIXES.
STAGING_PREFIX = ".agk-heal-"
# Where every adopted heal keeps what it replaced. Also ignored by the watcher.
BACKUP_DIRNAME = ".agk-backups"
MANIFEST_NAME = "heal.json"

# "resize" (the default) keeps UVs exactly: the content is resampled to the new
# canvas, so u,v still address the same texel. "pad" keeps every source pixel
# but leaves the content occupying a smaller fraction of the image - a UV of
# 0.5 lands 2.3% off on a 1000->1024 pad, and the padded edge is repeated
# border - so it is opt-in and documented as UV-shifting.
TEXTURE_MODES = ("resize", "pad")
HEALABLE_MESH_SUFFIXES = {".obj"}

# Text files that can name a sibling texture by filename. A GLB embeds its
# images, so it never needs rewriting; a .dae/.usda would, but the gate's own
# reference model does not read them yet either. Keep the two in step.
REFERENCE_TEXT_SUFFIXES = (".mtl", ".obj", ".gltf")

# What marks the top of a project, for the "is this texture referenced outside the
# watched folder?" check. The nearest ancestor holding one of these is searched
# (read-only); without one, only the watched folder's parent is.
# Strong markers say "this folder IS the project root". rules.toml is weaker: it
# sits next to the assets it governs (``--init`` writes it into the watch folder),
# so it only counts on an *ancestor* of the watch root.
PROJECT_MARKERS = (".git", ".hg", ".svn", ".p4config", ".p4ignore")
PROJECT_MARKER_GLOBS = ("*.uproject", "*.hip")
ANCESTOR_MARKERS = ("rules.toml", ".agk")
# Bounds for that outside search, so watching D:\Exports never walks all of D:\.
OUTSIDE_SCAN_MAX_DIRS = 5_000
OUTSIDE_SCAN_MAX_DEPTH = 8

# A scene file larger than this is not read for references. Blind substitution
# in a 40 MB glTF is slow and the rename is almost never the interesting part.
MAX_REFERENCE_BYTES = 4 * 1024 * 1024

# A rename may repoint this many files before the healer stops looking. A studio
# tree holds tens of thousands of .obj files; a texture named by more than this is
# either a shared library or a mistake, and both deserve a human.
MAX_REFERENCE_FILES = 20_000


class HealError(Exception):
    """Healing cannot run at all (missing Pillow, bad setting). Not a heal failure."""


@dataclass(frozen=True)
class HealResult:
    """Where the asset now lives, and what was done to it."""

    path: Path
    modifications: tuple[str, ...]
    # The .agk-backups/<id> folder holding what was replaced (None for a dry run).
    backup: Optional[Path] = None
    # True when nothing was written: the heal was verified in staging only.
    dry_run: bool = False
    # Reference files the rename would repoint (dry run) or did repoint.
    references: tuple[str, ...] = field(default=())

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

# Hints are matched against the *start of a word* inside the stem, longest/most
# specific first. Order matters twice over: "basecolor" has to beat "color",
# and "orm" must not match inside "n[orm]al".
_SUFFIX_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("basecolor", "base_color", "bc"), "_BaseColor"),
    (("orm",), "_ORM"),
    (("normal", "norm", "nrm"), "_Normal"),
    (("rough", "rgh", "roughness"), "_Roughness"),
    (("albedo", "diffuse", "colour", "color", "alb", "d"), "_Albedo"),
)


def _hint_matches(hint: str, lowered: str) -> bool:
    """True when the stem starts a word with this hint.

    Substring matching read "orm" inside "normal" and "bc" inside "abcd": a
    texture named ``wall_normal`` was renamed to ``wall_normal_ORM``, which is
    both wrong and expensive to notice.
    """
    return re.search(r"(?:^|[_\-\s.])" + re.escape(hint), lowered) is not None


def choose_suffix(stem: str, suffixes: Sequence[str]) -> Optional[str]:
    """Pick the suffix this filename is *asking* for, or None if it already has one."""
    lowered = stem.lower()
    for suffix in suffixes:
        if lowered.endswith(suffix.lower()):
            return None  # already compliant: never double-suffix
    for hints, suffix in _SUFFIX_HINTS:
        if suffix in suffixes and any(_hint_matches(hint, lowered) for hint in hints):
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


def heal_texture_file(path: Path, mode: str = "resize") -> Optional[str]:
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
# Renamed files and the references that named them
# --------------------------------------------------------------------------- #

def _name_pattern(filename: str) -> "re.Pattern[str]":
    """Match a filename as a whole path component, in prose or inside a URI.

    The lookarounds are what make this safe to run over a whole file: ``./`` and
    ``textures/`` may precede the name, but another name that merely *ends* in it
    (``oak_wood.png``) or a backup file (``wood.png.bak``) must not match.
    """
    return re.compile(r"(?<![A-Za-z0-9_\-])" + re.escape(filename) + r"(?![A-Za-z0-9_\-.])")


def rewrite_reference_names(text: str, renames: dict[str, str]) -> tuple[str, int]:
    """Point every mention of an old filename at its new name. Returns (text, count)."""
    total = 0
    for old, new in renames.items():
        text, count = _name_pattern(old).subn(lambda _match, _new=new: _new, text)
        total += count
    return text, total


#: Scene formats whose texture paths are not plain text the healer could rewrite.
BINARY_REFERENCE_SUFFIXES = (".fbx", ".glb")


def _file_contains(path: Path, needles: set) -> bool:
    """Whether any needle occurs in the file, read in overlapping 1 MB windows."""
    longest = max(len(needle) for needle in needles)
    try:
        with open(path, "rb") as handle:
            tail = b""
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    return False
                window = tail + chunk
                if any(needle in window for needle in needles):
                    return True
                tail = window[-longest:]
    except OSError:
        return True                          # cannot read it: assume it might


def _project_boundary(root: Path) -> Optional[Path]:
    """The folder to search for references outside ``root``, or None if none.

    ``root`` itself being a VCS or engine project root means the whole project
    is already being watched. Otherwise the nearest marked ancestor (up to four
    levels, where a rules.toml also counts), else ``root``'s parent.
    """
    def marked(folder: Path, weak: bool) -> bool:
        markers = PROJECT_MARKERS + (ANCESTOR_MARKERS if weak else ())
        if any((folder / marker).exists() for marker in markers):
            return True
        return any(next(folder.glob(pattern), None) is not None
                   for pattern in PROJECT_MARKER_GLOBS)

    try:
        if marked(root, weak=False):
            return None
        current = root
        for _ in range(4):
            parent = current.parent
            if parent == current:
                break
            if marked(parent, weak=True):
                return parent
            current = parent
    except OSError:
        pass
    return root.parent if root.parent != root else None


def project_root_for(folder: Path, levels: int = 6) -> Optional[Path]:
    """The nearest folder at or above ``folder`` that marks a project root.

    For healing one file: a texture's references usually live beside the
    meshes, not beside the texture, so ``--heal`` searches (and repoints) the
    whole project when it can find one. A ``.git``, ``.p4config``, ``*.uproject``,
    ``*.hip`` or a ``rules.toml`` marks it. None when nothing does.
    """
    current = folder
    for _ in range(levels + 1):
        try:
            if any((current / marker).exists()
                   for marker in PROJECT_MARKERS + ANCESTOR_MARKERS):
                return current
            if any(next(current.glob(pattern), None) is not None
                   for pattern in PROJECT_MARKER_GLOBS):
                return current
        except OSError:
            return None
        if current.parent == current:
            break
        current = current.parent
    return None


def _text_references(text: str, folder: Path, texture: Path, needles: set) -> bool:
    """Does ``text`` (a file in ``folder``) name ``texture`` by a path that resolves to it?"""
    target = os.path.normcase(str(texture))
    for needle in needles:
        pattern = re.compile(r"[^\s\"'<>=,]*" + re.escape(needle), re.IGNORECASE)
        for match in pattern.finditer(text):
            token = unquote(match.group(0).replace("\\", "/"))
            try:
                resolved = (folder / token).resolve()
            except (OSError, ValueError):
                return True                      # cannot tell: assume it does
            if os.path.normcase(str(resolved)) == target:
                return True
    return False


# --------------------------------------------------------------------------- #
# The healer
# --------------------------------------------------------------------------- #

class Healer:
    """Turns a failing asset into a passing one, or changes nothing at all.

    ``checker`` is the gate's own inspection function: the healer never decides
    on its own whether the result is acceptable.
    """

    def __init__(self, budget, *, textures: bool = True, meshes: bool = True,
                 texture_mode: str = "resize", root: Optional[Path] = None,
                 resolve_references: Optional[Callable[[Path], list]] = None,
                 backups: bool = True, dry_run: bool = False) -> None:
        if texture_mode not in TEXTURE_MODES:
            raise HealError(f"texture_mode must be one of {', '.join(TEXTURE_MODES)}, "
                            f"got {texture_mode!r}")
        if textures and Image is None:
            raise HealError("texture healing needs Pillow: python -m pip install pillow")
        self.budget = budget
        self.textures = textures
        self.meshes = meshes
        self.texture_mode = texture_mode
        # The project boundary, when the caller knows it. With a root the healer
        # finds references anywhere in the tree - meshes/, Content/Meshes/,
        # Shared/Textures - instead of only the texture's folder and its parent,
        # which missed most real layouts and broke the rename quietly.
        self.root = Path(root) if root else None
        # The gate's own reference resolver (FBX and GLB included). With it, a
        # binary scene only blocks a rename when it really points at *this*
        # texture; without it, any byte match on the name does.
        self.resolve_references = resolve_references
        # Keep what every heal replaces, so it can be undone. Off only for a
        # caller that heals a throwaway copy (the Art Workstation).
        self.backups = backups
        # Verify in staging and report, but write nothing.
        self.dry_run = dry_run
        self.last_refusal = ""

    # -- public ------------------------------------------------------------- #

    def heal(self, path: Path, checker: Callable[[Path], object]) -> Optional[HealResult]:
        """Try to heal ``path``. Returns None when nothing could be safely done.

        The original file is only replaced when the staged copy verifies as
        passing; every other outcome leaves the artist's bytes untouched.
        """
        # Why the last heal changed nothing, in words for whoever asked for it.
        self.last_refusal = ""
        staging_root = Path(tempfile.mkdtemp(dir=str(path.parent), prefix=STAGING_PREFIX))
        try:
            return self._heal_into(staging_root, path, checker)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    # -- internals ---------------------------------------------------------- #

    def _stage_obj_sidecars(self, source: Path, staging_root: Path) -> list[str]:
        """Copy the .mtl and the textures it names next to the staged .obj.

        A Blender or Houdini .obj names its material library with ``mtllib``.
        Verifying the staged copy without that file made every real export fail
        the reference check, so the heal was silently thrown away - the feature
        never worked outside synthetic fixtures.
        """
        staged: list[str] = []
        try:
            body = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return staged
        for match in re.finditer(r"^\s*mtllib\s+(.+?)\s*$", body, re.MULTILINE):
            name = match.group(1).strip().strip('"')
            mtl = source.parent / name
            if not mtl.is_file():
                continue
            target = staging_root / mtl.name
            try:
                shutil.copy2(mtl, target)
            except OSError:
                continue
            staged.append(mtl.name)
            # Textures the .mtl points at, relative to itself.
            try:
                mtl_body = mtl.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for ref in re.finditer(r"^\s*map_\w+\s+(.+?)\s*$", mtl_body, re.MULTILINE):
                texture = (mtl.parent / ref.group(1).strip().strip('"')).name
                candidate = mtl.parent / texture
                if candidate.is_file() and not (staging_root / texture).exists():
                    try:
                        shutil.copy2(candidate, staging_root / texture)
                        staged.append(texture)
                    except OSError:
                        pass
        return staged

    def _heal_into(self, staging_root: Path, path: Path, checker) -> Optional[HealResult]:
        staged = staging_root / path.name
        try:
            shutil.copy2(path, staged)
        except OSError as exc:
            LOG.debug("healing: cannot stage %s: %s", path.name, exc)
            self.last_refusal = f"cannot copy it to stage the heal: {exc}"
            return None

        modifications: list[str] = []
        # old filename -> new filename, applied to reference files at adoption.
        reference_renames: dict[str, str] = {}
        suffix = path.suffix.lower()

        if suffix == ".obj":
            staged_refs = self._stage_obj_sidecars(path, staging_root)
            if staged_refs:
                LOG.debug("healing: staged %s alongside %s",
                          ", ".join(staged_refs), path.name)

        if suffix == ".png" and self.textures:
            # Name first: the rename decides the final filename, and every later
            # step has to work on the renamed copy (a stale handle was a real bug).
            renamed = self._rename_for_suffix(staged)
            if renamed is not None:
                staged, note = renamed
                modifications.append(note)
                # The rename minted a name nothing points at yet. Record it, and
                # make the staged sidecars agree, or the verify below would fail
                # on a broken reference the heal itself just created.
                reference_renames[path.name] = staged.name
                # A glTF uri may be percent-encoded ("wood%2002.png" for
                # "wood 02.png"); the gate compares the decoded name, so encode the
                # replacement the same way or the file keeps pointing at the old one.
                quoted_old, quoted_new = quote(path.name), quote(staged.name)
                if quoted_old != path.name:
                    reference_renames[quoted_old] = quoted_new
                self._write_reference_edits(
                    self._prepare_reference_edits(staging_root, reference_renames,
                                                  include_parent=False))
            try:
                note = heal_texture_file(staged, self.texture_mode)
            except (OSError, ValueError) as exc:   # corrupt/unreadable: not healable
                LOG.debug("healing: texture failed (%s): %s", staged.name, exc)
                self.last_refusal = f"the image cannot be read: {exc}"
                return None
            if note:
                modifications.append(note)
        elif suffix in HEALABLE_MESH_SUFFIXES and self.meshes:
            try:
                note = strip_degenerate_obj(staged, self.budget.degenerate_epsilon)
            except (OSError, ValueError) as exc:
                LOG.debug("healing: cannot parse %s: %s", path.name, exc)
                self.last_refusal = f"the mesh cannot be parsed: {exc}"
                return None
            if note:
                modifications.append(note)

        if not modifications:
            self.last_refusal = ("nothing the healer can change would fix it (it renames and "
                                 "resizes .png textures and strips zero-area .obj faces)")
            return None

        # Renaming is only safe when we are not about to clobber another asset.
        target = path.parent / staged.name
        if target.exists() and target.name != path.name:
            LOG.warning("healing: refusing to overwrite existing %s", target.name)
            self.last_refusal = f"{target.name} already exists"
            return None

        # ... and when nothing *outside* the watched folder still names it. The
        # repointing below only edits files under the watch root; a mesh beside
        # it (gate watching Textures/, mesh in ../Meshes/) would be orphaned by
        # a heal that reports success. Refuse instead: the gate fails and locks.
        if reference_renames:
            blocker = self._binary_reference(path) or self._outside_reference(path)
            if blocker:
                advice = ("rename it in the DCC and re-export" if "binary" in blocker
                          else "rename it in the DCC, or run the gate on the project root "
                               "so every reference is updated (a rules.toml or .git at "
                               "the project root lets --heal find it)")
                LOG.warning("healing: not renaming %s - %s; %s", path.name, blocker, advice)
                self.last_refusal = f"{blocker}; {advice}"
                return None

        verdict = checker(staged)
        if getattr(verdict, "problems", ()):  # did not actually fix it: keep the original
            LOG.debug("healing: %s still fails after healing, discarding (%s)",
                      path.name, "; ".join(verdict.problems))
            self.last_refusal = ("the healed copy would still fail - "
                                 + "; ".join(verdict.problems)
                                 + " - so it was thrown away")
            return None

        if self.dry_run:
            pending = self._prepare_reference_edits(path.parent, reference_renames,
                                                    skip=(path,))
            notes = list(modifications)
            count = sum(item[2] for item in pending)
            if count:
                notes.append(f"repoint {count} reference{'s' if count != 1 else ''} "
                             f"to {target.name}")
            return HealResult(target, tuple(notes), dry_run=True,
                              references=tuple(str(item[0]) for item in pending))

        try:
            return self._adopt(path, staged, modifications, reference_renames)
        except OSError as exc:
            LOG.warning("healing: could not replace %s: %s", path.name, exc)
            self.last_refusal = f"could not replace it: {exc}"
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

    # -- references --------------------------------------------------------- #

    def _binary_reference(self, texture: Path) -> str:
        """A binary scene (.fbx / .glb) that names ``texture``, or "".

        Those cannot be repointed by a text substitution - an FBX string is
        length-prefixed, a GLB chunk is length-checked - so a rename would orphan
        them. Searched across the watch root and the project boundary; a byte
        match on the file name is enough to refuse (false positives only cost a
        heal, a false negative costs a broken asset).
        """
        roots = [root for root in (self.root, _project_boundary(self.root.resolve())
                                   if self.root is not None else None) if root is not None]
        if not roots:
            roots = [texture.parent, texture.parent.parent]
        needles = {texture.name.encode("utf-8"), quote(texture.name).encode("utf-8")}
        seen: set = set()
        for top in roots:
            for base, dirnames, filenames in os.walk(top, onerror=lambda _exc: None):
                dirnames[:] = [d for d in dirnames if not d.startswith(STAGING_PREFIX)
                               and d not in (".git", ".hg", ".svn", "__pycache__", "node_modules")]
                if len(seen) > OUTSIDE_SCAN_MAX_DIRS * 4:
                    return f"could not finish checking {top} for FBX/GLB references"
                for filename in filenames:
                    if not filename.lower().endswith(BINARY_REFERENCE_SUFFIXES):
                        continue
                    candidate = Path(base) / filename
                    if candidate in seen:
                        continue
                    seen.add(candidate)
                    if not _file_contains(candidate, needles):
                        continue
                    if self.resolve_references is not None:
                        try:
                            targets = [Path(target) for _, target in
                                       self.resolve_references(candidate)]
                            wanted = os.path.normcase(str(texture.resolve()))
                            if not any(os.path.normcase(str(t.resolve())) == wanted
                                       for t in targets):
                                continue        # names a different file of the same name
                        except Exception:       # unreadable scene: stay conservative
                            pass
                    return (f"{candidate.name} names it, and a binary "
                            f"{candidate.suffix.lower()} cannot be repointed safely")
        return ""

    def _outside_reference(self, texture: Path) -> str:
        """Why renaming ``texture`` would break a file outside the watch root, or "".

        Read-only. Searches the project boundary (nearest ancestor with a
        project marker, else the watch root's parent) minus the watch root
        itself, which the repointing already covers. A match only counts when it
        resolves to *this* texture, so an unrelated ``wood.png`` elsewhere does
        not block the heal. An incomplete search is treated as a match: the
        safe answer to "might this orphan a mesh?" is not "probably not".
        """
        if self.root is None:
            return ""
        try:
            root = self.root.resolve()
            texture = texture.resolve()
        except OSError:
            return ""
        boundary = _project_boundary(root)
        if boundary is None:
            return ""
        name = texture.name
        needles = {name, quote(name)}
        dirs_seen = 0
        base_depth = len(boundary.parts)
        for base, dirnames, filenames in os.walk(boundary, onerror=lambda _exc: None):
            here = Path(base)
            dirs_seen += 1
            if dirs_seen > OUTSIDE_SCAN_MAX_DIRS:
                return (f"could not finish checking {boundary} for references "
                        f"(over {OUTSIDE_SCAN_MAX_DIRS:,} folders)")
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(STAGING_PREFIX) and d != BACKUP_DIRNAME
                and d not in (".git", ".hg", ".svn", "__pycache__", "node_modules")
                and (here / d).resolve() != root
                and len(here.parts) - base_depth < OUTSIDE_SCAN_MAX_DEPTH)
            if here.resolve() == root:
                continue
            for filename in filenames:
                candidate = here / filename
                if candidate.suffix.lower() not in REFERENCE_TEXT_SUFFIXES:
                    continue
                try:
                    if candidate.stat().st_size > MAX_REFERENCE_BYTES:
                        continue
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if not any(needle.lower() in text.lower() for needle in needles):
                    continue
                if _text_references(text, candidate.parent, texture, needles):
                    try:
                        shown = candidate.relative_to(boundary)
                    except ValueError:
                        shown = candidate
                    return f"{shown} outside the watched folder still points at it"
        return ""

    def _reference_files(self, directory: Path, include_parent: bool = True) -> list[Path]:
        """Text files that could name a texture living in ``directory``.

        With a project root the whole tree is searched - bounded by suffix, size
        cap and a file cap. Without one (single-file mode, or a caller that does
        not know the root) it falls back to the texture's folder and its parent,
        which is only right for the co-located layout.

        ``include_parent`` is off for the staging folder: its parent *is* the
        artist's directory, and reaching up from staging would edit the originals
        before the heal has even been verified.
        """
        found: list[Path] = []
        seen: set[Path] = set()

        def consider(candidate: Path) -> None:
            if candidate.suffix.lower() not in REFERENCE_TEXT_SUFFIXES:
                return
            if candidate in seen or not candidate.is_file():
                return
            seen.add(candidate)
            if len(found) < MAX_REFERENCE_FILES:
                found.append(candidate)

        if self.root is not None and include_parent:
            for base, dirnames, filenames in os.walk(self.root, onerror=lambda _exc: None):
                # Never edit our own staging or backup folders (a later rename
                # would rewrite the backed-up .mtl, and undo would restore the
                # edit), never descend into a VCS.
                dirnames[:] = sorted(
                    name for name in dirnames
                    if not name.startswith(STAGING_PREFIX) and name != BACKUP_DIRNAME
                    and name not in (".git", ".hg", ".svn", "__pycache__"))
                for name in filenames:
                    consider(Path(base) / name)
                if len(found) >= MAX_REFERENCE_FILES:
                    LOG.warning("healing: stopped looking for references after %d files",
                                MAX_REFERENCE_FILES)
                    break
            return found

        folders = [directory, directory.parent] if include_parent else [directory]
        for folder in folders:
            try:
                entries = sorted(folder.iterdir())
            except OSError:
                continue
            for entry in entries:
                consider(entry)
        return found

    def _prepare_reference_edits(self, directory: Path, renames: dict[str, str],
                                 skip: Sequence[Path] = (),
                                 include_parent: bool = True) -> list[tuple[Path, str, int]]:
        """Read and substitute in memory. Nothing is written until adoption.

        Doing the reading first is the point: an unreadable reference file must
        abandon the heal while the artist's bytes are still untouched, not after.
        """
        if not renames:
            return []
        skipped = set(skip)
        pending: list[tuple[Path, str, int]] = []
        for candidate in self._reference_files(directory, include_parent):
            if candidate in skipped:
                continue
            try:
                if candidate.stat().st_size > MAX_REFERENCE_BYTES:
                    LOG.debug("healing: not rewriting references in %s (too large)",
                              candidate.name)
                    continue
                text = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue                 # binary or unreadable: not ours to edit
            updated, count = rewrite_reference_names(text, renames)
            if count:
                pending.append((candidate, updated, count))
        return pending

    @staticmethod
    def _write_reference_edits(pending: Sequence[tuple[Path, str, int]]) -> int:
        """Apply prepared edits atomically. Returns the number of references fixed."""
        updated = 0
        for path, text, count in pending:
            temporary = path.with_name(path.name + ".agk-edit")
            try:
                temporary.write_text(text, encoding="utf-8")
                os.replace(temporary, path)
                updated += count
                LOG.info("healed  %s: repointed %d reference(s)", path.name, count)
            except OSError as exc:
                LOG.warning("healing: could not update references in %s: %s",
                            path.name, exc)
                try:
                    temporary.unlink()
                except OSError:
                    pass
        return updated

    def _backup_base(self, original: Path) -> Path:
        """The folder whose .agk-backups/ holds this heal: the watch root if the
        asset is under it, else the asset's own folder."""
        for candidate in (self.root, original.parent):
            if candidate is None:
                continue
            try:
                original.resolve().relative_to(Path(candidate).resolve())
                return Path(candidate)
            except ValueError:
                continue
        return original.parent

    def _write_backup(self, original: Path, staged: Path, target: Path,
                      modifications: list[str],
                      pending: Sequence[tuple[Path, str, int]]) -> Path:
        """Copy everything the adoption is about to replace, plus a manifest.

        Written *before* the swap: if the copy fails, the heal is abandoned with
        the artist's file untouched, the same as a failed verification.
        """
        base = self._backup_base(original)
        folder = Path(tempfile.mkdtemp(
            dir=str(_ensure_dir(base / BACKUP_DIRNAME)),
            prefix=datetime.now().strftime("%Y%m%d-%H%M%S-%f-")))
        files = []
        for source in [original, *(item[0] for item in pending)]:
            relative = _relative(source, base)
            destination = folder / "files" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            files.append(relative)
        manifest = {
            "version": 1,
            "healed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "asset": _relative(original, base),
            "healed": _relative(target, base),
            "modifications": list(modifications),
            "texture_mode": self.texture_mode,
            "files": files,
            # What the heal leaves on disk, so undo can tell whether anyone has
            # edited the result since (and refuse rather than lose their work).
            "after": {_relative(target, base): _digest_file(staged),
                      **{_relative(item[0], base): hashlib.sha256(
                          item[1].encode("utf-8")).hexdigest() for item in pending}},
        }
        (folder / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return folder

    def _adopt(self, original: Path, staged: Path, modifications: list[str],
               reference_renames: Optional[dict[str, str]] = None) -> HealResult:
        target = original.parent / staged.name
        # Reference edits are prepared before anything is replaced, so a file we
        # cannot read abandons the heal with the original still in place.
        pending = self._prepare_reference_edits(original.parent, reference_renames or {},
                                                skip=(original, staged))
        backup = None
        if self.backups:
            backup = self._write_backup(original, staged, target, modifications, pending)
        # Atomic swap on the same filesystem; the original is replaced only now,
        # after the staged copy passed inspection.
        os.replace(staged, target)
        if target.name != original.name and original.exists():
            original.unlink()   # the rename case: old name must not survive
        repointed = self._write_reference_edits(pending)
        if repointed:
            modifications.append(f"repointed {repointed} reference"
                                 f"{'s' if repointed != 1 else ''} to {target.name}")
        for note in modifications:
            LOG.info("healed  %s: %s", target.name, note)
        if backup is not None:
            LOG.info("healed  %s: original kept in %s (undo: --undo-heal)",
                     target.name, backup)
        return HealResult(target, tuple(modifications), backup=backup,
                          references=tuple(str(item[0]) for item in pending))


# --------------------------------------------------------------------------- #
# Backups and undo
# --------------------------------------------------------------------------- #

def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _relative(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.name


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class HealRecord:
    """One adopted heal, read back from its manifest."""

    folder: Path            # .agk-backups/<id>
    base: Path              # the folder the manifest's paths are relative to
    manifest: dict

    @property
    def asset(self) -> Path:
        return self.base / self.manifest["asset"]

    @property
    def healed(self) -> Path:
        return self.base / self.manifest["healed"]

    @property
    def undone(self) -> bool:
        return bool(self.manifest.get("undone_at"))


def find_heals(start: Path) -> list[HealRecord]:
    """Every heal recorded in ``start``'s .agk-backups and its ancestors', newest first.

    Ancestors too: a heal made while watching ``Content/`` is recorded there,
    and an artist undoing one file passes ``Content/Props/rock.png``.
    """
    records: list[HealRecord] = []
    folder = start if start.is_dir() else start.parent
    for base in [folder, *folder.parents]:
        backups = base / BACKUP_DIRNAME
        if not backups.is_dir():
            continue
        for entry in backups.iterdir():
            manifest_path = entry / MANIFEST_NAME
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(manifest, dict) and "asset" in manifest and "healed" in manifest:
                records.append(HealRecord(entry, base, manifest))
    records.sort(key=lambda record: (record.manifest.get("healed_at", ""), record.folder.name),
                 reverse=True)
    return records


def is_inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def _same_file(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def undo_heal(path: Path) -> HealRecord:
    """Put back what the latest heal of ``path`` replaced.

    ``path`` may be the healed file or the name it had before (a rename).
    Refuses - changing nothing - when any file the heal wrote has been edited
    since: an undo must never throw away work done after the heal.
    """
    path = Path(path)
    for record in find_heals(path):
        if record.undone:
            continue
        if not (_same_file(record.healed, path) or _same_file(record.asset, path)):
            continue
        changed = []
        for relative, expected in (record.manifest.get("after") or {}).items():
            current = record.base / relative
            if not current.is_file() or _digest_file(current) != expected:
                changed.append(relative)
        if changed:
            later = [other for other in find_heals(path)
                     if not other.undone and other.manifest.get("healed_at", "") >=
                     record.manifest.get("healed_at", "") and other.folder != record.folder
                     and set(other.manifest.get("files", [])) & set(changed)]
            hint = (f" A later heal changed it: undo {later[-1].manifest['healed']} first."
                    if later else "")
            raise HealError(f"{', '.join(changed)} changed after it was healed; "
                            f"nothing was undone.{hint} The originals are in "
                            f"{record.folder / 'files'}")
        for relative in record.manifest.get("files", []):
            source = record.folder / "files" / relative
            if not source.is_file():
                raise HealError(f"the backup of {relative} is missing from {record.folder}; "
                                "nothing was undone")
        for relative in record.manifest["files"]:
            destination = record.base / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".agk-edit")
            shutil.copy2(record.folder / "files" / relative, temporary)
            os.replace(temporary, destination)
        if not _same_file(record.healed, record.asset) and record.healed.exists():
            record.healed.unlink()          # the renamed copy must not survive
        manifest = dict(record.manifest)
        manifest["undone_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (record.folder / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2),
                                                   encoding="utf-8")
        return HealRecord(record.folder, record.base, manifest)
    raise HealError(f"no heal of {path.name} to undo (looked in {BACKUP_DIRNAME} folders "
                    f"from {path.parent} up)")
