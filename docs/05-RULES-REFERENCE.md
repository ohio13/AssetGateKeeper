# 05 - Rules reference: every key, in order of precedence

`rules.toml` is the studio's technical contract. This page is the complete
reference: what every key does, what wins when two of them disagree, and where
the sharp edges are.

`templates/rules.toml` is the short starting point; `asset_gatekeeper.example.toml`
is the annotated long form. This page is the reasoning behind both.

## Precedence

Highest wins:

```
1. command line flags        --triangles 900 --textures 512 ...
2. remote rules              --remote-rules URL (a server's published policy)
3. platform profile          [platforms.<name>] selected by platform = "..." or --platform
4. preset                    --preset ps2   (budget numbers only)
5. rules.toml [mesh]/[texture]/[healer]
6. built-in defaults         50,000 tris / 2,048 px / 3 slots / PBR off
```

Two consequences worth knowing before you debug anything:

* A **preset stores numbers, never a platform name.** If presets stored
  `platform = "n64"`, the project's own `[platforms.n64]` would either override a
  director's edit silently or refuse the file outright when the table was
  missing. Numbers cannot do either.
* A **flag beats everything**, including a platform profile. That is deliberate:
  `--triangles 900` must mean 900, or it is a lie you will chase for an hour.

## [mesh]

| Key | Type | Default | Meaning |
|---|---|---|---|
| `max_triangles` | int | 50000 | hard cap per mesh file |
| `max_material_slots` | int | 3 | distinct materials - a draw-call proxy |
| `check_references` | bool | true | fail on missing `.bin`, `.mtl`, texture paths |
| `allow_outside_references` | bool | false | fail on a reference that resolves outside the watch folder; true downgrades it to a warning (shared texture library) |
| `max_bone_influences` | int | 4 | skin weights per vertex; 4 is the mobile limit |
| `allow_degenerate` | bool | false | flat or collapsed faces: Nanite and RT compute garbage from them |
| `degenerate_ratio` | float | 1e-06 | a triangle is flat when its area is at most this x its longest edge squared; judged by shape, so real-scale small props are not flagged |
| `name_prefixes` | list | [] | optional: mesh names must start with one of these, e.g. `["SM_", "SK_"]` (case-insensitive). The gate checks the file name; Maya, 3ds Max, Blender and Unreal check object names with the same rule |
| `degenerate_epsilon` | float | 0.0 | optional extra: also flag any triangle at or below this area (world units squared); 0 = off |
| `audit_transforms` | bool | false | report non-zero object transforms (review, not failure) |
| `require_indexed` | bool | false | report non-indexed meshes |

`max_triangles` is per **file**, not per scene. A `.glb` containing six crates is
one asset and gets one budget. If you need per-object budgets, export per object.

## [texture]

| Key | Type | Default | Meaning |
|---|---|---|---|
| `max_edge` | int | 2048 | longest edge in pixels |
| `power_of_two` | bool | true | require POT sizes (mobile GPUs and old consoles want this) |
| `naming` | bool | true | require one of `suffixes` |
| `suffixes` | list | `["_Albedo","_Normal","_Roughness","_BaseColor","_ORM"]` | accepted texture suffixes |
| `pbr` | bool | false | albedo must look like reflectance, not a painting |
| `pbr_floor` | float | 0.02 | mean luminance below this is "black" |
| `pbr_ceiling` | float | 0.94 | above this is "white" |
| `pbr_suffixes` | list | `["_Albedo","_Diffuse","_BaseColor"]` | which suffixes are tested for PBR sanity |

The suffix list is a studio convention, not a universal truth. Change the list
rather than switching `naming` off: a list documents the convention, a boolean
just hides the check.

## [healer]

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | false | folder-wide healing; `--auto-heal` turns it on for one run. `--heal <file>` heals one file whatever this says |
| `textures` | bool | true | `.png` and `.tga`: add a missing name suffix (and repoint what names it), make edges a power of two. A texture named `_Normal` (or `_nrm`, `_n`) is renormalised after a resize, so every pixel stays a unit vector |
| `jpg` | bool | false | also heal `.jpg`/`.jpeg`. Off by default: the healed file is re-saved (quality 95, no chroma subsampling), which loses a little detail every time |
| `meshes` | bool | true | `.obj`: delete zero-area faces |
| `texture_mode` | string | `"resize"` | how a non-power-of-two `.png` reaches the next power of two: `resize` resamples it (UVs unchanged); `pad` repeats the edge (every pixel kept, but UVs shift - a UV of 0.5 lands 2.3 % off on 1000 -> 1024) |

Healing never changes resolution, triangle counts or materials: a texture over the
edge budget, or a mesh over its triangles, keeps failing.

Healing is **adopt-only-if-clean**: the repaired file replaces the original only
if it verifies as passing under the same budget. Otherwise the original is
untouched and the failure stands. Every adopted heal keeps the original in
`.agk-backups/`; `asset-gatekeeper <file> --undo-heal` restores it, and
`--heal --dry-run` prints what would happen without writing anything.

## [platforms.<name>]

A project-defined profile: the budgets a *target* can afford. Selected by
`platform = "ps2"` in the file or `--platform ps2` on the command line.

```toml
platform = "ps2"

[platforms.ps2]
max_triangles = 5000
max_material_slots = 3
max_texture_edge = 256
max_bone_influences = 4
max_uv_channels = 1

[platforms.ps2.mesh]
allow_degenerate = false
```

Allowed keys: `max_triangles`, `max_material_slots`, `max_texture_edge`,
`max_bone_influences`, `max_uv_channels`, plus nested `[mesh]`/`[texture]`.

Two rules:

* **A profile can only tighten.** There is no key that disables a check, so a
  "low quality" profile cannot become a bypass. `PLATFORM_SECTION_KEYS` enforces
  this at parse time.
* **A profile does not survive a missing table.** `--platform foo` with no
  `[platforms.foo]` is an error with the known names listed - it is never
  silently ignored. (Ignoring it was a real bug in this project; the test that
  caught it is in `tests/test_gatekeeper.py`.)

## [tags.<name>]

Per-category budgets. A tag is a **folder name or a filename prefix** - never a
pattern.

```toml
[tags.character]
max_triangles = 40000
max_material_slots = 4

[tags.boss]
max_triangles = 120000
max_material_slots = 8
```

### How a path resolves to a tag

```
assets/environments/rock/cliff_01.obj
       ^^^^^^^^^^^^  <- folder tags (nearest ancestor first, 4 levels)
                        ^^^^^^^^  <- filename prefix
```

Order of precedence, most specific first:

1. **filename prefix**: `boss_dragon.obj` -> `boss` if a `[tags.boss]` exists
2. **full filename** (without extension): `cliff_01` -> `cliff 01`
3. **nearest ancestor folder**: `rock` -> `rock`
4. walking up to four levels
5. no tag -> the base `[mesh]`/`[texture]` budget applies

Names are normalised: `environment_rock`, `environment-rock` and
`Environment Rock` are the same tag. Comparison is case-insensitive because
Windows and Perforce both are.

### Typo protection

The gate compares an unknown tag against the known ones and reports near misses:

```
tag 'charcter' is not a known tag; closest match is 'character'
```

* `tag_mode = "suggest"` (default): a **warning**, the asset is still judged by
  whatever budget it resolved to.
* `tag_mode = "strict"`: an **error**, so a misspelled folder cannot slip through
  review with the wrong budget.
* The similarity threshold is 0.75 (difflib ratio). It is not a CLI flag: the
  behaviour must be predictable across every machine, and a per-run threshold
  makes two people disagree about whether a folder is a typo.

This is why tags are folders and not `[tags.*]` glob patterns in the file: a
pattern-based system cannot tell you the folder is misspelled, it just fails to
match and silently applies the default.

## Memory and texel density

Budgets for what an asset costs on the target, not just how big the file is.

```toml
[texture]
max_mb = 4                 # per texture, compressed, with mips (0 = off)
format = "bc7"             # colour textures: bc1 bc3 bc4 bc5 bc6h bc7 astc4x4 astc5x5
normal_format = "bc5"      #   astc6x6 astc8x8 etc2 etc2a rgba8 rgba16f

[mesh]
max_mb = 2                 # per mesh: vertex + index buffers (0 = off)
lightmap_uv = true         # the engine adds a lightmap UV set (Unreal: yes)
depth_index_buffer = true  # and a second index buffer for depth passes (Unreal: yes)
texel_density = [400, 1200]  # pixels per metre of surface
texel_texture = 1024       # texture size assumed when the mesh's material names none

[[totals]]                 # everything under a path, added up (--once runs)
path = "Props/*"
max_mb = 300

[platforms.switch.texture]
max_mb = 2
format = "astc8x8"
```

* **Texture memory** = width x height x bytes per pixel of the format (BC1 0.5,
  BC7 and BC5 1, ASTC 6x6 0.44, RGBA8 4) x 4/3 for mips. A 2048 texture is about
  5.3 MB as BC7 and 2.7 MB as BC1. Textures named `_Normal`, `_nrm` or `_n` use
  `normal_format`. Every texture reports its estimate (`memory_kb` in `--json`
  and telemetry); the budget only applies when `max_mb` is set.
* **Mesh memory** = vertices x vertex size + indices. A vertex is 12 bytes of
  position, 8 of packed tangent frame, 4 per UV set (half floats), 4 for vertex
  colours and 8 for skinning; indices are 16-bit up to 65,535 vertices, else
  32-bit. By default it also counts what Unreal adds to every static mesh on
  import: a generated lightmap UV set (`lightmap_uv`) and a second index buffer
  for depth-only passes (`depth_index_buffer`). Measured on UE 5.3.2 (Nov 2026,
  five meshes from 1,000 to 48,000 triangles): within 7% of Size Map with them,
  37-53% under without. An engine that adds neither (most Unity and Godot
  projects) sets both to `false`. glTF and OBJ only for now; an FBX gets a note
  instead.
* **Texel density** = sqrt(UV area x texture pixels / surface area), weighted by
  area over the asset's meshes, in pixels per metre (glTF is in metres; OBJ is
  read as metres). A 1 m plane with 0-1 UVs and a 1024 texture is 1024 px/m.
  The texture is the one the material names when it can be read, else
  `texel_texture` (and the failure says it assumed one). A mesh without UVs gets
  a note, not a failure.
* **`[[totals]]`** add up the estimate of every asset under `path` (a glob
  relative to the folder holding `rules.toml`, `*` crossing folders). A total
  over budget fails the run - even when every file passes - and is a failing
  case named `[total] <path>` in `--report junit`. Each total's use is printed.
  An asset under the path with no memory estimate (an FBX mesh, for now) fails
  the total, naming it, instead of counting as 0 MB: narrow the path or convert it.
* **At submit time** the Perforce trigger matches `[[exceptions]]` paths against
  the depot path below the folder of the `rules.toml` it used, as a local run
  does (`--exception-path`).
* These are **estimates** in the engine's terms; check a few against the
  engine's own numbers (Unreal: Size Map, `memreport`) before tightening a
  budget on them. `max_texture_mb` and `max_mesh_mb` can be relaxed per asset
  with [[exceptions]].

## [[exceptions]]

A named, owned, dated waiver: "the boss may use 80,000 triangles until alpha",
without loosening the rule for everyone.

```toml
[[exceptions]]
path = "Characters/Boss/*"      # glob under the folder holding rules.toml; * crosses folders
rule = "max_triangles"          # the setting it relaxes
allow = 80000                   # the value it allows instead
reason = "hero boss, art director sign-off"
owner = "lena"
expires = 2026-12-01            # the last day it applies
```

| Key | Meaning |
|---|---|
| `path` | case-insensitive glob relative to the folder holding `rules.toml` (the watch folder for remote rules); a bare file name (`boss.fbx`) matches that file in any folder |
| `rule` | one of `max_triangles`, `max_texture_edge`, `max_material_slots`, `max_bone_influences`, `max_uv_channels`, `require_power_of_two`, `enforce_texture_naming`, `allow_degenerate`, `check_references`, `pbr_check` |
| `allow` | a whole number for the counts, `true`/`false` for the switches |
| `reason`, `owner`, `expires` | all required: a waiver nobody owns, with no end date, becomes the real budget |

While it applies, the asset passes with the note *exception: max_triangles =
80,000 for Characters/Boss/\* until 2026-12-01 (lena: ...)*, and in its last 14
days the note says how many are left. The day after `expires` it no longer
applies: if the asset still fails, the failure says the exception expired; if it
now fits the normal budget, nothing is said. At start-up the gate prints how
many exceptions apply, how many expired and how many end within 14 days. An
incomplete or misspelt exception stops the run (exit 2) and names the missing
key. The AssetHealth budget planner publishes exceptions with the project's
budget, so `--remote-rules` clients get them too.

## Full example

```toml
platform = "pc"

[mesh]
max_triangles = 45000
max_material_slots = 3
check_references = true
max_bone_influences = 4
allow_degenerate = false

[texture]
max_edge = 2048
power_of_two = true
naming = true
suffixes = ["_Albedo", "_Normal", "_Roughness", "_AO"]
pbr = true

[healer]
enabled = false
texture_mode = "resize"

[platforms.mobile]
max_triangles = 15000
max_material_slots = 2
max_texture_edge = 1024

[tags.character]
max_triangles = 60000
max_material_slots = 6

[tags.environment]
max_triangles = 30000
max_material_slots = 2

[tags.boss]
max_triangles = 150000
max_material_slots = 8
```

## Where the file is looked for

1. `--rules <path>` if given (missing file is an error);
2. otherwise `rules.toml` next to the folder you are gating.

A rules file that is present but broken is always an error - unknown keys are
refused by name, because a typo in a budget key that silently falls back to a
default is how a project ships 200k-triangle props.

## Checking your rules without running the gate

```
asset-gatekeeper --rules rules.toml --init --yes      # write a commented starter file
asset-gatekeeper <folder> --once --json              # the verdict as JSON
```

There is no --describe flag. To answer "why is this 40k and not 15k", the gate's
startup banner prints the budget it resolved and the source it came from
(`rules <path>` or `rules none (defaults)`), which is the same information.

Next: [06 CLI reference](06-CLI-REFERENCE.md).
