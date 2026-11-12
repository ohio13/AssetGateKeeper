# 06 - CLI reference

Everything the `asset-gatekeeper` command does, every flag it accepts, and the
contracts (exit codes, JSON, telemetry) other tools depend on.

```
asset-gatekeeper <folder> [flags]
asset-gatekeeper <file> [flags]
asset-gatekeeper <file> <file> ... --once [flags]
```

One folder, one budget, one verdict per asset. There is no project file, no
state, and no network unless you ask for it.

## Exit codes - the contract

| Code | Meaning | What CI should do |
|---|---|---|
| `0` | every asset passed (or healed) | continue |
| `1` | the gate ran and found problems | fail the build, post the verdict |
| `2` | the gate could not run (bad rules, missing folder, bad flag) | treat as a broken pipeline, not a failed asset |

The difference between 1 and 2 matters: a red build from a genuine budget
violation is the tool working; a red build from a typo in `--preset` is the
*studio* working badly, and it should page someone different.

## Behaviour

* **It watches by default.** Run it once with `--once` (scripts, CI).
* **It only re-gates what changed** while watching; unchanged files are silent.
* **It never writes to your assets** unless `--auto-heal` is on or you run `--heal` on a file - and then it keeps the originals in `.agk-backups/`.
* **It finds `rules.toml` next to the folder** unless `--rules` says otherwise.
* **A `--once` run reuses what it already knows.** Verdicts are kept in a cache
  file outside the folder (one per folder, in `%LOCALAPPDATA%\AssetGatekeeper\cache`
  or `~/.cache/asset-gatekeeper`; `$AGK_CACHE_DIR` moves them all); a file that has
  not changed since - nor any file it references, nor the rules, nor the gate
  itself - gets its last verdict back without being read. `--no-cache` turns it
  off, `--cache FILE` names the file; a cache inside the folder being checked is
  refused, because anyone who can commit there could write one that passes
  anything. `--jobs N` checks N files at a time (`0` = one per CPU); the verdicts
  and their order are the same as one at a time.
* **It never follows symlinks** unless `--follow-symlinks`; dangling links are
  skipped and counted (a Windows 1920 symlink is not an asset problem).

## Several files in one run

```
asset-gatekeeper Props/SM_Crate.fbx Textures/T_Crate_Albedo.png SM_Gone.fbx --once --json --rules rules.toml
```

```json
{"file": "/proj/Props/SM_Crate.fbx", "name": "SM_Crate.fbx", "passed": true, "status": "PASSED", ...}
{"file": "/proj/Textures/T_Crate_Albedo.png", "name": "T_Crate_Albedo.png", "passed": false, "status": "FAILED", ...}
{"file": "/proj/SM_Gone.fbx", "name": "SM_Gone.fbx", "passed": false, "status": "ERROR", "problems": ["not found"], ...}
{"summary": {"total": 3, "passed": 1, "failed": 2, "totals_failed": []}}
```

What the Unity and Godot plug-ins run for an import: one Python start-up for
the lot instead of one (about 0.7 s) per file. With `--once`, several file
paths are checked one after another in one process:

* **Each file is checked exactly as a run with that file alone** checks it:
  the `--rules` file if given, else the `rules.toml` in the file's own folder;
  `[tags.*]` and `[[exceptions]]` relative to the `--rules` file's folder when
  the file is under it; `[[totals]]` are not judged (a few files are not the
  folder, and one file does not judge them either). Each JSON line is the same
  line that run would print.
* **One verdict line per file, in the order given**, each with its own `file`
  (the full path, symlinks resolved), then **one summary line** as a folder run
  prints (`{"summary": ...}`, no `file` key; readers of verdicts skip it). The
  text output is a line per file and `N asset(s): x passed, y failed`. The
  rules and budget lines on stderr are printed once per different budget, not
  once per file.
* **A missing file, or one that is not an asset type, is an `ERROR` line of
  its own** (`"problems": ["not found"]` / `["not a tracked asset type"]`); the
  files after it are still checked.
* **A file whose own `rules.toml` cannot be used** (no `--rules`, a broken file
  in its folder) gets no verdict line; the reason is on stderr, as for one file.
* **Exit code:** `2` if any file could not be checked that way, else `1` if any
  file failed (an `ERROR` line included), else `0`.
* `--report` writes one report holding every file.
* **Refused with exit 2**, before anything is checked: a folder among the paths
  (check a folder on its own), several paths without `--once`, and the flags
  that do one job on one path (`--heal`, `--exception-path`, `--bake-polypaint`,
  `--doctor`, `--init`, `--wizard`, `--audit`, `--suggest-naming`,
  `--undo-heal`, `--list-heals`). `--jobs` and the cache are not used for a
  list of files, as for one file.

One path - a file or a folder - behaves exactly as before: one file prints one
line and no summary, and exits 2 with no line when the file is not there.

Measured on a 2-core Linux machine, 50 small files (25 64 px PNGs, 25
768-triangle FBXs) with `--once --json --rules rules.toml`: one process per file
37.4-39.4 s, all 50 in one run 0.8-0.9 s.

## Budget flags

| Flag | Effect |
|---|---|
| `--preset NAME` | apply a shipped budget (`--list-presets`); numbers only |
| `--platform NAME` | select a `[platforms.NAME]` table from the rules file |
| `--triangles N` | override `mesh.max_triangles` |
| `--max-materials N` | override `mesh.max_material_slots` |
| `--max-texture N` | override `texture.max_edge` |
| `--max-influences N` | override `mesh.max_bone_influences` |
| `--max-uvs N` | override `mesh.max_uv_channels` |
| `--rules PATH` | read this rules file (a missing file is an error) |
| `--remote-rules URL` | fetch the budget from a server instead |

A flag always beats a preset, a preset beats the platform profile only when it was
passed explicitly, and both beat the file. See
[05](05-RULES-REFERENCE.md#precedence).

## Check switches

Tightening on the command line is common; loosening should be a rules change so
it is reviewable.

| Flag | Effect |
|---|---|
| `--no-reference-check` | skip missing `.mtl`/`.bin`/texture detection |
| `--allow-degenerate` | permit zero-area faces |
| `--allow-npot` | permit non-power-of-two textures |
| `--no-naming` | skip the texture suffix check |
| `--no-pbr-check` | skip the albedo luminance check |
| `--audit-transforms` | report non-zero object transforms |
| `--require-indexed` | report non-indexed meshes |

## Tag behaviour

| Flag | Effect |
|---|---|
| `--tag-mode suggest` | a misspelled folder is a warning (default) |
| `--tag-mode strict` | a misspelled folder is an error |

Reported as: `tag 'charcter' is not a known tag; closest match is 'character'`.

## Output

| Flag | Effect |
|---|---|
| `--json` | machine-readable verdicts on stdout |
| `--report junit:FILE` / `sarif:FILE` / `github:FILE` | with `--once` or one file, also write a JUnit, SARIF or GitHub-annotation report for CI (repeatable; see [10](10-CI-AND-VERSION-CONTROL.md#reports-your-ci-server-shows-per-asset)) |
| `--cache FILE` · `--no-cache` | with `--once`: where the verdict cache lives (outside the folder; default in the user's cache directory), or no cache at all |
| `--exception-path PATH` | with one file: the path `[[exceptions]]` match it as, relative to the rules file's folder (used by the Perforce trigger) |
| `--jobs N` | with `--once`: check N files at a time in separate processes (`0` = one per CPU; healing always runs one at a time) |
| `--no-color` | plain text (CI logs, redirected output) |
| `-v` | verbose |
| `--init [--yes]` | write a commented starter `rules.toml` |
| `--wizard [--yes] [--force]` · `--wizard-answers FILE` | the rules wizard: seven questions about the project (several answers each), one commented `rules.toml` for all of them - see [below](#the-rules-wizard) |
| `--list-presets` | print the ten shipped budgets |
| `--doctor` | environment report: interpreter, packages, ports, rules |
| `--suggest-naming` | learn a `[naming]` block from the names already in `<folder>`; prints it, writes nothing |
| `<file> --bake [--bake-triangles N] [--bake-texture PX]` (also `--bake-polypaint`) | make a game asset from an `.obj` whose colour is ZBrush Polypaint or vertex colours, or from a textured `.obj` (with its `.mtl` and images) or `.gltf`/`.glb`, every material's textures baked into one: a reduced `.glb` with UVs and a baked `T_..._BaseColor.png`, written beside it (`_HP` in the name becomes `_LP`), sized to the folder's budget (at most 30,000 triangles and 2048 px unless set), then checked. The source is never changed. `--bake-preset n64` makes an N64-style asset (about 1,200 triangles, 64 px, 16 colours, the face in a second 64 px texture); `--bake-face-texture` / `--bake-no-face-texture` turn the face texture on or off. Needs `fast-simplification`, `xatlas` and `scipy` on Python 3.10+ (double-click `Install_Extras.bat`, or `pip install "asset-gatekeeper[bake]"`) A `--bake-texture` or `--bake-triangles` above the folder's `[texture] max_edge` or `[mesh] max_triangles` is refused before the bake starts, since the result would fail the gate |
| `--version` | print the version |

### The JSON shape

```json
{"file": "C:\\Proj\\assets\\env\\cliff.obj", "name": "cliff.obj", "passed": false, "status": "FAILED", "metric": "72,500 tris / 4 mat", "problems": ["72,500 triangles exceeds the 50,000 budget (+22,500)", "4 material slots exceeds the 3 slot budget (draw calls)"], "modifications": [], "warnings": [], "stats": {"triangles": 72500, "material_slots": 4, "degenerate_triangles": 0}}
```

One JSON object per asset, one per line (JSON Lines, not one array), stable
keys: `file` (the full path), `name`, `passed`, `status`, `metric`, `problems`,
`modifications` (what a heal changed), `warnings` (notes that never fail the
asset) and `stats` (the numbers behind the metric). `status` is `PASSED`, `FAILED`, `HEALED` or
`ERROR`; `problems` is empty for a pass. A `--once` run over a folder or
[several files](#several-files-in-one-run) ends with one more line (one file
alone has none), `{"summary": {"total", "passed", "failed", "skipped", "totals_failed"}}`, where
`totals_failed` lists each `[[totals]]` budget over its limit (`{"total": "Props/*",
"problems": [...], "metric", "stats"}`; it fails the run without being an asset, so
`failed` does not count it) and `skipped`
counts the files it walked past: `{"total": 7, "unsupported": {".jfif": 6,
".blend": 1}, "excluded": 0}` (scratch, hidden and the tool's own files are not
counted). The text output says the same in one line, with a hint for the common
ones: `7 file(s) skipped: unsupported type .jfif x6 (a JPEG: rename it to .jpg
to check it), .blend x1 (a Blender scene: export FBX, glTF or OBJ)`. Parsers should key off `status`, not
`passed` - a healed asset is both.

`ERROR` means the gate could not read the file at all (a locked handle, a
permission the account does not have, a reparse point gone stale). It is not a
verdict on the art, and it still fails the run: an asset nobody could inspect is
an asset nobody can ship. A folder holding no tracked assets is also a failure
unless `--allow-empty` says it is meant to be empty.

## Healing

| Flag | Effect |
|---|---|
| `--auto-heal` | folder-wide: repair every failing asset, re-verify, adopt only if the repair passes (says at startup what it may change) |
| `--no-auto-heal` | turn healing off for this run (default when the rules say `enabled = false`) |
| `<file> --heal` | repair this one file now, against the project's rules; the original goes to `.agk-backups/` |
| `<file> --heal --dry-run` | say what `--heal` would change, write nothing |
| `<file> --undo-heal` | restore what the last heal of this file replaced (the healed name or the old one); refuses if it was edited since |
| `<folder> --list-heals` | the heals recorded under a folder, newest first, and whether each was undone |

When `--heal` changes nothing it says why: the healed copy would still fail (and
which rule), nothing it can change would help, a file outside the project names
the texture, or the new name is taken.

## Watching and notifications

| Flag | Effect |
|---|---|
| `--once` | gate everything, print, exit (no watcher) |
| `--allow-empty` | with `--once`: exit 0 when the folder holds no tracked assets. Without it an empty folder fails the run, because a wrong path is the usual reason a scan finds nothing |
| `--recursive` | include subfolders (the default for a folder target) |
| `--exclude GLOB` | skip matching paths; repeatable |
| `--follow-symlinks` | walk through symlinked folders (off by default) |
| `--notify` / `--no-notify` | desktop toasts for new failures |
| `--notify-max N` | toasts per minute (default 6; a wall of toasts is a wall of ignored toasts) |

## Telemetry

```
asset-gatekeeper assets --telemetry-url http://127.0.0.1:8080/api/telemetry
```

Environment alternative: `ASSET_GATEKEEPER_SERVER_URL`. The token is appended by
the client from `--telemetry-url`'s query string or the `AGK_TOKEN` environment
variable; the payload is a Post to `/api/telemetry` with:

### What the payload is allowed to say about the machine

`--telemetry-identity` (or `[server] identity`, or
`ASSET_GATEKEEPER_TELEMETRY_IDENTITY`) picks one of three levels:

| Level | `hostname` / `username` | `path` |
|---|---|---|
| `full` | sent | absolute |
| `host` (default) | machine name only | absolute |
| `none` | empty | relative to the watch folder |

The default is `host`: the dashboard's Send-to-Workstation bridge needs a real
file, and per-machine dashboards (and AssetHealth's `people.toml` alerts) need
the machine name. The OS account - the part that names a person - is only sent
with `full`.

```
timestamp, received_at, hostname, username, os,
filename, filepath, filesize, passed, metric, status,
problems[], project_id
```

Telemetry is **fire and forget**: if the dashboard is down the gate still exits
with the right code, and the verdict is simply not recorded. A gate that fails
because a dashboard is down turns a metrics problem into a pipeline outage.

## The rules wizard

```
asset-gatekeeper <folder> --wizard                       # asks, in a terminal
asset-gatekeeper <folder> --wizard-answers answers.json   # the same answers from a file
asset-gatekeeper <folder> --wizard --yes                  # every default, no questions
```

On Windows, double-click `tools\rules_wizard.bat` (or drag the export folder
onto it). `--init --wizard` is the same as `--wizard`; plain `--init` is
unchanged. Seven questions, each with a default on Enter; the first four and
the extra checks take several answers (`1,3` or `unreal, unity`):

| Question (answers-file key) | Choices | Becomes |
|---|---|---|
| Engines (`engines`) | `unreal` `unity` `godot` `other` | `[naming] preset = [...]`, one per engine (`other` adds `default`); engine differences (skin weights, UV sets, memory estimates) as comments |
| Art programs (`tools`) | `maya` `max` `blender` `houdini` `zbrush` `substance` `other` | `substance` and `blender` add the `substance` / `gltf` naming presets; comments point at the DCC panels ([09](09-DCC-PLUGINS.md)) and, for ZBrush, at `--bake-polypaint` |
| Platforms (`platforms`) | `pc` (high-end PC / current console), `midrange` (mid PC / last-gen console), `switch`, `mobile`, `vr`, `n64` (retro) | the tightest limit of all of them is the `[mesh]`/`[texture]` budget; each platform gets a `[platforms.<name>]` block for `--platform <name>` |
| Kinds of asset (`assets`) | `characters` `props` `environment` `ui` | two or more kinds of mesh: `[tags.characters]`, `[tags.props]`, `[tags.environment]` (a tag is a folder name). With several platforms they are written as comments, because a tag's budget wins over `--platform`. `ui` lets textures named `UI_...` skip the map suffix |
| Style (`style`) | `realistic` `stylised` `lowpoly` | the albedo brightness check (`pbr`) is on for realistic art only |
| Extra checks (`checks`) | `lod` `collision` `skeleton` `lightmap` `none` (default) | `[lod] min_levels = 3` (2 for a retro-only project), `max_ratio = 0.6`; `[collision] require = true` (as a comment unless Unreal is one of the engines); `[skeleton] single_root = true` with Unreal and `max_bones_per_section = 75` for mobile and VR, with a `[platforms.<name>.skeleton]` block per platform; `[lightmap] require = true`, `uv_channel = 1`, `max_overlap = 0` |
| Broken geometry (`broken_geometry`) | `block` `warn` | `[mesh] degenerate` |

The numbers come from the shipped tables, not from the wizard: every platform
is the `--preset` target of the same name (`pc`, `midrange`, `switch`,
`mobile`, `vr`, `n64`). The file's header
lists the answers (and a `# wizard-answers:` line you can save as
`answers.json`); every value has a comment saying where it came from.

An answers file is JSON with any of the keys above; a missing key takes the
default, a list or a comma-separated string both work, and an unknown key or
choice is an error that names it:

```json
{"engines": ["unreal", "unity"], "tools": ["maya", "substance"],
 "platforms": ["pc", "switch"], "assets": ["characters", "props"],
 "style": "realistic", "broken_geometry": "block", "replace": false}
```

An existing `rules.toml` is replaced only after a yes (default no), with
`"replace": true` in the answers file, or with `--force`; the old one is copied
to `rules.backup-<date>-<time>.toml` first. Without a terminal and without an
answers file (or `--yes`) the wizard refuses instead of waiting. Exit codes:
`0` written, `1` nothing written (you said no, or kept the existing file), `2`
a bad answer, a bad answers file or no terminal.

## Recipes

**A single pass for CI, with a captured history**

```
asset-gatekeeper assets --once --json --no-color --no-notify > gate.json
```

**Judge a folder with the rules of a different project**

```
asset-gatekeeper C:\Other\assets --rules C:\Proj\rules.toml --once
```

**Enforce the studio standard but let one asset through**

```
[tags.hero_prop]        # in rules.toml, not on the command line
max_triangles = 90000
```

**Start a rules file from scratch**

```
asset-gatekeeper --init --yes      # writes ./rules.toml if absent
asset-gatekeeper --list-presets    # then copy the numbers you want
```

**Check the environment before blaming the tool**

```
asset-gatekeeper --doctor
```

## What it deliberately cannot do

* **No per-file overrides.** Overrides belong in rules, by tag, reviewed.
* **No style opinions.** It measures cost, not quality.
* **No silent format conversion.** `.blend`/`.max`/`.ma` are reported as
  `unverified`, never converted and never counted as zero triangles.
* **No daemon.** It is a process you run; `agk-serve` is for the web surfaces.

Next: [07 Art Workstation](07-ART-WORKSTATION.md) for the review screen, or
[09 DCC plugins](09-DCC-PLUGINS.md) to catch problems before export.
