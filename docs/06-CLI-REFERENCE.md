# 06 - CLI reference

Everything the `asset-gatekeeper` command does, every flag it accepts, and the
contracts (exit codes, JSON, telemetry) other tools depend on.

```
asset-gatekeeper <folder> [flags]
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
| `--list-presets` | print the ten shipped budgets |
| `--doctor` | environment report: interpreter, packages, ports, rules |
| `--suggest-naming` | learn a `[naming]` block from the names already in `<folder>`; prints it, writes nothing |
| `<file.obj> --bake-polypaint [--bake-triangles N] [--bake-texture PX]` | make a game asset from an `.obj` whose colour is ZBrush Polypaint or vertex colours: a reduced `.glb` with UVs and a baked `T_..._BaseColor.png`, written beside it (`_HP` in the name becomes `_LP`), sized to the folder's budget (at most 30,000 triangles and 2048 px unless set), then checked. The source is never changed. `--bake-preset n64` makes an N64-style asset (about 1,200 triangles, 64 px, 16 colours, the face in a second 64 px texture); `--bake-face-texture` / `--bake-no-face-texture` turn the face texture on or off. Needs `fast-simplification`, `xatlas` and `scipy` on Python 3.10+ (double-click `Install_Extras.bat`, or `pip install "asset-gatekeeper[bake]"`) |
| `--version` | print the version |

### The JSON shape

```json
[
  {
    "path": "C:\\Proj\\assets\\env\\cliff.obj",
    "name": "cliff.obj",
    "passed": false,
    "status": "FAILED",
    "metric": "72,500 tris / 4 mat",
    "problems": [
      "72,500 triangles exceeds the 50,000 budget (+22,500)",
      "4 material slots exceeds the 3 slot budget (draw calls)"
    ]
  }
]
```

One object per asset, stable keys. `status` is `PASSED`, `FAILED`, `HEALED` or
`ERROR`; `problems` is empty for a pass. A `--once` run ends with one more
line, `{"summary": {"total", "passed", "failed", "skipped"}}`, where `skipped`
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
