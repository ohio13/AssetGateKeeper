# AssetGatekeeper

**Catch an over-budget or broken 3D asset the moment it leaves Maya, Max or Blender, not three days later in a build review.**

> **Alpha (0.1.0-alpha.1).** It works, it is tested, and it will still have rough edges. Please [report anything odd](https://github.com/ohio13/AssetGateKeeper/issues/new/choose), with the line the tool printed.

AssetGatekeeper checks game assets against a budget you set: triangles, materials, textures, bone weights, UV sets, broken texture links and damaged geometry. It runs on a folder, in your editor, in Unreal, on every pull request, or as a Perforce trigger, and gives one line per asset with every problem and the number that broke it:

```
14:22:34 FAILED: 214,301 triangles exceeds the 50,000 budget (+164,301); 7 material slots exceeds the 3 slot budget (draw calls)  hero_prop.gltf 214,301 tris / 7 mat
14:22:41 FAILED: embedded texture 'image 1 of 5' 4096x4096 exceeds the 2048x2048 maximum resolution  MosquitoInAmber.glb 14,349 tris / 3 mat / 3 uv
14:22:41 PASSED  DamagedHelmet.glb 15,452 tris / 1 mat / 1 uv
14:22:41 3 asset(s): 1 passed, 2 failed
```

It reads FBX (binary and ASCII), OBJ, glTF and GLB itself, with no Autodesk SDK. Before this release its counts were compared with an independent reader on 460 public models: they agreed on every file both could read except three test files built from triangle fans, which are now counted correctly too.

## Quick start (5 minutes)

**On Windows, no command line:** [download the ZIP](https://github.com/ohio13/AssetGateKeeper/archive/refs/heads/main.zip), unzip it, double-click **`Install_Extras.bat`**, then **`Start AssetGatekeeper.bat`**. The page that opens offers *Just me on this PC* (no account) and asks three things: your project folder, your engine and what it has to run on. Then it checks the project. `Stop.bat` stops it. You need [Python](https://www.python.org/downloads/) 3.10 or newer (tick "Add python.exe to PATH").

**Anywhere, from a terminal** (Python 3.9 or newer):

```bash
pip install "asset-gatekeeper[full] @ git+https://github.com/ohio13/AssetGateKeeper"

asset-gatekeeper path/to/Exports --init                # write a rules.toml with your budget
asset-gatekeeper path/to/Exports --once --recursive    # check everything once
asset-gatekeeper path/to/Exports --recursive           # keep watching; a pop-up on each failure
```

`--once` exits with `1` when anything failed, so the same line works in a build script. `asset-gatekeeper --doctor` checks the machine if something doesn't start.

## What it checks

| Files | Rule | Default |
| :--- | :--- | :--- |
| `.fbx` `.obj` `.gltf` `.glb` | triangles (every placed copy, as Unreal imports them) | 50,000 |
| | material slots (draw calls) | 3 |
| | bone weights per vertex | 4 |
| | UV sets | 4 |
| | flat or collapsed triangles | blocked (or `warn` / `allow`) |
| | texture links that point at nothing | none allowed |
| | textures packed inside the file | same rules as textures |
| | mesh name prefix such as `SM_` | off |
| `.png` `.tga` `.jpg` `.tif` `.webp` `.dds` `.exr` `.ktx2` | longest edge | 2,048 px |
| | power of two | on |
| | name ends in a map name (`_BaseColor`, `_Normal`, `_ORM`, …) | on, 28 names |

Every rule lives in a `rules.toml` next to the assets and can be changed per folder, per platform or per asset type. See the [rules reference](https://github.com/ohio13/AssetGateKeeper/blob/main/docs/05-RULES-REFERENCE.md).

Naming is yours to define: pick one or more ready-made conventions (`unreal`, `unity`, `gltf`, `substance`, `godot`), add your own prefixes, endings or a pattern, or run `asset-gatekeeper <folder> --suggest-naming` to have the tool learn a `[naming]` block from the files you already have.

Sculpts are covered too: an `.obj` whose colour is ZBrush Polypaint is flagged
(most programs show it grey), and `asset-gatekeeper <file.obj> --bake-polypaint`
turns it into a game asset: a reduced `.glb` with UVs, a baked BaseColor
texture and a normal map, sized to your budget. `--bake-preset n64` makes a
Nintendo 64 style one instead (about 1,200 triangles, 64 px, 16 colours).

## Where it runs

| Where | How |
| :--- | :--- |
| A folder | `asset-gatekeeper <folder> --once` or a live watch |
| Maya, 3ds Max, Blender, Houdini | a button that checks the selection before export ([DCC plug-ins](https://github.com/ohio13/AssetGateKeeper/blob/main/docs/09-DCC-PLUGINS.md)) |
| Unreal Engine | checks on import and in Data Validation ([DCC plug-ins](https://github.com/ohio13/AssetGateKeeper/blob/main/docs/09-DCC-PLUGINS.md#unreal-engine-5)) |
| GitHub | a ready-made workflow marks each failing asset on the pull request ([CI](https://github.com/ohio13/AssetGateKeeper/blob/main/docs/10-CI-AND-VERSION-CONTROL.md)) |
| Git, Perforce | a pre-commit hook and a submit trigger ([CI and version control](https://github.com/ohio13/AssetGateKeeper/blob/main/docs/10-CI-AND-VERSION-CONTROL.md)) |

Tested with Maya 2027.2, 3ds Max 2027.2, Unreal Engine 5.3, Blender 4 and Perforce, on Windows 11 and Linux.

## Experimental parts

These ship in the repository but are newer and less tested than the gate itself. Use them, and expect changes:

- **AssetHealth**, a team dashboard of pass rates and repeat offenders, with Slack, Discord and Teams alerts ([docs](https://github.com/ohio13/AssetGateKeeper/blob/main/docs/08-ASSETHEALTH.md)).
- **Art Workstation**, a browser review screen with a 3D viewer, versions and notes ([docs](https://github.com/ohio13/AssetGateKeeper/blob/main/docs/07-ART-WORKSTATION.md)).
- **Auto-healing**, which can resize textures and remove broken triangles from OBJ files; it is off unless you switch it on.
- **PostgreSQL** storage and the enterprise tools ([docs](https://github.com/ohio13/AssetGateKeeper/blob/main/docs/ENTERPRISE.md)).

## Known limitations

- glTF compressed with meshopt is counted exactly, but its shape checks are skipped. Draco needs DracoPy, which installs with the tool.
- Mesh memory and texel density are measured for glTF and OBJ, not FBX yet.
- Budgets are calibrated against Unreal Engine 5.3; other engines may count slightly differently.

## Documentation

- [Quickstart](https://github.com/ohio13/AssetGateKeeper/blob/main/QUICKSTART.md): setting up the web pages without a terminal.
- [Full documentation](https://github.com/ohio13/AssetGateKeeper/blob/main/docs/README.md): solo developer, studio rollout, rules, every CLI flag, CI, troubleshooting.
- [Reference](https://github.com/ohio13/AssetGateKeeper/blob/main/docs/REFERENCE.md): the long single-page manual.
- [Release notes](https://github.com/ohio13/AssetGateKeeper/blob/main/RELEASE_NOTES.md).

## Contributing

Bug reports, sample files that break it, and pull requests are all welcome. See [CONTRIBUTING.md](https://github.com/ohio13/AssetGateKeeper/blob/main/CONTRIBUTING.md). Security problems: please follow [SECURITY.md](https://github.com/ohio13/AssetGateKeeper/blob/main/SECURITY.md) instead of opening a public issue.

## Licence

MIT. See [LICENSE](https://github.com/ohio13/AssetGateKeeper/blob/main/LICENSE).
