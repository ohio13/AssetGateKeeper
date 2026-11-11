"""AssetGatekeeper, part 5 of 7: naming suggestions, the repository audit and the enterprise helpers.

Split out of asset_gatekeeper.py on 11 Nov 2026 with no change in behaviour;
asset_gatekeeper.py still exposes every name. See agk_gate/__init__.py.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agk_gate.common import (
    DEFAULT_IDENTITY, GATE_HOME, HTTP_TIMEOUT, IDENTITY_MODES, LOG, MESH_SUFFIXES,
    NAMING_PRESETS, REMOTE_RULES_ENV, RULES_FILENAME, SERVER_ENV_IDENTITY, SERVER_ENV_TOKEN,
    SERVER_ENV_URL, TEXTURE_SUFFIXES, tomllib, TRACKED_SUFFIXES, USER_AGENT, walk_files,
)
from agk_gate.config import (
    apply_platform, apply_preset, Budget, build_tag_budgets, ConfigError,
    DEFAULT_TAG_SIMILARITY, parse_rules, _read_bool, _read_float, _read_str, _reject_unknown,
    resolve_budget, TAG_MODES,
)
from agk_gate.readers import (
    mesh_name_problem, texture_name_problem,
)
from agk_gate.gate import (
    missing_references, orphaned_files, resolve_references,
)


# --- #
# Naming rules from the files a studio already has
# --- #

def _name_tokens(stem: str) -> list[str]:
    return [token for token in re.split(r"[_\-.]+", stem) if token]


def suggest_naming(root: Path, recursive: bool = True) -> str:
    """A ready-to-paste [naming] block learned from the names already in a folder.

    Scores every preset against the textures and meshes found, picks the ones
    that fit, adds the other map endings and prefixes the folder uses often,
    and says how many files the result would pass. Nothing is written."""
    files = [path for path in walk_files(root, recursive=recursive)
             if path.suffix.lower() in TRACKED_SUFFIXES]
    textures = sorted({path.stem for path in files if path.suffix.lower() in TEXTURE_SUFFIXES})
    meshes = sorted({path.stem for path in files if path.suffix.lower() in MESH_SUFFIXES})
    lines = [f"# Learned from {len(textures)} texture name(s) and {len(meshes)} mesh name(s) "
             f"in {root}"]
    if not textures and not meshes:
        return lines[0] + "\n# nothing to learn from: no textures or meshes here\n"

    def passing(budget: "Budget", names: list[str], kind: str) -> int:
        check = texture_name_problem if kind == "texture" else mesh_name_problem
        return sum(1 for name in names if not check(name, budget))

    def budget_for(document: dict) -> "Budget":
        return Budget.from_mapping({"naming": document})

    chosen: list[str] = []
    if textures:
        scores = sorted(((passing(budget_for({"preset": name}), textures, "texture"), name)
                         for name in NAMING_PRESETS), reverse=True)
        lines.append("# How many textures each preset already passes:")
        lines += [f"#   {name:<10} {count:>5} of {len(textures)}" for count, name in scores]
        covered: set[str] = set()
        for count, name in scores:          # greedy: add a preset while it helps
            budget = budget_for({"preset": [name]})
            gain = {stem for stem in textures if not texture_name_problem(stem, budget)} - covered
            if count and len(gain) >= max(2, len(textures) // 20):
                chosen.append(name)
                covered |= gain
    # Endings no chosen preset knows, used by two or more textures.
    budget = budget_for({"preset": chosen}) if chosen else None
    leftover = [stem for stem in textures if budget is None or texture_name_problem(stem, budget)]
    endings: dict[str, int] = {}
    spelling: dict[str, str] = {}
    for stem in leftover:
        tokens = _name_tokens(re.sub(r"[_\-]?\d+$", "", stem))
        # A map name is a word: not a number, a resolution (2k) or one letter.
        if len(tokens) >= 2 and re.fullmatch(r"[A-Za-z]{2,}", tokens[-1]):
            key = tokens[-1].lower()
            endings[key] = endings.get(key, 0) + 1
            spelling.setdefault(key, "_" + tokens[-1])
    extra = [spelling[key] for key, count in sorted(endings.items(), key=lambda item: -item[1])
             if count >= 2][:15]
    starts: dict[str, int] = {}
    for stem in meshes:
        tokens = _name_tokens(stem)
        if len(tokens) >= 2 and len(tokens[0]) <= 4:
            starts[tokens[0] + "_"] = starts.get(tokens[0] + "_", 0) + 1
    mesh_prefixes = [prefix for prefix, count in sorted(starts.items(), key=lambda item: -item[1])
                     if count >= max(2, len(meshes) // 5)]

    document: dict = {}
    block = ["[naming]"]
    if chosen:
        document["preset"] = chosen
        block.append("preset = [" + ", ".join(f'"{name}"' for name in chosen) + "]")
    if extra:
        document["texture_suffixes"] = extra
        block.append("texture_suffixes = [" + ", ".join(f'"{e}"' for e in extra)
                     + "]   # other endings used here")
    if mesh_prefixes:
        document["mesh_prefixes"] = mesh_prefixes
        block.append("mesh_prefixes = [" + ", ".join(f'"{p}"' for p in mesh_prefixes)
                     + "]   # most meshes here start with these")
    if len(block) == 1:
        lines.append("# No common convention found; [texture] suffixes or [naming] texture_pattern "
                     "can describe yours.")
        return "\n".join(lines) + "\n"
    result = budget_for(document)
    lines.append(f"# With this block: {passing(result, textures, 'texture')} of {len(textures)} "
                 f"textures and {passing(result, meshes, 'mesh')} of {len(meshes)} meshes pass the "
                 "naming rules.")
    lines.append("# Paste it into rules.toml (and edit freely):")
    return "\n".join(lines + [""] + block) + "\n"


# --- #
# Repository audit: the whole folder, not one asset
# --- #


def run_audit(root: Path) -> int:
    """Report broken references and orphaned textures across a repository.

    Exit 0 when the repository is clean, 1 when anything is wrong - a folder that
    looks shut but is missing its textures is exactly the failure this catches.
    """
    assets = sorted(p for p in walk_files(root)
                    if p.suffix.lower() in MESH_SUFFIXES)
    broken_total = 0
    for asset in assets:
        broken = missing_references(asset)
        if not broken:
            continue
        broken_total += len(broken)
        print(f"BROKEN  {asset.relative_to(root)}")
        for written, target in broken:
            print(f"          -> {written}   (expected {target})")

    orphans = orphaned_files(root)
    for orphan in orphans:
        try:
            relative = orphan.relative_to(root)
        except ValueError:       # pragma: no cover - outside the root
            relative = orphan
        print(f"ORPHAN  {relative}")

    referenced = sum(len(resolve_references(asset)) for asset in assets)
    print()
    print(f"{len(assets):,} mesh file(s), {referenced:,} reference(s), "
          f"{broken_total:,} broken, {len(orphans):,} orphaned texture(s)")
    return 1 if (broken_total or orphans) else 0


# --- #
# Enterprise: central rules endpoint, offline cache, server settings
# --- #

def _utc_now() -> str:
    """ISO-8601 UTC with a real offset, so logs from two studios can be merged."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _http_body(url: str, timeout: float = HTTP_TIMEOUT, token: str = "",
               accept: str = "application/json") -> str:
    """GET a URL and return its body as text. Raises on anything abnormal.

    Kept dumb on purpose: the caller decides what a failure means (a cached
    budget, versus nothing at all).
    """
    request = urllib.request.Request(url, headers={"Accept": accept,
                                                   "User-Agent": USER_AGENT})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        charset = "utf-8"
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                charset = headers.get_content_charset() or "utf-8"
            except AttributeError:  # plain dict in a test double
                charset = "utf-8"
        return response.read().decode(charset, errors="replace")


@dataclass(frozen=True)
class ServerSettings:
    """Where the platform lives: telemetry sink, central budget, shared token."""

    telemetry_url: str = ""
    remote_rules_url: str = ""
    token: str = ""
    # How much identifying detail rides along with each verdict. Every payload
    # used to carry the machine name, the OS account and an absolute path -
    # "who worked on what, from which box" - to an endpoint the artist never
    # chose. That is a studio decision, so it is explicit and conservative:
    #   "full" - hostname, username and an absolute path
    #   "host" - hostname and an absolute path, no OS account (DEFAULT)
    #   "none" - no host or user, and the path relative to the watch folder
    identity: str = DEFAULT_IDENTITY

    @property
    def telemetry_enabled(self) -> bool:
        return bool(self.telemetry_url)

    @classmethod
    def from_mapping(cls, table: dict, where: str = "[server]") -> "ServerSettings":
        if not isinstance(table, dict):
            raise ConfigError(f"{where} must be a table")
        _reject_unknown(table, ("url", "token", "remote_rules_url", "identity"), where)
        identity = _read_str(table, "identity", where)
        if identity is not None and identity not in IDENTITY_MODES:
            raise ConfigError(f"{where}.identity must be one of "
                              f"{', '.join(IDENTITY_MODES)}, got {identity!r}")
        return cls(
            telemetry_url=_read_str(table, "url", where) or "",
            remote_rules_url=_read_str(table, "remote_rules_url", where) or "",
            token=_read_str(table, "token", where) or "",
            identity=DEFAULT_IDENTITY if identity is None else identity,
        )


def _loads_with(module, body: str, origin: str) -> dict:
    """Parse, turning a syntax error into a readable refusal, not a traceback."""
    try:
        return module.loads(body)
    except Exception as exc:                       # json.JSONDecodeError, TOMLDecodeError
        raise ConfigError(f"{origin}: {exc}") from exc


def parse_rules_text(text: str, origin: str = "<remote>") -> dict:
    """Parse a rules payload: JSON first (every Python), TOML second (3.11+).

    Accepting JSON first means a studio can serve the same object from any web
    stack, and older interpreters still work without ``tomli``.
    """
    body = text.lstrip("\ufeff").strip()
    if not body:
        raise ConfigError(f"{origin}: empty rules payload")
    try:
        if body.startswith("{"):
            document = _loads_with(json, body, origin)
        elif tomllib is not None:
            document = _loads_with(tomllib, body, origin)
        else:
            # 3.9/3.10: tomli is a declared dependency now, so a plain pip install
            # can read the same rules file. Missing it is still a clear refusal.
            try:
                import tomli
            except ImportError as exc:
                raise ConfigError(
                    f"{origin}: TOML needs Python 3.11+ or 'pip install tomli'"
                ) from exc
            document = _loads_with(tomli, body, origin)
    except ConfigError:
        raise
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{origin}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"{origin}: expected a JSON/TOML table at the top level")
    return document


def read_rules_cache(cache_path: Path) -> Optional[dict]:
    """Return the last good remote profile, or None if there is nothing usable."""
    try:
        wrapper = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        LOG.debug("rules cache unreadable (%s): %s", cache_path, exc)
        return None
    rules = wrapper.get("rules") if isinstance(wrapper, dict) else None
    return rules if isinstance(rules, dict) else None


def write_rules_cache(cache_path: Path, url: str, document: dict) -> None:
    """Persist the remote profile atomically: a half-written cache is worse than none."""
    payload = {"source": url, "fetched_at": _utc_now(), "rules": document}
    staging = cache_path.with_name(cache_path.name + ".tmp")
    try:
        staging.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(staging, cache_path)
    except OSError as exc:  # read-only watch folder: keep running, just say so
        LOG.debug("could not write rules cache %s: %s", cache_path, exc)


def fetch_remote_rules(url: str, cache_path: Path, timeout: float = HTTP_TIMEOUT,
                       token: str = "") -> Optional[dict]:
    """GET the central budget, falling back to the last good cached copy.

    Raises ``ConfigError`` only when the server cannot supply a usable budget
    (unreachable, or serving an invalid one) *and* there is no cached profile.

    The failure contract is the whole point of this function: a studio's network
    being down must degrade to "yesterday's budget", not to a traceback that
    stops the artists working. The broad ``except`` is deliberate.
    """
    problem = ""
    try:
        text = _http_body(url, timeout=timeout, token=token,
                          accept="application/json, application/toml")
    except Exception as exc:  # network, DNS, TLS, HTTP status
        problem = f"unreachable ({type(exc).__name__}: {exc})"
        failure: BaseException = exc
    else:
        try:
            document = parse_rules_text(text, url)
            Budget.from_mapping(document)  # validate before caching: a broken payload
            write_rules_cache(cache_path, url, document)  # must not poison the good cache
            LOG.debug("remote rules %s: %d bytes accepted", url, len(text))
            return document
        except Exception as exc:  # malformed payload, not a table, impossible values
            # The server answered; what it served is wrong. Saying "unreachable"
            # here sent people to debug the network instead of the rules.
            problem = f"invalid ({exc})"
            failure = exc
    LOG.debug("remote rules fetch failed: %s", problem)
    cached = read_rules_cache(cache_path)
    if cached is not None:
        if problem.startswith("invalid"):
            LOG.warning("Central rules at %s are %s; using the last good cached profile (%s)",
                        url, problem, cache_path.name)
        else:
            LOG.warning("Central server offline; using cached rules profile (%s)", cache_path.name)
        return cached
    # Fail closed. Falling back to the local 50k default would pass assets the
    # central budget rejects, which is the worst possible answer from a gate.
    # Keep the "remote rules unreachable" wording for the network case: pipelines
    # and the test suite match on it.
    raise ConfigError(
        f"remote rules {problem.split(' ', 1)[0]} ({url}: {problem.split(' ', 1)[1][1:-1]}) "
        f"and no cached profile at {cache_path.name}: refusing to validate against "
        f"local defaults"
    ) from failure


@dataclass(frozen=True)
class HealSettings:
    """Opt-in healing policy.

    ``enabled`` is False unless a human says otherwise - via ``--auto-heal`` or
    ``[healer] enabled = true``. Nothing in this tool ever repairs an asset as a
    side effect of failing it; automated mesh decimation and resampling destroy
    artistic intent, so the default answer is "no".
    """

    enabled: bool = False
    textures: bool = True       # NPOT + missing-suffix renames
    meshes: bool = True         # zero-area faces, .obj only
    # resize keeps UVs exact; pad keeps every pixel but shifts UVs (opt-in)
    texture_mode: str = "resize"
    # JPEG is re-saved (quality 95) when healed, which loses a little each time.
    jpg: bool = False

    @classmethod
    def from_mapping(cls, table: dict, where: str = "[healer]") -> "HealSettings":
        if not isinstance(table, dict):
            raise ConfigError(f"{where} must be a table")
        _reject_unknown(table, ("enabled", "textures", "meshes", "texture_mode", "jpg"), where)
        mode = _read_str(table, "texture_mode", where)
        if mode is not None and mode not in ("pad", "resize"):
            raise ConfigError(f"{where}.texture_mode must be 'pad' or 'resize', got {mode!r}")
        enabled = _read_bool(table, "enabled", where)
        textures = _read_bool(table, "textures", where)
        meshes = _read_bool(table, "meshes", where)
        jpg = _read_bool(table, "jpg", where)
        defaults = cls()
        return cls(
            enabled=defaults.enabled if enabled is None else enabled,
            textures=defaults.textures if textures is None else textures,
            meshes=defaults.meshes if meshes is None else meshes,
            texture_mode=defaults.texture_mode if mode is None else mode,
            jpg=defaults.jpg if jpg is None else jpg,
        )


def resolve_healer(root: Path, args: argparse.Namespace,
                   remote_document: Optional[dict] = None) -> HealSettings:
    """Merge healing policy: explicit CLI flag > remote rules > ``[healer]`` > off.

    The remote document matters here as much as it does for the budget: a
    producer who ticks "Enable auto-heal" in the budget planner expects the
    artists' clients to pick it up, and the planner's output arrives as remote
    rules. ``--no-auto-heal`` exists so CI can still pin healing off when a
    project (or a producer) turns it on.
    """
    from_source = HealSettings()
    if remote_document is not None:
        if not isinstance(remote_document, dict):
            raise ConfigError("remote rules: expected a table of settings, got "
                              f"{type(remote_document).__name__}")
        try:
            from_source = HealSettings.from_mapping(remote_document.get("healer", {}))
        except ConfigError as exc:
            raise ConfigError(f"remote rules: {exc}") from exc
    else:
        rules_path = Path(args.rules).expanduser() if getattr(args, "rules", None) else root / RULES_FILENAME
        if rules_path.is_file():
            try:
                from_source = HealSettings.from_mapping(parse_rules(rules_path).get("healer", {}))
            except (ConfigError, ValueError, OSError) as exc:
                raise ConfigError(f"{rules_path}: {exc}") from exc
    forced = getattr(args, "auto_heal", None)
    if forced is None:
        return from_source
    return replace(from_source, enabled=bool(forced))


def healer_module():
    """The companion ``healer.py``, loaded by path (once per process)."""
    cached = sys.modules.get("agk_healer")
    if cached is not None and getattr(cached, "__file__", None) == str(GATE_HOME / "healer.py"):
        return cached
    module_path = GATE_HOME / "healer.py"
    if not module_path.is_file():
        raise ConfigError(f"healing is enabled but {module_path.name} is missing "
                          f"(expected next to asset_gatekeeper.py)")
    spec = importlib.util.spec_from_file_location("agk_healer", module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ConfigError(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    # Register before executing: @dataclass resolves its own module through
    # sys.modules, and an unregistered module makes it raise AttributeError.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_healer(budget: Budget, settings: HealSettings, root: Optional[Path] = None,
                **options):
    """Import the companion ``healer.py`` next to this file - only when asked.

    Loaded by path rather than by import name so the tool still works when it is
    copied alone into a studio's tools folder next to its healer.
    """
    module = healer_module()
    try:
        return module.Healer(budget, textures=settings.textures, meshes=settings.meshes,
                             texture_mode=settings.texture_mode, root=root,
                             resolve_references=resolve_references, jpg=settings.jpg,
                             **options)
    except module.HealError as exc:
        raise ConfigError(f"healer cannot start: {exc}") from exc


def describe_auto_heal(watch_dir: Path, settings: HealSettings, args: argparse.Namespace,
                       remote_document: Optional[dict]) -> None:
    """Say, before the first file, exactly what folder-wide healing may change.

    Healing rewrites artists' files; whoever turned it on (a flag, rules.toml, a
    producer in the budget planner) is named, so nobody finds out from a diff.
    """
    if getattr(args, "auto_heal", None):
        source = "--auto-heal"
    elif remote_document is not None:
        source = "the remote rules (AssetHealth budget planner)"
    else:
        source = f"[healer] in {RULES_FILENAME}"
    changes = []
    if settings.textures:
        kinds = "PNG/TGA" + ("/JPG" if settings.jpg else "")
        changes.append(f"rename {kinds} textures missing a suffix (and repoint the "
                       ".mtl/.gltf naming them)")
        changes.append(f"{settings.texture_mode} non-power-of-two {kinds} textures"
                       + (" - pad shifts UVs" if settings.texture_mode == "pad" else ""))
    if settings.meshes:
        changes.append("strip zero-area faces from .obj")
    LOG.warning("healing ON for every failing asset in %s (turned on by %s): may %s. "
                "Each change is verified first; originals kept in %s "
                "(undo one: asset-gatekeeper <file> --undo-heal).",
                watch_dir, source, "; ".join(changes) or "change nothing",
                watch_dir / ".agk-backups")


def run_heal_history(target: Path, undo: bool) -> int:
    """``--list-heals`` and ``--undo-heal``. Exit 0 done, 1 nothing to do, 2 refused."""
    try:
        module = healer_module()
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2
    if undo:
        if target.is_dir():
            LOG.error("--undo-heal takes the healed file (or its old name), not a folder; "
                      "--list-heals shows what was healed there")
            return 2
        try:
            record = module.undo_heal(target)
        except module.HealError as exc:
            LOG.error("not undone: %s", exc)
            return 2 if "changed after" in str(exc) or "missing" in str(exc) else 1
        LOG.info("undone  %s: restored %s from %s", record.manifest["healed"],
                 ", ".join(record.manifest["files"]), record.folder)
        return 0
    records = module.find_heals(target)
    if target.is_dir():
        records = [record for record in records if module.is_inside(record.asset, target)]
    else:
        records = [record for record in records
                   if module._same_file(record.healed, target)
                   or module._same_file(record.asset, target)]
    if not records:
        print(f"no heals recorded under {target}")
        return 1
    for record in records:
        state = "undone" if record.undone else "kept  "
        print(f"{record.manifest.get('healed_at', '?')[:19].replace('T', ' ')}  {state}  {record.manifest['asset']}"
              f"{' -> ' + record.manifest['healed'] if record.manifest['healed'] != record.manifest['asset'] else ''}"
              f"  {'; '.join(record.manifest.get('modifications', []))}")
    return 0


def resolve_tagging(root: Path, args: argparse.Namespace,
                    remote_document: Optional[dict] = None
                    ) -> tuple[dict[str, Budget], str, float]:
    """Compile the per-tag budgets and the matching policy for this run.

    Reads the same document the budget came from, so `[tags.*]` inherits the
    project's platform/preset baseline instead of being resolved in a vacuum.
    """
    if remote_document is not None:
        document = remote_document
    else:
        rules_path = Path(args.rules).expanduser() if getattr(args, "rules", None) else root / RULES_FILENAME
        document = parse_rules(rules_path) if rules_path.is_file() else {}

    try:
        platform = ((getattr(args, "platform", None) or "").strip()
                    or str(document.get("platform", "") or ""))
        preset = ((getattr(args, "preset", None) or "").strip()
                  or str(document.get("preset", "") or ""))
        document = apply_preset(apply_platform(document, platform), preset)

        tagging = document.get("tagging") or {}
        _reject_unknown(tagging, ("mode", "min_similarity"), "[tagging]")
        mode = ((getattr(args, "tag_mode", None) or "").strip()
                or _read_str(tagging, "mode", "[tagging]") or "suggest")
        if mode not in TAG_MODES:
            raise ConfigError(f"[tagging].mode must be one of {', '.join(TAG_MODES)}, got {mode!r}")
        similarity = _read_float(tagging, "min_similarity", "[tagging]")
        budgets = build_tag_budgets(document)
    except ConfigError as exc:
        raise ConfigError(str(exc)) from exc
    return budgets, mode, DEFAULT_TAG_SIMILARITY if similarity is None else similarity


def _budget_summary(budget: Budget) -> dict[str, object]:
    """One budget, flattened into the handful of numbers a UI can show.

    ``max_vertices`` is carried through as None: the gate enforces triangles,
    not vertices, and inventing a number here would be a lie an artist acts on.
    """
    return {
        "max_triangles": int(budget.max_triangles),
        "max_vertices": int(getattr(budget, "max_vertices", 0) or 0) or None,
        "max_texture_edge": int(budget.max_texture_edge),
        "max_material_slots": int(budget.max_material_slots),
        "max_bone_influences": int(budget.max_bone_influences),
        "max_uv_channels": int(budget.max_uv_channels),
        "power_of_two": bool(budget.require_power_of_two),
        "texture_naming": bool(budget.enforce_texture_naming),
        "allow_degenerate": bool(budget.allow_degenerate),
        "check_references": bool(budget.check_references),
        "mesh_name_prefixes": list(budget.mesh_name_prefixes),
        "naming_conventions": [list(c[:1]) + [list(c[1]), list(c[2]), c[3], list(c[4]), c[5]]
                               for c in budget.naming_conventions],
    }


def describe_rules(root: Path, rules_path: Optional[str | Path] = None,
                   platform: str = "", preset: str = "",
                   project_id: Optional[int] = None,
                   project_name: Optional[str] = None) -> dict[str, object]:
    """A read-only snapshot of the configuration the gate would enforce here.

    The web inspectors show this, so it deliberately runs the *real* resolvers
    instead of re-reading the TOML: one code path, one truth, no second parser
    to drift. Nothing is written and no socket is opened, so it is safe to serve
    to any artist who can open the dashboard.

    The shape is stable and documented in the servers' ``/api/rules`` routes;
    ``max_vertices`` is None because the gate has no vertex budget to report.
    """
    argv = [str(root)]
    if rules_path:
        argv += ["--rules", str(rules_path)]
    if platform:
        argv += ["--platform", platform]
    if preset:
        argv += ["--preset", preset]
    from agk_gate.cli import build_parser   # cli comes after this part
    args = build_parser().parse_args(argv)

    budget, source, _overrides = resolve_budget(root, args)
    tag_budgets, tag_mode, similarity = resolve_tagging(root, args)

    return {
        "read_only": True,
        "generated_at": _utc_now(),
        "root": str(root),
        "source": str(source) if source else "built-in defaults",
        "rules_file_found": bool(source),
        "project_id": project_id,
        "project_name": project_name,
        "target_platform": budget.platform or "default",
        "preset": str(getattr(budget, "preset", "") or "") or "none",
        "tag_mode": tag_mode,
        "tag_similarity": round(float(similarity), 3),
        "vertex_budget_enforced": False,
        "global_budget": _budget_summary(budget),
        "tag_budgets": [
            dict(tag=name, **_budget_summary(tag_budgets[name]))
            for name in sorted(tag_budgets)
        ],
    }


class RulesFileError(ConfigError):
    """The rules file could not be read or parsed at all."""


def resolve_server(root: Path, args: argparse.Namespace) -> ServerSettings:
    """Merge server settings: CLI flag > environment > ``[server]`` > nothing.

    Kept separate from :func:`resolve_budget` because the two answer different
    questions - "what is allowed" versus "where does it come from, where does it
    report" - and a studio may centralise one without the other.
    """
    from_file = ServerSettings()
    rules_path = Path(args.rules).expanduser() if getattr(args, "rules", None) else root / RULES_FILENAME
    if rules_path.is_file():
        try:
            document = parse_rules(rules_path)
        except (ConfigError, ValueError, OSError) as exc:
            # The file itself is broken: say "rules", not "server settings"
            # (the [server] reader is merely the first to open it).
            raise RulesFileError(f"{rules_path}: {exc}") from exc
        try:
            from_file = ServerSettings.from_mapping(document.get("server", {}))
        except (ConfigError, ValueError, OSError) as exc:
            raise ConfigError(f"{rules_path}: {exc}") from exc
    return replace(
        from_file,
        telemetry_url=(getattr(args, "telemetry_url", None)
                       or os.environ.get(SERVER_ENV_URL) or from_file.telemetry_url),
        remote_rules_url=(getattr(args, "remote_rules", None)
                          or os.environ.get(REMOTE_RULES_ENV) or from_file.remote_rules_url),
        token=os.environ.get(SERVER_ENV_TOKEN) or from_file.token,
        identity=(getattr(args, "telemetry_identity", None)
                  or os.environ.get(SERVER_ENV_IDENTITY) or from_file.identity),
    )
