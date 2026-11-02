"""Run AssetGatekeeper's Data Validation inside a real Unreal Editor (G6, check N6).

    UnrealEditor-Cmd.exe "<Project>.uproject" -run=pythonscript ^
        -script="<AssetGatekeeper>\\tests\\unreal\\validate_in_editor.py" ^
        -unattended -nopause -nosplash -NullRHI -nosound

Needs ASSETGATEKEEPER_HOME (the AssetGatekeeper folder) and, optionally,
AGK_VALIDATION_OUT (default: validation_result.json in the AssetGatekeeper folder).

It imports two meshes from tests/fixtures/fbx into /Game/AGK_ValidationTest -
one within the default budget and one built over it - runs the validator on
each, writes what happened to the JSON file, and deletes the test folder. The
verdict is in the JSON ("ok": true), not in UnrealEditor-Cmd's exit code.
"""

import json
import os
import sys
import traceback

import unreal

HOME = os.environ.get("ASSETGATEKEEPER_HOME", "")
OUT = os.environ.get("AGK_VALIDATION_OUT") or os.path.join(HOME or ".", "validation_result.json")
FOLDER = "/Game/AGK_ValidationTest"
result = {"ok": False, "checks": [], "engine": unreal.SystemLibrary.get_engine_version()}


def check(label, condition, detail=""):
    result["checks"].append({"check": label, "ok": bool(condition), "detail": str(detail)})


def import_mesh(path, name):
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", path)
    task.set_editor_property("destination_path", FOLDER)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("automated", True)
    task.set_editor_property("save", False)
    task.set_editor_property("replace_existing", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    return unreal.EditorAssetLibrary.load_asset(f"{FOLDER}/{name}")


def heavy_obj(path, triangles=60_000):
    """A strip of quads with `triangles` triangles: over the 50,000 default."""
    columns = triangles // 2
    with open(path, "w", encoding="ascii") as handle:
        for x in range(columns + 1):
            handle.write(f"v {x * 0.01:.4f} 0 0\nv {x * 0.01:.4f} 1 0\n")
        for x in range(columns):
            a = 2 * x + 1
            handle.write(f"f {a} {a + 2} {a + 1}\nf {a + 1} {a + 2} {a + 3}\n")


try:
    sys.path.insert(0, os.path.join(HOME, "plugins", "unreal"))
    import agk_validator

    validator = agk_validator.register_validator(unreal)
    check("the validator registers", validator is not None)
    good = import_mesh(os.path.join(HOME, "tests", "fixtures", "fbx", "tex_relative.fbx"), "SM_Good")
    heavy_path = os.path.join(unreal.Paths.project_saved_dir(), "agk_heavy.obj")
    heavy_obj(heavy_path)
    heavy = import_mesh(heavy_path, "SM_Heavy")
    check("both test meshes imported", good is not None and heavy is not None)
    for name, asset, want_ok in (("SM_Good", good, True), ("SM_Heavy", heavy, False)):
        checked, problems = agk_validator.asset_problems(asset, unreal)
        check(f"{name}: the gate's rules give the expected verdict",
              checked and (not problems) == want_ok, problems)
    subsystem = unreal.get_editor_subsystem(unreal.EditorValidatorSubsystem)
    try:
        settings = unreal.ValidateAssetsSettings()
        settings.set_editor_property("show_if_no_failures", False)
        try:
            results = subsystem.validate_assets_with_settings(
                [unreal.EditorAssetLibrary.find_asset_data(f"{FOLDER}/SM_Heavy")], settings)
        except TypeError:
            results = subsystem.validate_assets_with_settings(
                [unreal.EditorAssetLibrary.find_asset_data(f"{FOLDER}/SM_Heavy")], settings,
                unreal.ValidateAssetsResults())
        detail = results
        failed = results[0] if isinstance(results, tuple) else results
        check("Unreal's own Validate Assets fails the heavy mesh", int(failed) >= 1, detail)
    except Exception as error:   # API names differ between 5.x releases
        check("Unreal's own Validate Assets fails the heavy mesh", False,
              f"could not call it from Python here ({error}); use right-click > Validate")
    result["ok"] = all(item["ok"] for item in result["checks"])
except Exception:
    result["error"] = traceback.format_exc()
finally:
    try:
        unreal.EditorAssetLibrary.delete_directory(FOLDER)
    except Exception:
        pass
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    unreal.log(f"[AssetGatekeeper] validation test {'PASSED' if result['ok'] else 'FAILED'}: {OUT}")
