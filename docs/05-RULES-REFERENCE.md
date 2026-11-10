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
| `degenerate` | string | "block" | what a flat or collapsed triangle does: `"block"` fails the asset, `"warn"` passes it with a note, `"allow"` skips the check. Unreal removes such triangles on import, so `"warn"` suits teams that would rather not be blocked by them |
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

## [naming]

Naming conventions differ between studios and tools, so you can describe yours
instead of adopting ours. Without a `[naming]` table, textures must end in one of
the `[texture] suffixes` (28 common map names by default) and meshes follow
`[mesh] name_prefixes` (off by default).

```toml
[naming]
preset = ["unreal", "substance"]     # ready-made conventions; a name passes if it follows ANY
texture_prefixes = ["TX_"]           # plus your own convention, as one more option
texture_suffixes = ["_Diff", "_Nrm"]
texture_pattern = "^TX_[A-Z][A-Za-z0-9]+_(Diff|Nrm)$"   # a regular expression for the whole name
mesh_prefixes = ["GEO_"]
mesh_pattern = "^GEO_[a-z0-9_]+$"
```

| Preset | Textures | Meshes |
|---|---|---|
| `default` | end in a common map name (`_Albedo`, `_BaseColor`, `_Normal`, `_ORM`, ... 28 in all) | any name |
| `unreal` | start `T_`, end `_D` `_N` `_R` `_M` `_A` `_O` `_E` `_ORM` `_MRA` ... | start `SM_` or `SK_` |
| `unity` | end `_Albedo` `_MetallicSmoothness` `_MaskMap` `_Normal` `_Occlusion` ... | any name |
| `gltf` | end `_baseColor` `_metallicRoughness` `_normal` `_occlusion` `_emissive` ... | any name |
| `substance` | Substance 3D Painter's export names (`_BaseColor`, `_OcclusionRoughnessMetallic`, ...) | any name |
| `godot` | end `_albedo` `_normal` `_roughness` `_metallic` `_ao` `_orm` ... | any name |

Matching ignores case and the separators inside a name (`_BaseColor` also
accepts `_base_color`). A `[texture] suffixes` or `[mesh] name_prefixes` list in
the same file counts as one more convention. `[texture] naming = false` still
switches texture naming off.

**Let the tool write it.** `asset-gatekeeper <folder> --suggest-naming` reads
the names already in a folder, scores each preset against them, adds the other
map endings and mesh prefixes you use often, and prints a `[naming]` block with
how many of your files it would pass. Paste it into `rules.toml` and edit it.
Nothing is written for you.

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
`max_bone_influences`, `max_uv_channels`, plus nested `[mesh]`/`[texture]`
and `[skeleton]` (`max_bones`, `max_bones_per_section`, `max_depth`; see
[\[skeleton\]](#skeleton)). `[tags.<name>]` takes the same nested tables.

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
  projects) sets both to `false`. For an FBX the vertex count is what an engine
  builds from it: polygon corners that share a position, normal and UV within
  one material are one vertex, and a UV seam or a hard edge splits it (a
  flat-shaded cube is 24 vertices, not 8).
* **Texel density** = sqrt(UV area x texture pixels / surface area), weighted by
  area over the asset's meshes, in pixels per metre (glTF is in metres; OBJ is
  read as metres; an FBX is placed with its own transforms and converted from
  its unit scale, so a centimetre file and a metre file of the same model agree). A 1 m plane with 0-1 UVs and a 1024 texture is 1024 px/m.
  The texture is the one the material names when it can be read (for an FBX,
  the base-colour map when it sits beside the file), else
  `texel_texture` (and the failure says it assumed one). A mesh without UVs gets
  a note, not a failure.
* **`[[totals]]`** add up the estimate of every asset under `path` (a glob
  relative to the folder holding `rules.toml`, `*` crossing folders). A total
  over budget fails the run - even when every file passes - and is a failing
  case named `[total] <path>` in `--report junit`. Each total's use is printed.
  An asset under the path with no memory estimate (a mesh that could not be read
  or measured; its warning says why) fails the total, naming it, instead of
  counting as 0 MB.
* **At submit time** the Perforce trigger matches `[[exceptions]]` paths against
  the depot path below the folder of the `rules.toml` it used, as a local run
  does (`--exception-path`).
* These are **estimates** in the engine's terms; check a few against the
  engine's own numbers (Unreal: Size Map, `memreport`) before tightening a
  budget on them. `max_texture_mb` and `max_mesh_mb` can be relaxed per asset
  with [[exceptions]].

## [skeleton]

Budgets for the skeleton a skinned mesh uses. Off by default: without a
`[skeleton]` table no skeleton is read. A mesh with no skin is never checked.

```toml
[skeleton]
max_bones = 256              # bones in the skeleton (0 = off)
max_bones_per_section = 75   # bones weighting one material section (0 = off)
single_root = true           # exactly one root bone
max_depth = 0                # longest root-to-tip chain, the root counting 1 (0 = off)

[platforms.mobile.skeleton]  # a target can be stricter
max_bones_per_section = 75
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `max_bones` | int | 0 (off) | bones in the skeleton(s) the asset's skinned meshes use |
| `max_bones_per_section` | int | 0 (off) | bones with weight on the triangles of one material |
| `single_root` | bool | false | the skeleton must have exactly one root bone |
| `max_depth` | int | 0 (off) | bones in the longest chain from a root to a tip |

What counts as a bone:

* **glTF**: the joints (`skins[].joints`) of every skin a mesh node uses. A
  root is a joint whose parent node is not a joint.
* **FBX**: every `LimbNode` joint, and every node a skin cluster binds
  vertices to, connected to the bound ones - up to the root and down through
  unweighted bones (IK targets, twist helpers, tip bones), which are still part
  of the skeleton the engine imports. A plain group above the skeleton (the
  armature object Blender writes, a Maya group) is not a bone, so Blender bones
  without a parent bone are each a root: `skin4.fbx` in the tests has four.

Why each rule exists:

* **`max_bones_per_section`**. A section is one material slot. The engine
  skins a section in one draw call with at most a fixed number of bones; a
  section weighted to more is split into chunks, each an extra draw call.
  Unreal Engine's default limit is 65,536 bones per section (256 without 16-bit
  bone indices), and mobile platforms are capped at **75 bones per section**
  (Epic's [Skeletal Mesh Rendering Paths](https://dev.epicgames.com/documentation/en-us/unreal-engine/skeletal-mesh-rendering-paths-in-unreal-engine)
  page; Project Settings > Engine > Rendering > Skinning). A bone counts for a
  section when its weight on a vertex of that section's triangles is above
  0.0001. Meshes that share a material are one section, as Unreal imports them
  into one skeletal mesh. The failure names the section: give some of its faces
  another material, or weight it to fewer bones.
* **`single_root`**. Unreal's FBX importer refuses a skeleton with more than
  one root ("Multiple roots are found in the bone hierarchy. We only support
  single root bone"). Unreal can also take a plain group node directly above
  the bones as the root bone (it skips Blender's object named `Armature`), so
  such a file may still import; the gate does not guess at that and asks for
  one root bone, which imports everywhere.
* **`max_bones`** and **`max_depth`** are budgets for animation cost (every bone
  is evaluated every frame) and for targets with a hard skeleton limit. A hero
  character can get more with an [[exceptions]] entry (`rule = "max_bones"`).

`--json` and telemetry carry `bones`, `root_bones`, `bone_depth` and
`max_bones_per_section` for a skinned mesh whenever a `[skeleton]` rule is on.
An OBJ has no skin. A meshopt- or Draco-compressed glTF whose weights cannot be
decoded gets a warning that the per-section count was skipped.

## [lightmap]

An authored lightmap UV set on static (non-skinned) meshes. Off by default:
without a `[lightmap]` table no lightmap UV is read. Not the same as `[mesh]
lightmap_uv`, which only adds the set Unreal *generates* to the memory estimate.

```toml
[lightmap]
require = true               # every static mesh has the set
uv_channel = 1               # which set: 0-7, 1 = the second (Unreal's convention)
inside_unit_square = true    # every lightmap UV within 0-1
max_overlap = 0              # percent of lightmap texels covered twice or more
resolution = 256             # the grid the overlap is measured on
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `require` | bool | false | a static mesh without UV set `uv_channel` fails |
| `uv_channel` | int | 1 | the lightmap set: FBX's UV layer of that index, glTF `TEXCOORD_<n>` |
| `inside_unit_square` | bool | true once `require` or `max_overlap` is set, else false | every lightmap UV within 0-1 (0.0001 tolerance) |
| `max_overlap` | float | not checked | 0-100: the largest share of covered texels that may be covered more than once; 0 = none |
| `resolution` | int | 256 | 16-4096 texels per side of the overlap grid |

Why: a lightmap stores the baked light of every surface at its own place in
the texture. Two triangles at the same place share one value, so one side's
shadow shows on the other ("bleeding"), and a lightmap does not tile, so a UV
outside 0-1 lands on another surface's texels. Unreal's Generate Lightmap UVs
avoids both, which is why `require` is off unless the studio authors its own.

How the overlap is measured: each lightmap triangle is rasterised into a
`resolution` x `resolution` grid; a texel belongs to a triangle when its centre
is inside it, with the top-left rule GPUs use for a centre exactly on an edge.
So the triangles of one island, and islands that only touch along an edge, never
overlap; mirrored islands count like any other. The figure is texels covered
twice or more, as a percentage of texels covered at all, for the worst mesh of
the asset (each mesh has its own lightmap). A mesh whose lightmap covers no
texel at all (collapsed to a point) fails when `max_overlap` is set. Islands
smaller than a texel are not seen: raise `resolution` to find them. Padding
between islands (bilinear filtering bleeds across one or two texels) is not
checked.

Cost: about two sampled texel centres per covered texel, whatever the
triangle count. A
100,000-triangle glTF checks in about 0.15 s at 256 and 0.4 s at 1024 (one core).
A layout whose triangles each cover the square many times over would need far
more, so above 8.4 million samples the grid is halved until it fits, and a
warning names the grid used. An FBX is parsed once more for these checks.

Formats: FBX (the `LayerElementUV` with that index, `ByPolygonVertex` or
`ByControlPoint`), glTF (`TEXCOORD_<uv_channel>`, also quantised; a
Draco-compressed primitive is skipped with a warning). An OBJ holds one UV set,
so `require` with `uv_channel = 1` fails it and says so; with `uv_channel = 0`
its set is checked like any other. `--json` carries `lightmap_missing`,
`lightmap_uvs_outside`, `lightmap_overlap_texels` and `lightmap_texels`.

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
| `rule` | one of `max_triangles`, `max_texture_edge`, `max_material_slots`, `max_bone_influences`, `max_uv_channels`, `max_bones`, `max_bones_per_section`, `require_power_of_two`, `enforce_texture_naming`, `allow_degenerate`, `check_references`, `pbr_check`, `max_texture_mb`, `max_mesh_mb` |
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
