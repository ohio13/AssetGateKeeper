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

## [lod]

LOD levels: does the asset ship its lower levels, and does each one actually
save triangles? Off until a key is set.

```toml
[lod]
min_levels = 3      # LOD0 + LOD1 + LOD2 at least (0 = off)
max_ratio = 0.6     # each level at most 60% of the triangles of the one above (0 = off)
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `min_levels` | int | 0 (off) | the asset must have at least this many levels, **LOD0 included**. An asset with no LODs has 1 level and fails any value above 1, saying so: *no LODs (LOD0 only); at least 3 levels are required: add LOD1-LOD2 (export an LOD group)* |
| `max_ratio` | float | 0 (off) | above 0 and at most 1: every level may keep at most this fraction of the triangles of the level above it. The failure names the level, its ratio and both counts, and the count to get under: *LOD2 of 'Tree' keeps 0.80 of LOD1's triangles (80 of 100); at most 0.6 is allowed: reduce it to 60 triangles or fewer*. A level with no triangles fails too |

Where the levels come from:

| Format | Read as LODs | Who imports them that way |
|---|---|---|
| FBX | an **LOD group** (`LodGroup` node): its children in order are LOD0, LOD1, ... | Unreal ("Import Mesh LODs") and Unity |
| FBX without an LOD group | objects named `<name>_LOD0`, `<name>_LOD1`, ... | Unity builds an LOD group from these; **Unreal does not** (it reads an FBX's LODs only from an LOD group), so the verdict carries a note saying so |
| glTF / GLB | the `MSFT_lod` node extension: the node is LOD0 and its `ids` are LOD1, LOD2, ... (they sit outside the scene) | Babylon.js and other MSFT_lod readers |
| glTF / GLB without MSFT_lod | nodes (or, for an unnamed node, meshes) named `<name>_LOD0`, `<name>_LOD1`, ... | the common export convention; we have not verified an engine that builds LODs from it on glTF import |
| OBJ | nothing: an .obj cannot carry LODs, so `min_levels` above 1 fails it and says to export FBX | - |

Details:

* `_LOD<n>` matching ignores case and DCC namespaces (`props:Rock_LOD1`). A
  plain `<name>` stands in for a missing `<name>_LOD0`. A chain stops at the
  first missing number (`_LOD0`, `_LOD2` is one level), and a lone `_LOD0` is
  a mesh without LODs.
* A level's triangles are those of its node and everything under it, collision
  meshes left out. The `max_triangles` budget is still LOD0's for an FBX LOD
  group and for MSFT_lod (as before); objects that are LODs only by name are
  all counted in it, as before.
* A file with several LOD chains (several meshes, each with its own LOD group)
  is judged by its weakest chain for `min_levels`, and every chain is checked
  for `max_ratio`.
* LODs kept as separate files (`Rock_LOD1.fbx` beside `Rock.fbx`) are not
  joined up: each file is its own asset.
* `--json` and telemetry carry `lod_levels` and `lod0_triangles`,
  `lod1_triangles`, ... (for the chain with the fewest levels); the one-line
  metric says `3 LODs` or `no LODs`. Nothing is added while `[lod]` is off.
* `lod_min_levels` can be waived per asset with [[exceptions]] (`allow = 0`).

## [collision]

Unreal's collision convention: meshes named `UCX_` (convex hull), `UBX_` (box),
`USP_` (sphere) or `UCP_` (capsule) followed by the name of the render mesh
they belong to. Off until the table is there.

```toml
[collision]
require = true            # a static mesh must carry collision (default false)
name_must_match = true    # default true once [collision] exists
convex = true             # default true once [collision] exists
max_hulls = 8             # UCX_ hulls per asset (0 = off)
max_hull_vertices = 32    # vertices per UCX_ hull (0 = off)
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `require` | bool | false | a static mesh must have at least one collision mesh. **Skinned meshes are exempt**: a skeletal mesh uses a physics asset, not UCX_ meshes. The failure gives the name to use: *no collision: a static mesh needs a UCX_ (or UBX_/USP_/UCP_) mesh named after its render mesh, such as UCX_Barrel_00* |
| `name_must_match` | bool | true with a `[collision]` table | every collision mesh must be named after a render mesh, or Unreal ignores it on import and the asset ships without that collision. The failure names the stray mesh and the render meshes it could be named after |
| `convex` | bool | true with a `[collision]` table | every `UCX_` hull must be convex. Unreal does not refuse a concave one: it silently replaces it with its convex hull, filling the dent (a U-shaped hull becomes a block). A flat `UCX_` (a single plane) fails too: there is no volume to build a hull from |
| `max_hulls` | int | 0 (off) | at most this many `UCX_` hulls per asset (boxes, spheres and capsules are cheap and not counted). Each hull is a physics body to test |
| `max_hull_vertices` | int | 0 (off) | at most this many distinct vertices per `UCX_` hull; 0, or 4 and more (a hull needs 4 corners) |

The name rule, as Unreal's FBX importer reads it: `UCX_<RenderMeshName>`, or
`UCX_<RenderMeshName>_<suffix>` where the suffix is anything after a last
underscore, by convention a two-digit number (Unreal's documentation:
`UCX_Tree_01_00` and `UCX_Tree_01_01` both belong to `Tree_01`). Names
compare exactly, **case included**, as Unreal compares them; a DCC namespace is
ignored. The render meshes are the LOD0 meshes, because Unreal imports collision
for LOD0 only: in an FBX LOD group the hull is named after the LOD0 mesh
(Unreal's own export writes `UCX_cube_LOD0` for `cube_LOD0`). In a glTF a hull
may be named after the render node or its mesh.

How convex is tested: for every face of the hull, all of the hull's vertices
must lie on one side of that face's plane (the test does not depend on which
way faces are wound). A vertex further than **1% of the hull's bounding-box
diagonal** on the wrong side makes it concave: a 1.7 cm dent in a 1 m crate.
Smaller deviations, such as a slightly bent quad, change nothing in play and
pass. A hull thinner than 0.01% of its diagonal is flat. The failure gives the
dent as a share of the hull's size and how to fix it (split it into convex
pieces, `UCX_<mesh>_00`, `_01`, ...). Boxes, spheres and capsules (`UBX_`,
`USP_`, `UCP_`) are fitted to their vertices by Unreal and are not tested.

Choosing `max_hull_vertices`: PhysX (Unreal 4) cooks a convex hull of at most
255 vertices, so 255 is the hard ceiling for a UCX_ hull that must survive
import unchanged. Unreal 5's physics (Chaos) has no limit documented; large
hulls are reported to be simplified on import. Unreal's own convex
decomposition tool makes hulls of up to 32 vertices (16 by default), and that
is a good budget for hand-made hulls: 8 for a box, 16 to 32 for a rounded
shape. We have not measured Chaos's behaviour ourselves.

What is read, and what is not:

| Format | Collision read from |
|---|---|
| FBX (binary and ASCII) | objects named `UCX_`/`UBX_`/`USP_`/`UCP_` (as before, they are never part of the triangle budget) |
| glTF / GLB | nodes named so (or, for an unnamed node, meshes). Unreal before 5.4 imports such nodes as ordinary meshes (a bug Epic confirmed and fixed for 5.4); not verified here in an editor |
| OBJ | not checked: with `require` on, an .obj gets a note that its collision was not checked, not a failure |

* Draco-compressed hulls are counted but not tested for convexity (the
  verdict says so); so are hulls made of strips or fans.
* Unreal's other collision sources are not read: simple collision added in the
  Static Mesh editor, "Auto Generate Collision" on import, and `MCDCX_` hulls.
* `--json` and telemetry carry `collision_shapes`, `collision_hulls` and
  `max_hull_vertices`; the metric adds `1 collision`. Nothing is added while
  `[collision]` is absent.
* `collision_require` can be waived per asset with [[exceptions]]
  (`allow = false`).

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
| `rule` | one of `max_triangles`, `max_texture_edge`, `max_material_slots`, `max_bone_influences`, `max_uv_channels`, `require_power_of_two`, `enforce_texture_naming`, `allow_degenerate`, `check_references`, `pbr_check`, `max_texture_mb`, `max_mesh_mb`, `lod_min_levels`, `collision_require` |
| `allow` | a whole number for the counts (`lod_min_levels = 0` waives the LOD rule), a number of MB for the memory budgets, `true`/`false` for the switches (`collision_require = false`) |
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
