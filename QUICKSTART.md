# Quickstart: gate a real project

Two ways in. Both are the same code; pick one.

| | Use it when | You get |
|---|---|---|
| **A. Wheel** (`pip install`) | you want the gate, the dashboard and the review screen on your PATH | `asset-gatekeeper`, `asset-health`, `art-workstation`, `art-workstation-upload` |
| **B. From a checkout** | you want the VCS hooks, the CI templates, the tests and the demo | the above, run as `python asset_gatekeeper.py …` |

Nothing here edits your assets. The gate only reads them, auto-healing is off by
default, and the workstation copies uploaded bytes into its own folder.

---

## 1. Install

### A. Wheel

```bat
python -m venv .venv
.venv\Scripts\activate
pip install dist\asset_gatekeeper-1.0.0-py3-none-any.whl
asset-gatekeeper --version
```

`trimesh` and `Pillow` are pulled in automatically — they are genuinely required
(the mesh profiling and texture lookups cannot be faked). Everything else, both
web UIs included, is standard library only.

### B. From a checkout

```bat
python -m venv .venv
.venv\Scripts\activate
pip install trimesh pillow
python asset_gatekeeper.py --version
```

Run the tests once to prove the install is sane:

```bat
python tests\test_gatekeeper.py
python tests\test_server.py
python tests\test_workstation.py
```

Expected: `FAILURES: none` from each (327 / 191 / 118 assertions).

> On Windows `python` is often Python 2.7 or a launcher stub. Use `py -3` (or the
> `.venv\Scripts\python.exe` path) everywhere below if `python --version` is not 3.9+.

---

## 2. Look before you configure

Point it at the real folder. This is read-only and takes seconds per hundred files:

```bat
asset-gatekeeper "D:\Projects\MyGame\Assets" --recursive --once
```

One line per asset, every violation on that line, and an exit code you can trust:

```
FAILED  Monsters/Boss/statue.gltf  72,500 tris / 4 mat
        72,500 triangles exceeds the 50,000 budget (+22,500); 4 material slots exceeds the 3 slot budget (draw calls)
PASSED  Props/crate.glb  1,204 tris / 1 mat
2 asset(s): 1 passed, 1 failed        (exit code 1)
```

Budget in force is printed at startup, including where it came from. If the
defaults are wrong for you, that is step 3.

Useful early flags:

| Flag | Why |
|---|---|
| `--exclude "preview_*"` | review renders and 4x paint sources are not shipped assets |
| `--recursive` | without it you only scan the top level |
| `--once` | scan and exit; drop it for watch mode |
| `--json` | one JSON object per asset, for your own tooling |

---

## 3. Write the budget for your project

```bat
asset-gatekeeper "D:\Projects\MyGame\Assets" --init
```

An interactive wizard: five questions, each with a default, and each answer can
also be passed as a flag (`--triangles 15000 --max-texture 1024 --max-materials 4
--platform mobile`) so a CI script never sits waiting on stdin. It writes a
`rules.toml` next to the assets. Edit it — this is the one file the whole pipeline
reads:

```toml
# rules.toml
platform = "mobile"                # applies [platforms.mobile] on top of the base

[mesh]
max_triangles     = 15000
max_material_slots = 4
max_uv_channels    = 2
max_bone_influences = 4

[texture]
max_edge        = 1024
power_of_two    = true
naming          = false            # off unless your engine demands suffixes

[tagging]
mode = "suggest"                   # auto | suggest | strict

[tags.boss]
max_triangles      = 60000
max_material_slots = 8

[tags.environment_rock]
max_triangles   = 3000
max_texture_edge = 512

[filters]
exclude = ["preview_*", "*_src4x.png", "*_c.png"]

[platforms.mobile]
max_texture_edge = 512
```

How it resolves, highest first:

```
CLI flag  >  central/remote rules  >  [platforms.<name>]  >  rules.toml  >  built-in defaults
```

Unknown keys are a hard error (exit 2) on purpose: a typo that silently greps to
nothing makes a gate look like it works while enforcing defaults.

`[tags.*]` matches by folder or filename prefix (`Monsters_Boss_01.gltf` → the
`boss` tag). `tagging.mode` decides what happens on a near-miss: `suggest`
(default) binds and warns, `auto` binds and logs, `strict` refuses to guess and
fails the asset — which is what you want once naming has settled.

---

## 4. Watch mode while people are working

```bat
asset-gatekeeper "D:\Projects\MyGame\Assets" --recursive --notify
```

Events queue; one worker waits for a file to stop changing (350 ms quiet, plus a
grace for zero-byte temp files) and only then inspects it. Failing assets raise a
native toast.

Auto-healing exists but is **opt-in** and verified before it ships:

```bat
asset-gatekeeper "…\Assets" --recursive --auto-heal
```

The repair is staged beside the asset as `.agk-heal-*`, re-inspected with the same
budget, and adopted only if it then passes — otherwise your original stays
byte-identical. glTF geometry is never rewritten.

---

## 5. The dashboard (AssetHealth)

```bat
asset-health --port 8080 --rules "D:\Projects\MyGame\Assets\rules.toml"
```

Open <http://127.0.0.1:8080/> — `/dashboard` is the table view,
`/budget-planner` edits budgets, `/api/rules` serves the read-only inspector.

**First-run seeding — read this once.** A brand-new database is filled with a
deterministic demo set (780 verdicts: 716 pass, 64 fail, 1 healed) so the page is
not empty. Those are sample rows, and `/api/summary` aggregates the whole table,
so real runs appear to be "already full". Use a clean database for a real project:

```bat
asset-health --no-seed --db mygame_health.db --rules "…\rules.toml"
```

Now point the gate at it, on every run:

```bat
asset-gatekeeper "…\Assets" --recursive --once --telemetry-url http://127.0.0.1:8080/api/telemetry
```

Or set it once per shell so nobody has to remember the flag:

```bat
set ASSET_GATEKEEPER_SERVER_URL=http://127.0.0.1:8080/api/telemetry
```

For a real studio, give the project its own tenant — the command prints a token
exactly once, and only its hash is stored:

```bat
asset-health --db mygame_health.db --add-project "MyGame"
set ASSET_GATEKEEPER_TOKEN=<the token it printed>
```

A `project_id` in a request body can never redirect a write; the token decides.

---

## 6. The 3D review screen (Art Workstation)

```bat
art-workstation --port 8100 --rules "D:\Projects\MyGame\Assets\rules.toml"
```

Open <http://127.0.0.1:8100/viewer>. The **📋 Rules & Budgets** tab shows the
budget in force (platform, tag mode, per-tag limits) straight from `rules.toml`.
Drag a `.glb` / `.obj` / `.gltf` anywhere onto the page, or load a whole folder:

```bat
art-workstation-upload "D:\Projects\MyGame\Assets" --recursive --skip-uploaded
```

Per asset you get textured / geometry / wireframe views, a collision proxy
(box, sphere or hull, exportable as `.obj`), LOD levels, material overrides,
review notes and an HTML report. LODs are inspection-grade on purpose: they drop
UVs and materials, so do not ship them.

Both servers are loopback-only by default and have no login until you pass
`--token` or create a project. That is deliberate: a review screen on the studio
LAN is a leak, so exposing it is an explicit act.

---

## 7. Put it in front of the repo

Gate every commit, so bad assets never land:

```bat
copy vcs_hooks\git_pre_commit.py .git\hooks\pre-commit
```

Perforce: add `vcs_hooks\p4_pre_submit.py` as a submit trigger; it passes
`--no-auto-heal` and fails closed. Hooks always disable healing — a hook that
rewrites binaries during a commit is a support ticket waiting to happen.

CI: `.github/workflows/asset_gatekeeper.yml` is a drop-in PR gate, and
`asset_gatekeeper.yml` is the reusable template. Keep the exit code; do not
`|| true` it.

---

## 8. When something is wrong

| Symptom | Cause |
|---|---|
| `error: cannot bind 127.0.0.1:8080: … in use` | a previous run is still alive: find the pid on that port and stop it |
| dashboard numbers look pre-filled | first-run seeding — see step 5, use `--no-seed` |
| `/viewer` is blank / tabs do nothing | open the browser console; a syntax error in `viewer.js` blanks the whole module |
| `unknown key` / exit 2 | `rules.toml` has a typo. Compare against `asset_gatekeeper.example.toml` |
| an asset "passes" that should not | check the `budget` line the gate prints first: it names the file it used |
| non-ASCII filenames crash a script of yours | reconfigure stdout/stderr to UTF-8; the tools already do |

Everything is read-only with respect to your source art. The only writes are the
SQLite databases, the workstation's own storage folder, and — with `--auto-heal`
explicitly on — a verified replacement beside the asset.
