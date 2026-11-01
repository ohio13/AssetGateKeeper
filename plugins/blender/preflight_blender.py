"""Headless pre-flight probe for Blender (4.x).

    blender --background --factory-startup \
        --python plugins/blender/preflight_blender.py -- \
        --fixture <cube.obj> --preset retro --out <result.json>

Why a separate entry point rather than the panel: the panel is UI, this is the
same engine driven by a test runner. Both call `plugins/preflight_core.py`, so
what this proves - geometry counting, budget resolution, fuzzy tags - is what the
artist sees in the sidebar.

Exit codes: 0 everything matched the budget, 1 a budget failure was reported,
2 the harness itself failed (import error, unreadable fixture). The runner
asserts on both the JSON and the code, because a probe that always exits 0 is
indistinguishable from a broken one.
"""
import json
import os
import sys

import bpy

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _candidate in (_ROOT, _HERE, os.path.dirname(_HERE)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

try:
    from plugins import preflight_core as core
except ImportError:                                   # pragma: no cover
    import preflight_core as core                     # type: ignore[no-redef]
try:
    import preflight_panel as panel
except ImportError:                                   # pragma: no cover
    from plugins.blender import preflight_panel as panel  # type: ignore[no-redef]


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    options = {"fixture": "", "preset": "", "rules": "", "out": "",
               "tag_mode": "suggest", "folder": ""}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("--") and index + 1 < len(argv):
            options[token[2:].replace("-", "_")] = argv[index + 1]
            index += 2
            continue
        if token == "--register":
            options["register"] = "1"
        index += 1
    return options


def main() -> int:
    options = parse_args()
    result = {
        "dcc": "blender",
        "version": bpy.app.version_string,
        "registered": False,
        "fixture": {},
        "report": {},
        "exit": 2,
    }
    try:
        # 1. the add-on registers (the panel would otherwise be inert)
        panel.register()
        result["registered"] = bool(getattr(bpy.types, "AGK_PT_preflight", None))

        # 2. live geometry, evaluated the way the panel sees it
        bpy.ops.mesh.primitive_cube_add(size=1.0)
        live = panel.scene_facts()
        result["live_cube"] = {"triangles": live[0], "materials": live[2]}

        # 3. the fixture through the same core the panel calls
        fixture = options["fixture"]
        if not fixture or not os.path.isfile(fixture):
            raise RuntimeError(f"fixture not found: {fixture!r}")
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=fixture)
        else:                                          # Blender 3.x
            bpy.ops.import_scene.obj(filepath=fixture)

        triangles, vertices, materials, textures, degenerate = panel.scene_facts()
        budget = core.load_budget(options["rules"] or None,
                                  preset=options["preset"] or None)
        report = core.assess(
            asset=os.path.basename(fixture), triangles=triangles, vertices=vertices,
            materials=materials, textures=textures, degenerate=degenerate,
            folder=options["folder"] or "charcter",      # deliberate typo: fuzzy check
            tag_budgets={"character": core.load_budget(None, preset="retro")},
            tag_mode=options["tag_mode"] or "suggest",
            budget=budget)
        result["fixture"] = {
            "path": fixture,
            "triangles": triangles,
            "vertices": vertices,
            "materials": materials,
        }
        result["report"] = report.as_dict()
        result["tag"] = report.tag
        result["tag_suggestion"] = report.tag_suggestion
        result["exit"] = 0 if report.ok else 1
    except Exception as exc:                           # noqa: BLE001 - reported in JSON
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["exit"] = 2
    finally:
        if options["out"]:
            with open(options["out"], "w", encoding="utf-8") as handle:
                json.dump(result, handle, indent=2)
        print("AGK_PROBE_RESULT " + json.dumps(result.get("exit")))
    return int(result["exit"])


if __name__ == "__main__":
    code = main()
    # Quit explicitly: --background normally exits, but an add-on or a handler can
    # keep the process alive, and a test runner must never wait for that.
    bpy.ops.wm.quit_blender()
    sys.exit(code)
