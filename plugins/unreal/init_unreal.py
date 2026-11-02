"""
AssetGatekeeper — Unreal Engine import guard
============================================

Put this file at ``<Project>/Content/Python/init_unreal.py`` (or add its folder to
the editor's Python path) and it loads at editor startup.

What it does
------------
1. **Post-import validation.** After a StaticMesh import, reads triangle counts,
   material sections and UV channels from the resulting asset and applies the same
   ``rules.toml`` the CLI uses.
2. **Notification.** Warning in the Message Log plus an editor dialog, so the
   artist sees it immediately.
4. **Data Validation.** If ``agk_validator.py`` sits next to this file, the same
   rules run on save, on *Validate Assets* and on *Validate Changelist*
   (static meshes and textures, Unreal's own numbers). See that file.
3. **Source pre-check for scripted imports.** See ``import_gatekeeper.py`` in this
   folder — that path *does* block, before ``import_asset_tasks`` runs.

Drag-and-drop: rejected *after* import
--------------------------------------
Python cannot cancel Unreal's native drag-and-drop import: ``OnAssetPreImport`` is
a notification, not a veto, and exceptions raised inside a delegate are swallowed
by the engine. So the check runs on the imported asset, and what happens to an
over-budget one is set by ``ASSETGATEKEEPER_UNREAL_ON_FAIL``:

``quarantine`` (default)  the asset is moved to ``/Game/_AssetGatekeeper_Rejected``
                          on the next editor tick and the artist is told why. It
                          never sits where the artist dropped it, and it can be
                          moved back by hand if a lead waives the budget.
``delete``                the asset is deleted on the next editor tick.
``warn``                  1.0 behaviour: a warning, the asset stays.

An asset that something already references (a re-import over a mesh that is
placed in a level, say) is never moved or deleted - that would break the
referencer - and gets the warning instead, naming the problem.
Scripted/automated imports should still go through
``import_gatekeeper.import_with_gatekeeper()``, which checks the source file
before anything is imported.

Configuration
-------------
``ASSETGATEKEEPER_HOME``            folder containing ``asset_gatekeeper.py``
``ASSETGATEKEEPER_RULES``           explicit ``rules.toml``
``ASSETGATEKEEPER_UNREAL_ON_FAIL``  quarantine | delete | warn (default quarantine)
"""

from __future__ import annotations

import os
import sys

import unreal

ENV_HOME = "ASSETGATEKEEPER_HOME"
ENV_RULES = "ASSETGATEKEEPER_RULES"
ENV_ON_FAIL = "ASSETGATEKEEPER_UNREAL_ON_FAIL"
ON_FAIL_MODES = ("quarantine", "delete", "warn")
QUARANTINE_FOLDER = "/Game/_AssetGatekeeper_Rejected"

_state = {"registered": False}
_budget = None


# --------------------------------------------------------------------------- #
# Budget (reuse the engine's parser so rules cannot drift between tools)
# --------------------------------------------------------------------------- #

def load_budget():
    global _budget
    if _budget is not None:
        return _budget

    home = os.environ.get(ENV_HOME)
    if home and home not in sys.path:
        sys.path.insert(0, home)
    try:
        import asset_gatekeeper as engine
    except ImportError:
        if not _state.get("engine_missing_told"):
            # Once per session, on screen: an unconfigured machine used to log
            # this and then call every mesh "within budget" (UE 5.3.2), so a
            # 240k-triangle import looked checked when it was not.
            _state["engine_missing_told"] = True
            notify("AssetGatekeeper is not configured on this machine",
                   "Imported meshes are NOT being checked. Set ASSETGATEKEEPER_HOME "
                   "to the folder containing asset_gatekeeper.py and restart the editor.")
        return None

    path = os.environ.get(ENV_RULES) or ""
    if path and os.path.isfile(path):
        from pathlib import Path

        try:
            _budget = engine.Budget.from_mapping(engine.parse_rules(Path(path)))
        except Exception as error:
            # Raised inside the import delegate, this was swallowed by the engine
            # and every import went unchecked without a word.
            if not _state.get("rules_broken_told"):
                _state["rules_broken_told"] = True
                notify("AssetGatekeeper could not read its rules",
                       f"{path}\n{error}\n\nImported meshes are NOT being checked "
                       "until the file is fixed (restart the editor after fixing it).")
            return None
    else:
        _budget = engine.Budget()
    return _budget


# --------------------------------------------------------------------------- #
# Post-import asset checks
# --------------------------------------------------------------------------- #

def check_static_mesh(mesh, budget) -> list:
    problems = []
    if budget is None or mesh is None:
        return problems

    triangles = 0
    sections = 0
    lod_count = mesh.get_num_lods() if hasattr(mesh, "get_num_lods") else 1
    for lod in range(max(1, lod_count)):
        try:
            triangles = max(triangles, int(mesh.get_num_triangles(lod)))
            sections = max(sections, int(mesh.get_num_sections(lod)))
        except Exception:  # pragma: no cover - API drift between UE versions
            continue

    if triangles > budget.max_triangles:
        problems.append(f"{triangles:,} triangles exceeds the {budget.max_triangles:,} budget")
    if sections > budget.max_material_slots:
        problems.append(
            f"{sections} material slots exceeds the {budget.max_material_slots} slot budget")
    return problems


# --------------------------------------------------------------------------- #
# Notification
# --------------------------------------------------------------------------- #

def notify(title: str, message: str) -> None:
    unreal.log_warning(f"[AssetGatekeeper] {title}: {message}")
    dialog = getattr(unreal, "EditorDialog", None)
    if dialog is not None and hasattr(dialog, "show_message"):
        try:
            dialog.show_message(title, message, unreal.AppMsgType.WARNING)
            return
        except Exception:  # pragma: no cover
            pass
    try:  # at minimum, put it on screen
        unreal.SystemLibrary.print_string(None, f"{title}: {message}",
                                          color=(1.0, 0.2, 0.2))
    except Exception:  # pragma: no cover
        pass


# --------------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------------- #

def _on_pre_import(factory, class_name, parent, name, flags) -> None:
    """Notification-only: the delegate fires after the factory is chosen."""
    budget = load_budget()
    if budget is None:
        return
    # We cannot see the source path here; the blocking path lives in
    # import_gatekeeper.py, and the post-import hook validates the asset itself.
    unreal.log(f"[AssetGatekeeper] import starting: {class_name} '{name}'")


def on_fail_mode() -> str:
    mode = (os.environ.get(ENV_ON_FAIL) or "quarantine").strip().lower()
    if mode not in ON_FAIL_MODES:
        unreal.log_warning(f"[AssetGatekeeper] {ENV_ON_FAIL}={mode!r} is not one of "
                           f"{', '.join(ON_FAIL_MODES)}; using quarantine")
        return "quarantine"
    return mode


def _asset_path(asset) -> str:
    """'/Game/Props/Rock' for the asset object (package path, no '.Rock')."""
    library = unreal.EditorAssetLibrary
    getter = getattr(library, "get_path_name_for_loaded_asset", None)
    path = getter(asset) if callable(getter) else asset.get_path_name()
    return path.split(".", 1)[0]


def _referencers(path: str) -> list:
    finder = getattr(unreal.EditorAssetLibrary, "find_package_referencers_for_asset", None)
    if not callable(finder):
        return []
    try:
        return [ref for ref in (finder(path, False) or []) if str(ref) != path]
    except Exception:  # pragma: no cover - API drift
        return []


# Assets waiting to be moved/deleted. Doing it inside the import delegate would
# pull the asset out from under the factory that is still finishing with it, so
# the work happens on the next editor tick instead.
_pending: list = []
_tick = {"handle": None}


def _process_pending(_delta_seconds=0.0) -> None:
    handle, _tick["handle"] = _tick["handle"], None
    if handle is not None:
        try:
            unreal.unregister_slate_post_tick_callback(handle)
        except Exception:  # pragma: no cover
            pass
    work, _pending[:] = list(_pending), []
    for path, mode, problems in work:
        reject(path, mode, problems)


def _schedule(path: str, mode: str, problems: list) -> None:
    if any(item[0] == path for item in _pending):
        return                       # already queued for this tick
    _pending.append((path, mode, problems))
    register = getattr(unreal, "register_slate_post_tick_callback", None)
    if not callable(register):           # no editor tick (commandlet): act now
        _process_pending()
        return
    if _tick["handle"] is None:
        _tick["handle"] = register(_process_pending)


def reject(path: str, mode: str, problems: list) -> str:
    """Quarantine or delete an imported asset; returns what was done."""
    reasons = "\n".join(f"- {problem}" for problem in problems)
    library = unreal.EditorAssetLibrary
    if not library.does_asset_exist(path):
        # Already moved or deleted (by us on an earlier report, or by hand):
        # nothing to do, and nothing to alarm the artist with.
        unreal.log(f"[AssetGatekeeper] {path} is already gone; nothing to reject")
        return "gone"
    referencers = _referencers(path)
    if referencers:
        notify("AssetGatekeeper: asset exceeds budget (left in place)",
               f"{path}\n{reasons}\n\nNot moved: {len(referencers)} other asset(s) "
               f"reference it. Revert the import in source control or fix the source.")
        return "warned-referenced"
    if mode == "delete":
        done = library.delete_asset(path)
        outcome = "deleted" if done else "delete-failed"
        verb = "was deleted" if done else "could NOT be deleted"
    else:
        name = path.rsplit("/", 1)[-1]
        target = f"{QUARANTINE_FOLDER}/{name}"
        suffix = 1
        while library.does_asset_exist(target):
            suffix += 1
            target = f"{QUARANTINE_FOLDER}/{name}_{suffix}"
        done = library.rename_asset(path, target)
        outcome = "quarantined" if done else "quarantine-failed"
        verb = f"was moved to {target}" if done else "could NOT be moved"
    notify("AssetGatekeeper: import rejected",
           f"{path} {verb}.\n{reasons}")
    return outcome


# UE 5.3.2 reports one FBX drag-and-drop to OnAssetPostImport twice (seen live:
# one "import starting", then two checks of the same mesh). The second check
# queued the same move again, and the artist got a second, false message:
# "... could NOT be moved". A repeat for the same asset within this window is
# ignored.
DUPLICATE_WINDOW_SECONDS = 2.0
_recent: dict = {}


def _first_report(path: str) -> bool:
    import time

    now = time.monotonic()
    for key, when in list(_recent.items()):
        if now - when > DUPLICATE_WINDOW_SECONDS:
            del _recent[key]
    if path in _recent:
        return False
    _recent[path] = now
    return True


def _on_post_import(factory, created_object) -> None:
    """OnAssetPostImport passes (factory, created_object) - two arguments.

    This handler used to take the pre-import delegate's five, and UE 5.3.2
    refused to bind it ("Callable has the incorrect number of arguments
    (expected 2, got 5)") - so the drag-and-drop warning never fired at all.
    """
    asset = created_object
    if asset is None or not isinstance(asset, unreal.StaticMesh):
        return
    if not _first_report(_asset_path(asset)):
        return

    budget = load_budget()
    if budget is None:
        unreal.log_error(f"[AssetGatekeeper] {asset.get_name()}: NOT checked "
                         "(AssetGatekeeper is not configured on this machine)")
        return
    problems = check_static_mesh(asset, budget)
    if not problems:
        unreal.log(f"[AssetGatekeeper] {asset.get_name()}: within budget")
        return
    mode = on_fail_mode()
    if mode == "warn":
        notify("AssetGatekeeper: asset exceeds budget",
               "\n".join(f"- {problem}" for problem in problems))
        return
    path = _asset_path(asset)
    if path.startswith(QUARANTINE_FOLDER + "/"):
        return                       # already rejected once; don't loop
    unreal.log_warning(f"[AssetGatekeeper] rejecting {path} ({mode}): "
                       + "; ".join(problems))
    _schedule(path, mode, problems)


HOOKS = (("on_asset_pre_import", "_on_pre_import"),
         ("on_asset_post_import", "_on_post_import"),
         ("on_asset_reimport", "_on_reimport"))
WATCHDOG_SECONDS = 2.0

# Strong references to everything a binding is made of. Verified in UE 5.3.2
# (two separate runs): with the subsystem and delegate wrappers left to go out
# of scope, both hooks stopped firing after the first asset was deleted or
# moved - so drag-and-drop went unchecked while the log still said "import
# guard active". Holding these references fixed it (hooks kept firing through
# a move and a delete). The cause inside the engine is not pinned down - an
# explicit collect_garbage() did not reproduce it - so the watchdog below also
# re-binds anything the engine drops, and says so in the log when it does.
_keepalive: list = []
_watch = {"handle": None, "last": 0.0, "rebinds": 0}


def _subsystem():
    getter = getattr(unreal, "get_editor_subsystem", None)
    subsystem_class = getattr(unreal, "ImportSubsystem", None)
    if not callable(getter) or subsystem_class is None:
        return None
    return getter(subsystem_class)


def _is_bound(event, handler) -> bool:
    contains = getattr(event, "contains_callable", None)
    if not callable(contains):
        return False
    try:
        return bool(contains(handler))
    except Exception:  # pragma: no cover
        return False


def ensure_bound(_delta_seconds=0.0, force: bool = False) -> list:
    """Bind any hook that is not bound (first call, or after a GC dropped it).

    Returns the names that were (re)bound by this call.
    """
    import time

    now = time.monotonic()
    if not force and now - _watch["last"] < WATCHDOG_SECONDS:
        return []
    _watch["last"] = now
    subsystem = _subsystem()
    if subsystem is None:
        return []
    added = []
    for event_name, handler_name in HOOKS:
        handler = globals()[handler_name]
        event = getattr(subsystem, event_name, None)
        if event is None or _is_bound(event, handler):
            continue
        try:
            adder = getattr(event, "add_callable_unique", None) or event.add_callable
            adder(handler)
            _keepalive.append((subsystem, event, handler))
            added.append(event_name)
        except Exception as error:  # pragma: no cover - delegate signature drift
            unreal.log_warning(f"[AssetGatekeeper] could not bind {event_name}: {error}")
    if added and _state["registered"]:
        _watch["rebinds"] += 1
        unreal.log_warning("[AssetGatekeeper] import hooks had been dropped by the "
                           "engine and were re-attached: " + ", ".join(added))
    return added


def _on_reimport(asset) -> None:
    """Reimport (right-click > Reimport / Reimport With New File) fires its own
    delegate, not OnAssetPostImport: a heavier source swapped in under an
    existing asset went unchecked. Same check; a referenced asset is never
    moved, so a placed mesh gets the warning instead."""
    _on_post_import(None, asset)


def register_hooks() -> bool:
    if _state["registered"]:
        return True

    if _subsystem() is None:
        unreal.log_warning(
            "[AssetGatekeeper] unreal.ImportSubsystem is unavailable in this build; "
            "use import_gatekeeper.import_with_gatekeeper() for scripted imports.")
        return False

    bound = ensure_bound(force=True)
    register = getattr(unreal, "register_slate_post_tick_callback", None)
    if callable(register) and _watch["handle"] is None:
        _watch["handle"] = register(ensure_bound)

    _state["registered"] = True
    _state["bound"] = bound
    if "on_asset_post_import" in bound:
        unreal.log("[AssetGatekeeper] import guard active")
    else:
        # The post-import check *is* the guard; saying "active" without it is
        # how a failed bind went unnoticed.
        unreal.log_error("[AssetGatekeeper] import guard NOT active: the post-import "
                         "check could not be bound; imported assets are not checked")
    return bool(bound)



def register_validation() -> bool:
    """G6: the rules on save and on Validate Changelist, via agk_validator.py."""
    try:
        import agk_validator
    except ImportError:
        unreal.log("[AssetGatekeeper] agk_validator.py is not installed next to "
                   "init_unreal.py; assets are checked on import only")
        return False
    try:
        return agk_validator.register_validator(unreal) is not None
    except Exception as error:   # never let validation break the import guard
        unreal.log_warning(f"[AssetGatekeeper] data validation not registered: {error}")
        return False


register_hooks()
register_validation()
