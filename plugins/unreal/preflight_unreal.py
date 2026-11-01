"""Headless pre-flight probe for Unreal Engine 5.

    UnrealEditor-Cmd.exe <Project.uproject> -run=pythonscript \
        -script="plugins/unreal/preflight_unreal.py" -unattended -nopause -nosplash

Configuration comes from the environment, not from argv: Unreal's own command
line is already crowded, and `sys.argv` differs between `-run=pythonscript` and a
menu-triggered script.

    AGK_PROBE_OUT       where to write result.json   (required)
    AGK_PROBE_FIXTURE   an .obj to import and measure (optional)
    AGK_PROBE_PRESET    e.g. "retro"

If an editor StaticMesh can be produced, the probe measures that. Otherwise it
falls back to reading the fixture, and says so in ``mode`` - a probe that pretends
to have measured an engine asset it never imported is worse than no probe.

Exit codes: 0 pass, 1 budget failure, 2 harness failure.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import unreal  # noqa: E402  (only importable inside the editor)


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def fixture_triangles(path):
    """Count faces in a Wavefront OBJ - the fallback when no asset import is possible."""
    triangles = 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("f "):
                triangles += max(0, len(line.split()) - 3)
    return triangles


def import_and_measure(path):
    """Import the fixture into /Game and read the StaticMesh, if the API allows."""
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", path)
    task.set_editor_property("destination_path", "/Game/AGKProbe")
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", False)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    imported = task.get_editor_property("imported_object_paths") or []
    for asset_path in imported:
        asset = unreal.EditorAssetLibrary.load_asset(asset_path)
        if isinstance(asset, unreal.StaticMesh):
            return asset, int(asset.get_num_triangles(0) or 0)
    raise RuntimeError(f"import produced no StaticMesh: {list(imported)}")


def main() -> int:
    from plugins import preflight_core as core

    out = _env("AGK_PROBE_OUT")
    fixture = _env("AGK_PROBE_FIXTURE")
    preset = _env("AGK_PROBE_PRESET") or None
    result = {"dcc": "unreal", "version": unreal.SystemLibrary.get_engine_version(),
              "mode": "none", "fixture": {}, "report": {}, "exit": 2}
    try:
        triangles = 0
        materials = 0
        if fixture and os.path.isfile(fixture):
            try:
                asset, triangles = import_and_measure(fixture)
                materials = len(asset.get_editor_property("static_materials") or [])
                result["mode"] = "asset"
                result["asset_path"] = asset.get_path_name()
            except Exception as exc:                 # noqa: BLE001 - fall back, say so
                triangles = fixture_triangles(fixture)
                result["mode"] = "file"
                result["import_error"] = f"{type(exc).__name__}: {exc}"[:200]

        budget = core.load_budget(None, preset=preset)
        report = core.assess(asset=os.path.basename(fixture) or "untitled",
                             triangles=triangles, materials=materials,
                             folder="charcter",           # deliberate typo
                             tag_budgets={"character": core.load_budget(None, preset="retro")},
                             tag_mode="suggest", budget=budget)
        result["fixture"] = {"path": fixture, "triangles": triangles,
                             "materials": materials}
        result["report"] = report.as_dict()
        result["tag_suggestion"] = report.tag_suggestion
        result["exit"] = 0 if report.ok else 1
    except Exception as exc:                          # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["exit"] = 2
    finally:
        if out:
            with open(out, "w", encoding="utf-8") as handle:
                json.dump(result, handle, indent=2)
        print("AGK_PROBE_RESULT " + json.dumps(result.get("exit")))
        # Quit explicitly: a headless editor that is not told to exit sits in the
        # test harness until its timeout, which is exactly what this suite forbids.
        try:
            unreal.SystemLibrary.quit_editor()
        except Exception:
            pass
    return int(result["exit"])


raise SystemExit(main())
