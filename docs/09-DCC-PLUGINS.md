# 09 - DCC plugins: Blender, Maya, 3ds Max, Houdini, Unreal

Two things ship for every DCC, and they are the same engine:

| Thing | Runs in | Purpose |
|---|---|---|
| **Panel / hook** | a live session | tell the artist *before* export |
| **Headless probe** | a process, no UI | prove the integration in CI |

Both call `plugins/preflight_core.py`, which applies the same `Budget` objects as
the CLI. That is the reason a panel and a CI run can never disagree: they are not
two implementations, they are two front doors.

```
plugins/
  preflight_core.py              the decision engine (budgets, degenerate faces,
                                 texture sizes, fuzzy tag near-miss)
  blender/preflight_panel.py     add-on, N-panel
  blender/preflight_blender.py   headless entry point
  maya/maya_preflight.py         window + shelf button
  max/max_preflight.py           scripted panel
  houdini/preflight_houdini.py   headless entry point
  unreal/unreal_preflight.py     editor script + headless entry point
tools/houdini_export_hook.py     the shell-out hook Houdini actually uses
```

## The shared report

Every adapter prints the same shape, because an artist who learns it in Blender
should not have to relearn it in Maya:

```
BLOCKED - 3 error(s)
triangles 4,000   vertices 2,800
materials 4
budget    tris<=1,500  materials<=1  texture<=64px
SUGGESTED character  (rename the folder)
[x] 4,000 triangles exceeds the 1,500 budget (+2,500); decimate or move it to a tag with a bigger budget
[x] 4 material slots exceeds the 1 slot budget (each slot is a draw call); merge or atlas
[x] texture 'boss_Albedo.png' is 256x256, over the 64px budget
```

`SUGGESTED` is the line artists actually act on: the folder is misspelled, and
the panel knows which folder was meant.

---

## Blender (3.6 / 4.x)

### Install the panel

1. Set the environment variable `ASSETGATEKEEPER_HOME` to the AssetGatekeeper
   folder (Blender copies an installed add-on into its own folder, away from the
   engine). Restart Blender after setting it.
2. `Edit > Preferences > Add-ons > Install...`
3. Choose `<AGK>/plugins/blender/preflight_panel.py`
4. Tick **AssetGatekeeper Pre-flight**
5. The panel appears in the 3D Viewport sidebar (`N`) as **Asset Gatekeeper**.
   If it shows "AssetGatekeeper not found", step 1 is missing.

(Verified in Blender 4.0.2, Nov 2026. `plugins/blender/addon.py` is the other
panel: it exports the selection and runs the full gate on it - install it the
same way, it appears as **AssetGatekeeper**.)

The panel needs no restart and no build step. It reads:

* triangles and vertices from the **evaluated depsgraph** - so a subdivision
  surface, a modifier stack or a geo-nodes setup is measured as it will export,
  not as it was modelled;
* material slots that actually have a material;
* image sizes from the image texture nodes feeding those materials;
* zero-area faces (degenerate geometry breaks Nanite and ray tracing).

### Using it

Select the objects you care about (or select nothing to measure the whole scene),
and read the panel. It re-runs as you change selection.

**Budgets:** the panel uses the built-in default budget unless you point it at
your project. Two ways:

```python
# in Blender's Python console, once per session - then reopen the panel
import bpy, sys
sys.path.insert(0, r"C:\Tools\AssetGatekeeper\plugins\blender")
import preflight_panel as p
report = p.run_preflight(rules_path=r"C:\Projects\MyGame\rules.toml")
print(report.as_text())
```

or give the panel a preset (no rules file needed):

```python
p.run_preflight(preset="ps2")
```

**Fuzzy tags:** the panel takes the folder name from the `.blend` path, so
`.../assets/charcter/hero.blend` reports `SUGGESTED character`.

### Headless (for CI)

```
blender.exe --background --factory-startup ^
  --python "<AGK>\plugins\blender\preflight_blender.py" -- ^
  --fixture "C:\Proj\assets\char\hero.obj" --preset retro --out result.json
```

Exit codes: `0` pass, `1` budget failure, `2` harness failure (bad fixture, import
error). The probe registers the add-on, measures a live cube (12 triangles, a
sanity check that the depsgraph is really being evaluated), imports the fixture,
and writes JSON - so a Blender banner cannot corrupt the result.

---

## Maya (2022+)

### Install

No package, no shelf file:

```python
import sys
sys.path.append(r"C:\Tools\AssetGatekeeper\plugins\maya")   # the maya folder itself
import maya_preflight as p
p.show()
```

Put those three lines on a shelf button (middle-drag the script to a shelf) and
it is one click from then on.

### What it measures

* faces and triangles from `polyEvaluate` - the same numbers the Attribute Editor
  shows;
* materials from the shading engines actually assigned (not from the material
  editor's list);
* texture sizes from the `file` nodes feeding those materials.

### Notes

* `p.run_preflight(rules_path=..., preset=...)` returns the report object if you
  want to drive it from your own script.
* The window is Qt-free (plain `cmds`): it works in Maya 2022 to 2026 without a
  PySide version fight, which is the single most common reason DCC tools break.

### Shelf button and menu

Copy `plugins/maya/userSetup.py` and `gatekeeper_maya.py` into
`Documents/maya/scripts` (or put `plugins/maya` on `PYTHONPATH`; `MAYA_SCRIPT_PATH`
alone is not enough) and restart Maya: an **AGK** button on the current shelf and
an **AssetGatekeeper** menu appear. Verified in Maya 2027.2, Nov 2026, together
with the pre-flight window and the check itself (`mayapy`, 9 scene cases).

---

## 3ds Max (2021+)

### Install

```maxscript
python.ExecuteFile @"C:\Tools\AssetGatekeeper\plugins\max\max_preflight.py"
```

It checks the selection (or the whole scene when nothing is selected) and shows
the result straight away: a warning icon when it is blocked, an information icon
when it is clean. There is no panel to open.

To get an **AGK** button, run this once:

```maxscript
python.ExecuteFile @"C:\Tools\AssetGatekeeper\plugins\max\install_max_button.py"
```

It adds the action *AssetGatekeeper pre-flight check* (category
**AssetGatekeeper**), which Max keeps across restarts. Put it on a toolbar or menu
from **Customize > Customize User Interface > Toolbars** (or **Menus**): pick the
AssetGatekeeper category and drag the action across.

### What it measures

* `getPolygonCount` triangles and vertices of every geometry node (primitives
  included; camera targets and bones are skipped);
* materials of any type (Physical, Standard, Arnold...), Multi/Sub-Object
  expanded to its sub-materials;
* bitmap sizes from Standard (diffuse/bump/specular) and Physical (base colour,
  bump, roughness, metalness, normal) map slots.

Verified in 3ds Max 2027.2 (Python 3.13.9) through `3dsmaxbatch.exe`, Nov 2026.

---

## Houdini (19+ / 22 tested)

Houdini is the one DCC that needs care, because **its bundled Python has neither
trimesh nor Pillow**. The gate cannot run inside hython; it has to be a
subprocess.

### The hook

`tools/houdini_export_hook.py` is that subprocess. From a Houdini Python shell:

```python
import sys
sys.path.append(r"C:\Tools\AssetGatekeeper\tools")
import houdini_export_hook as agk

agk.verify_export([
    r"C:\Proj\export\env\cliff.obj",
    r"C:\Proj\export\char\hero.obj",
], extra_args=["--preset", "ps2"])
```

It returns one record per file: `path`, `returncode` (0 pass / 1 fail / 2 error),
`verdict` (the parsed JSON), and the gate's `output`.

### Point it at a Python that has the packages

```
setx AGK_PYTHON "C:\Tools\agk-venv\Scripts\python.exe"
setx AGK_HOME   "C:\Tools\AssetGatekeeper"
```

`AGK_PYTHON` is the studio Python that has `trimesh` and `Pillow`. Without it the
hook uses `sys.executable`, which is hython - and hython will report the gate as
broken rather than silently passing, which is the right failure.

**The gotcha that cost a day:** Houdini exports `PYTHONHOME` and `PYTHONPATH`
into every child process. A child Python then loads *Houdini's* standard library
and dies with `SRE module mismatch` before it reaches argparse. The hook strips
those four variables (`PYTHONHOME`, `PYTHONPATH`, `PYTHONSTARTUP`,
`PYTHONEXECUTABLE`) before spawning the gate. If you write your own shell-out
from a DCC, do the same.

### Where to call it

* a **shelf tool** after a ROP render/fetch;
* a **pre-export callback** on your geometry ROP (Houdini's `Before Render` or a
  Python post-write callback), so a failed gate cancels the file being written;
* a **PDG/lop** publish step, where the shell-out is natural anyway.

### Headless

```
hython.exe "<AGK>\plugins\houdini\preflight_houdini.py" ^
  --out result.json --preset retro crate.obj boss.obj
```

Exit codes follow the worst file: `0` all passed, `1` at least one failed,
`2` harness error. The probe records `AGK_PYTHON` in its JSON so a test can
assert the shell-out really used the configured interpreter.

---

## Unreal Engine 5

### Install

Unreal's Python is enabled per project: **Edit > Plugins > Python Editor Script
Plugin**. Then either

* run `plugins/unreal/unreal_preflight.py` from **Tools > Execute Python Script**,
  or
* copy it next to `init_unreal.py` in your project's `Content/Python` folder and
  call `run_and_log()` from a menu entry.

```python
import unreal_preflight as p
p.run_and_log()      # writes the report to the Output Log
p.show()             # or a modal dialog
```

### What it measures

* `get_num_triangles(0)` and `get_num_vertices(0)` on the **LOD 0** StaticMesh -
  the same numbers the Statistics panel shows;
* `static_materials` slots (draw calls).

### Drag-and-drop imports (`init_unreal.py`)

Put `init_unreal.py` in the project's `Content/Python` folder. It checks every
StaticMesh after it is imported (triangles and material slots, LOD0) and, by
default, **moves an over-budget mesh to `/Game/_AssetGatekeeper_Rejected`** on
the next editor tick, with a dialog saying why. Python cannot cancel a native
import, so this is reject-after-import; the mesh never stays where it was dropped.

| `ASSETGATEKEEPER_UNREAL_ON_FAIL` | Over-budget drag-and-drop |
| --- | --- |
| `quarantine` (default) | moved to `/Game/_AssetGatekeeper_Rejected` |
| `delete` | deleted |
| `warn` | 1.0 behaviour: dialog only, the asset stays |

Reimports (*Reimport*, *Reimport With New File*) are checked the same way. A
mesh that other assets already reference (a reimport over a placed mesh) is
never moved or deleted; it gets the warning. The Output Log says
`[AssetGatekeeper] import guard active` at startup, and
`... within budget` / `rejecting ...` per import. On a machine where
`ASSETGATEKEEPER_HOME` is not set it says `NOT checked` instead, and shows a
dialog once.

### On save and on Validate Changelist (`agk_validator.py`)

Copy `agk_validator.py` next to `init_unreal.py` in `Content/Python` and restart
the editor; the Output Log says `[AssetGatekeeper] data validation active`. The
same `rules.toml` then runs through Unreal's **Data Validation**:

* when an asset is **saved** (Project Settings > Editor > Data Validation >
  *Validate On Save*, on by default);
* on **right-click > Asset Actions > Validate Assets**, or **Validate Data** on
  a folder;
* on **Validate Changelist** in the Revision Control window. With changelist
  validation turned on for the project, a failing asset blocks the submit.

It reads Unreal's own numbers for the imported asset - LOD0 triangles, material
slots and UV channels of a static mesh; size, power of two and the naming suffix
of a Texture2D - and puts each failure in the Message Log (Data Validation) in
the gate's own words, e.g. `AssetGatekeeper: 60,000 triangles exceeds the
50,000 budget (+10,000)`. `[[exceptions]]` apply, with `path` matched against
the content path below `/Game` (`Characters/Boss/*` covers
`/Game/Characters/Boss/SM_Boss`); the day after one ends, the failure says so.

On a machine without `ASSETGATEKEEPER_HOME` an asset is reported *NOT checked*
(a warning, never a pass); a `rules.toml` that cannot be read fails validation
with the reason. `ASSETGATEKEEPER_UNREAL_VALIDATE=off` turns this part off and
leaves the import check. Needs the **Data Validation** plugin (enabled by
default in Unreal 5).

Check it in your engine without clicking (it imports a good and an over-budget
mesh into `/Game/AGK_ValidationTest`, validates them and deletes the folder):

```
set ASSETGATEKEEPER_HOME=C:\Users\you\Desktop\AssetGatekeeper
UnrealEditor-Cmd.exe "<Project>.uproject" -run=pythonscript ^
  -script="%ASSETGATEKEEPER_HOME%\tests\unreal\validate_in_editor.py" ^
  -unattended -nopause -nosplash -NullRHI -nosound
type "%ASSETGATEKEEPER_HOME%\validation_result.json"
```

`"ok": true` is a pass. Written against the UE 5.0-5.3 validator API
(`validate_loaded_asset`); tested with stand-ins for `unreal` here, and to be
confirmed in your 5.3 project (check N6 in the test report).

### Blocking scripted imports (`import_gatekeeper.py`)

`import_with_gatekeeper(tasks)` runs the gate on every task whose source is a
format it measures (`.fbx`, `.obj`, `.gltf`, `.glb`, `.png`, `.tga`, `.jpg`) and
imports nothing if any fails; other files (audio, `.uasset`, USD) import as
before. It **fails closed**: an unset `ASSETGATEKEEPER_PYTHON`/`ASSETGATEKEEPER_HOME`,
a missing engine, a crash or a missing source file blocks that file with the
reason. (Before 1.1, `.fbx` sources got no verdict at all and imported unchecked.)

It does not measure textures: Unreal resolves those through material instances
and a texture may be shared by fifty assets, so a per-asset texture budget would
be a different feature. State that to your artists rather than letting them
assume.

### Headless

```
UnrealEditor-Cmd.exe "<Proj>\AGKProbe.uproject" ^
  -run=pythonscript -script="<AGK>\plugins\unreal\preflight_unreal.py" ^
  -unattended -nopause -nosplash -NullRHI -nosound
```

Configuration is by environment, because Unreal's command line is already
crowded:

```
AGK_PROBE_OUT      where to write result.json   (required)
AGK_PROBE_FIXTURE  an .obj to import and measure
AGK_PROBE_PRESET   e.g. retro
```

**Exit codes:** UnrealEditor-Cmd returns the *editor's* status, not the script's;
a scripted quit reports `-1`. The verdict lives in the JSON (`exit` field). CI
must read the JSON for Unreal and not `$LASTEXITCODE`.

---

## Testing the DCC layer without opening a DCC

```
python tests/test_dcc_integration.py --only blender
python tests/test_dcc_integration.py --deep --deep-timeout 90
```

It builds two exact fixtures (12 triangles / 1 material, and 4,000 / 4), runs each
DCC headlessly with a hard timeout, and asserts geometry counts, the fuzzy-tag
suggestion and the exit codes. A missing DCC is a SKIP; a wrong number is a
failure. Set `--require` on a machine where a missing DCC is a real problem.

Verified on this machine: Blender 4.0.2, Houdini 22.0.459 and Unreal 5.3 all
pass headless (34 assertions, 0 skips, ~20 s in `--deep` mode).

The plugins have also been run inside the interactive editors (1 Nov 2026):
Houdini 22.0.459 - the SOP on a box, a 300x300 grid, ten packed copies and 100
open curves (12 / 178,802 / 196,020 / 0 triangles, warning badge on the two
over budget); Unreal 5.3.2 - `import_with_gatekeeper()` on good, 240k-triangle,
5-material, textured and unconfigured imports, plus a drag-and-drop style import
reaching `init_unreal.py`'s post-import warning. Unreal's own triangle counts
(768, 240,000) match the gate's FBX reader.

---

## DCC troubleshooting, by symptom

| Symptom | Cause | Fix |
|---|---|---|
| Blender panel missing after install | add-on ticked but the file was moved | reinstall from the new path; the panel registers on enable |
| Maya window does not appear | script run inside an IDE context | run it from the Maya script editor or a shelf button |
| Max prints `python` is not defined | you are in the MAXScript listener | `python.ExecuteFile @"..."` |
| Houdini gate always fails with no verdict | `PYTHONHOME` leak (see above) | set `AGK_PYTHON`; the hook already strips the variables |
| Unreal reports no Python module | Python Editor Script Plugin disabled | enable it and restart the editor |
| Unreal blocks every import, "SRE module mismatch" | editor inherited another Python's `PYTHONHOME` (launched from a DCC or launcher) | fixed in 1.1.0 - the gate cleans its child environment; update `import_gatekeeper.py` |
| Unreal log says "NOT checked (AssetGatekeeper is not configured...)" | `ASSETGATEKEEPER_HOME` not set for the editor | set it, restart the editor |
| Unreal log says "import hooks had been dropped by the engine and were re-attached" | expected occasionally; the watchdog did its job | nothing; report it if it repeats every few seconds |
| Unreal log says "import guard NOT active" | `on_asset_post_import` could not bind (engine signature changed) | drag-and-drop is unchecked; use `import_with_gatekeeper()` and report the engine version |
| Unreal headless run hangs | editor waiting for a project or a splash | pass a `.uproject`, `-unattended -nopause -nosplash -NullRHI` |
| Panel numbers differ from the gate | panel reads the scene, gate reads the export | that is expected; check the export settings, not the panel |

Next: [10 CI and version control](10-CI-AND-VERSION-CONTROL.md), or back to the
[index](README.md).
