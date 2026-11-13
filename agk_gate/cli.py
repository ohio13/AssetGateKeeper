"""AssetGatekeeper, part 7 of 7: watching, entry points, the verdict cache, CI reports and main().

Split out of asset_gatekeeper.py on 11 Nov 2026 with no change in behaviour;
asset_gatekeeper.py still exposes every name. See agk_gate/__init__.py.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import quote

from agk_gate.common import (
    engine_module, FileSystemEvent, FileSystemEventHandler, GATE_HOME, IDENTITY_MODES, Image,
    is_candidate, LOG, Observer, polypaint_module, REMOTE_RULES_ENV, RULES_CACHE_FILENAME,
    RULES_FILENAME, SERVER_ENV_IDENTITY, SERVER_ENV_URL, trimesh, __version__, walk_files,
)
from agk_gate.config import (
    AssetError, Budget, check_totals, ConfigError, EXCEPTION_WARN_DAYS, MemoryTotal,
    preset_summary, PRESETS, resolve_budget, resolve_exceptions, resolve_totals, run_init,
    TAG_MODES, Verdict,
)
from agk_gate.readers import (
    FBX_SUFFIXES, read_fbx,
)
from agk_gate.gate import (
    Gatekeeper, resolve_references,
)
from agk_gate.audit import (
    describe_auto_heal, fetch_remote_rules, healer_module, load_healer, resolve_healer,
    resolve_server, resolve_tagging, RulesFileError, run_audit, run_heal_history,
    suggest_naming,
)
from agk_gate.report import (
    describe_skipped, printable, Reporter, skipped_files, Telemetry,
)


# --- #
# Watching
# --- #


def wait_until_stable(path: Path, quiet: float = 0.35, timeout: float = 30.0,
                      settled_age: float = 5.0) -> bool:
    """Block until the file stops growing and can be opened.

    Exporters (Blender, Substance, Unreal, Maya) fire a create/modify event the
    instant the handle opens, so inspecting immediately means reading a truncated
    file. Poll size until it has been unchanged for ``quiet`` seconds.

    A file nothing has touched for ``settled_age`` seconds is not being written
    right now, so it is taken as stable immediately. Without that, ``--once`` on
    a finished repository paid 0.35 s per asset - a 4,000-asset project waited
    23 minutes before reporting a single verdict.
    """
    try:
        if time.time() - path.stat().st_mtime >= settled_age:
            return True
    except OSError:
        return False  # deleted or moved away before we looked

    deadline = time.monotonic() + timeout
    last_size: Optional[int] = None
    stable_since = 0.0

    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            return False  # deleted or moved away while we waited

        # A zero-byte file settles quickly and is a failure worth reporting, but
        # give exporters that open a handle before writing a little extra grace.
        required_quiet = quiet if size else max(quiet, 1.5)

        now = time.monotonic()
        if size == last_size:
            if now - stable_since >= required_quiet:
                try:
                    with path.open("rb") as handle:
                        handle.read(1)
                    return True
                except OSError:
                    pass  # still locked by the exporter
        else:
            last_size = size
            stable_since = now
        time.sleep(0.1)

    LOG.debug("timed out waiting for %s to settle", path.name)
    return False


class AssetEventHandler(FileSystemEventHandler):  # type: ignore[misc]
    """Feeds candidate paths into the work queue; never analyses on the event thread."""

    def __init__(self, work_queue: "queue.Queue[Optional[Path]]") -> None:
        self._queue = work_queue

    def _submit(self, raw_path: str) -> None:
        path = Path(raw_path)
        if is_candidate(path, getattr(self, "exclude", ())):
            self._queue.put(path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._submit(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._submit(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._submit(event.dest_path)


class WatchWorker(threading.Thread):
    """Single worker thread: debounces duplicates, inspects, reports."""

    def __init__(
        self,
        work_queue: "queue.Queue[Optional[Path]]",
        gatekeeper: Gatekeeper,
        reporter: Reporter,
    ) -> None:
        super().__init__(name="assetgatekeeper-worker", daemon=True)
        self._queue = work_queue
        self._gatekeeper = gatekeeper
        self._reporter = reporter
        self._last_seen: dict[Path, tuple[int, int]] = {}

    def run(self) -> None:
        while True:
            path = self._queue.get()
            try:
                if path is None:  # shutdown sentinel
                    return
                self._process(path)
            except Exception:  # never let one bad asset kill the watcher
                LOG.exception("unexpected error while processing %s", path)
            finally:
                self._queue.task_done()

    def _process(self, path: Path) -> None:
        if not is_candidate(path, getattr(self, "exclude", ())) or not path.exists():
            return
        if not wait_until_stable(path):
            LOG.debug("skipping %s (vanished or never settled)", path.name)
            return

        stat = path.stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        if self._last_seen.get(path) == fingerprint:
            return  # duplicate event from the same write (created + modified)
        self._last_seen[path] = fingerprint

        self._reporter.report(self._gatekeeper.check(path))


# --- #
# Entry points
# --- #

def describe_budget(budget: Budget) -> str:
    """One--line summary of the active rules, printed at startup."""
    return (
        f"tris<={budget.max_triangles:,}  "
        f"texture<={budget.max_texture_edge}px  "
        f"pot={'on' if budget.require_power_of_two else 'off'}  "
        f"materials<={budget.max_material_slots}  "
        f"influences<={budget.max_bone_influences}  "
        f"uvs<={budget.max_uv_channels}  "
        f"degenerate={'allowed' if budget.allow_degenerate or budget.degenerate_mode == 'allow' else ('warned' if budget.degenerate_mode == 'warn' else 'blocked')}  "
        f"naming={'on' if budget.enforce_texture_naming else 'off'}  "
        f"refs={'checked' if budget.check_references else 'ignored'}  "
        f"pbr={'on' if budget.pbr_check else 'off'}  "
        f"audit={'on' if budget.audit_transforms else 'off'}"
        + (f"  texture<={budget.max_texture_mb:g}MB({budget.texture_format})"
           if budget.max_texture_mb else "")
        + (f"  mesh<={budget.max_mesh_mb:g}MB" if budget.max_mesh_mb else "")
        + (f"  names={','.join(budget.mesh_name_prefixes)}" if budget.mesh_name_prefixes else "")
        + (f"  conventions={','.join(c[0] for c in budget.naming_conventions)}"
           if budget.naming_conventions else "")
        + (f"  texel={budget.texel_density_min:g}-{budget.texel_density_max:g}px/m"
           if budget.texel_density_max else "")
        + (f"  lod>={budget.lod_min_levels}" if budget.lod_min_levels else "")
        + (f"  lod_ratio<={budget.lod_max_ratio:g}" if budget.lod_max_ratio else "")
        + ("  collision=" + ",".join(name for name, on in (
            ("required", budget.collision_require), ("names", budget.collision_name_match),
            ("convex", budget.collision_convex),
            (f"hulls<={budget.collision_max_hulls}", budget.collision_max_hulls),
            (f"hull_verts<={budget.collision_max_hull_vertices}",
             budget.collision_max_hull_vertices)) if on)
           if (budget.collision_require or budget.collision_name_match or budget.collision_convex
               or budget.collision_max_hulls or budget.collision_max_hull_vertices) else "")
        + (f"  bones<={budget.max_bones}" if budget.max_bones else "")
        + (f"  bones/section<={budget.max_bones_per_section}"
           if budget.max_bones_per_section else "")
        + (f"  bone-depth<={budget.max_bone_depth}" if budget.max_bone_depth else "")
        + ("  single-root" if budget.single_root_bone else "")
        + (f"  lightmap=uv{budget.lightmap_uv_channel}"
           + ("(required)" if budget.lightmap_require else "")
           + ("(0-1)" if budget.lightmap_inside_unit_square else "")
           + (f"(overlap<={budget.lightmap_max_overlap:g}%)"
              if budget.lightmap_max_overlap >= 0 else "")
           if budget.lightmap_checked else "")
        + (f"  platform={budget.platform}" if budget.platform else "")
    )


# --- #
# Incremental runs (G4): a verdict cache, and checks in parallel
# --- #

CACHE_FILENAME = ".agk-cache.json"
CACHE_FORMAT = 1
CACHE_DIR_ENV = "AGK_CACHE_DIR"


def default_cache_path(root: Path) -> Path:
    """Where a folder's verdict cache lives when --cache does not say.

    Never inside the folder being checked: a cache file committed with the
    assets could carry a "passed" for any file whose hash it names, and a CI
    run on a fresh checkout would believe it. One file per folder, in the
    user's cache directory (``%LOCALAPPDATA%\\AssetGatekeeper\\cache`` on
    Windows, ``~/.cache/asset-gatekeeper`` elsewhere, or ``$AGK_CACHE_DIR``).
    """
    base = os.environ.get(CACHE_DIR_ENV)
    if not base:
        if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
            base = str(Path(os.environ["LOCALAPPDATA"]) / "AssetGatekeeper" / "cache")
        else:
            base = str(Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
                       / "asset-gatekeeper")
    try:
        resolved = str(root.resolve())
    except OSError:
        resolved = str(root.absolute())
    key = hashlib.sha256(resolved.lower().encode("utf-8")).hexdigest()[:20]
    return Path(base) / f"{root.name or 'root'}-{key}.json"


def cache_inside(root: Path, cache: Path) -> bool:
    """Is the cache file somewhere the assets' authors can write it (the tree)?"""
    try:
        cache.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _file_state(path: Path) -> Optional[list]:
    """[size, mtime_ns] of a file, or None when it is not there."""
    try:
        info = path.stat()
    except OSError:
        return None
    if not os.path.isfile(path):
        return None
    return [info.st_size, info.st_mtime_ns]


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def dependency_paths(path: Path) -> list[Path]:
    """Every file a verdict on ``path`` can depend on, besides the file itself.

    The .mtl and maps of an .obj, a .gltf's buffers and images, and for an FBX
    every place an engine would look for each texture (so a texture that turns
    up in the fallback location invalidates the cached "missing reference").
    """
    if path.suffix.lower() in FBX_SUFFIXES:
        try:
            references = read_fbx(path).references
        except AssetError:
            return []
        found: list[Path] = []
        for _written, candidates in references:
            for candidate in candidates:
                normalised = candidate.replace("\\", "/")
                found.append(path.parent / normalised)
                found.append(path.parent / normalised.rsplit("/", 1)[-1])
        return found
    return [target for _written, target in resolve_references(path)]


def _verdict_to_json(verdict: Verdict, root: Path) -> dict:
    return {"problems": list(verdict.problems), "metric": verdict.metric,
            "stats": [list(item) for item in verdict.stats],
            "warnings": list(verdict.warnings)}


def _verdict_from_json(path: Path, data: dict) -> Verdict:
    return Verdict(path, tuple(data.get("problems", ())), data.get("metric", ""),
                   tuple(tuple(item) for item in data.get("stats", ())), (),
                   tuple(data.get("warnings", ())))


def rules_fingerprint(gatekeeper: "Gatekeeper") -> str:
    """Everything outside the file that decides its verdict, as one hash.

    The rules (budget, tag budgets, CLI overrides, tag mode, exceptions), the
    gate's own code and version, and the mesh library's version. An exception
    within its warning window says "ends in N days", so the fingerprint also
    carries those day counts: the cache turns over on those days, not otherwise.
    """
    today = gatekeeper.today or date.today()
    exceptions = []
    for item in gatekeeper.exceptions:
        left = (item.expires - today).days
        exceptions.append((repr(item), min(max(left, -1), EXCEPTION_WARN_DAYS + 1)))
    try:
        # The gate's code is the facade and every part: a change to any of
        # them must invalidate cached verdicts, as an edit to the one file did.
        digest = hashlib.sha256()
        for source in [GATE_HOME / "asset_gatekeeper.py",
                       *sorted(Path(__file__).resolve().parent.glob("*.py"))]:
            digest.update(source.read_bytes())
        code = digest.hexdigest()
    except OSError:
        code = ""
    try:
        mesh_library = getattr(importlib.import_module("trimesh"), "__version__", "")
    except Exception:
        mesh_library = "none"
    parts = [__version__, code, mesh_library, repr(gatekeeper.budget),
             repr(sorted((name, repr(budget)) for name, budget in gatekeeper.tag_budgets.items())),
             repr(sorted(gatekeeper.overrides.items())), gatekeeper.tag_mode,
             repr(gatekeeper.tag_similarity), repr(exceptions), repr(gatekeeper.want_memory),
             str(gatekeeper.root), str(gatekeeper.tag_root)]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


class VerdictCache:
    """``.agk-cache.json``: reuse the verdict of a file that has not changed.

    An entry is reused when the rules fingerprint matches and the file - and
    every file its verdict depends on (``dependency_paths``) - has the same size
    and modification time as last run. A file whose time changed but whose bytes
    did not (a fresh checkout) is hashed, found identical and reused. Verdicts
    that are unreadable or healed are never stored; with healing on, a stored
    failure is re-checked so the healer gets its chance.
    """

    def __init__(self, path: Path, root: Path, fingerprint: str,
                 reuse_failures: bool = True) -> None:
        self.path = path
        self.root = root
        self.fingerprint = fingerprint
        self.reuse_failures = reuse_failures
        self.entries: dict[str, dict] = {}
        self.seen: set[str] = set()
        self.hits = 0
        self.misses = 0
        self.dirty = False
        self.reset_reason = ""
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self.reset_reason = f"unreadable ({exc.__class__.__name__}), starting fresh"
            self.dirty = True
            return
        if not isinstance(document, dict) or document.get("format") != CACHE_FORMAT:
            self.reset_reason = "from another version, starting fresh"
            self.dirty = True
            return
        if document.get("fingerprint") != fingerprint:
            self.reset_reason = "rules or gate changed, re-checking everything"
            self.dirty = True
            return
        entries = document.get("entries")
        self.entries = entries if isinstance(entries, dict) else {}

    def _key(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def lookup(self, path: Path) -> Optional[Verdict]:
        key = self._key(path)
        self.seen.add(key)
        entry = self.entries.get(key)
        state = _file_state(path)
        if not isinstance(entry, dict) or state is None:
            self.misses += 1
            return None
        verdict_data = entry.get("verdict") or {}
        if verdict_data.get("problems") and not self.reuse_failures:
            self.misses += 1
            return None
        if entry.get("state") != state:
            try:
                same_bytes = entry.get("sha256") == _sha256_of(path)
            except OSError:
                same_bytes = False
            if not same_bytes:
                self.misses += 1
                return None
            entry["state"] = state          # touched, not changed
            self.dirty = True
        for name, recorded in (entry.get("deps") or {}).items():
            if _file_state(Path(name) if os.path.isabs(name) else self.root / name) != recorded:
                self.misses += 1
                return None
        self.hits += 1
        return _verdict_from_json(path, verdict_data)

    def store(self, path: Path, verdict: Verdict) -> None:
        key = self._key(path)
        self.seen.add(key)
        if verdict.unreadable or verdict.healed or verdict.path != path:
            self.entries.pop(key, None)
            self.dirty = True
            return
        state = _file_state(path)
        if state is None:
            return
        try:
            sha = _sha256_of(path)
            deps = {}
            for dependency in dependency_paths(path):
                deps[self._key(dependency)] = _file_state(dependency)
        except (OSError, AssetError):
            return
        self.entries[key] = {"state": state, "sha256": sha, "deps": deps,
                             "verdict": _verdict_to_json(verdict, self.root)}
        self.dirty = True

    def save(self) -> None:
        stale = set(self.entries) - self.seen
        for key in stale:
            del self.entries[key]
        if not (self.dirty or stale):
            return
        document = {"format": CACHE_FORMAT, "tool": f"asset-gatekeeper {__version__}",
                    "fingerprint": self.fingerprint, "entries": self.entries}
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            LOG.warning("could not write the cache %s: %s", self.path, exc)
            try:
                temporary.unlink()
            except OSError:
                pass


_POOL_GATEKEEPER: Optional["Gatekeeper"] = None


def _pool_start(gatekeeper: "Gatekeeper") -> None:
    global _POOL_GATEKEEPER
    _POOL_GATEKEEPER = gatekeeper


def _pool_check(path: Path) -> Verdict:
    assert _POOL_GATEKEEPER is not None
    try:
        return _POOL_GATEKEEPER.check(path)
    except Exception as exc:    # one bad file must not take down the whole run
        return Verdict(path, (f"could not be checked ({exc.__class__.__name__}: {exc})",),
                       unreadable=True)


def bake_defaults(budget: "Budget") -> tuple[int, int]:
    """(triangles, texture edge) a baked asset should aim for under ``budget``."""
    triangles = max(100, min(budget.max_triangles, 30_000))
    edge = 16
    while edge * 2 <= min(budget.max_texture_edge, 2048):
        edge *= 2
    return triangles, edge


def run_bake_polypaint(source: Path, budget: "Budget", args: argparse.Namespace) -> int:
    """--bake-polypaint / --bake: make a game asset from a Polypaint .obj or a
    textured model, then gate it."""
    if not source.is_file():
        LOG.error("--bake takes one file: an .obj with ZBrush Polypaint or vertex colours, "
                  "or a textured .obj, .gltf or .glb")
        return 2
    module = polypaint_module()
    problem = module.bake_problem(source)
    if problem:
        LOG.error("%s", problem)
        return 2
    preset = getattr(args, "bake_preset", None) or ""
    triangles, edge = bake_defaults(budget)
    # Asked for more than the folder allows, the bake would run (an 8192 bake:
    # 85 s and 4 GB) and then fail the gate it just ran for. Say so first.
    over = []
    if args.bake_texture and budget.max_texture_edge and args.bake_texture > budget.max_texture_edge:
        over.append(f"--bake-texture {args.bake_texture} is larger than the "
                    f"{budget.max_texture_edge} px texture limit here ([texture] max_edge); use "
                    f"--bake-texture {budget.max_texture_edge} or less, or raise max_edge")
    if args.bake_triangles and budget.max_triangles and args.bake_triangles > budget.max_triangles:
        over.append(f"--bake-triangles {args.bake_triangles:,} is more than the "
                    f"{budget.max_triangles:,} triangle budget here ([mesh] max_triangles); use "
                    f"--bake-triangles {budget.max_triangles} or less, or raise max_triangles")
    if over:
        for message in over:
            LOG.error("%s: the baked asset would fail the gate", message)
        return 2
    if preset:
        # A preset sets everything, within the folder's rules: n64 under a
        # 1,200-triangle, 64 px rules.toml is exactly that.
        try:
            settings = module.resolve_settings(
                preset, args.bake_triangles or 0, args.bake_texture or 0,
                getattr(args, "bake_normal", None), getattr(args, "bake_colours", None),
                max_triangles=budget.max_triangles, max_texture=budget.max_texture_edge,
                face_texture=getattr(args, "bake_face", None),
                max_materials=budget.max_material_slots,
                head_share=getattr(args, "bake_head_share", None),
                face_layout=getattr(args, "bake_face_layout", None) or "",
                body_textures=getattr(args, "bake_body_textures", None) or 0,
                painted_parts=getattr(args, "bake_paint_parts", None))
        except module.PolypaintError as exc:
            LOG.error("%s", exc)
            return 2
    else:
        # The defaults follow the folder's rules, so a strict rules.toml makes a
        # tiny asset; say so, with the flag that overrides it.
        if not args.bake_texture and edge < 512:
            LOG.warning("the texture budget here is %d px, so the baked texture will be %dx%d; "
                        "use --bake-texture 2048 for more detail, or --bake-preset n64 for a "
                        "retro asset (16 colours, no normal map)", edge, edge, edge)
        if not args.bake_triangles and triangles < 5000:
            LOG.warning("the triangle budget here is %s, so the baked mesh will have %s triangles; "
                        "use --bake-triangles 30000 for more", f"{triangles:,}", f"{triangles:,}")
        normal = getattr(args, "bake_normal", None)
        try:
            settings = module.resolve_settings(
                "", args.bake_triangles or triangles, args.bake_texture or edge,
                True if normal is None else normal, getattr(args, "bake_colours", None) or 0,
                face_texture=getattr(args, "bake_face", None),
                max_materials=budget.max_material_slots,
                head_share=getattr(args, "bake_head_share", None),
                face_layout=getattr(args, "bake_face_layout", None) or "",
                body_textures=getattr(args, "bake_body_textures", None) or 0,
                painted_parts=getattr(args, "bake_paint_parts", None))
        except module.PolypaintError as exc:
            LOG.error("%s", exc)
            return 2
    if settings.get("face_texture_note"):
        LOG.warning("%s; raise max_material_slots in rules.toml to allow more",
                    settings["face_texture_note"])
    edge = settings["texture"]
    layout = settings.get("face_layout", "single")
    LOG.info("bake    %s -> %s triangles, %dx%d BaseColor%s%s%s%s%s", source.name,
             f"{settings['triangles']:,}", edge, edge,
             " + Normal" if settings["normal_map"] else "",
             f" in {settings['colours']} colours" if settings["colours"] else "",
             (" + a face texture" + (f" ({layout})" if layout != "single" else ""))
             if settings.get("face_texture") else "",
             " + a second body texture" if settings.get("body_textures", 1) > 1 else "",
             f" (preset {preset})" if preset else "")
    try:
        skip = [name.strip() for name in (getattr(args, "bake_skip_materials", None) or "").split(",")
                if name.strip()]
        result = module.bake(source, source.parent, triangles=settings["triangles"],
                             texture=edge, normal_map=settings["normal_map"],
                             colours=settings["colours"], preset=preset,
                             face_texture=settings.get("face_texture", False),
                             head_share=settings.get("head_share", 0.0),
                             skip_materials=skip,
                             face_layout=settings.get("face_layout", "single")
                             if settings.get("face_texture") else "single",
                             body_textures=settings.get("body_textures", 1),
                             painted_parts=settings.get("painted_parts", False),
                             progress=lambda name, i, n: LOG.info("bake    %d/%d %s", i + 1, n, name))
    except module.PolypaintError as exc:
        LOG.error("%s", exc)
        return 2
    except MemoryError:
        # --bake-texture 65536 asked numpy for 16 GB and ended in a traceback.
        LOG.error("not enough memory to bake %s at %dx%d with %s triangles; use a smaller "
                  "--bake-texture (2048 is the default; 8192 needs about 4 GB) or "
                  "--bake-triangles", source.name, edge, edge, f"{settings['triangles']:,}")
        return 2
    made = [Path(result["mesh"])] + [Path(name) for name in result["textures"]]
    LOG.info("bake    wrote %s in %s s (%s)", ", ".join(path.name for path in made),
             result["seconds"], result["note"])
    # A texture an earlier bake wrote that this one did not (a normal map before
    # an N64 bake, say) is not part of the new .glb; say so rather than leave it
    # looking current. Nothing is deleted.
    for name in module.texture_names(source):
        old = source.parent / name
        if old.is_file() and old not in made:
            LOG.warning("bake    %s is from an earlier bake and is not used by %s; "
                        "delete it if you no longer need it", name, Path(result["mesh"]).name)
    gatekeeper = Gatekeeper(budget)
    reporter = Reporter(color=not getattr(args, "no_color", False), root=source.parent,
                        json_mode=bool(getattr(args, "json", False)))
    failed = 0
    for path in made:
        verdict = gatekeeper.check(path)
        reporter.report(verdict)
        failed += 0 if verdict.passed else 1
    return 1 if failed else 0


def resolve_jobs(requested: Optional[int]) -> int:
    if requested is None:
        return 1
    if requested == 0:
        return max(1, os.cpu_count() or 1)
    return requested


#: Peak memory of one check, as a multiple of the file's size. Measured on a
#: 90 MB ZBrush OBJ (1.08 M triangles): 748 MB, so about 8.3x; rounded up.
MEMORY_PER_FILE_BYTE = 9
#: What a worker needs before it opens anything (Python, numpy, trimesh).
MEMORY_PER_WORKER = 256 * 1024 * 1024


def available_memory() -> Optional[int]:
    """Bytes of memory free for new work, or None when this system will not say."""
    try:
        if os.name == "nt":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                            ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                            ("total_virtual", ctypes.c_ulonglong),
                            ("avail_virtual", ctypes.c_ulonglong),
                            ("avail_extended", ctypes.c_ulonglong)]
            status = _Status()
            status.length = ctypes.sizeof(_Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return int(status.avail_phys)
            return None
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            for line in meminfo.read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        page = int(os.sysconf("SC_PAGE_SIZE"))
        try:
            return int(os.sysconf("SC_AVPHYS_PAGES")) * page    # other Unix
        except (OSError, ValueError):
            # macOS has no free-page count here (found by CI, 9 Nov 2026): assume
            # half the installed memory is free rather than giving up.
            return int(os.sysconf("SC_PHYS_PAGES")) * page // 2
    except (OSError, ValueError, AttributeError, ImportError):
        return None


def memory_safe_jobs(jobs: int, paths: Sequence[Path],
                     free: Optional[int] = None) -> int:
    """Lower ``jobs`` so N workers on the largest files fit in free memory.

    --jobs 0 on a 16-core PC used to start 16 workers whatever the files were;
    sixteen 90 MB sculpts at about 750 MB each is 12 GB (9 Nov 2026 audit, B8).
    Assumes each worker may get one of the largest files, and leaves 30 % free.
    """
    if jobs <= 1 or not paths:
        return max(1, jobs)
    free = available_memory() if free is None else free
    if not free:
        return jobs
    sizes = []
    for path in paths:
        try:
            sizes.append(path.stat().st_size)
        except OSError:
            continue
    largest = sorted(sizes, reverse=True)[:jobs]
    need_each = MEMORY_PER_WORKER + MEMORY_PER_FILE_BYTE * (max(largest) if largest else 0)
    allowed = max(1, int(free * 0.7) // need_each)
    if allowed < jobs:
        LOG.warning("checking %d files at a time, not %d: the largest is %.0f MB and needs "
                    "about %.0f MB each, with %.0f MB free", allowed, jobs,
                    (max(largest) if largest else 0) / 1048576, need_each / 1048576,
                    free / 1048576)
    return min(jobs, allowed)


def run_once(root: Path, gatekeeper: Gatekeeper, reporter: Reporter,
             recursive: bool, exclude: Sequence[str] = (),
             follow_symlinks: bool = False, allow_empty: bool = False,
             totals: Sequence["MemoryTotal"] = (), cache: Optional[VerdictCache] = None,
             jobs: int = 1) -> int:
    unreadable: list[str] = []
    walked = walk_files(root, recursive=recursive, follow_symlinks=follow_symlinks,
                        on_skip=unreadable.append)
    candidates = sorted(p for p in walked if is_candidate(p, exclude))
    skipped = skipped_files(walked, exclude)
    del walked

    # An entry the walker could not even list used to disappear without a word:
    # a locked 90,000-triangle mesh reported "no tracked assets found" and exit 0,
    # so CI stayed green on a repository nobody had actually gated.
    for message in unreadable:
        reporter.report(Verdict(Path(message), (f"cannot be read ({message})",),
                                unreadable=True))

    if not candidates and not unreadable:
        found = f" ({describe_skipped(skipped)})" if skipped["total"] else ""
        if allow_empty:
            LOG.info("no tracked assets found in %s%s (--allow-empty)", root, found)
            return 0
        LOG.error("no tracked assets found in %s%s - check the path, or pass "
                  "--allow-empty if this folder is meant to be empty", root, found)
        return 1

    checked: list[Verdict] = []
    ready: list[tuple[Path, Optional[Verdict]]] = []
    for path in candidates:
        if wait_until_stable(path):
            ready.append((path, cache.lookup(path) if cache is not None else None))
        else:
            LOG.warning("%s is still being written by another process; skipped", path.name)

    pending = [path for path, cached in ready if cached is None]
    futures: dict[Path, object] = {}
    pool = None
    if jobs > 1 and len(pending) > 1:
        jobs = memory_safe_jobs(min(jobs, len(pending)), pending)
    if jobs > 1 and len(pending) > 1:
        try:
            from concurrent.futures import ProcessPoolExecutor
            pool = ProcessPoolExecutor(max_workers=min(jobs, len(pending)),
                                       initializer=_pool_start, initargs=(gatekeeper,))
            futures = {path: pool.submit(_pool_check, path) for path in pending}
        except Exception as exc:   # no processes on this machine: one at a time
            LOG.warning("--jobs %d unavailable (%s); checking one file at a time", jobs, exc)
            if pool is not None:
                pool.shutdown(cancel_futures=True)
            pool, futures = None, {}
    try:
        # Reported in path order whatever finishes first, so two runs - and
        # --jobs 1 against --jobs 8 - print and report the same thing.
        for path, cached in ready:
            if cached is not None:
                verdict = cached
            elif path in futures:
                try:
                    verdict = futures[path].result()  # type: ignore[attr-defined]
                except Exception as exc:          # a worker died (out of memory)
                    verdict = Verdict(path, (f"could not be checked ({exc.__class__.__name__})",),
                                      unreadable=True)
            else:
                verdict = gatekeeper.check(path)
            if cached is None and cache is not None:
                cache.store(path, verdict)
            checked.append(verdict)
            reporter.report(verdict)
    finally:
        if pool is not None:
            pool.shutdown()
        if cache is not None:
            cache.save()
    if cache is not None:
        LOG.info("cache   %d reused, %d checked (%s)", cache.hits, cache.misses, cache.path.name)

    over = check_totals(totals, checked) if totals else []
    for verdict in over:
        # Printed and counted, and in the JUnit/SARIF report, but never sent as
        # telemetry: a folder total is not an asset on the dashboard.
        LOG.error("FAILED: %s  %s", printable(verdict.reason), str(verdict.path).replace("\\", "/"))
        if reporter.keep_verdicts:
            reporter.verdicts.append(verdict)
    reporter.summary(skipped, totals=over)
    return 1 if (reporter.failed or over) else 0


def run_watch(root: Path, gatekeeper: Gatekeeper, reporter: Reporter,
              recursive: bool, exclude: Sequence[str] = ()) -> int:
    if Observer is None:
        LOG.error("watchdog is not installed: python -m pip install watchdog")
        return 2

    work_queue: "queue.Queue[Optional[Path]]" = queue.Queue()
    worker = WatchWorker(work_queue, gatekeeper, reporter)
    worker.exclude = tuple(exclude)
    worker.start()

    handler = AssetEventHandler(work_queue)
    handler.exclude = tuple(exclude)
    observer = Observer()
    observer.schedule(handler, str(root), recursive=recursive)
    observer.start()

    LOG.info("watching %s - Ctrl+C to stop", root)

    try:
        while observer.is_alive():
            time.sleep(0.25)
    except KeyboardInterrupt:
        LOG.info("stopping")
    finally:
        observer.stop()
        observer.join(timeout=5)
        work_queue.put(None)
        worker.join(timeout=5)
        reporter.summary()
    return 0


def port_in_use_hint(port: int) -> str:
    """Platform-appropriate commands for finding and freeing a busy TCP port.

    "Address already in use" is the single most common first-run failure: the
    artist's own previous server is still alive, or another tool owns 8080. The
    message has to say what to type next, not just what went wrong.
    """
    if os.name == "nt":
        find_it = f"netstat -ano | findstr :{port}"
        stop_it = "taskkill /PID <pid> /F"
    else:
        find_it = f"lsof -i :{port}    (or: fuser -k {port}/tcp)"
        stop_it = "kill <pid>"
    # Suggest a port that is neither busy nor another server's default: telling
    # someone to move AssetHealth onto the workstation's 8100 is not a fix.
    spare = next(candidate for candidate in range(port + 1, port + 10)
                 if candidate not in (8080, 8100))
    return (f"hint: port {port} is already in use by another process.\n"
            f"      who has it:  {find_it}\n"
            f"      stop it:     {stop_it}\n"
            f"      or just move: --port {spare}")


def is_addr_in_use(exc: BaseException) -> bool:
    """Was this bind failure "somebody else has the port", or something worse?

    Worth separating because the *only* safe reaction to a busy port is to move
    to the next one. A missing interface (EADDRNOTAVAIL), a privilege problem on
    a low port, or a malformed host must stay a hard error - walking forward
    through ten ports would just bury the real message.

    Windows reports a port held by another process as either WSAEADDRINUSE
    (10048) or, maddeningly, WSAEACCES (10013, "access permissions"). Both are
    treated as "try the next port", and a range excluded by Hyper-V simply
    exhausts the retry budget and reports the original error.
    """
    import errno

    numbers = {getattr(exc, "errno", None), getattr(exc, "winerror", None)}
    busy = {errno.EADDRINUSE, 98, 48, 10048, 10013}
    if numbers & busy:
        return True
    text = str(exc).lower()
    return "in use" in text or "address already" in text or "access permissions" in text


def port_is_busy(host: str, port: int) -> bool:
    """Is somebody using this port right now?

    Two probes, because either one alone lies somewhere:

    * ``connect()`` - authoritative on Windows, where ``SO_REUSEADDR`` lets a
      second socket bind a port another process is already *listening* on. A
      bind test alone would call that port free and two servers would then fight
      over the same port, silently.
    * a bind with no ``SO_REUSEADDR`` - catches a listener that is not accepting
      connections (still occupied) and is the only signal on POSIX for a socket
      that is bound but not listening.
    """
    import socket

    address = ("127.0.0.1" if host in ("", "0.0.0.0", "::") else host, int(port))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        if probe.connect_ex(address) == 0:
            return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(address)
        except OSError:
            return True
    return False


def next_free_port(host: str, start: int, reserved: tuple[int, ...] = (),
                   tries: int = 20) -> Optional[int]:
    """First port at or after ``start`` that nobody is using.

    A *pre-check*, for callers that must know the port before starting anything
    (a launcher writing a URL file, the ``--port-auto`` loop). It is inherently
    racy - another process can take the port between this call and the bind - so
    callers still have to handle a bind failure.
    """
    for candidate in range(start, start + tries):
        if candidate in reserved:
            continue
        if port_is_busy(host, candidate):
            continue
        return candidate
    return None


def bind_with_fallback(build, host: str, port: int, auto: bool = False,
                       tries: int = 10, reserved: tuple[int, ...] = (),
                       report=None):
    """Build a server on ``port``, optionally walking forward when it is busy.

    Returns ``(server, port)``. Re-raises the original ``OSError`` when ``auto``
    is off, when the failure is not "port busy", or when the budget is spent -
    a caller that asked for the default port and did not ask for fallback must
    still get an honest failure.

    ``report(old_port, new_port, exc)`` is called once per hop so the CLI can
    say what happened; silence here would be the dangerous part, because a
    client configured with ``--telemetry-url http://host:8080/...`` would keep
    posting to whatever now owns 8080.
    """
    import errno

    attempt = int(port)
    last: Optional[BaseException] = None
    hops = 0
    while True:
        # Pre-check, because on Windows a bind onto an occupied port does not
        # necessarily raise - and a second server silently sharing a port is
        # worse than an error message.
        if auto and port_is_busy(host, attempt):
            last = OSError(errno.EADDRINUSE, f"port {attempt} is in use")
            if hops >= tries:
                raise last
            spare = next_free_port(host, attempt + 1, reserved)
            if spare is None:
                raise last
            hops += 1
            if report is not None:
                report(attempt, spare, last)
            attempt = spare
            continue
        try:
            return build(host, attempt), attempt
        except OSError as exc:
            last = exc
            if not auto or not is_addr_in_use(exc) or hops >= tries:
                raise
            spare = next_free_port(host, attempt + 1, reserved)
            if spare is None:
                raise last from None
            hops += 1
            if report is not None:
                report(attempt, spare, exc)
            attempt = spare


def run_doctor(target: Optional[Path], args: argparse.Namespace,
               out: Optional[object] = None) -> int:
    """Answer "can this machine run the gate?" in one command.

    Every check that fails comes with the fix, because a setup tool that only
    reports failure is just a slower error message. Ports are only a warning:
    something else using 8080 does not stop you gating a folder.
    """
    say = out or print
    problems = 0

    def line(status: str, label: str, detail: str = "", hint: str = "") -> None:
        nonlocal problems
        # Lowercase on purpose: the test harness greps "^  FAIL", and a doctor
        # line must never be mistaken for a failing assertion.
        mark = {"ok": "ok  ", "warn": "warn", "fail": "fail"}[status]
        if status == "fail":
            problems += 1
        say(f"  {mark}  {label}" + (f"  [{detail}]" if detail else ""))
        if hint and status != "ok":
            say(f"        -> {hint}")

    say(f"AssetGatekeeper {__version__} - environment check")
    say("")

    version = sys.version_info
    line("ok" if version >= (3, 9) else "fail", "python",
         f"{version.major}.{version.minor}.{version.micro}",
         "the gate needs Python 3.9+; install it, then re-create the venv")

    for name, module in (("trimesh", trimesh), ("pillow", Image)):
        if module is None:
            line("fail", name, "not importable",
                 f"python -m pip install {name if name != 'pillow' else 'pillow'}")
        else:
            found = getattr(module, "__version__", "?")
            line("ok", name, f"version {found}")

    try:
        import charset_normalizer  # presence check only
        line("ok", "charset-normalizer",
             f"{getattr(charset_normalizer, '__version__', '?')} - non-UTF-8 .obj/.mtl")
    except Exception:
        line("warn", "charset-normalizer", "not installed",
             "python -m pip install charset-normalizer (an .obj/.mtl that is not UTF-8 "
             "fails as unreadable without it)")

    try:
        import watchdog  # presence check only
        line("ok", "watchdog", f"{getattr(watchdog, '__version__', '?')} - native events")
    except Exception:
        line("warn", "watchdog", "not installed",
             "optional: python -m pip install watchdog (polling fallback is in use)")

    say("")
    root = target or Path(".")
    if not root.exists():
        line("fail", "target folder", str(root), "check the path, or run without a folder")
        root = None
    elif not root.is_dir():
        line("ok", "target folder", f"{root} (single file)")
    else:
        readable = os.access(str(root), os.R_OK)
        line("ok" if readable else "fail", "target folder", str(root),
             "the gate cannot read this folder - check the share permissions")

    say("")
    try:
        budget, source, _overrides = resolve_budget(root or Path("."), args)
        if source is None:
            line("warn", "rules.toml", "not found, using built-in defaults",
                 "write one with: asset-gatekeeper <folder> --init")
        else:
            line("ok", "rules.toml", str(source))
        line("ok", "budget", f"{budget.max_triangles:,} tris / "
                             f"{budget.max_texture_edge}px / "
                             f"{budget.max_material_slots} slots"
                             + (f" / platform {budget.platform}" if budget.platform else ""))
        tags, mode, _sim = resolve_tagging(root or Path("."), args)
        if tags:
            line("ok", "tag budgets", f"{len(tags)}: {', '.join(sorted(tags))} (mode {mode})")
        else:
            line("warn", "tag budgets", f"none (mode {mode})",
                 "optional: [tags.<name>] gives a folder its own limits")
    except ConfigError as exc:
        line("fail", "rules.toml", str(exc),
             "fix the file, or compare it with asset_gatekeeper.example.toml")

    say("")
    for port, who in ((8080, "AssetHealth"), (8100, "Art Workstation")):
        if not port_is_busy("127.0.0.1", port):
            line("ok", f"port {port}", "free")
        else:
            line("warn", f"port {port}", f"in use (would block {who})",
                 port_in_use_hint(port).splitlines()[0].replace("hint: ", ""))

    say("")
    if problems:
        say(f"{problems} problem(s) found - fix the 'fail' lines above.")
    else:
        say("No blocking problems. Next:")
        if root is not None and root.is_dir():
            say(f'  asset-gatekeeper "{root}" --recursive --once')
        else:
            say('  asset-gatekeeper "<folder>" --recursive --once')
        say("  asset-health --port 8080        (dashboard)")
        say("  art-workstation --port 8100     (3D review screen)")
    return 1 if problems else 0


RULES_HEADER = """\
# rules.toml - AssetGatekeeper
#
# Plain text on purpose: this file is reviewed in pull requests, diffed in git
# and read by CI. Edit it here, or through the admin page (`asset-health` ->
# /admin), which writes it atomically and records who changed what.
#
# Precedence: CLI flag > central/remote rules > [platforms.<name>] > this file
# > shipped defaults. Unknown keys are a hard error, deliberately.
"""


def _toml_value(value: object) -> str:
    """One TOML value. Only the types a rules file can legally contain."""
    import json as _json

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ConfigError(f"cannot write {value!r} to TOML")
        return repr(value)
    if isinstance(value, str):
        return _json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ConfigError(f"cannot write {type(value).__name__} to TOML")


def _emit_table(lines: list[str], path: str, table: dict) -> None:
    if lines:
        lines.append("")
    lines.append(f"[{path}]")
    nested: list[tuple[str, dict]] = []
    for key, value in table.items():
        if isinstance(value, dict):
            nested.append((key, value))
        else:
            lines.append(f"{key} = {_toml_value(value)}")
    for key, value in nested:
        _emit_table(lines, f"{path}.{key}", value)


def to_toml(document: dict, header: str = RULES_HEADER) -> str:
    """Serialise a rules document as canonical, diff-friendly TOML.

    Canonical matters more than pretty: the admin page rewrites this file on
    every apply, so a stable key order and stable spacing are what keep the git
    history readable. Because the file is regenerated, hand-written comments are
    not preserved - the header says so, and the audit table plus `git log` are
    the record of who changed what.

    ``parse_rules`` of the output round-trips to the same document (tested).
    """
    lines: list[str] = []
    if header:
        lines.extend(header.rstrip("\n").splitlines())
        lines.append("")
    scalars = [(k, v) for k, v in document.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in document.items() if isinstance(v, dict)]
    for key, value in scalars:
        lines.append(f"{key} = {_toml_value(value)}")
    for key, table in tables:
        _emit_table(lines, key, table)
    return "\n".join(lines).rstrip("\n") + "\n"


def _non_negative_int(text: str) -> int:
    """argparse type for a count that may legitimately be 0 (e.g. materials).

    ``--triangles -1`` used to be accepted and then failed every mesh in the
    repository, which reads as an art problem rather than a typo.
    """
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return value


def _positive_int(text: str) -> int:
    """argparse type for a size or a rate, which has no meaningful zero."""
    value = _non_negative_int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more, got 0")
    return value


# --- #
# CI reports (G3): one entry per asset, in the formats build servers display
# --- #

REPORT_FORMATS = ("junit", "sarif", "github")


def _report_target(text: str) -> tuple[str, str]:
    kind, sep, path = str(text).partition(":")
    kind = kind.strip().lower()
    if not sep or kind not in REPORT_FORMATS or not path.strip():
        raise argparse.ArgumentTypeError(
            f"use junit:<file>, sarif:<file> or github:<file>, got {text!r}")
    return kind, path.strip()


def _relative_name(path: Path, root: Optional[Path]) -> str:
    if str(path).startswith("[total] "):       # a [[totals]] line, not a file
        return str(path).replace("\\", "/")
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return path.name


#: Characters XML 1.0 cannot carry at all (control characters, lone surrogates).
_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def junit_report(verdicts: Sequence[Verdict], root: Optional[Path] = None) -> str:
    """JUnit XML: one test case per asset. Jenkins, TeamCity, Horde, GitLab and
    Azure DevOps all show these per test, so a failing asset is a red line with
    its path, not a word in a build log."""
    from xml.sax.saxutils import escape as _escape, quoteattr as _quoteattr

    # XML 1.0 has no way to write most control characters, even escaped: one in
    # a file or node name made the whole report unreadable to a CI server.
    def clean(text: str) -> str:
        return _XML_INVALID.sub("�", text)

    def escape(text: str) -> str:
        return _escape(clean(text))

    def quoteattr(text: str) -> str:
        return _quoteattr(clean(text))

    failures = sum(1 for verdict in verdicts if not verdict.passed and not verdict.unreadable)
    errors = sum(1 for verdict in verdicts if verdict.unreadable)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<testsuites name="AssetGatekeeper" tests="{len(verdicts)}" failures="{failures}" '
             f'errors="{errors}">',
             f'  <testsuite name="asset budgets" tests="{len(verdicts)}" failures="{failures}" '
             f'errors="{errors}" skipped="0">']
    for verdict in verdicts:
        name = _relative_name(verdict.path, root)
        folder = name.rsplit("/", 1)[0] if "/" in name else "."
        lines.append(f"    <testcase classname={quoteattr(folder)} name={quoteattr(name)}>")
        body = "\n".join(verdict.problems)
        if verdict.unreadable:
            lines.append(f"      <error message={quoteattr(verdict.reason)}>{escape(body)}</error>")
        elif not verdict.passed:
            lines.append(f"      <failure message={quoteattr(verdict.reason)} "
                         f"type=\"budget\">{escape(body)}</failure>")
        notes = list(verdict.heal_notes) + list(verdict.warnings)
        if verdict.metric or notes:
            out = "\n".join(([verdict.metric] if verdict.metric else []) + notes)
            lines.append(f"      <system-out>{escape(out)}</system-out>")
        lines.append("    </testcase>")
    lines += ["  </testsuite>", "</testsuites>", ""]
    return "\n".join(lines)


def sarif_report(verdicts: Sequence[Verdict], root: Optional[Path] = None) -> str:
    """SARIF 2.1.0: GitHub code scanning shows each failing asset as an annotation
    on the file in the pull request."""
    results = []
    for verdict in verdicts:
        if verdict.passed:
            continue
        name = _relative_name(verdict.path, root)
        # A URI, not a path: spaces, '#' and non-ASCII are percent-encoded. A folder
        # total is not a file; it is reported against the rules file that set it.
        target = RULES_FILENAME if name.startswith("[total] ") else name
        location = {"physicalLocation": {"artifactLocation": {
            "uri": quote(target.replace("\\", "/"), safe="/"), "uriBaseId": "%SRCROOT%"}}}
        for problem in verdict.problems or ("could not be checked",):
            results.append({
                "ruleId": "agk/unreadable" if verdict.unreadable else "agk/budget",
                "level": "error",
                "message": {"text": problem},
                "locations": [location]})
    document = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "AssetGatekeeper", "version": __version__,
                "informationUri": "https://github.com/ohio13/AssetGateKeeper",
                "rules": [
                    {"id": "agk/budget", "name": "AssetBudget",
                     "shortDescription": {"text": "Asset exceeds the project's budget"}},
                    {"id": "agk/unreadable", "name": "AssetUnreadable",
                     "shortDescription": {"text": "Asset could not be read"}}]}},
            "results": results}]}
    return json.dumps(document, indent=2) + "\n"


def _github_escape(text: str, property_value: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_value:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


def github_report(verdicts: Sequence[Verdict], root: Optional[Path] = None) -> str:
    """GitHub Actions annotation commands (``::error file=...::...``).

    Printed by any workflow step (``cat`` the file), each failing asset is marked
    on the pull request's Files tab - free, in private repositories too, where
    SARIF upload needs paid code scanning."""
    lines = []
    cwd = Path.cwd().resolve()
    for verdict in verdicts:
        if verdict.passed:
            continue
        name = _relative_name(verdict.path, root)
        if not name.startswith("[total] "):
            try:
                name = verdict.path.resolve().relative_to(cwd).as_posix()
            except (ValueError, OSError):
                pass
        title = "AssetGatekeeper: could not read" if verdict.unreadable else "AssetGatekeeper"
        for problem in verdict.problems or ("could not be checked",):
            lines.append(f"::error file={_github_escape(name, True)},"
                         f"title={_github_escape(title, True)}::{_github_escape(problem)}")
    return "\n".join(lines) + ("\n" if lines else "")


def write_reports(targets: Sequence[tuple[str, str]], verdicts: Sequence[Verdict],
                  root: Optional[Path]) -> None:
    writers = {"junit": junit_report, "sarif": sarif_report, "github": github_report}
    for kind, destination in targets:
        text = writers[kind](verdicts, root)
        path = Path(destination).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        # errors="replace": a file name that is not valid UTF-8 (Linux allows any
        # bytes) reaches Python as surrogates, and used to stop the run here.
        path.write_text(text, encoding="utf-8", errors="replace")
        LOG.info("report  %s written to %s (%d assets)", kind, path, len(verdicts))


class _GateParser(argparse.ArgumentParser):
    """``args.folder`` stays the one path every caller reads; ``args.paths`` has them all.

    Intermixed, so ``a.fbx --once b.png`` takes both files, as ``a.fbx b.png --once`` does.
    """

    def parse_args(self, args=None, namespace=None):  # type: ignore[override]
        parsed = self.parse_intermixed_args(args, namespace)
        paths = list(parsed.folder or [])
        parsed.paths = paths
        parsed.folder = paths[0] if paths else None
        return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = _GateParser(
        prog="asset_gatekeeper",
        description="Watch a folder of art exports and enforce asset budgets.",
        epilog=(
            "common first runs:\n"
            "  asset-gatekeeper <folder> --recursive --once     one scan, then exit\n"
            "  asset-gatekeeper a.fbx b.png --once --json       several files in one run\n"
            "  asset-gatekeeper <folder> --init                 write a rules.toml\n"
            "  asset-gatekeeper <folder> --wizard               ...from questions about your project\n"
            "  asset-gatekeeper --doctor                        check this machine\n"
            "  asset-gatekeeper <folder> --recursive            watch and toast\n"
            "\n"
            "exit codes: 0 all passed, 1 at least one failed, 2 bad setup.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("folder", nargs="*", default=None, metavar="PATH",
                        help="folder to watch or check (default: current directory), or one "
                             "asset file; several asset files with --once check each as one "
                             "file would be, one verdict each in the order given")
    parser.add_argument("--triangles", type=_non_negative_int, default=None,
                        help="max triangles per mesh (overrides rules.toml; default 50,000)")
    parser.add_argument("--max-texture", type=_positive_int, default=None, metavar="EDGE",
                        help="max texture width/height in pixels (overrides rules.toml; default 2,048)")
    parser.add_argument("--max-materials", type=_non_negative_int, default=None, metavar="N",
                        help="max material slots per mesh (overrides rules.toml; default 3)")
    parser.add_argument("--max-influences", type=_non_negative_int, default=None, metavar="N",
                        help="max bone weights per vertex (overrides rules.toml; default 4)")
    parser.add_argument("--max-uvs", type=_positive_int, default=None, metavar="N",
                        help="max UV channels per mesh (overrides rules.toml; default 4)")
    parser.add_argument("--allow-degenerate", action="store_true", default=None,
                        help="do not fail meshes containing zero-area triangles")
    parser.add_argument("--allow-npot", action="store_false", dest="require_power_of_two", default=None,
                        help="accept textures whose edges are not a power of two")
    parser.add_argument("--no-naming", action="store_false", dest="enforce_texture_naming", default=None,
                        help="accept texture names without a standard suffix")
    parser.add_argument("--rules", metavar="PATH",
                        help=f"rules file to use instead of <folder>/{RULES_FILENAME}")
    parser.add_argument("--preset", metavar="NAME", default=None,
                        help=f"apply a shipped preset ({', '.join(sorted(PRESETS))}); "
                             "beats rules.toml but not an explicit numeric flag")
    parser.add_argument("--tag-mode", choices=TAG_MODES, default=None, dest="tag_mode",
                        help="how a near-miss asset tag is handled: auto (accept), "
                             "suggest (accept + note, default), strict (fail with the fix)")
    parser.add_argument("--notify", action="store_true", dest="notify", default=None,
                        help="native desktop toasts on failure/heal (default: on while watching)")
    parser.add_argument("--no-notify", action="store_false", dest="notify", default=None,
                        help="never raise desktop toasts")
    parser.add_argument("--notify-max", type=_positive_int, default=None, metavar="N",
                        help="maximum toasts per minute (default 6)")
    parser.add_argument("--exclude", action="append", metavar="GLOB", default=None,
                        help="ignore files matching this glob, e.g. 'preview_*' "
                             "(repeatable; same as [filters] exclude)")
    parser.add_argument("--platform", metavar="NAME", default=None,
                        help="apply the [platforms.<NAME>] compression profile from rules.toml "
                             "(e.g. mobile, console, pc)")
    parser.add_argument("--no-reference-check", action="store_false", dest="check_references",
                        default=None, help="do not fail assets with missing texture/buffer links")
    parser.add_argument("--no-pbr-check", action="store_false", dest="pbr_check", default=None,
                        help="do not clamp albedo luminance")
    parser.add_argument("--audit-transforms", action="store_true", dest="audit_transforms",
                        default=None,
                        help="flag unbaked node transforms, uncentred pivots and unindexed geometry")
    parser.add_argument("--require-indexed", action="store_true", dest="require_indexed",
                        default=None, help="also fail triangle-soup geometry (implies the audit)")
    parser.add_argument("--doctor", action="store_true",
                        help="check this machine can run the gate (deps, rules, ports), then exit")
    parser.add_argument("--bake-polypaint", "--bake", action="store_true", dest="bake_polypaint",
                        help="turn an .obj with ZBrush Polypaint (or vertex colours), or a "
                             "textured .obj/.gltf/.glb, into a game asset beside it: a reduced "
                             ".glb with UVs, a baked _BaseColor texture (every material's "
                             "textures in one) and a _Normal map, sized to the folder's rules, "
                             "then check them. The source "
                             "is never changed. Needs: pip install \"asset-gatekeeper[bake]\"")
    parser.add_argument("--bake-triangles", type=int, default=None, metavar="N",
                        help="with --bake-polypaint: triangles to reduce to (default: the "
                             "budget, at most 30,000)")
    parser.add_argument("--bake-texture", type=int, default=None, metavar="PX",
                        help="with --bake-polypaint: texture size, a power of two (default: "
                             "the texture budget, at most 2048)")
    parser.add_argument("--bake-preset", default=None, metavar="NAME", choices=("n64", "n64-pak"),
                        help="with --bake-polypaint: n64 = Nintendo 64 style: about 1,200 "
                             "triangles, a 64 px texture in 16 colours, no normal map; "
                             "n64-pak = with the Expansion Pak, for a character with little "
                             "else on screen: 3,000 triangles (60%% for the head), the face "
                             "mirrored with an eye strip of its own "
                             "(never more than the folder's rules allow)")
    parser.add_argument("--bake-no-normal", action="store_false", dest="bake_normal",
                        default=None,
                        help="with --bake-polypaint: do not bake a normal map (one is made "
                             "by default, except with --bake-preset n64)")
    parser.add_argument("--bake-face-texture", action="store_true", dest="bake_face", default=None,
                        help="with --bake-polypaint: give the face (the sculpt's densest colour "
                             "detail) a texture of its own, the same size, as a second material "
                             "(on with --bake-preset n64)")
    parser.add_argument("--bake-no-face-texture", action="store_false", dest="bake_face",
                        help="with --bake-polypaint: one texture for the whole model")
    parser.add_argument("--bake-colours", type=int, default=None, metavar="N",
                        help="with --bake-polypaint: reduce the BaseColor to N colours "
                             "(16 or 256 like the N64's palette textures; 0 keeps them all)")
    parser.add_argument("--bake-head-share", type=float, default=None, metavar="SHARE",
                        help="with --bake: give the head (above the neck, found from the face) "
                             "this share of the triangles, 0.35-0.8 say, and the body the "
                             "rest: more keeps more of the head's shape (0 or unset: even)")
    parser.add_argument("--bake-face-layout", default=None, metavar="LAYOUT",
                        choices=("single", "mirror", "eyes", "eyes-mirror"),
                        help="with --bake and a face texture: single (the face from the front), "
                             "mirror (half the face, drawn mirrored: twice the texels across), "
                             "eyes (a strip of its own for the eyes and brows) or eyes-mirror "
                             "(both); each extra texture is another material")
    parser.add_argument("--bake-body-textures", type=int, default=None, choices=(1, 2),
                        metavar="N",
                        help="with --bake: 2 gives the rest of the head (hair, ears) a "
                             "texture of its own, split from the body at the neck - or the "
                             "lower body, on a model with no face (another material); 1 by "
                             "default")
    parser.add_argument("--bake-paint-parts", action="store_true", dest="bake_paint_parts",
                        default=None,
                        help="with --bake: leave the small parts in the head (eyeballs, lashes, "
                             "teeth) off the reduced mesh and paint them on the surface behind "
                             "them, as an N64 face is (on with the n64 presets)")
    parser.add_argument("--bake-no-paint-parts", action="store_false", dest="bake_paint_parts",
                        help="with --bake: keep every small part as geometry")
    parser.add_argument("--bake-skip-materials", default=None, metavar="NAMES",
                        help="with --bake on a textured model: leave out the materials named "
                             "(comma-separated), e.g. a game rip's outline shell or weapon")
    parser.add_argument("--suggest-naming", action="store_true", dest="suggest_naming",
                        help="read the names in <folder> and print a [naming] block for "
                             "rules.toml that fits them, then exit (nothing is written)")
    parser.add_argument("--list-presets", action="store_true", dest="list_presets",
                        help="print the shipped platform targets and their budgets, then exit")
    parser.add_argument("--init", action="store_true",
                        help="interactive setup: create the watch folder and its rules.toml")
    parser.add_argument("--yes", action="store_true",
                        help="with --init or --wizard: accept every default instead of prompting")
    parser.add_argument("--force", action="store_true",
                        help="with --init: overwrite an existing rules.toml (with --wizard: replace "
                             "it without asking; a copy is still kept)")
    parser.add_argument("--wizard", action="store_true",
                        help="ask seven plain-English questions (engines, art tools, platforms, "
                             "kinds of asset, style, extra checks, broken geometry; several "
                             "answers each) and "
                             "write one commented rules.toml for all of them. Asks before "
                             "replacing a rules.toml and keeps a copy. With --yes: every "
                             "default, no questions")
    parser.add_argument("--wizard-answers", metavar="FILE", default=None, dest="wizard_answers",
                        help="the --wizard answers from a JSON file instead of a terminal "
                             "(for scripts), e.g. {\"engines\": [\"unreal\", \"unity\"], "
                             "\"platforms\": [\"pc\", \"switch\"]}")
    parser.add_argument("--audit", action="store_true",
                        help="audit the whole folder for broken references and orphaned "
                             "textures, then exit")
    parser.add_argument("--auto-heal", action="store_true", dest="auto_heal", default=None,
                        help="repair failing assets in place (NPOT textures, missing texture "
                             "suffixes, zero-area .obj faces) - off unless this or "
                             "[healer] enabled = true is set")
    parser.add_argument("--no-auto-heal", action="store_false", dest="auto_heal",
                        help="force healing off, even if rules.toml enables it (use in CI)")
    parser.add_argument("--heal", action="store_true",
                        help="repair this one file now, even with healing off: verified "
                             "before adoption, the original kept in .agk-backups/")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="with --heal: say what would change and write nothing")
    parser.add_argument("--undo-heal", action="store_true", dest="undo_heal",
                        help="put back what the last heal of this file replaced (refuses "
                             "if the healed file was edited since)")
    parser.add_argument("--list-heals", action="store_true", dest="list_heals",
                        help="list the heals recorded in this folder, newest first")
    parser.add_argument("--remote-rules", metavar="URL", default=None,
                        help="fetch the budget from a central endpoint; cached locally for "
                             f"offline use (or set {REMOTE_RULES_ENV})")
    parser.add_argument("--telemetry-url", metavar="URL", default=None,
                        help="POST every verdict to this endpoint (or set "
                             f"{SERVER_ENV_URL})")
    parser.add_argument("--telemetry-identity", choices=IDENTITY_MODES, default=None,
                        metavar="MODE",
                        help="what identifying detail telemetry carries (or set "
                             f"{SERVER_ENV_IDENTITY}): full (host, user, absolute path), "
                             "host (host and absolute path, the default), none (relative path)")
    parser.add_argument("--version", action="version",
                        version=f"asset-gatekeeper {__version__}",
                        help="print the version and exit (pipelines log this)")
    parser.add_argument("--once", action="store_true", help="scan existing files and exit")
    parser.add_argument("--allow-empty", action="store_true", dest="allow_empty",
                        help="with --once: exit 0 when the folder holds no tracked assets "
                             "(without it, an empty folder fails the run: a wrong path "
                             "is the usual reason)")
    parser.add_argument("--recursive", action="store_true", help="include subfolders")
    parser.add_argument("--cache", metavar="FILE", default=None,
                        help="with --once: where the verdict cache lives, outside the folder "
                             "(default: one file per folder in the user's cache directory, "
                             f"or ${CACHE_DIR_ENV}); unchanged files reuse their verdict")
    parser.add_argument("--no-cache", action="store_true", dest="no_cache",
                        help="with --once: check every file and leave the cache alone")
    parser.add_argument("--jobs", type=_non_negative_int, default=None, metavar="N",
                        help="with --once: check N files at a time in separate processes "
                             "(0 = one per CPU; default 1; healing always runs one at a time)")
    parser.add_argument("--follow-symlinks", action="store_true",
                        help="descend into symlinked/junctioned folders. Off by default: "
                             "a link re-counts the same tree and a self-link never ends. "
                             "Unreadable entries are always skipped, never fatal")
    parser.add_argument("--report", action="append", type=_report_target, default=[],
                        metavar="FORMAT:FILE",
                        help="with --once or a single file: also write junit:gate.xml (Jenkins, "
                             "TeamCity, Horde) or sarif:gate.sarif (GitHub code scanning); "
                             "repeatable")
    parser.add_argument("--exception-path", metavar="PATH", default=None,
                        help="with one file: the path [[exceptions]] match it as, relative to "
                             "the rules file's folder (the Perforce trigger passes the depot "
                             "path; its temp copy is laid out differently)")
    parser.add_argument("--json", action="store_true",
                        help="one JSON object per file on stdout (used by the DCC plugins)")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


#: Flags that do one job on one path: refused when several paths are given.
_ONE_PATH_FLAGS = (("doctor", "--doctor"), ("init", "--init"), ("wizard", "--wizard"),
                   ("wizard_answers", "--wizard-answers"), ("suggest_naming", "--suggest-naming"),
                   ("audit", "--audit"), ("heal", "--heal"), ("dry_run", "--dry-run"),
                   ("undo_heal", "--undo-heal"), ("list_heals", "--list-heals"),
                   ("bake_polypaint", "--bake-polypaint"), ("exception_path", "--exception-path"))


class _SetupLinesOnce(logging.Filter):
    """In a run over several files, each file's setup lines (rules, budget, tags...)
    are printed the first time only: they are the same for every file under one rules.toml."""

    PREFIXES = ("rules   ", "cli     ", "budget  ", "heal    ", "report  ", "tags    ",
                "except  ", "toasts  ", "healing ON")

    def __init__(self) -> None:
        super().__init__()
        self.seen: set[str] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        if not (isinstance(record.msg, str) and record.msg.startswith(self.PREFIXES)):
            return True
        text = record.getMessage()
        if text in self.seen:
            return False
        self.seen.add(text)
        return True


def run_many(args: argparse.Namespace) -> int:
    """``--once`` over several files in one run: ``a.fbx b.png c.glb --once --json``.

    For the Unity and Godot plug-ins, which check what one import brought in
    with one Python start-up (~0.7 s) instead of one per file. Each file is
    checked exactly as a run with that file alone checks it: --rules if given,
    else its own folder's rules.toml; [tags.*] and [[exceptions]] relative to
    the --rules folder when the file is under it; no [[totals]] (a few files
    are not the folder). Verdicts come in the order given, each with its own
    "file", then one summary line. A missing file, or one that is not an asset
    type, is an ERROR verdict of its own; a file whose rules cannot be used
    gets no verdict and makes the run exit 2. Exit: 2 if any file could not be
    checked that way, else 1 if any failed, else 0.
    """
    refused = [flag for attr, flag in _ONE_PATH_FLAGS if getattr(args, attr, None)]
    if refused:
        LOG.error("%s takes one path, and %d were given", refused[0], len(args.paths))
        return 2
    if not args.once:
        LOG.error("several paths are checked with --once (watching takes one folder)")
        return 2
    paths = [Path(raw).expanduser().resolve() for raw in args.paths]
    folder = next((path for path in paths if path.is_dir()), None)
    if folder is not None:
        LOG.error("%s is a folder: several paths must all be files. Check a folder on its "
                  "own (asset-gatekeeper <folder> --once)", folder)
        return 2
    if trimesh is None or Image is None:
        missing = [name for name, module in (("trimesh", trimesh), ("pillow", Image)) if module is None]
        LOG.error("missing dependencies: %s - python -m pip install %s", ", ".join(missing), " ".join(missing))
        return 2

    verdicts: list[Verdict] = []
    own = Reporter(color=not args.no_color and not args.json, json_mode=args.json)
    status = 0
    once = _SetupLinesOnce()
    LOG.addFilter(once)
    try:
        for raw, path in zip(args.paths, paths):
            problem = ("not found" if not path.exists()
                       else "not a tracked asset type" if not is_candidate(path) else "")
            if problem:
                verdict = Verdict(path, (problem,), unreadable=True)
                own.report(verdict)
                verdicts.append(verdict)
                status = max(status, 1)
                continue
            single = argparse.Namespace(**vars(args))
            single.folder, single.paths, single.report = raw, [raw], []
            status = max(status, run_args(single, sink=verdicts))
    finally:
        LOG.removeFilter(once)
    own.passed = sum(1 for verdict in verdicts if verdict.passed)
    own.failed = len(verdicts) - own.passed
    own.summary()
    if args.report:
        try:
            base: Optional[Path] = (Path(args.rules).expanduser().resolve().parent if args.rules
                                    else Path(os.path.commonpath([str(path.parent) for path in paths])))
        except ValueError:            # files on two Windows drives share no folder
            base = None
        write_reports(args.report, verdicts, base)
    return status


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        # Keep non-ASCII asset paths from killing cp1252/cp437 consoles. stderr
        # matters too: --json mode routes logs there, and a CJK filename would
        # otherwise raise UnicodeEncodeError mid-run.
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    # In JSON mode stdout is a data channel: logs must not pollute it.
    stream = sys.stderr if args.json else sys.stdout
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=stream)
    if len(getattr(args, "paths", ())) > 1:
        return run_many(args)
    return run_args(args)


def run_args(args: argparse.Namespace, sink: Optional[list] = None) -> int:
    """Everything main() does once the command line is parsed, for one path.

    ``sink``: run_many's list, which collects every verdict of a run over
    several files (for --report and the summary line).
    """
    if getattr(args, "list_presets", False):
        rows = preset_summary()
        print(f"{'preset':<10}{'platform':<26}{'char tris':>10}{'prop tris':>11}"
              f"{'texture':>9}{'slots':>7}{'POT':>5}{'PBR':>5}")
        for row in rows:
            print(f"{row['name']:<10}{str(row['label'])[:25]:<26}{row['character_triangles']:>10,}"
                  f"{row['prop_triangles']:>11,}{row['max_texture_edge']:>9}"
                  f"{row['max_material_slots']:>7}{'yes' if row['power_of_two'] else 'no':>5}"
                  f"{'yes' if row['pbr'] else 'no':>5}")
        print("\nlegacy aliases (retro, mobile) are kept so old scripts keep working.")
        print("apply one: asset-gatekeeper <folder> --preset n64")
        return 0

    if getattr(args, "suggest_naming", False):
        if not args.folder:
            print("error: --suggest-naming needs a folder", file=sys.stderr)
            return 2
        target = Path(args.folder).expanduser()
        if not target.is_dir():
            print(f"error: not a folder: {target}", file=sys.stderr)
            return 2
        print(suggest_naming(target, recursive=True), end="")
        return 0

    # The doctor runs before the dependency check on purpose: reporting a missing
    # trimesh is exactly its job.
    if getattr(args, "doctor", False):
        target = Path(args.folder).expanduser() if args.folder else None
        return run_doctor(target, args)

    # The wizard runs before anything touches the folder: it may be creating it.
    if getattr(args, "wizard", False) or getattr(args, "wizard_answers", None):
        target = Path(args.folder).expanduser() if args.folder else None
        answers = Path(args.wizard_answers).expanduser() if args.wizard_answers else None
        return engine_module("rules_wizard").run_wizard(
            target, answers_file=answers, force=args.force, assume_yes=args.yes)
    if getattr(args, "init", False):
        target = Path(args.folder).expanduser() if args.folder else None
        return run_init(target, ask=input, assume_yes=args.yes, force=args.force,
                        max_triangles=None, max_edge=None, max_material_slots=None)

    root = Path(args.folder or ".").expanduser().resolve()
    # Undo and the heal list read backups only: no budget, no trimesh. Before the
    # existence check: undoing a rename names a file that no longer exists.
    if getattr(args, "undo_heal", False) or getattr(args, "list_heals", False):
        return run_heal_history(root, undo=args.undo_heal)
    if not root.exists():
        LOG.error("not found: %s", root)
        return 2
    # Single-file mode lets a DCC plugin gate one temporary export directly.
    watch_dir = root if root.is_dir() else root.parent

    if getattr(args, "dry_run", False) and not getattr(args, "heal", False):
        LOG.error("--dry-run goes with --heal")
        return 2
    if getattr(args, "heal", False):
        if not root.is_file():
            LOG.error("--heal repairs one file: pass the file, not a folder "
                      "(folder-wide healing is --auto-heal)")
            return 2
        if getattr(args, "auto_heal", None) is False:
            LOG.error("--heal and --no-auto-heal contradict each other")
            return 2
        # Heal against the project, not the file's folder: the project's rules
        # judge the result, and the meshes that name a texture get repointed.
        try:
            project = healer_module().project_root_for(root.parent)
        except ConfigError as exc:
            LOG.error("%s", exc)
            return 2
        if project is not None:
            heal_project = project
            if not args.rules and not (root.parent / RULES_FILENAME).is_file() \
                    and (project / RULES_FILENAME).is_file():
                args.rules = str(project / RULES_FILENAME)
        else:
            heal_project = root.parent

    # Folder audit is a report, not a gate: it needs no budget and no deps
    # beyond the parser, so it runs before the dependency check on purpose.
    if getattr(args, "audit", False):
        if root.is_file():
            LOG.error("--audit needs a folder, not a single file")
            return 2
        return run_audit(watch_dir)

    if trimesh is None or Image is None:
        missing = [name for name, module in (("trimesh", trimesh), ("pillow", Image)) if module is None]
        LOG.error("missing dependencies: %s - python -m pip install %s", ", ".join(missing), " ".join(missing))
        return 2

    try:
        server = resolve_server(watch_dir, args)
    except RulesFileError as exc:
        LOG.error("invalid rules: %s", exc)
        return 2
    except ConfigError as exc:
        LOG.error("invalid server settings: %s", exc)
        return 2

    # Fetch before resolution so precedence is one flat decision:
    # CLI flag > remote rules > local rules.toml > defaults.
    remote_document = None
    if server.remote_rules_url:
        try:
            remote_document = fetch_remote_rules(
                server.remote_rules_url, watch_dir / RULES_CACHE_FILENAME, token=server.token
            )
        except ConfigError as exc:
            # Exit 2 ("the gate could not run"), never a traceback and exit 1,
            # which CI would read as "the assets failed".
            LOG.error("%s", exc)
            return 2

    try:
        budget, rules_source, overrides = resolve_budget(watch_dir, args, remote_document)
    except ConfigError as exc:
        LOG.error("invalid rules file: %s", exc)
        return 2

    if remote_document is not None:
        LOG.info("rules   %s (remote, cached as %s)",
                 server.remote_rules_url, RULES_CACHE_FILENAME)
    else:
        LOG.info("rules   %s", rules_source if rules_source is not None else "none (defaults)")
    if overrides:
        LOG.info("cli     overriding %s", ", ".join(overrides))
    LOG.info("budget  %s", describe_budget(budget))

    if getattr(args, "bake_polypaint", False):
        return run_bake_polypaint(root, budget, args)

    try:
        heal_settings = resolve_healer(watch_dir, args, remote_document)
    except ConfigError as exc:
        LOG.error("invalid healer settings: %s", exc)
        return 2

    one_file = bool(getattr(args, "heal", False))
    if one_file:
        heal_settings = replace(heal_settings, enabled=True)
    healer = None
    if heal_settings.enabled:
        try:
            healer = load_healer(budget, heal_settings,
                                 root=heal_project if one_file else watch_dir,
                                 dry_run=bool(getattr(args, "dry_run", False)))
        except ConfigError as exc:
            LOG.error("%s", exc)
            return 2
        if one_file:
            LOG.info("heal    %s%s  mode=%s  project=%s", root.name,
                     " (dry run: nothing is written)" if args.dry_run else "",
                     heal_settings.texture_mode, heal_project)
        else:
            describe_auto_heal(watch_dir, heal_settings, args, remote_document)

    telemetry = None
    if server.telemetry_enabled:
        telemetry = Telemetry(server.telemetry_url, root=watch_dir, token=server.token,
                              identity=server.identity)
        LOG.info("report  %s", server.telemetry_url)

    try:
        tag_budgets, tag_mode, tag_similarity = resolve_tagging(watch_dir, args, remote_document)
    except ConfigError as exc:
        LOG.error("invalid tag configuration: %s", exc)
        return 2
    if tag_budgets:
        LOG.info("tags    %s  (%s matching, %.2f similarity)",
                 ", ".join(sorted(tag_budgets)), tag_mode, tag_similarity)

    try:
        exceptions = resolve_exceptions(watch_dir, args, remote_document)
    except ConfigError as exc:
        LOG.error("invalid exception: %s", exc)
        return 2
    if exceptions:
        today = date.today()
        live = [item for item in exceptions if item.active(today)]
        soon = [item for item in live if (item.expires - today).days <= EXCEPTION_WARN_DAYS]
        LOG.info("except  %d active, %d expired%s", len(live), len(exceptions) - len(live),
                 f", {len(soon)} ending within {EXCEPTION_WARN_DAYS} days" if soon else "")

    # Toasts default on for a human watching a folder, off for CI and hooks:
    # a build agent has nobody to interrupt.
    notify_wanted = (args.notify if args.notify is not None
                     else (not args.once and root.is_dir()))
    notifier = None
    if notify_wanted:
        try:
            import notifier as notifier_module
            notifier = notifier_module.Notifier(max_per_minute=args.notify_max or 6)
            if not notifier.available:
                LOG.debug("no desktop notification backend on this machine")
                notifier = None
        except Exception as exc:      # missing module, no backend, locked session
            LOG.debug("desktop notifications unavailable: %s", exc)
            notifier = None
    if notifier is not None:
        LOG.info("toasts  on (%d/min)", notifier.max_per_minute)

    try:
        totals = resolve_totals(watch_dir, args, remote_document)
    except ConfigError as exc:
        LOG.error("invalid totals: %s", exc)
        return 2
    # One file checked with --rules from a folder above it (the Perforce trigger,
    # a Git hook, --heal): that folder is the project, as in a --once run there.
    # With the file's own folder as the root, [tags.props] never matched
    # Props/crate.fbx and a ../Textures reference "left the project".
    project_dir = watch_dir
    if root.is_file() and args.rules:
        rules_dir = Path(args.rules).expanduser().resolve().parent
        try:
            root.relative_to(rules_dir)
            project_dir = rules_dir
        except ValueError:
            pass
    gatekeeper = Gatekeeper(budget, healer=healer, overrides=overrides,
                            tag_budgets=tag_budgets,
                            tag_mode=tag_mode, tag_similarity=tag_similarity,
                            tag_root=project_dir, exceptions=exceptions)
    gatekeeper.want_memory = bool(totals)
    reporter = Reporter(color=not args.no_color and not args.json, root=watch_dir,
                        json_mode=args.json, telemetry=telemetry, notifier=notifier)
    reporter.keep_verdicts = bool(args.report)
    if sink is not None:
        reporter.keep_verdicts, reporter.verdicts = True, sink

    if root.is_file():
        if not is_candidate(root):
            LOG.error("not a tracked asset type: %s", root.name)
            return 2
        if getattr(args, "exception_path", None) and exceptions:
            relative = str(args.exception_path).replace("\\", "/").lstrip("/")
            if ".." in relative.split("/"):
                LOG.error("--exception-path must not contain '..'")
                return 2
            virtual = Path("/agk-exception-root")
            gatekeeper.exceptions = tuple(replace(item, base=virtual) for item in exceptions)
            gatekeeper.exception_paths[root] = virtual / relative
        verdict = gatekeeper.check(root)
        reporter.report(verdict)
        reporter.close()
        if args.report:
            write_reports(args.report, reporter.verdicts, watch_dir)
        if one_file and not verdict.healed and not verdict.passed:
            reason = getattr(healer, "last_refusal", "")
            if not any(note.startswith("--heal would:") for note in verdict.warnings):
                LOG.warning("not healed: %s. %s is unchanged.",
                            reason or "the healer had nothing safe to do", root.name)
        return 0 if verdict.passed else 1

    if args.once:
        jobs = resolve_jobs(args.jobs)
        if jobs > 1 and healer is not None:
            LOG.warning("--jobs %d ignored: healing rewrites files, so it runs one file at "
                        "a time", jobs)
            jobs = 1
        cache = None
        if not args.no_cache:
            cache_path = (Path(args.cache).expanduser() if args.cache
                          else default_cache_path(watch_dir))
            if cache_inside(watch_dir, cache_path):
                LOG.error("--cache %s is inside the folder being checked: anyone who can "
                          "commit there could write a cache that passes any file. Put it "
                          "outside (CI: the runner's temp folder), or use --no-cache",
                          cache_path)
                return 2
            cache = VerdictCache(cache_path, watch_dir, rules_fingerprint(gatekeeper),
                                 reuse_failures=healer is None)
            if cache.reset_reason:
                LOG.info("cache   %s: %s", cache_path.name, cache.reset_reason)
        status = run_once(watch_dir, gatekeeper, reporter, args.recursive, budget.exclude,
                          follow_symlinks=getattr(args, "follow_symlinks", False),
                          allow_empty=getattr(args, "allow_empty", False), totals=totals,
                          cache=cache, jobs=jobs)
        if args.report:
            write_reports(args.report, reporter.verdicts, watch_dir)
    else:
        if args.report:
            LOG.warning("--report is written by --once runs only; ignored while watching")
        status = run_watch(watch_dir, gatekeeper, reporter, args.recursive, budget.exclude)
    reporter.close()
    return status
