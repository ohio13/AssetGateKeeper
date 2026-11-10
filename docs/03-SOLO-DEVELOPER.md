# 03 - Solo developer: twenty minutes, no server

For one person with one machine. No database server, no accounts, no containers.
If you are already past that, jump to [04 Studio rollout](04-STUDIO-ROLLOUT.md).

## Minute 0-2: install

```
python -m pip install "asset_gatekeeper-1.0.0-py3-none-any.whl" "trimesh>=4" "pillow>=10"
asset-gatekeeper --version
```

Unzipping instead of installing is fine too - see
[02 Installation](02-INSTALLATION.md#path-a---drop-in-zip-no-install).

## Minute 2-6: decide what "too big" means

Start from a shipped preset rather than from zero. `--list-presets` prints the
budget for each:

```
asset-gatekeeper --list-presets
```

| Target | Triangles (char / prop) | Texture | Slots | Power of two | PBR |
|---|---|---|---|---|---|
| `retro` | 1,500 / 500 | 256 | 1 | yes | no |
| `ps1` | 1,100 / 350 | 256 | 1 | yes | no |
| `nds` | 900 / 300 | 128 | 2 | yes | no |
| `gamecube` | 6,000 / 2,000 | 512 | 4 | yes | no |
| `ps2` | 5,000 / 1,800 | 256 | 3 | yes | no |
| `n3ds` | 3,000 / 1,200 | 256 | 2 | yes | no |
| `wii` | 5,000 / 2,000 | 512 | 3 | yes | no |
| `xbox` | 9,000 / 3,000 | 1,024 | 4 | yes | no |
| `ps3` | 20,000 / 8,000 | 1,024 | 4 | no | no |
| `pc` | 120,000 / 30,000 | 4,096 | 8 | no | yes |

Run it once with a preset and no rules file:

```
asset-gatekeeper "C:\Projects\MyGame\assets" --preset ps2
```

That works - a preset is a complete budget on its own. You do not need a
`rules.toml` until you disagree with the preset.

Or let the rules wizard pick for you: `asset-gatekeeper "C:\Projects\MyGame\assets"
--wizard` (or double-click `tools\rules_wizard.bat`) asks which engines, art
programs and platforms you use - several of each is fine - plus the kinds of
asset, the art style and how strict to be about broken geometry, and writes a
commented `rules.toml` from these same presets.

## Minute 6-12: make it yours

Copy the template and edit three numbers:

```
copy "<AGK>\templates\rules.toml" "C:\Projects\MyGame\rules.toml"
```

```toml
platform = "ps2"          # or delete this and set the numbers outright

[mesh]
max_triangles = 3500      # my props are allowed more than a PS2 prop
max_material_slots = 2

[texture]
max_edge = 512            # because of the UI textures, not the characters
```

Then, for the two things a per-path budget cannot express:

```toml
[tags.boss]               # any folder or filename prefix called "boss"
max_triangles = 40000     # the one asset that is allowed to be huge
max_material_slots = 6
```

Run it:

```
cd C:\Projects\MyGame
asset-gatekeeper assets
```

The gate finds `rules.toml` next to the folder, applies it, and prints one line
per violation. Exit code 0 means clean, 1 means problems.

## Minute 12-16: keep watching

```
asset-gatekeeper assets
```

There is no --watch flag: watching is the default. The gate gates what changed and
stays quiet about what did not. Use `--once` for a single pass (that is what CI
uses). Toasts appear only for new failures; `--no-notify` silences them.

If you would rather not think about it at all, put it in a Git pre-commit hook -
see [10 CI and version control](10-CI-AND-VERSION-CONTROL.md#git-hook).

## Minute 16-20: see your own art, once

Two optional extras, both local, both one command:

```
agk-serve up
# AssetHealth      http://127.0.0.1:8080
# Art Workstation  http://127.0.0.1:8100
```

Drop a `.glb` onto the workstation to orbit it in 3D. Open AssetHealth to see the
verdicts you just produced, and `/analytics` for the trend. Neither is needed for
the gate to work; they are there so you can look at something other than a
terminal once in a while.

`agk-serve down` stops them. Nothing is installed as a service.

## The daily loop, after setup

```
# before you start
asset-gatekeeper assets

# when you export
#   the watcher reports the new file within a second

# a single pass, for a script
asset-gatekeeper assets --once --json > gate.json
```

## Optional: the panel inside your DCC

If you live in Blender, install `plugins/blender/preflight_panel.py` and press the
button before exporting instead of after. The whole setup is in
[09 DCC plugins](09-DCC-PLUGINS.md#blender). Same budget, same verdict, 20
seconds earlier.

## Optional: the healer

```
asset-gatekeeper assets/bark.png --heal --dry-run    # what would change
asset-gatekeeper assets/bark.png --heal              # fix that one file
asset-gatekeeper assets/bark.png --undo-heal         # changed your mind
```

It resizes a non-power-of-two PNG to the next power of two, adds a missing name
suffix (repointing the files that name it), strips zero-area faces from an .obj,
and re-verifies the result. It does not shrink textures or meshes that are over
budget. `--auto-heal` does the same for every failing asset in a folder. A file is only replaced if the repaired version passes; otherwise the
original stays and the failure is reported - with the reason. Every adopted heal
keeps the original in `.agk-backups/`, so `--undo-heal` can put it back.

## Optional: the number that matters

After a few days of running, open `http://127.0.0.1:8080/analytics` and look at
two panels:

* **Compliance** - is the project getting cleaner or dirtier?
* **Polygon growth** - is the average asset getting more expensive over time?

A rising average with a flat asset count means the content is getting heavier.
That is the early warning a producer usually gets too late, and it is free once
the gate is running.

## What to skip, deliberately

For one person, these are noise. Leave them off until you need them:

* PostgreSQL, OIDC, SAML, RBAC - see [Enterprise](ENTERPRISE.md)
* Perforce and runner farms - see [10](10-CI-AND-VERSION-CONTROL.md)
* the admin console at `/admin` - one account is not a governance problem

Next: [05 Rules reference](05-RULES-REFERENCE.md) when you want every key, or
[06 CLI reference](06-CLI-REFERENCE.md) when you want every flag.
