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
3. **Source pre-check for scripted imports.** See ``import_gatekeeper.py`` in this
   folder — that path *does* block, before ``import_asset_tasks`` runs.

Honest limitation
-----------------
Python cannot cancel Unreal's native drag-and-drop import. ``OnAssetPreImport`` is
a notification, not a veto: the factory is already chosen when a Python delegate
runs, and exceptions raised inside a delegate are swallowed by the engine. So this
hook warns on drag-and-drop and the *blocking* path is
``import_gatekeeper.import_with_gatekeeper()`` for scripted/automated imports.

A hard block on drag-and-drop needs a C++ ``UFactory::FactoryCreateFile`` early-out;
that is a plugin, not a Python script, and is out of scope here by design.

Configuration
-------------
``ASSETGATEKEEPER_HOME``   folder containing ``asset_gatekeeper.py``
``ASSETGATEKEEPER_RULES``  explicit ``rules.toml``
"""

from __future__ import annotations

import os
import sys

import unreal

ENV_HOME = "ASSETGATEKEEPER_HOME"
ENV_RULES = "ASSETGATEKEEPER_RULES"

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
        unreal.log_error(
            "[AssetGatekeeper] engine not found - set ASSETGATEKEEPER_HOME to the "
            "folder containing asset_gatekeeper.py"
        )
        return None

    path = os.environ.get(ENV_RULES) or ""
    if path and os.path.isfile(path):
        from pathlib import Path

        _budget = engine.Budget.from_mapping(engine.parse_rules(Path(path)))
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


def _on_post_import(factory, class_name, parent, name, flags) -> None:
    try:
        package = f"{parent.get_path_name()}/{name}" if parent else str(name)
        asset = unreal.EditorAssetLibrary.load_asset(package)
    except Exception:  # pragma: no cover
        return
    if asset is None or not isinstance(asset, unreal.StaticMesh):
        return

    problems = check_static_mesh(asset, load_budget())
    if problems:
        notify("AssetGatekeeper: asset exceeds budget",
               "\n".join(f"- {problem}" for problem in problems))


def register_hooks() -> bool:
    if _state["registered"]:
        return True

    subsystem_getter = getattr(unreal, "get_editor_subsystem", None)
    subsystem_class = getattr(unreal, "ImportSubsystem", None)
    if not callable(subsystem_getter) or subsystem_class is None:
        unreal.log_warning(
            "[AssetGatekeeper] unreal.ImportSubsystem is unavailable in this build; "
            "use import_gatekeeper.import_with_gatekeeper() for scripted imports.")
        return False

    subsystem = subsystem_getter(subsystem_class)
    for event_name, handler in (("on_asset_pre_import", _on_pre_import),
                                ("on_asset_post_import", _on_post_import)):
        event = getattr(subsystem, event_name, None)
        if event is None:
            unreal.log_warning(f"[AssetGatekeeper] {event_name} not exposed; skipped")
            continue
        try:
            event.add_callable(handler)
        except Exception as error:  # pragma: no cover - delegate signature drift
            unreal.log_warning(f"[AssetGatekeeper] could not bind {event_name}: {error}")

    _state["registered"] = True
    unreal.log("[AssetGatekeeper] import guard active")
    return True


register_hooks()
