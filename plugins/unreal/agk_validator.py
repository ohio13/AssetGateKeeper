"""
AssetGatekeeper - Unreal Data Validation
========================================

Registers an ``EditorValidatorBase`` validator so the gate's rules run where an
Unreal team already looks for asset problems:

* **on save** (when Project Settings > Editor > Data Validation > "Validate On
  Save" is on - it is by default);
* on **right-click > Asset Actions > Validate Assets**, and **Validate Data** for
  a folder;
* on **Validate Changelist** in the Revision Control window, which blocks the
  submit when a project has turned changelist validation on.

It reads Unreal's own numbers for the asset as imported - not the source file -
and applies the same ``rules.toml``:

========================  ================================================
Static mesh               LOD0 triangles, material slots, UV channels
Texture2D                 width/height, power of two, naming suffix
========================  ================================================

A failure goes to the Message Log (Data Validation) with the gate's wording; a
pass is silent. ``[[exceptions]]`` in the rules apply, with ``path`` matched
against the content path below ``/Game`` (``Characters/Boss/*`` matches
``/Game/Characters/Boss/SM_Boss``).

Installed by ``init_unreal.py`` (same folder). Set
``ASSETGATEKEEPER_UNREAL_VALIDATE=off`` to leave validation to the import
check alone.

The rule logic (``mesh_problems``, ``texture_problems``) does not import
``unreal``, so it is tested outside the editor (tests/test_formats.py);
``tests/unreal/validate_in_editor.py`` runs it inside a real editor.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

ENV_HOME = "ASSETGATEKEEPER_HOME"
ENV_RULES = "ASSETGATEKEEPER_RULES"
ENV_VALIDATE = "ASSETGATEKEEPER_UNREAL_VALIDATE"
#: What content paths are taken relative to when [[exceptions]] are matched.
CONTENT_ROOT = Path("/agk-unreal-content")

_cache: dict = {}


def engine_module():
    """The gate itself (asset_gatekeeper.py), from ASSETGATEKEEPER_HOME."""
    home = os.environ.get(ENV_HOME)
    if home and home not in sys.path:
        sys.path.insert(0, home)
    import asset_gatekeeper   # raises ImportError when the machine is not set up
    return asset_gatekeeper


def load_rules(engine=None):
    """(Budget, exceptions) from ASSETGATEKEEPER_RULES, or the defaults."""
    engine = engine or engine_module()
    path = os.environ.get(ENV_RULES) or ""
    key = (path, os.path.getmtime(path) if path and os.path.isfile(path) else 0)
    if _cache.get("key") == key:
        return _cache["rules"]
    exceptions = ()
    if path and os.path.isfile(path):
        document = engine.parse_rules(Path(path))
        budget = engine.Budget.from_mapping(document)
        exceptions = tuple(replace(item, base=CONTENT_ROOT)
                           for item in engine.parse_exceptions(document, None))
    else:
        budget = engine.Budget()
    _cache.update(key=key, rules=(budget, exceptions))
    return budget, exceptions


def content_path(game_path: str) -> Path:
    """'/Game/Props/SM_Rock.SM_Rock' -> <CONTENT_ROOT>/Props/SM_Rock."""
    package = game_path.split(".", 1)[0]
    parts = [part for part in package.split("/") if part]
    if parts and parts[0].lower() == "game":
        parts = parts[1:]
    return CONTENT_ROOT.joinpath(*parts) if parts else CONTENT_ROOT


def _apply_exceptions(budget, exceptions, game_path: str, today=None):
    """The budget with active exceptions applied, plus notes and expiries."""
    today = today or date.today()
    asset = content_path(game_path)
    changes, notes, expired = {}, [], []
    for exception in exceptions:
        if not exception.matches(asset):
            continue
        if exception.active(today):
            changes[exception.rule] = exception.allow
            notes.append(f"exception: {exception.label()} until {exception.expires.isoformat()} "
                         f"({exception.owner}: {exception.reason})")
        else:
            expired.append(f"the exception {exception.label()} ({exception.owner}) expired on "
                           f"{exception.expires.isoformat()}")
    if changes:
        budget = replace(budget, **changes)
    return budget, notes, expired


def mesh_problems(facts: dict, budget, exceptions=(), game_path: str = "", today=None) -> list:
    """The gate's reasons a static mesh (as Unreal imported it) is over budget."""
    budget, _notes, expired = _apply_exceptions(budget, exceptions, game_path, today)
    problems = []
    triangles = int(facts.get("triangles", 0))
    if triangles > budget.max_triangles:
        problems.append(f"{triangles:,} triangles exceeds the {budget.max_triangles:,} budget "
                        f"(+{triangles - budget.max_triangles:,})")
    slots = int(facts.get("material_slots", 0))
    if slots > budget.max_material_slots:
        problems.append(f"{slots} material slots exceeds the {budget.max_material_slots} slot "
                        "budget (draw calls)")
    uvs = facts.get("uv_channels")
    if uvs is not None and int(uvs) > budget.max_uv_channels:
        problems.append(f"{int(uvs)} UV channels exceeds the {budget.max_uv_channels} channel budget")
    return problems + expired if problems else problems


def texture_problems(facts: dict, budget, engine, exceptions=(), game_path: str = "",
                     today=None) -> list:
    """The gate's reasons a texture (as Unreal imported it) breaks the rules."""
    budget, _notes, expired = _apply_exceptions(budget, exceptions, game_path, today)
    problems = []
    width, height = int(facts.get("width", 0)), int(facts.get("height", 0))
    if max(width, height) > budget.max_texture_edge:
        problems.append(f"{width}x{height} exceeds the {budget.max_texture_edge}x"
                        f"{budget.max_texture_edge} maximum resolution")
    if budget.require_power_of_two and not (engine.is_power_of_two(width)
                                            and engine.is_power_of_two(height)):
        problems.append(f"{width}x{height} is not a power of two")
    if budget.enforce_texture_naming:
        pattern = engine.build_naming_pattern(budget.texture_suffixes)
        name = str(facts.get("name", ""))
        if pattern is not None and not pattern.search(name):
            problems.append("name does not end with a standard suffix "
                            f"({', '.join(budget.texture_suffixes)})")
    return problems + expired if problems else problems


# --- the editor side: everything below needs ``unreal`` --- #

def static_mesh_facts(mesh, unreal) -> dict:
    # None = could not be read: never taken as 0 (a 0 passes every budget).
    facts = {"triangles": None, "material_slots": 0, "uv_channels": None,
             "lods": int(mesh.get_num_lods()) if hasattr(mesh, "get_num_lods") else 1}
    try:
        facts["triangles"] = int(mesh.get_num_triangles(0))
    except Exception:
        pass
    try:
        facts["material_slots"] = len(mesh.get_editor_property("static_materials"))
    except Exception:
        try:
            facts["material_slots"] = int(mesh.get_num_sections(0))
        except Exception:
            pass
    for source in ("StaticMeshEditorSubsystem", "EditorStaticMeshLibrary"):
        holder = getattr(unreal, source, None)
        if holder is None:
            continue
        try:
            target = (unreal.get_editor_subsystem(holder) if source.endswith("Subsystem")
                      else holder)
            facts["uv_channels"] = int(target.get_num_uv_channels(mesh, 0))
            break
        except Exception:
            continue
    return facts


def texture_facts(texture) -> dict:
    return {"width": int(texture.blueprint_get_size_x()),
            "height": int(texture.blueprint_get_size_y()),
            "name": texture.get_name()}


def asset_problems(asset, unreal) -> tuple:
    """(checked, problems). checked is False for asset types the gate ignores."""
    engine = engine_module()
    budget, exceptions = load_rules(engine)
    path = asset.get_path_name()
    if isinstance(asset, unreal.StaticMesh):
        facts = static_mesh_facts(asset, unreal)
        if facts["triangles"] is None:
            return True, ["could not read the triangle count from Unreal, so this mesh was "
                          "NOT checked - fails until it can be"]
        return True, mesh_problems(facts, budget, exceptions, path)
    if isinstance(asset, unreal.Texture2D):
        return True, texture_problems(texture_facts(asset), budget, engine, exceptions, path)
    return False, []


def enabled() -> bool:
    return (os.environ.get(ENV_VALIDATE) or "on").strip().lower() not in ("off", "0", "false", "no")


def build_validator_class(unreal):
    """The EditorValidatorBase subclass, built only inside the editor."""

    @unreal.uclass()
    class AssetGatekeeperValidator(unreal.EditorValidatorBase):

        @unreal.ufunction(override=True)
        def can_validate_asset(self, asset):
            return isinstance(asset, (unreal.StaticMesh, unreal.Texture2D))

        @unreal.ufunction(override=True)
        def validate_loaded_asset(self, asset, validation_errors):
            try:
                checked, problems = asset_problems(asset, unreal)
            except ImportError:
                message = ("AssetGatekeeper is not configured on this machine "
                           f"(set {ENV_HOME}); this asset was NOT checked")
                self.asset_warning(asset, message)
                return unreal.DataValidationResult.NOT_VALIDATED, validation_errors
            except Exception as error:   # a broken rules.toml must say so, not pass
                message = f"AssetGatekeeper could not check this asset: {error}"
                validation_errors = _fail(self, asset, message, validation_errors)
                return unreal.DataValidationResult.INVALID, validation_errors
            if not checked:
                return unreal.DataValidationResult.NOT_VALIDATED, validation_errors
            if not problems:
                self.asset_passes(asset)
                return unreal.DataValidationResult.VALID, validation_errors
            for problem in problems:
                validation_errors = _fail(self, asset, f"AssetGatekeeper: {problem}",
                                          validation_errors)
            return unreal.DataValidationResult.INVALID, validation_errors

    return AssetGatekeeperValidator


def _fail(validator, asset, message, validation_errors):
    """asset_fails across engine versions (5.0-5.3 return the error list)."""
    try:
        returned = validator.asset_fails(asset, message, validation_errors)
        return returned if returned is not None else validation_errors
    except TypeError:
        validator.asset_fails(asset, message)
        return validation_errors


_registered: dict = {}


def register_validator(unreal=None):
    """Add the validator to the editor's validator subsystem (once). Returns it."""
    if unreal is None:
        import unreal  # noqa: PLC0415 - only exists inside the editor
    if "validator" in _registered:
        return _registered["validator"]
    if not enabled():
        unreal.log(f"[AssetGatekeeper] data validation off ({ENV_VALIDATE})")
        return None
    if getattr(unreal, "EditorValidatorBase", None) is None or \
            getattr(unreal, "EditorValidatorSubsystem", None) is None:
        unreal.log_warning("[AssetGatekeeper] Data Validation is not available (enable the "
                           "DataValidation plugin); assets are checked on import only")
        return None
    validator_class = build_validator_class(unreal)
    validator = validator_class()
    subsystem = unreal.get_editor_subsystem(unreal.EditorValidatorSubsystem)
    subsystem.add_validator(validator)
    # Held here: a Python-made UObject nothing references can be collected.
    _registered.update(validator=validator, cls=validator_class, subsystem=subsystem)
    unreal.log("[AssetGatekeeper] data validation active (save, Validate Assets, "
               "Validate Changelist)")
    return validator
