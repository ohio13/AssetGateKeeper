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
| `allow_degenerate` | bool | false | zero-area faces: Nanite and RT compute garbage from them |
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
| `textures` | bool | true | `.png`: add a missing name suffix (and repoint what names it), make edges a power of two |
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
