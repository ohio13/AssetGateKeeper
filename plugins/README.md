# DCC integrations

Same rules, same `rules.toml`, four different front doors - so the verdict reaches
the artist where they actually work instead of in a terminal they never open.

| Host | Path | Feedback | Can it block? |
| :--- | :--- | :--- | :--- |
| **Blender** | `blender/addon.py` | Panel in the 3D Viewport sidebar, problems in red | Pre-export check (artist decides) |
| **Maya** | `maya/gatekeeper_maya.py` + `userSetup.py` | Shelf button + dialog + Script Editor list | Pre-export, in-scene |
| **Houdini** | `houdini/sop_gatekeeper.py` | Node warning badge on cook | Non-fatal warning (never breaks the cook) |
| **Unreal** | `unreal/init_unreal.py` + `import_gatekeeper.py` + `agk_validator.py` | Message Log + dialog after import; Data Validation on save and Validate Changelist | **Yes** for scripted imports; drag-and-drop moved to `_AssetGatekeeper_Rejected` (or warn/delete) |
| **3ds Max** | `max/max_preflight.py` | Message box with the full report | Pre-export check (artist decides) |

## In-viewport pre-flight (v1.1)

These four are the lightweight panels: they read the numbers from the live scene
and answer before anything is exported. All four share one decision engine,
`plugins/preflight_core.py`, so the panel and the gate cannot disagree.

| Host | File | Entry point |
| :--- | :--- | :--- |
| Blender | `blender/preflight_panel.py` | Add-on, N-panel **Asset Gatekeeper** |
| Maya | `maya/maya_preflight.py` | `import maya_preflight as p; p.show()` |
| 3ds Max | `max/max_preflight.py` | `python.ExecuteFile` then `show()` |
| Unreal | `unreal/unreal_preflight.py` | `run_and_log()` (Output Log) or `show()` |

### Headless entry points (for CI and the integration suite)

These run the same engine without a UI, and are what
`tests/test_dcc_integration.py` drives:

| Host | File | Invocation |
| :--- | :--- | :--- |
| Blender | `blender/preflight_blender.py` | `blender --background --python plugins/blender/preflight_blender.py -- --fixture x.obj --preset retro --out r.json` |
| Houdini | `houdini/preflight_houdini.py` | `hython plugins/houdini/preflight_houdini.py --out r.json --preset retro a.obj b.obj` |
| Unreal | `unreal/preflight_unreal.py` | `UnrealEditor-Cmd.exe P.uproject -run=pythonscript -script=... -unattended -NullRHI` (config via `AGK_PROBE_*`) |

Each writes JSON and exits 0/1/2 (pass / budget failure / harness error).
Process exit codes are authoritative except in Unreal, where the editor
reports its own status and the verdict lives in the JSON.

They report the geometry and material numbers, the budget they were judged
against, and a fuzzy tag suggestion (`SUGGESTED character  (rename the folder)`)
so a misspelled department folder is caught in the viewport, not at submit.

All four read the project budget through the engine's own parser
(`asset_gatekeeper.Budget.from_mapping`), so a rule can never mean different things
in different apps.

## Common environment

```bash
# folder containing asset_gatekeeper.py
export ASSETGATEKEEPER_HOME=/path/to/asset-gatekeeper
# optional: an explicit budget instead of <folder>/rules.toml
export ASSETGATEKEEPER_RULES=/path/to/rules.toml
# Unreal only: an interpreter that has trimesh + Pillow (for scripted imports)
export ASSETGATEKEEPER_PYTHON=/path/to/python
```

Windows: set them in *System Properties > Environment Variables* or your launch
script.

## Blender

1. *Edit > Preferences > Add-ons > Install...* -> pick `blender/addon.py`, tick it.
2. Sidebar (`N`) -> **AssetGatekeeper** tab. Select objects, press
   **Run Gatekeeper Check**.
3. If the panel reports "engine error", the add-on's Python either lacks
   `trimesh`/`Pillow` or `ASSETGATEKEEPER_HOME` is unset. Set *Engine python* in the
   panel to any interpreter that has them - Blender's bundled Python usually does
   not until you install them:
   `<blender>/4.2/python/bin/python.exe -m pip install trimesh pillow watchdog`.

The add-on exports the selection to a temp `.gltf` (so the check sees exactly what
the exporter would write, not your viewport), runs the engine with `--json`, and
paints the returned `problems` list into the panel.

## Maya

1. Copy `maya/gatekeeper_maya.py` and `maya/userSetup.py` to a directory on
   `MAYA_SCRIPT_PATH` (e.g. `~/maya/scripts`), **or** merge the two blocks marked
   in `userSetup.py` into your existing one.
2. Restart Maya -> an `AGK` shelf button and an **AssetGatekeeper** menu appear.
3. Run it: `python("import gatekeeper_maya; gatekeeper_maya.run()")`.

Maya is checked **in-scene, without exporting**: triangles via `polyEvaluate`,
UV sets via `polyUVSet`, material slots via the connected `shadingEngine`s,
influences via `polyEvaluate -weightedVertexInfluence` (falling back to a
`skinPercent` sample), and zero-area faces via OpenMaya face areas. This catches
things an exporter would silently fix.

## Houdini

1. Create a SOP of type **Python** (or use a *Python SOP* inside your HDA).
2. Paste `houdini/sop_gatekeeper.py` into its Code field.
3. On every cook it counts triangles, material slots (`shop_materialpath`), UV
   channel attributes (`uv`, `uv2`, ...) and zero-area prims, then posts a **node
   warning badge**.

It never raises `hou.Error` - a gate that breaks the cook is worse than no gate.
Areas are computed on a sample (`MAX_PRIMS_FOR_AREA_SCAN`) so a 4M-prim cook stays
interactive.

## Unreal

1. Copy `unreal/init_unreal.py`, `unreal/import_gatekeeper.py` and
   `unreal/agk_validator.py` (checks on save and on Validate Changelist) into
   `<Project>/Content/Python/`.
2. Enable **Python Editor Script Plugin**.
3. Set `ASSETGATEKEEPER_PYTHON` and `ASSETGATEKEEPER_HOME`; restart the editor.

**Drag-and-drop cannot be vetoed, so it is rejected after import.**
`OnAssetPreImport` is a notification, not a veto - by the time a Python delegate
runs, the factory is already chosen and exceptions inside a delegate are
swallowed. Imported StaticMeshes are evaluated post-import and, by default,
moved to `/Game/_AssetGatekeeper_Rejected` with a dialog
(`ASSETGATEKEEPER_UNREAL_ON_FAIL=quarantine|delete|warn`).

**Scripted imports can be blocked for real:**

```python
from import_gatekeeper import import_with_gatekeeper, GatekeeperBlocked

try:
    import_with_gatekeeper(tasks)          # validates every source file first
except GatekeeperBlocked as blocked:
    unreal.log_error(f"{len(blocked.problems)} file(s) rejected")
```

A hard veto on drag-and-drop requires a C++ `UFactory::FactoryCreateFile` early-out.
That is a plugin, not a Python script, and deliberately out of scope here.

## What has been verified

Honesty matters more than a green table: **none of these four hosts were available
on the machine this was built on.** The plugin files are syntax-checked
(`py_compile`) and written with capability probing plus fallbacks so a wrong API
name degrades to a warning instead of taking the host down. Treat the first run in
each app as a smoke test, and please open an issue with the version you tried.

What *is* verified end-to-end is the contract they all depend on:

```bash
python asset_gatekeeper.py path/to/asset.gltf --json
# {"file": "...", "name": "asset.gltf", "passed": false,
#  "metric": "214,301 tris / 7 mat", "problems": ["214,301 triangles exceeds ..."]}
```
