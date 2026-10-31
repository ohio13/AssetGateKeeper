<div align="center">

# AssetGatekeeper

**A budget gate for the moment an asset leaves the DCC — not three days later in a build review.**

[![Python](https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/deps-watchdog%20%C2%B7%20trimesh%20%C2%B7%20Pillow-informational?style=flat-square)](https://pypi.org/)
[![Tests](https://img.shields.io/badge/tests-82%20assertions%2C%20green-brightgreen?style=flat-square)](#testing)
[![License](https://img.shields.io/badge/license-MIT-black?style=flat-square)](#license)
[![PRs](https://img.shields.io/badge/PRs-welcome-ff69b4?style=flat-square)](#contributing)

*One file. Three dependencies. Zero excuses for a 214k-triangle hero asset.*

</div>

---

## The problem: pipeline whack-a-mole

Somebody exports a hero prop. It looks gorgeous in the viewport. It is also **214,000 triangles**, has **seven material slots**, and its albedo is **8192×8192** because the sculpt was three times scale.

You find out three days later. The build is at 40 FPS, the texture pool is over budget, and the artist has already moved on to the next sprint. The fix is a four-hour round trip through the sculpt, the retopo, the bake, and a re-export — and it happens *again* next week, because nothing in the pipeline ever told anyone at the moment of the mistake.

Perforce and Git do not catch this. They store pixels and vertices; they have no opinion about them. Review catches it late, if at all. What's missing is a gate at the **exact moment the file lands on disk** — while the artist still has the file open and the export preset on screen.

That is the entire job of AssetGatekeeper: watch one folder, inspect every export the instant it arrives, and say **`PASSED`** or **`FAILED: <reason>`** on one terminal line.

```
14:22:31 rules   D:\Exports\rules.toml
14:22:31 budget  tris<=50,000  texture<=2048px  pot=on  materials<=3  naming=on
14:22:34 FAILED: 214,301 triangles exceeds the 50,000 budget (+164,301); 7 material slots exceeds the 3 slot budget (draw calls)  hero_prop.gltf 214,301 tris / 7 mat
14:22:41 FAILED: 8192x8192 exceeds the 2048x2048 maximum resolution; name does not end with a standard suffix (_Albedo, _Normal, _Roughness)  hero_prop_diffuse.png 8192x8192
14:23:02 PASSED  crate_A_albedo.png 1024x1024
```

Two problems, one line, told to the artist *while they still have the export dialog open*. That's the difference between a five-second fix and a five-day defect.

---

## What it checks

| Asset | Rule | Default | Why it matters |
| :--- | :--- | :--- | :--- |
| `.obj` `.gltf` `.glb` | Triangles | `50,000` | The classic budget overrun; also catches empty/truncated exports at `0` tris |
| `.obj` `.gltf` `.glb` | Material slots | `3` | A draw-call proxy — 12 materials on one prop is 12 draw calls before it's even instanced |
| `.png` | Max edge | `2,048 px` | VRAM and streaming pressure; 8192² is 64× the memory of 1024² |
| `.png` | Power of two | on | Every mip-chain, every compressed texture format, every "why is this blurry" bug report |
| `.png` | Naming suffix | `_Albedo` `_Normal` `_Roughness` | Assets named `final_final_diffuse_v2.png` are the ones that never get replaced |
| `.gltf` `.glb` | Bone influences per vertex | `4` | Above four weights a vertex is undefined behaviour on mobile and web runtimes |
| `.obj` `.gltf` `.glb` | UV channels | `4` | Extra TEXCOORD sets cost memory in every shader permutation; engines cap at 4–8 |
| `.obj` `.gltf` `.glb` | Degenerate triangles | `0` | Zero-area faces poison Nanite clusters and ray-tracing BVHs, invisibly |

Everything is configurable per folder with a `rules.toml`, and every rule can be overridden or disabled from the command line.

---

## Install

```bash
python -m pip install watchdog trimesh pillow
```

Then grab the single file and run it — there is nothing to install, build, or register.

```bash
curl -O https://raw.githubusercontent.com/ohio13/asset-gatekeeper/main/asset_gatekeeper.py
```

**Requirements:** Python 3.11+ (uses the standard-library `tomllib`). Python 3.9–3.10 works if you `pip install tomli`, or if you provide rules as JSON.

---

## Use it — for artists

Point it at the folder your DCC exports into and leave it running in a terminal tab:

```bash
python asset_gatekeeper.py "D:\Exports\Incoming"
```

```
14:22:31 watching D:\Exports\Incoming - Ctrl+C to stop
14:22:31 budget  tris<=50,000  texture<=2048px  pot=on  materials<=3  naming=on
14:23:02 FAILED: name does not end with a standard suffix (_Albedo, _Normal, _Roughness)  hero_prop_diffuse.png 4096x4096
14:23:19 PASSED  rock_cliff_Albedo.png 2048x2048
14:24:55 PASSED  crate_01.glb 8,411 tris / 2 mat
```

Drop a file in, get a verdict. That's it. Colour-coded on a real terminal, plain text when redirected. Files that are still being written are ignored until they settle, so a Blender export in progress never produces a false failure.

---

## Use it — for build engineers

`--once` sweeps a folder, prints one line per asset, prints a summary, and **exits non-zero if anything failed**. That single design decision is what makes it usable in automation.

```bash
python asset_gatekeeper.py Assets/Exports --once --recursive
```

```
14:31:02 842 asset(s): 837 passed, 5 failed
$ echo $?
1
```

### Pre-commit hook

`.git/hooks/pre-commit`:

```sh
#!/bin/sh
python tools/asset_gatekeeper.py Assets/Exports --once --recursive
status=$?
if [ $status -ne 0 ]; then
    echo ""
    echo "Blocked: the assets listed above exceed the project budget."
    echo "Fix them, or relax the rule in Assets/Exports/rules.toml if the gate is wrong."
fi
exit $status
```

### CI

```yaml
- name: Asset budget gate
  run: |
    python -m pip install watchdog trimesh pillow
    python tools/asset_gatekeeper.py Assets/Exports --once --recursive
```

### Exit codes

| Code | Meaning |
| :--- | :--- |
| `0` | Every inspected asset passed (or folder was empty) |
| `1` | At least one asset failed a budget rule |
| `2` | Setup problem — bad folder, missing dependency, or an invalid `rules.toml` |

---

## Configuration: `rules.toml`

Drop a `rules.toml` in the folder you're watching and it becomes the project's budget. Every key is optional; anything you leave out keeps the shipped default, so a studio file only states what is different.

```toml
# rules.toml — lives next to the assets it governs.
#
# Precedence:  explicit CLI flag  >  this file  >  built-in defaults

[mesh]
max_triangles = 50000        # hard cap per mesh file
max_material_slots = 3       # draw-call proxy; distinct materials per mesh
max_bone_influences = 4      # per-vertex weights (mobile/web GPU limit)
max_uv_channels = 4          # TEXCOORD_n sets
allow_degenerate = false     # zero-area triangles break Nanite and ray tracing
degenerate_epsilon = 1e-08   # area below which a triangle counts as degenerate

[texture]
max_edge = 2048              # longest edge, in pixels
power_of_two = true          # reject 1000x560, accept 512x256
naming = true                # require a standard map suffix
suffixes = ["_Albedo", "_Normal", "_Roughness"]   # case-insensitive, allows _Albedo_02
```

A copy ships as [`asset_gatekeeper.example.toml`](asset_gatekeeper.example.toml).

**Why flags still win.** CI must be able to tighten one number without editing a file that artists own and submit to source control:

```bash
# artists get 50k from rules.toml; the ship build gets 25k, everything else unchanged
python asset_gatekeeper.py Assets/Exports --once --triangles 25000
```

```
14:40:11 rules   Assets\Exports\rules.toml
14:40:11 cli     overriding max_triangles
14:40:11 budget  tris<=25,000  texture<=2048px  pot=on  materials<=3  naming=on
```

Load a shared file explicitly with `--rules path/to/rules.toml`. On Python < 3.11 without `tomli`, the same structure is accepted as JSON:

```json
{ "mesh": { "max_triangles": 50000 }, "texture": { "naming": false } }
```

**Typos fail loudly, not silently.** A misspelled key (`max_triange`) or a bad type stops the run with exit code `2` instead of quietly enforcing the default — a silent typo in a budget file is worse than a crash, because the gate *looks* like it's working.

```
14:41:07 invalid rules file: Assets\Exports\rules.toml: unknown key(s) in [mesh]: max_triange
```

Files saved by Notepad or PowerShell carry a UTF-8 BOM; those load fine.

---

## DCC integrations

Same rules, four front doors — the verdict arrives where the artist actually works.

| Host | Path | Feedback | Can it block? |
| :--- | :--- | :--- | :--- |
| **Blender** | `plugins/blender/addon.py` | Viewport sidebar panel, problems in red | Pre-export check |
| **Maya** | `plugins/maya/gatekeeper_maya.py` | Shelf button, dialog, Script Editor | In-scene, before export |
| **Houdini** | `plugins/houdini/sop_gatekeeper.py` | Node warning badge on every cook | Non-fatal (never breaks a cook) |
| **Unreal** | `plugins/unreal/init_unreal.py` | Message Log + dialog after import | Yes for scripted imports; warn-only for drag-and-drop |

Every plugin reads the budget through the engine's own parser, so a rule can never
mean different things in different applications. Install steps live in
[plugins/README.md](plugins/README.md).

All four speak one contract, and that contract is just the CLI:

```bash
python asset_gatekeeper.py path/to/asset.gltf --json
# {"file": "...", "name": "asset.gltf", "passed": false,
#  "metric": "214,301 tris / 7 mat", "problems": ["214,301 triangles exceeds ..."]}
```

> **Verified vs. untested:** the plugin files are syntax-checked and written with
> capability probing + fallbacks, but **no DCC was installed on the machine this was
> built on**, so none of them has been run inside its host yet. The `--json` contract
> they depend on *is* covered by the test suite.

---

## Architecture

Four layers, one responsibility each. Data flows one way, and nothing that touches the disk is decided by the layer that formats output.

```
   artist export
        │
        ▼
┌────────────────┐   watchdog: created / modified / moved
│    WATCHER     │   filters by extension, drops .tmp / ~$ / .part scratch files
└───────┬────────┘
        │  candidate paths only — never analyses anything on the observer thread
        ▼
┌────────────────┐   queue.Queue, one producer, one worker
│    QUEUE       │   collapses the created+modified pair from a single save
└───────┬────────┘
        ▼
┌────────────────┐   size unchanged for 350 ms and file openable
│   STABILITY    │   (1.5 s grace for zero-byte files, so a slow exporter
│    WINDOW      │    that opens a handle before writing is not misjudged)
└───────┬────────┘
        ▼
┌────────────────┐        ┌──────────────────────┐
│  GATEKEEPER    │◀───────│        BUDGET        │  rules.toml > defaults
│  (pure logic)  │        └──────────────────────┘
└───────┬────────┘   ┌─────────────────────────────────────────┐
        │            │ ANALYZERS: trimesh · Pillow · stdlib json│
        │            └─────────────────────────────────────────┘
        ▼  Verdict(path, problems=(), metric="")
┌────────────────┐
│   REPORTER     │   one line per asset, all violations at once
└────────────────┘   PASSED / FAILED: reason; reason; reason
```

### The watcher never does the work

`watchdog` fires `on_created` the instant a file handle opens — before a single byte is written. Analysing on the observer thread means reading half an OBJ and reporting a nonsense triangle count, or worse, a `PASSED` on an empty file that is about to become a 200k-tri monster. Events therefore only **enqueue a path**; a single worker thread does the waiting and the reading. Because there is exactly one worker, there is no scenario where two threads inspect the same file at once, and a pathological asset can never block the event loop.

### The stability window

The worker polls the file size until it has been unchanged for **350 ms** *and* the file can be opened for reading. Blender, Maya, Substance, and Unreal all write incrementally, so the quiet period is what separates "file exists" from "file is finished". Zero-byte files get a longer 1.5 s grace: they settle fast and are worth failing immediately, but an exporter on a network share may legitimately open first and write later.

Verified against a writer that dribbles bytes for 2.5 seconds — **one** correct verdict, not twenty partial ones.

### One line, every problem

A file usually breaks more than one rule at once. Failing on the first violation means the artist fixes the name, re-exports, and *then* discovers the material count — the exact whack-a-mole this tool exists to delete. So a `Verdict` carries a tuple of reasons:

```python
@dataclass(frozen=True)
class Verdict:
    path: Path
    problems: tuple[str, ...] = ()
    metric: str = ""

    @property
    def passed(self) -> bool:
        return not self.problems

    @property
    def tag(self) -> str:
        return "PASSED" if self.passed else f"FAILED: {'; '.join(self.problems)}"
```

An empty tuple is the only way to pass. One asset, one line, all the bad news.

### Counting from the source, not from the loaded object graph

The naive material-slot implementation counts material objects in the loaded scene — and over-reports badly, because loaders build a **separate material object per primitive**. A glTF whose five primitives share material index `0` looks like five slots. AssetGatekeeper counts from the file itself: unique `material` indices in the glTF JSON (including binary `.glb`, parsed straight from the container's JSON chunk with `struct`), and unique `usemtl` names in OBJ text.

The same instinct governs triangles. `trimesh` is the fast path, but a `gltf`/`glb` also has a **dependency-free rescue path**: triangle counts are derived from accessor counts and walked through the node graph so instanced meshes count per use. If `trimesh` or one of its optional loaders is missing or chokes on a file, the gate still gives a verdict instead of dying.

### Layering for contributors

| Layer | Knows about | Deliberately doesn't know |
| :--- | :--- | :--- |
| Analyzers | file formats | budgets, logging, folders |
| `Gatekeeper` | budgets + `Verdict` | the filesystem's mood, output formatting |
| `Reporter` | colour, counters | how a verdict was reached |
| Watcher | events, queue, stability | what any of the rules mean |

`Gatekeeper.check(path)` is pure logic: no logging, no watching, no side effects. That is why the test suite can assert on rules without ever starting a watcher.

---

## CLI reference

```
python asset_gatekeeper.py [folder] [options]
```

| Flag | Effect |
| :--- | :--- |
| `folder` | Folder to watch (default: current directory) |
| `--once` | Inspect everything present, print a summary, exit `0`/`1` |
| `--recursive` | Include subfolders |
| `--rules PATH` | Use a specific rules file instead of `<folder>/rules.toml` |
| `--triangles N` | Override the triangle budget |
| `--max-texture EDGE` | Override the max texture edge, in pixels |
| `--max-materials N` | Override the material-slot budget |
| `--max-influences N` | Override the per-vertex bone-weight budget |
| `--max-uvs N` | Override the UV-channel budget |
| `--allow-degenerate` | Permit zero-area triangles |
| `--json` | One JSON object per file on stdout (the contract the DCC plugins use) |
| `--allow-npot` | Accept non-power-of-two textures |
| `--no-naming` | Accept texture names without a standard suffix |
| `--no-color` | Disable ANSI colour |
| `-v`, `--verbose` | Debug logging (including why a file was skipped) |

---

## Extending: one new rule in three places

1. Add the field to `Budget` (and its default).
2. Add a `_read_*` call plus the argument in `Budget.from_mapping` so `rules.toml` can set it.
3. Append to `problems` in the relevant `Gatekeeper._check_*` method.

```python
@dataclass(frozen=True)
class Budget:
    ...
    max_vertex_count: int = 65_000   # 1. new field

def _check_mesh(self, path: Path) -> Verdict:
    ...
    if vertices > self.budget.max_vertex_count:   # 3. new check
        problems.append(f"{vertices:,} vertices exceeds the {limit:,} budget")
```

No changes are needed in the watcher, the reporter, or the queue. Rules are data; everything else is plumbing.

---

## Testing

Two layers, because unit tests and DCC reality fail in different ways.

```bash
# 1. rules, parsers, config precedence — 82 assertions, no network, no GPU
python tests/gen_test_assets.py
python tests/test_gatekeeper.py

# 2. real exporter -> real file -> real verdict (headless Blender / Unreal)
python tests/run_headless_integration.py
```

```
asset_gatekeeper.py              the tool (single file)
asset_gatekeeper.example.toml    rules.toml template
README.md  LICENSE  .gitignore
.github/workflows/
  ci.yml                         every push/PR: lint + 82 assertions + integration
  dcc-nightly.yml                nightly Blender export through the real watcher
tests/
  gen_test_assets.py             builds the fixture corpus (dropbox/)
  test_gatekeeper.py             82 assertions over rules, parsers, config
  run_headless_integration.py    drives headless DCC exports through the gate
plugins/
  blender/addon.py               sidebar panel + selection check
  maya/gatekeeper_maya.py        in-scene checks via maya.cmds / OpenMaya
  houdini/sop_gatekeeper.py      Python SOP with a node warning badge
  unreal/init_unreal.py          import hooks; import_gatekeeper.py blocks scripted imports
```

### Unit suite

The suite generates its own fixtures — OBJ + MTL with 1 and 4 materials, an OBJ with faces before any `usemtl`, embedded `.gltf` meshes at 1,200 and 72,500 triangles, hand-assembled binary `.glb` containers, and PNGs covering POT/NPOT, compliant and non-compliant names, a JPEG wearing a `.png` extension, empty and truncated files, and unicode paths (`墙体.obj`, `树皮_Albedo.png`). It asserts the fallback loader against the real trimesh path, the `created`+`modified` dedupe, config precedence, and that broken config fails with the right exit code.

```
FAILURES: none
```

### Headless DCC integration

`tests/run_headless_integration.py` is standalone — no pytest, no fixtures framework — so a build engineer can copy it into a nightly job. It starts the gatekeeper in **watch mode**, asks a real DCC to export into the folder, and asserts the verdict that comes back through the actual `watchdog` path (queue, stability window, dedupe), not a simulated event.

```
[2/4] blender headless export
    | 13:16:11 watching D:\Exports\Incoming - Ctrl+C to stop
    cmd /c ...blender.cmd --background --factory-startup --python blender_export_dense.py -- <watchdir> 180000
    | [blender-mock] wrote dense_blender_sphere.gltf with 180,000 triangles
    | 13:16:13 FAILED: 180,000 triangles exceeds the 50,000 budget (+130,000)  dense_blender_sphere.gltf 180,000 tris / 1 mat

==============================================================================
  PASSED  gatekeeper script            python.exe, dependencies importable
  PASSED  blender headless export      FAILED: 180,000 triangles exceeds the 50,000 budget (+130,000)
          asset: dense_blender_sphere.gltf
          reported: 180,000
  SKIPPED unreal headless export       script written to ...\dcc_snippets\ue_export_dense.py
  PASSED  --once CI sweep              exit 1 (failures detected)
==============================================================================
3 passed, 0 failed, 1 skipped
```

| Step | Proves |
| :--- | :--- |
| `[1/4] environment` | The chosen interpreter can `import watchdog, trimesh, PIL` — the most common cause of a broken nightly |
| `[2/4] blender` | A headless export lands in the watch folder and is **failed** on the real event path |
| `[3/4] unreal` | The editor command line is correct and, with `--run-unreal`, actually executes |
| `[4/4] --once` | The CI path agrees with the watcher: non-zero exit for a dense asset |

**Blender.** The snippet builds a UV sphere, *applies a triangulate modifier* so the exporter cannot hand back quads and under-count, rescales until it is within 3% of the triangle target, then exports `GLTF_SEPARATE` exactly like a manual export. A UV sphere of `segments=400, ring_count=226` is analytically `2 * segments * (ring_count - 1)` = **180,000 triangles**.

```bash
python tests/run_headless_integration.py \
    --blender "C:\Program Files\Blender Foundation\Blender 4.2\blender.exe" --keep
```

**Unreal.** Needs `--unreal-project` and the "Python Editor Script Plugin" plus "glTF Exporter" enabled in that project. Without them the harness prints the command it *would* run:

```bat
"C:\Program Files\Epic Games\UE_5.4\Engine\Binaries\Win64\UnrealEditor-Cmd.exe" "C:\Game\MyGame.uproject" ^
  -run=pythonscript -script="ue_export_dense.py" -unattended -nosplash -nopause -nullrhi
```

```bash
python tests/run_headless_integration.py --unreal-project "C:\Game\MyGame.uproject" --run-unreal
```

**No DCC on the build agent?** `--synthetic` writes the same 180,000-triangle indexed sphere in pure Python, so CI can still exercise the whole pipeline:

```bash
python tests/run_headless_integration.py --synthetic
```

Both DCC snippets are written to `<workdir>/dcc_snippets/` every run, ready to paste into your own automation.

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request: `pyflakes` over the
engine and tests, a syntax check of all four plugins (`compileall`, because
`bpy.props` annotations make pyflakes emit bogus F722 forward-annotation errors),
the 82-assertion suite, and the integration harness in `--synthetic` mode, on
Ubuntu and Windows × Python 3.11 and 3.12.

`.github/workflows/dcc-nightly.yml` downloads a pinned Blender (cached between
runs) and drives `run_headless_integration.py --blender ... --keep` against it — the
only job that proves a *real* exporter produces a file a real gate rejects. It runs
nightly and on demand; Unreal, Maya and Houdini stay manual because their licences
can't live in a runner.

> **Known gaps, stated plainly:** the harness itself is verified in `--synthetic` mode and against a mocked DCC binary (which exercises the Blender argv contract, exit codes and verdict assertions); the Blender and Unreal command lines have not yet been executed against real installs in this repository. If you run them, a PR with the output — or with the UE version you tested — is very welcome.

---

## Design limits (read before you file the bug)

- **Triangles are topology, not render cost.** Instanced scenes are counted once per unique geometry. UV *channel* count, per-vertex bone influences and degenerate faces are measured (see `[mesh]` in `rules.toml`); draw calls, vertex counts and UV seams are not — yet.
- **Material slots are per file, not per object.** A merged export with three materials across forty objects reads as three slots. Correct for draw calls; not a per-object budget.
- **Stability is a heuristic.** A file that pauses longer than the quiet window mid-write can still be inspected early. Prefer atomic writes (write to `.tmp`, then rename) in your exporter and this problem disappears entirely.
- **Textures embedded inside a glTF** aren't inspected — only standalone `.png` files.
- **Not a substitute for an engine-side audit.** This gate stops mistakes at source; it doesn't replace profiling.

---

## Contributing

Issues and PRs are genuinely welcome — especially from technical artists, because the hard part of this tool is never the Python.

**The most valuable contribution is not code, it's a rule.** If your studio has a budget that saves you pain — texture memory, bone counts, LOD chain completeness, naming for engine import — open an issue describing the rule and how it fails in practice. Rules are data and they live in one dataclass; a good rule costs twenty lines.

Housekeeping:

- Keep `Gatekeeper.check()` pure. If your rule needs the filesystem, put the I/O in an analyzer and return a plain value.
- Add a case to `tests/test_gatekeeper.py` that *fails* before your fix. Generate the fixture if it doesn't exist.
- One log line per asset, all violations at once, `FAILED: <reason>` — the output format is the contract.
- New dependencies need a very good reason. The whole point is that this drops into any pipeline in ten seconds.

---

## Dedication

For every technical artist who has spent a Friday afternoon re-baking a texture that was 4× too large in the first place, and for every tools engineer who has written the same validation script at four different studios.

Good pipelines are invisible when they work. The best thing this tool can do is print `PASSED` and become boring — because boring means the artist never had to think about it, the build engineer never had to file the bug, and nobody spent the afternoon in whack-a-mole.

*Built with respect for the people on both sides of the export button.*

---

## License

MIT — see [LICENSE](LICENSE). Use it in your studio, fork it, ship it, rename the suffixes to your convention.
