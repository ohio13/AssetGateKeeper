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
* **It never writes to your assets** unless `--auto-heal` is on.
* **It finds `rules.toml` next to the folder** unless `--rules` says otherwise.
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
| `--no-color` | plain text (CI logs, redirected output) |
| `-v` | verbose |
| `--init [--yes]` | write a commented starter `rules.toml` |
| `--list-presets` | print the ten shipped budgets |
| `--doctor` | environment report: interpreter, packages, ports, rules |
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

One object per asset, stable keys. `status` is `PASSED`, `FAILED` or `HEALED`;
`problems` is empty for a pass. Parsers should key off `status`, not `passed` -
a healed asset is both.

## Healing

| Flag | Effect |
|---|---|
| `--auto-heal` | repair, re-verify, adopt only if the repair passes |
| `--no-auto-heal` | turn healing off for this run (default when the rules say `enabled = false`) |

## Watching and notifications

| Flag | Effect |
|---|---|
| `--once` | gate everything, print, exit (no watcher) |
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
* **No silent format conversion.** `.blend`/`.max`/`.fbx` are reported as
  `unverified`, never converted and never counted as zero triangles.
* **No daemon.** It is a process you run; `agk-serve` is for the web surfaces.

Next: [07 Art Workstation](07-ART-WORKSTATION.md) for the review screen, or
[09 DCC plugins](09-DCC-PLUGINS.md) to catch problems before export.
