# AssetGatekeeper — the manual

Everything in the system: what each feature is, how to set it up, when to use it,
and how the pieces work for **one person on one machine** versus **a team
co-operating**.

- **Quick install?** → [Quickstart](QUICKSTART.md)
- **Just the flags?** → [§4 The gate](#4-the-gate-every-flag)
- **Just the config file?** → [§5 rules.toml](#5-rulestoml-complete-reference)

---

## Contents

1. [What it is](#1-what-it-is)
2. [Core concepts](#2-core-concepts)
3. [Install and verify](#3-install-and-verify)
4. [The gate: every flag](#4-the-gate-every-flag)
5. [rules.toml: complete reference](#5-rulestoml-complete-reference)
6. [Categories and tags](#6-categories-and-tags)
7. [Healing](#7-healing)
8. [Audit mode](#8-audit-mode)
9. [Telemetry and central rules](#9-telemetry-and-central-rules)
10. [Integrations: DCC, VCS, CI, Python](#10-integrations-dcc-vcs-ci-python)
11. [AssetHealth: the dashboard](#11-assethealth-the-dashboard)
12. [Art Workstation: the 3D review screen](#12-art-workstation-the-3d-review-screen)
13. [Workflows: solo versus team](#13-workflows-solo-versus-team)
14. [Daily recipes](#14-daily-recipes)
15. [Operations](#15-operations)
16. [Feature matrix](#16-feature-matrix)
17. [Limits and non-goals](#17-limits-and-non-goals)
18. [Troubleshooting](#18-troubleshooting)
19. [Repository map](#19-repository-map)

---

## 1. What it is

Three surfaces, one config file, one verdict format.

| Surface | Command | Answers |
|---|---|---|
| **The gate** | `asset-gatekeeper` | "Is this asset within budget?" |
| **AssetHealth** | `asset-health` | "How is the project doing, and who is blocking it?" |
| **Art Workstation** | `art-workstation` | "Show me this asset properly, in 3D." |

They share `rules.toml` and share the *verdict*: one asset, one line, every
violation at once.

```
   DCC export ──► gate ──► verdict ──► stdout / exit code   (the artist, right now)
                    │                  ├─► telemetry ──► AssetHealth   (the team, over time)
                    │                  └─► toast         (ambient, while watching)
                    └─► heal (opt-in, verified before it touches anything)

   GLB/OBJ ─────────────────────────► Art Workstation  (geometry, LOD, collision, notes)
```

**Design rules that show up everywhere below:**

- The gate **fails loudly and never guesses**: unknown config keys are a hard
  error, a typo'd category does not silently fall back to the project default.
- Nothing is modified unless explicitly asked for and then *verified*.
- The web surfaces are **read-mostly**; the numbers come from real verdicts.
- Zero frameworks. `trimesh` + `Pillow` for the gate; everything else is stdlib.

---

## 2. Core concepts

### The verdict

Every check produces one `Verdict` per asset:

| Field | Meaning |
|---|---|
| `status` | `PASSED`, `HEALED` (failed, repaired, re-verified) or `FAILED` |
| `problems` | **all** violations for this asset, as readable sentences |
| `metric` | the headline number, e.g. `12,480 tris / 4 mat` |
| `stats` | structured numbers for machines (triangles, width, height, slots…) |
| `warnings` | non-fatal advisories — never change `status` or the exit code |
| `modifications` | what healing changed, if anything |

One asset = one line = every problem. That is deliberate: fixing violations one
at a time, discovering the next only after re-exporting, is the loop this tool
exists to end.

### Exit codes

| Code | Meaning | Use in CI |
|---|---|---|
| `0` | every asset passed (or was healed and verified) | continue |
| `1` | at least one asset failed | block |
| `2` | bad setup: missing dependency, bad path, invalid `rules.toml` | block, and tell a human |

**Never** `|| true` the gate.

### Budget precedence

Highest wins:

```
CLI flag  >  central/remote rules  >  [platforms.<name>]  >  rules.toml  >  built-in defaults
```

Why this order: the artist's folder stays authoritative day to day; CI can
tighten one number for one run; a studio can centrally own the profile without
taking the file away from the artist.

### Checks available

| Check | Default | Notes |
|---|---|---|
| Triangles per mesh | on, 50,000 | the headline budget |
| Material slots (draw calls) | on, 3 | counted from the **source file**, not trimesh's per-primitive split |
| Texture edge in pixels | on, 2,048 | longest edge |
| Power-of-two textures | on | platforms want it; toggle with `power_of_two` |
| Texture name suffixes | on | `_Albedo`, `_Normal`, `_Roughness`, configurable |
| PBR albedo range | on | pure black/white albedo is a bug, not a style |
| Broken references | **on** | `.gltf` pointing at a texture nobody shipped |
| Degenerate triangles | **on** (zero-area = fail) | poison for Nanite and ray tracing |
| Bone influences per vertex | on, 4 | mobile/web GPU limit |
| UV channels | on, 4 | engines stop at 4–8 |
| Unbaked transforms / pivots | **off** | opt-in: a door hinged at its edge is centred wrong *by design* |
| Unindexed geometry | **off** | opt-in, implies the transform audit |

"Off" is not laziness — it is the difference between a gate people leave running
and a gate people switch off.

---

## 3. Install and verify

### A. Double-click (recommended for the whole studio)

Install once, then never open a terminal again:

```bat
Start AssetGatekeeper.bat
```

It finds Python, starts the admin page **and** the 3D review screen as detached
background services (auto port fallback, logs in `.agk\logs`), and opens your
browser on the admin dashboard. Running it again while they are up just re-opens
the page. `Stop.bat` (double-click) stops them; `Allow_Folder.bat` lets the
page open another folder.

To let the project picker reach folders outside this repo, set once:

```bat
setx AGK_PROJECT_ROOTS "D:\Projects;C:\Work\MyGame"
```

### B. From a wheel (for one machine, with a terminal)

```bat
python -m venv .venv
.venv\Scripts\activate
pip install asset_gatekeeper-1.0.0-py3-none-any.whl
asset-gatekeeper --version
```

Gives you four commands: `asset-gatekeeper`, `asset-health`,
`art-workstation`, `art-workstation-upload`.

### C. From a checkout (for a team, CI and hooks)

```bat
git clone <repo> && cd asset-gatekeeper
python -m venv .venv
.venv\Scripts\activate
pip install trimesh pillow
python tests\test_gatekeeper.py && python tests\test_server.py && python tests\test_workstation.py
```

Expected: `FAILURES: none` from each (857 assertions total).

### D. Verify the machine

```bat
asset-gatekeeper --doctor
```

Checks Python, `trimesh`, `Pillow`, optional `watchdog`, the target folder, the
`rules.toml` it would actually use (printing the parsed budget and tag list),
and whether ports 8080/8100 are free. Every failing line prints the fix. Exit
code `1` means a real problem; a busy port is only a warning.

> On Windows, `python` is often 2.7 or a stub. Use `py -3` or the venv's
> `Scripts\python.exe` everywhere.

### E. Let the wizard write a starter config

```bat
asset-gatekeeper "D:\Project\Assets" --init
```

Five questions with defaults; each answer is also a flag (`--triangles`,
`--max-texture`, `--max-materials`, `--platform`), and `--yes` accepts them all
so it never blocks a script.

---

## 4. The gate: every flag

Run `asset-gatekeeper --help` for this list on your build.

### Budget overrides (beat `rules.toml`)

| Flag | Effect |
|---|---|
| `--triangles N` | max triangles per mesh |
| `--max-materials N` | draw-call proxy (material slots) |
| `--max-texture EDGE` | longest texture edge |
| `--max-influences N` | bone weights per vertex |
| `--max-uvs N` | UV channel count |
| `--allow-degenerate` | stop failing zero-area triangles |
| `--allow-npot` | accept non-power-of-two textures |
| `--no-naming` | accept texture names without a standard suffix |
| `--no-reference-check` | don't fail missing texture/buffer links |
| `--no-pbr-check` | don't clamp albedo luminance |
| `--audit-transforms` | flag unbaked transforms, uncentred pivots, triangle soup |
| `--require-indexed` | also *fail* unindexed geometry (implies the audit) |
| `--preset NAME` | shipped preset: `mobile`, `pc`, `retro` |
| `--platform NAME` | apply `[platforms.<NAME>]` from `rules.toml` |
| `--rules PATH` | use this file instead of `<folder>/rules.toml` (TOML or JSON) |

### Categories

| Flag | Effect |
|---|---|
| `--tag-mode auto\|suggest\|strict` | what to do with a near-miss category name |

### Scope and mode

| Flag | Effect |
|---|---|
| `--once` | scan what's there and exit (default is watch) |
| `--recursive` | include subfolders |
| `--json` | one JSON object per asset on stdout (for DCC plugins and your own scripts) |
| `--exclude GLOB` | ignore matching files (repeatable; same as `[filters] exclude`) |
| `-v` / `--verbose` | debug logging |
| `--no-color` | plain output for logs |

### What the review screen accepts

Anything a DCC exports, not only `.obj`/`.glb`:

* **Measured**: every format the installed trimesh can open (`.obj`, `.gltf`,
  `.glb`, `.ply`, `.stl`, `.dae`, `.3mf`, ...).
* **Stored with dimensions**: textures - `.png`, `.tga`, `.dds`, `.exr`, `.hdr`,
  `.ktx`, `.ktx2`, ...
* **Stored without a 3D preview**: `.fbx`, `.abc`, `.usd`/`.usdz`, `.blend`,
  `.max`, `.ma`, `.mb`, `.lwo`, `.mdl`, ... The geometry is not parsed, but the
  asset is reviewable, its notes and verdict survive, and the reason is shown.

Only executables (`.exe`, `.dll`, `.ps1`, ...) are refused, with 415. A mesh that
fails to parse degrades to "unverified" instead of being dropped - the difference
between a review tool and a filter.

Unverified assets are stored but **never counted as zero triangles**. Their
count is unknown, not small, so:

* the library header and `GET /api/stats` add up measured assets only and report
  `measured` / `unverified` next to the total;
* the asset card and the status bar say `unmeasured` rather than `0 tris`;
* the printable report shows `not measured` and `UNVERIFIED`, and states that no
  triangle or slot budget applies until the file is exported to a mesh format.

A scene of `.blend` sources therefore cannot make the project look cheap, and a
budget review cannot quietly miss the heaviest asset in the library.

When the dashboard sends a `.gltf`, the sibling files that make it loadable
(`.bin`, textures sharing the stem) are copied beside it, up to 20 files and
32 MB each, so the preview is not a blank box.

### Safety and repair

| Flag | Effect |
|---|---|
| `--auto-heal` | repair failing assets in place, verified before adoption |
| `--no-auto-heal` | force healing off even if `rules.toml` enables it (use in CI/hooks) |

### Wiring

| Flag | Effect |
|---|---|
| `--telemetry-url URL` | POST every verdict here (or `$ASSET_GATEKEEPER_SERVER_URL`) |
| `--remote-rules URL` | fetch the budget centrally, cached for offline (or `$ASSET_GATEKEEPER_REMOTE_RULES`) |
| `--notify` / `--no-notify` | desktop toasts (default: on while watching) |
| `--notify-max N` | toast rate limit per minute (default 6) |

### Utility

| Flag | Effect |
|---|---|
| `--doctor` | machine check, then exit |
| `--init` `--yes` `--force` | write a starter `rules.toml` |
| `--wizard` · `--wizard-answers FILE` | write a `rules.toml` from six questions about the project (several engines, tools and platforms at once) |
| `--audit` | whole-folder report: broken references + orphaned textures |
| `--version` | print the version |
| `--list-presets` | print the ten hardware targets and their budgets, then exit |

### Behaviour worth knowing

- **Watch mode** (no `--once`): events are only *enqueued* by the observer
  thread; one worker waits for a file to settle (350 ms quiet **and** openable;
  zero-byte files get a 1.5 s grace for exporters that create then fill) and
  only then inspects it. Duplicates are suppressed by `(mtime, size)`.
- **Never analysed on the observer thread** — a slow `trimesh` load can't make
  the watcher drop events.
- **Colour** appears only on a TTY (and Windows VT is enabled at runtime); pipes
  and log files get plain text automatically.
- **`--json` routes logs to stderr**, so stdout stays a clean data channel.

---

## 5. rules.toml: complete reference

Drop `rules.toml` next to the assets; the gate finds it. Every key is optional —
you only state what differs from the defaults. `--rules rules.json` works too
(same structure as JSON).

```toml
platform = ""            # which [platforms.*] to apply by default

# ---------------------------------------------------------------- mesh
[mesh]
max_triangles      = 50000
max_material_slots = 3
max_bone_influences = 4
max_uv_channels    = 4
allow_degenerate   = false
degenerate_ratio   = 1e-06
degenerate_epsilon = 0.0
check_references   = true      # broken texture/buffer links fail the asset
audit_transforms   = false     # unbaked transforms, uncentred pivots (opt-in)
require_indexed    = false     # fail triangle soup (opt-in)
pivot_tolerance    = 0.05      # fraction of the bounding-box diagonal
transform_epsilon  = 0.0001    # metres; below this a transform counts as baked

# ------------------------------------------------------------- texture
[texture]
max_edge     = 2048
power_of_two = true
naming       = true
suffixes     = ["_Albedo", "_Normal", "_Roughness"]
pbr          = true
pbr_floor    = 0.02            # 5/255, mean Rec.709 luminance
pbr_ceiling  = 0.94            # 240/255
pbr_suffixes = ["_Albedo", "_Diffuse"]

# -------------------------------------------------------------- filters
[filters]
exclude = ["preview_*", "*_src4x.png", "*_c.png"]   # not shipped assets

# -------------------------------------------------------------- tagging
[tagging]
mode           = "suggest"   # auto | suggest | strict
min_similarity = 0.75        # typo tolerance for category names

# ------------------------------------------------------- categories
[tags.boss]
max_triangles      = 10000
max_material_slots = 8

[tags.normal_enemy]
max_triangles = 20000

[tags.wastes_rock.texture]     # nested section: tighten one aspect only
pbr_floor = 0.05

# ------------------------------------------------------------ platforms
[platforms.mobile]
max_triangles      = 15000
max_material_slots = 2
max_texture_edge   = 1024

[platforms.mobile.texture]
pbr_floor = 0.05

# --------------------------------------------------------------- server
[server]
# url = "https://studio/api/telemetry"          # prefer the env var
# remote_rules_url = "https://studio/api/rules"
# token = ""                                     # prefer ASSET_GATEKEEPER_TOKEN

# --------------------------------------------------------------- healer
[healer]
enabled      = false          # the only safe default
textures     = true           # NPOT padding, missing suffixes
meshes       = true           # zero-area .obj faces
texture_mode = "pad"          # "pad" (no data loss) or "resize" (LANCZOS)
```

### Rules about the rules file

- **Unknown key = exit 2.** A typo that silently enforces defaults makes a gate
  look like it works. This is the single most important safety property of the
  config format.
- **Categories and platforms may only *tighten*.** Allowed: `max_triangles`,
  `max_material_slots`, `max_texture_edge`, `max_bone_influences`,
  `max_uv_channels`, plus nested `[*.mesh] pivot_tolerance/transform_epsilon`
  and `[*.texture] pbr_floor/pbr_ceiling`. On/off switches are refused, so no
  category can ship unaudited art by declaring itself special.
- **Presets** (`mobile`, `pc`, `retro`) live in code, not in your file, so a
  project file can't quietly degrade them. `--preset` beats the file; an
  explicit numeric flag beats the preset.

---

## 6. Categories and tags

One project, many kinds of asset, each with its own ceiling — without a second
config file.

```toml
[mesh]
max_triangles = 2000            # the default for anything uncategorised

[tags.boss]
max_triangles = 10000

[tags.normal_enemy]
max_triangles = 20000
```

### How an asset is categorised

Two mechanisms, no bookkeeping, evaluated in this order:

1. **Filename prefix** — first `_`-separated token: `Boss_statue.glb` → `boss`.
2. **Full filename** — `Boss_statue` as a whole.
3. **Ancestor folders**, nearest first, up to 4 levels:
   `Assets/Boss/statue.glb` → `boss`; `Assets/Enemies/Boss/x.glb` → `boss`
   (nearest wins), not `enemies`.

Names are normalised — lowercase, with `_`, `-` and spaces treated as the same
character. So `[tags.normal_enemy]`, `Normal Enemy/` and `Normal-Enemy/` are one
category. (The dashboard and the rules panel show the normalised form, because
that is the string the matcher compares against.)

### Naming a category

A category is **the folder name or the filename prefix itself**, never a pattern:

| You want to gate | Type in *Add category* | Becomes | Matches |
|---|---|---|---|
| `Assets\Actors\*` | `actors` | `[tags.actors]` | the `actors` folder, or `Actors_x.glb` |
| `Assets\Enemies\Normal\*` | `normal_enemy` | `[tags.normal_enemy]` | `normal_enemy` / `Normal Enemy` / `Normal-Enemy` |
| one specific mesh | `w01`, with the mesh named `W01_room.glb` | `[tags.w01]` | that prefix only |

`[actors.*]`, `actors/*`, `*.glb` and other pattern syntax are **not** valid: the
matcher compares whole names. Brackets belong to the TOML header
(`[tags.actors]`), which the admin page writes for you - you supply `actors`. A
tag that cannot match anything would be a silent hole in the gate, so the page
refuses one and suggests the plain name.

Names are normalised (lowercase; `_`, `-` and space are one character), so
`normal_enemy`, `Normal-Enemy` and `Normal Enemy` are the same category.

### Typos are not forgiven silently

`tagging.mode`:

| Mode | Behaviour | Use when |
|---|---|---|
| `suggest` *(default)* | binds the closest category **and warns** | day one, naming still in flux |
| `auto` | binds and logs quietly | naming is settled, warnings are noise |
| `strict` | **refuses** the guess and fails the asset with the fix | the naming convention is law |

This exists because silently falling back to the project default is exactly how
a 10k-triangle boss sails through a 2k budget. `min_similarity` (0.75) tunes how
close a name has to be.

### Visibility

- The gate prints `tags boss, normal_enemy (suggest matching, 0.75 similarity)` at startup.
- `asset-gatekeeper --doctor` lists them with their limits.
- The Workstation's **📋 Rules & Budgets** tab renders them as table rows.
- `GET /api/rules` serves them as JSON (see §11).

### What a category cannot do

- **It cannot target one file.** Use a unique prefix (`[tags.w01]` +
  `W01_room.glb`) or its own folder. There is no per-file budget list.
- **It cannot switch checks off** — only tighten thresholds (§5).
- **It cannot combine**: one asset resolves to one category, the most specific.

---

## 7. Healing

Opt-in, verified, and deliberately narrow.

```bat
asset-gatekeeper "…\Assets" --recursive --auto-heal
```

or `[healer] enabled = true`. Hooks and CI always pass `--no-auto-heal`.

### The protocol

1. The asset fails.
2. A repair is staged **beside** the original as `.agk-heal-*`.
3. The staged file is re-inspected **with the same budget**.
4. Only if it now passes is it adopted, atomically (`os.replace`).
5. Otherwise the staging file is deleted and the original stays **byte-identical**.

### What can be healed

| Target | Repair | Why it's safe |
|---|---|---|
| `.png` | missing `_Albedo`/`_Normal`/`_Roughness` suffix → rename | reversible, meaning unchanged |
| `.png` | non-power-of-two → pad (default) or resize (LANCZOS) | padding replicates edge pixels and loses nothing |
| `.obj` | delete zero-area faces | a face that renders no pixels costs nothing to remove |

### What is deliberately never healed

Triangle budgets (decimation destroys intent), material merging (changes
shading), texture resolution (throws away detail), and **any glTF/glb geometry
surgery** — rewriting accessors and buffers blind is how you ship a corrupt
file. Those keep failing, which is the safe answer.

---

## 8. Audit mode

```bat
asset-gatekeeper "…\Assets" --recursive --audit
```

A **report**, not a gate: no budget needed, no assets touched.

Finds broken references (a `.gltf` pointing at a texture nobody shipped) and
**orphaned textures** (files nothing references). Use it when taking over a
project, before a milestone, or when disk usage smells wrong.

---

## 9. Telemetry and central rules

Both are **off unless configured**, both are non-blocking.

### Push verdicts to AssetHealth

```bat
asset-gatekeeper "…\Assets" --recursive --once ^
  --telemetry-url http://127.0.0.1:8080/api/telemetry
```

or set it once per machine:

```bat
set ASSET_GATEKEEPER_SERVER_URL=http://127.0.0.1:8080/api/telemetry
set ASSET_GATEKEEPER_TOKEN=<project token>       :: if the server is token-protected
```

Behaviour: one JSON object per verdict, 2 s timeout, background thread, bounded
queue. A dead dashboard **cannot** slow down or fail a gate — failures are a
debug log line (`telemetry: 0 sent, 1 failed`).

### Pull the budget centrally

```bat
asset-gatekeeper "…\Assets" --remote-rules http://127.0.0.1:8080/api/rules?project_id=1
```

The fetched document has the same shape as `rules.toml` (so there is no second
format to learn) and is **cached** to `.rules.cache.json`: an outage degrades to
the last known-good profile instead of stopping artists.

Round trip that matters: a producer edits the budget in the **budget planner**
(§11), the gate picks it up on the next run — nobody edits a TOML on a share.

---

## 10. Integrations: DCC, VCS, CI, Python

### DCC plugins (`plugins/`)

| Host | File | What it does |
|---|---|---|
| Blender | `plugins/blender/addon.py` | the verdict **inside the viewport, before export** — you never leave the host to find out |
| Houdini | `plugins/houdini/sop_gatekeeper.py` | Python SOP: a warning badge per cook as a procedural graph multiplies geometry; **never** breaks the cook |
| Maya | `plugins/maya/gatekeeper_maya.py` + `userSetup.py` | in-scene validation, no export round trip: queries the live scene |
| Unreal | `plugins/unreal/import_gatekeeper.py` + `init_unreal.py` | blocking import wrapper — drag-and-drop can only be *warned about* from Python, so imports are routed through the wrapper |

They shell out to the gate using **`AGK_PYTHON`** — set it to the interpreter
that has `trimesh`/`Pillow`. The Houdini hook refuses to use Houdini's own
interpreter on purpose (its embedded Python is not yours to pollute).

### VCS hooks (`vcs_hooks/`)

| Hook | Install | Behaviour |
|---|---|---|
| `git_pre_commit.py` | copy to `.git/hooks/pre-commit` | gates staged assets; blocks the commit |
| `git_pre_push.sh` | copy to `.git/hooks/pre-push` | gates every commit on the branch |
| `p4_pre_submit.py` | Perforce submit trigger | gates the changelist; fails closed |

All three pass `--no-auto-heal`: a hook that rewrites binaries during a commit
is a support ticket waiting to happen. They **fail closed** — an unreadable
changelist or a fetch failure blocks the submit.

### CI (`.github/workflows/`)

| File | Purpose |
|---|---|
| `ci.yml` | the project's own tests (pyflakes, three suites, `node --check`, integration) |
| `asset_gatekeeper.yml` | drop-in PR gate for **your** asset repo |
| `dcc-nightly.yml` | headless Blender/Unreal export → gate, nightly |

### Python API

```python
import asset_gatekeeper as agk

gate = agk.Gatekeeper(agk.Budget(max_triangles=1500))
verdict = gate.check(path)          # one asset
verdict.passed, verdict.problems, verdict.stats, verdict.metric
verdict.status                      # PASSED / HEALED / FAILED

# with categories:
gate = agk.Gatekeeper(budget, tag_budgets=agk.build_tag_budgets(document),
                      tag_mode="suggest", tag_root=assets_root)

# the read-only config snapshot the web UIs use:
agk.describe_rules(assets_root, "rules.toml")
```

This is how the servers and the DCC hooks work internally — no separate code
path, so the tool and the UI can never disagree.

---

## 11. AssetHealth: the dashboard

```bat
asset-health --port 8080 --rules "D:\Project\Assets\rules.toml"
```

Or let `tools\agk-serve.bat up` start this and the workstation together on free ports
([§15](#15-operations)); `--port-auto` moves instead of failing.

Open <http://127.0.0.1:8080/>. Loopback-only by default: exposing a review
screen on the studio LAN is an explicit act (`--host 0.0.0.0`), not a default.

### Pages and endpoints

| Route | What |
|---|---|
| `/` → `/dashboard` | totals, pass rate, recent failures, bottlenecks, alert state |
| `/budget-planner` | edit a project's published budget |
| `/api/summary` | JSON totals + bottlenecks + alert state |
| `/api/telemetry` | `POST` one verdict (this is what `--telemetry-url` calls) |
| `/api/rules` | the read-only config snapshot; **with `?project_id`** the published budget in `rules.toml` shape (what `--remote-rules` eats) |
| `/api/save-budget` | `POST` the planner's form |
| `/api/events` | SSE stream: verdicts as they land; keeps a live tab current |
| `/?refresh=5` | wallboard mode — auto-refresh every 5 s |

### Solo mode versus project mode

| | Solo (default) | Projects |
|---|---|---|
| When | one person, or a scratch dashboard | a team, more than one project, or anything not on loopback |
| Setup | nothing | `asset-health --add-project "MyGame"` |
| Auth | none (`open` mode) | a project token is required for writes and scoped reads |
| Isolation | one bucket | each project sees only its own rows |

`--add-project` prints the token **once** and stores only `sha256:`. A
`project_id` in a request body can never redirect a write — the token decides
which tenant you are.

### Alerts

```bat
set SLACK_WEBHOOK_URL=https://hooks.slack.com/…
asset-health --webhook-url %SLACK_WEBHOOK_URL% --webhook-provider slack
```

Slack / Discord / Teams envelopes are inferred from the URL (override with
`--webhook-provider`). Only *critical* failures alert, and alerts are throttled
to **one per project per 300 s** (`--alert-cooldown`) by a single atomic upsert,
so a hundred failing files don't become a hundred messages. Suppressed count is
visible in `/api/summary`.

### Demo data

A new database is **empty**. `--demo` loads the bundled sample set (14 verdicts)
so you can see a populated page; when present the dashboard says so in a red
banner. `--no-seed` is still accepted for old scripts.

### Changing budgets from the browser (`/admin`)

The planner edits a per-project budget row. The **admin page** edits the real
`rules.toml` — the same file the gate reads, in plain text, so the change shows
up in `git log` like any other commit.

#### Picking the project folder

The first panel on `/admin` is **Project**. Type or paste a path, or pick from
the allow-list dropdown / recent chips, then **Use this folder**. From that point
on every save writes `<project>\rules.toml` and every audit scans that folder.

There is no folder browser, because a web page cannot enumerate your disks, and
more importantly **the browser must not be able to name any path it likes**: a
dashboard that writes `rules.toml` wherever it is told is an arbitrary-file-write
primitive. So the page can only open folders inside the roots fixed at launch:

```bat
asset-health ... --allow-root "D:\Projects" --allow-root "C:\Work"
:: or, for the double-click workflow:
setx AGK_PROJECT_ROOTS "D:\Projects;C:\Work"
```

Paths are resolved before comparison (`..` and symlinks are not a way around the
list), the chosen folder is remembered in `project_roots` for the recent chips,
and every switch is written to the audit trail as `project: old → new`.

Both buttons say exactly what they do:

- **Save only** — validate, write, stop.
- **Save & Run Audit** — validate, write into the project, audit each changed
  key, then scan the project and stream verdicts into the page.

```bat
asset-health --add-admin alice --admin-role director   :: prints the password ONCE
asset-health --list-admins
asset-health --port 8080 --rules "D:\Project\Assets\rules.toml" ^
             --scan-root "D:\Project\Assets"              :: folder 'Apply & Rescan' scans
```

Then <http://127.0.0.1:8080/admin>.

| Role | May read | May change budgets |
|---|---|---|
| `director` | yes | yes |
| `producer` | yes | yes |
| `artist` | yes | no — read-only, and told why |

**The page never edits while you type.** Sliders that rescan on every keystroke
are a way to make a shared machine unusable, so both actions are explicit.

On save the server:

1. merges your form over the current file, **keeping sections the form does not
   show** (`[filters]`, `[platforms]`, `[server]`, `[healer]` survive an apply);
2. refuses anything the gate would refuse (unknown keys, bad numbers, a
   `platform` name with no matching `[platforms.<name>]` table) — a refused
   document never reaches the disk;
3. writes atomically (temp file + `fsync` + `os.replace`), so a reader sees the
   old file or the new one, never half of each;
4. records one audit row per changed key — timestamp, user, role, the dotted key
   (`tags.boss.max_triangles`), and both old and new values;
5. only then starts the scan, one at a time (a second request gets `409`), over
the active project folder and posting telemetry back to the port that ran it.

**Platform presets.** The dropdown offers ten hardware targets — N64, PS1,
Nintendo DS, GameCube, Xbox, PS2, 3DS, Wii, PS3 and Modern PC — plus the legacy
`retro`/`mobile` aliases. Each carries a character budget, a prop budget, a
texture edge, material slots and the texture policy of that machine. Choosing one
**fills the numbers**; the file stores the numbers, not the preset's name, so a
later hand-edit to `[mesh]` is never silently overridden on the next run. Inspect
the catalogue without a browser:

```bat
asset-gatekeeper --list-presets
```

**Security, stated plainly.** Passwords are PBKDF2-HMAC-SHA256 with a per-user
salt and 120,000 rounds; only the hash is stored. Sessions keep the *hash* of the
cookie token, expire after 12 hours, are `HttpOnly; SameSite=Strict`, and every
state-changing request needs a session-bound CSRF token plus a same-origin
`Origin`/`Referer`. Failed sign-ins are throttled per IP. `/static/` on this
server is an allow-list of three files, so it cannot be used to read the disk.

The honest limit: this is authentication for a **loopback** dashboard. If you
bind `--host 0.0.0.0`, put it behind TLS (a reverse proxy) — roles cannot protect
a password sent in clear text, and a director's token on a shared LAN is only as
private as the network.

---

## 12. Art Workstation: the 3D review screen

```bat
art-workstation --port 8100 --rules "D:\Project\Assets\rules.toml"
```

Or `tools\agk-serve.bat up`, which starts both services and opens the dashboard
([§15](#15-operations)). `--port-auto` moves to the next free port instead of
failing.

Open <http://127.0.0.1:8100/viewer>.

### Getting assets in

| Path | Command | Use |
|---|---|---|
| Drag and drop | drop a `.glb`/`.gltf`/`.obj` on the page | one asset, quickest |
| Upload button | top right | one asset |
| Bulk upload | `art-workstation-upload "…\Assets" --recursive --skip-uploaded` | a whole project |
| HTTP | `POST /api/assets?filename=x.glb` with the raw body | your own pipeline |

Uploads are **copied** into `workstation_data\` — your source art is never
touched. Up to 512 MB per file (`--max-upload-mb` changes it).

### What you get per asset

| Feature | Detail |
|---|---|
| Views | Textured / Geometry (flat normals) / Wireframe / Collision overlay |
| Exposure | scene-level brightness |
| Materials | per-material **exposure** and **roughness** sliders, live |
| LODs | up to three levels at 50 % / 25 % / 12.5 % of source triangles (inspection-grade: they drop UVs and materials — **do not ship them**) |
| Collision | box / sphere / convex hull; rebuild from the mesh; export as a standalone `.obj` |
| Notes | categories Geometry/Texture/LOD/Collision/Shading/Other, author, Markdown-ish body |
| Report | per-asset HTML report endpoint |
| Rules panel | **📋 Rules & Budgets** — platform, tag mode, per-tag limits, read-only, `#rules` or `?tab=rules` deep link |
| Live | SSE: a new upload appears in every open tab |

### Solo versus token mode

Default is solo (no login, loopback only). `--token` (or `$AUTH_TOKEN`) requires
a bearer token; the viewer prompts for it once and keeps it in `localStorage`.
Use token mode whenever the port is reachable by anyone else.

---

## 13. Workflows: solo versus team

### A. Solo, single worker — no servers at all

**Who:** one artist or one TA on one machine. **When:** you want the checks, not
the paperwork.

```bat
cd D:\Project\Assets
asset-gatekeeper . --once                    :: did I break anything?
asset-gatekeeper . --recursive --notify      :: while I work: watch + toasts
```

- Everything lives in one `rules.toml`, next to the assets.
- No database, no ports, no tokens. Nothing to administer.
- Heal if you like: `--auto-heal` is safe because it verifies before adopting.
- Review geometry with `art-workstation` on demand (it needs no project setup).

**He said, she said:** none — there is exactly one source of truth, the file.

### B. Solo, with your own dashboard

**Who:** the same person. **When:** you want history — "was this passing last
week?", "what do I keep failing?"

```bat
asset-health --port 8080 --rules rules.toml                        :: window 1
asset-gatekeeper . --once --telemetry-url http://127.0.0.1:8080/api/telemetry   :: window 2
```

Add `/?refresh=5` on a second monitor for a wallboard. Alerts as toasts are
already covered by `--notify`; webhooks are optional.

### C. Two people — artist + TA (co-op, minimal)

**Who:** an artist exporting, a TA owning the budget. **When:** the budget needs
a human decision but a whole dashboard is overkill.

| Who | Does | Shares |
|---|---|---|
| TA | owns `rules.toml` in the repo (reviewed like code) | the file, via VCS |
| Artist | runs the gate locally, or the DCC plugin does it on export | — |
| Both | read the same verdicts in the same PR | CI output |

```bat
:: TA, once per repo
copy vcs_hooks\git_pre_commit.py .git\hooks\pre-commit
```

The gate runs on commit, the PR shows the failure, the artist fixes or the TA
adjusts the budget — in the PR, where the argument belongs.

### D. Team with a shared dashboard (the usual studio setup)

**Who:** several artists, one TA or producer who watches the numbers.

1. **Server** (one machine on the LAN):
   ```bat
   asset-health --host 0.0.0.0 --port 8080 --db D:\agk\health.db ^
                --token %ADMIN_TOKEN% --rules "\\share\project\rules.toml" ^
                --webhook-url %SLACK_WEBHOOK_URL%
   ```
2. **One project row per project** (prints the token once):
   ```bat
   asset-health --db D:\agk\health.db --add-project "MyGame"
   ```
3. **Every artist** sets, once:
   ```bat
   set ASSET_GATEKEEPER_SERVER_URL=http://studio:8080/api/telemetry
   set ASSET_GATEKEEPER_TOKEN=<that token>
   ```
4. **The budget** is published from the planner and consumed centrally:
   ```bat
   asset-gatekeeper . --remote-rules http://studio:8080/api/rules?project_id=1
   ```
5. **Hooks** block bad commits; **CI** blocks bad merges; the dashboard shows who
   is blocking what.

What each surface is for in this mode: the **artist** needs exit codes and
toasts; the **TA** needs the recent failures and bottlenecks; the **producer**
needs the pass rate and the Slack alerts; **nobody** needs to read a TOML by hand.

### E. Studio with central control

Same as D, plus: `rules.toml` is not distributed at all — the gate fetches
`--remote-rules`, caches it (`.rules.cache.json`), and works offline from the
last good copy. Budget changes are one click in the planner, and every artist
picks it up on their next run.

### Which one am I?

| You have… | Use |
|---|---|
| one machine, no plans to share | **A** |
| one machine, want history | **B** |
| one repo, two people, budget disputes | **C** |
| a team, one studio network | **D** |
| several projects, or a rule you must not edit locally | **E** |

Rule of thumb: **start at A.** Move to C or D the first time two people disagree
about whether an asset passes — that is the signal that a shared verdict is worth
a server.

---

## 14. Daily recipes

**First day on a project (read-only, 10 minutes)**

```bat
asset-gatekeeper --doctor
asset-gatekeeper "…\Assets" --recursive --audit          :: what is broken already
asset-gatekeeper "…\Assets" --recursive --once -v        :: what would fail today
```

Then write the budget (`--init`), agree it with whoever owns the platform, commit it.

**Every export**

Nothing to remember: the DCC plugin or the pre-commit hook runs the gate. If you
work outside a host, `asset-gatekeeper . --once`.

**Before a milestone**

```bat
asset-gatekeeper "…\Assets" --recursive --once --json > verdicts.json
asset-gatekeeper "…\Assets" --recursive --audit
```

**Changing the budget (whoever owns it)**

```bat
asset-health --add-admin alice --admin-role director    :: once; password shown once
:: then http://127.0.0.1:8080/admin  ->  pick a preset, edit, Apply & Rescan
```

Every change is written atomically to `rules.toml`, committed like any other
file, and appended to the audit trail with old and new values.

### Browsing the whole history

The dashboard trims to a top-N; these two pages do not.

* /audit/full - every verdict, 50 per page, newest first.
* /audit/passes - the same list locked to passes.

Both take ?status=all|pass|fail, repeatable ?tag=<category>, and ?page=N.
Rows are grouped into collapsible <details> blocks per category (with an
Uncategorised group), the filter bar offers a multi-select category picker, and
every row keeps its **Send to Workstation** button.

Tag filtering happens after the rows are loaded because categories are derived
from the path rather than stored per verdict - bounded at 20,000 rows so a huge
history cannot exhaust memory. Pagination is applied after filtering, so page
counts are always truthful.

### Sending a single asset to the review screen

Every row in the dashboard's failure list - and in "Recently passed" - carries a
**Send to Workstation** button. It ingests *that one file* (not the folder), with
its verdict, the exact rule violations and the categories that applied, and the
review screen shows them as a badge, a "Gate verdict" panel and a status filter.

How it is wired matters if you plan to run the two services apart:

* The dashboard posts the bytes **server-to-server** to the workstation. The
  browser never talks to the other origin, so there is no CORS surface and no
  workstation token in the page.
* It finds the workstation through `--workstation-url`, `$AGK_WORKSTATION_URL`, or
  the `.agk/ports.json` the launcher writes (so port fallback cannot point it at a
  stale port).
* **Ten assets is the ceiling**, enforced in the server, not just the page: a
  stuck click cannot queue the review machine. The bulk uploader CLI keeps its own
  escape for deliberate folder loads.
* A verdict whose file is no longer reachable fails on its own with a message -
  it never half-imports a batch.

**Weekly review**

Dashboard → recent failures (who/what is repeatedly failing) → bottlenecks (which
rule is everyone hitting) → if a bottleneck is systemic, fix the budget or the
pipeline, not the artists.

**Onboarding an artist**

`--doctor`, then one `--once` run, then show them the toasts and the review
screen. That is the whole training.

---

## 15. Operations

### Ports and processes

| Service | Default | Note |
|---|---|---|
| AssetHealth | 8080 | loopback only unless `--host 0.0.0.0` |
| Art Workstation | 8100 | same |

### One command for both services (recommended)

```bat
tools\agk-serve.bat up          :: start both, detached, print the URLs
tools\agk-serve.bat up --open   :: ...and open the dashboard
tools\agk-serve.bat status      :: running? which port? which pid?
tools\agk-serve.bat urls        :: URLs + the exact gate command
tools\agk-serve.bat logs        :: recent server output
tools\agk-serve.bat down        :: stop both (Stop.bat does the same)
```

(or `python tools/agk_serve.py up`). No windows to keep alive: the servers run
**detached** with their output in `.agk/logs/`, and `up` is idempotent — if they
are already healthy it says so and exits 0.

The part that matters: `up` resolves the ports **first**, writes them to
`.agk/ports.json`, and prints the gate command that actually matches them
(`… --telemetry-url http://127.0.0.1:8081/api/telemetry`). If you prefer to run
the servers yourself, that file is still the record of where they are.

`down` refuses to kill a pid whose command line is not one of our servers: a
stale `ports.json` from a previous boot must never take out an unrelated process
that has since inherited the pid.

### Automatic port fallback

Both servers accept:

```bat
asset-health --port-auto            :: 8080 busy -> 8081 -> 8082 … (prints which)
art-workstation --port-auto --port-tries 20
```

It is **off by default**, on purpose, and here is the honest reasoning:

- **The dangerous failure is silent and it is about telemetry, not binding.**
  If the dashboard moves to 8081 while every artist's `--telemetry-url` still
  says 8080, verdicts land wherever 8080 now points — a stale server, another
  project, or nowhere. A startup error is annoying for ten seconds; wrong
  metrics are wrong for a milestone. So the *default* is an error plus the
  commands to fix the port, and the launcher is the path that moves ports
  **and tells every consumer the new URL**.
- On a successful move the server prints `note: port 8080 is busy, using 8081`,
  and reprints the telemetry URL for this run.
- Fallback only walks forward on a *busy port*. A missing interface, a privilege
  error or a malformed host stays a hard failure — walking ten ports would bury
  the real message.
- It never takes the other service's default port: the dashboard skips 8100 and
  the workstation skips 8080.
- On Windows, a port held by another listener can report `WinError 10013
  "access permissions"` rather than 10048, and — worse — `SO_REUSEADDR` can let
  a second bind *succeed* on a port someone is already serving. That is why the
  check is a `connect()` probe **before** binding, not a try/except after it.
- A range excluded by Hyper-V/WinNAT will simply exhaust the retry budget and
  report the original error.

**When not to use it:** CI, VCS hooks, or any script that hardcodes a URL. There,
a deterministic failure is the correct outcome — let it fail and pin the port.

A busy port otherwise prints the exact command to find and stop the owner, and
suggests a spare that is neither busy nor the other service's port.

### Data and backup

| What | Where |
|---|---|
| Verdict history | `asset_health.db` (or `--db`) |
| Workstation library, notes, LODs | `art_workstation.db` + `workstation_data/` |
| Remote-rules cache | `.rules.cache.json` beside the assets |

Both databases are plain SQLite — back them up by copying the file while the
server is stopped (or use `sqlite3 file .backup`). Nothing else is stateful.

### Tokens

- Project tokens: printed once, only the hash stored. Treat them as credentials;
  put them in env vars, never in a committed `rules.toml`.
- `--token` on either server is the admin/studio-wide token.
- Rotate by `--add-project` a new one and retiring the old row.

### Performance

- Inspection is per file and parallel across the worker, not the observer: a
  1,000-file `--once` scan is dominated by parsing, not by the gate.
- On a network share, the gate reads each file once; telemetry is one HTTP POST
  per asset, off the critical path.
- `--exclude` before blaming the gate: review renders and 4x paint sources are
  usually the bulk of a folder and are not shipped assets.

---

## 16. Feature matrix

| Feature | Gate | Dashboard | Workstation | When to use |
|---|---|---|---|---|
| Triangle/material/texture budgets | ✅ | reflects | shows | always |
| Categories (`[tags.*]`) | ✅ | via `/api/rules` | panel | more than one asset type |
| Platform profiles | ✅ | via planner | panel | multi-platform project |
| Presets | ✅ | — | — | quick starting point |
| Watch mode + toasts | ✅ | — | — | while producing |
| Auto-heal (verified) | ✅ | counts HEALED | — | solo/local only |
| Broken-reference check | ✅ | in reasons | per-asset, via `asset_references()` | always |
| Audit (orphans) | ✅ | — | — | taking over a project |
| Telemetry | ✅ | ✅ | — | more than one machine |
| Central rules | ✅ | ✅ planner | — | studio-managed budget |
| Per-asset review (LOD/collision/notes) | — | — | ✅ | art review |
| Collision export | — | — | ✅ | engine hand-off |
| VCS hooks / CI | ✅ | — | — | any team |
| DCC plugins | ✅ | — | — | artists in-host |
| Alerts (Slack/Discord/Teams) | — | ✅ | — | a producer watching |
| Read-only rules inspector | ✅ | ✅ | ✅ | "what am I measured against?" |

---

## 17. Limits and non-goals

Documented on purpose, so nobody discovers them at the wrong moment:

- **No vertex budget.** The gate counts triangles; `/api/rules` reports
  `max_vertices: null` and the panel shows "—" rather than inventing a number.
- **A category is not a single asset.** No per-file budget list (yet).
- **Categories only tighten**; they can never disable a check.
- **LODs are inspection-grade.** They drop UVs and materials; shipping anything
  still needs a proper decimation pass in the DCC.
- **Healing never touches glTF/glb geometry**, and never decimates.
- **The gate does not upload or publish anything.** Telemetry is the only
  outbound call, and only if you configure it.
- **No asset registry.** It reads the files it is pointed at; it does not track
  where they came from.
- **Room-scale composition, shaders and animation are out of scope** — this is a
  technical-budget gate, not a review suite for art direction.

---

## 18. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `cannot bind … in use` | another process owns the port; the message prints the exact find/kill command |
| dashboard shows demo numbers | it was started with `--demo`; the banner says so — use a fresh `--db` |
| `/viewer` blank, tabs dead | a JS syntax error blanks the whole module — check the browser console |
| `unknown key` / exit 2 | typo in `rules.toml`; compare with `asset_gatekeeper.example.toml` |
| an asset passes that shouldn't | read the `budget` line the gate prints first: it names the file it used |
| the budget I edited has no effect | precedence: a platform, a preset, `--remote-rules` or a CLI flag may be winning |
| a category isn't applied | `--tag-mode strict`? Is the folder within 4 levels, or the prefix before the first `_`? `--doctor` lists the categories |
| telemetry silently does nothing | `telemetry: 0 sent, 1 failed` in `-v` output; check URL/token; a failure never blocks the gate |
| non-ASCII filenames crash *your* script | reconfigure stdout/stderr to UTF-8 (the tools already do) |
| healing changed nothing | it only repairs NPOT/suffix/zero-area; everything else stays failed by design |

---

## 19. Repository map

```
asset_gatekeeper.py            the gate (single file), 4 entry points documented in §4
healer.py                      opt-in repair engine, loaded only when healing is on
notifier.py                    native toasts (WinRT/balloon, notify-send, osascript)
asset_gatekeeper.example.toml  annotated rules.toml template
QUICKSTART.md                  the 5-minute path
MANUAL.md                      this file

server/asset_health_server.py  dashboard, telemetry ingest, alerts, planner, /api/rules
server/art_workstation_server.py  viewer, uploads, LODs, collision, notes, reports
server/static/                 viewer.html, viewer.js, app.css (no frameworks)
tools/agk_serve.py             up/down/status/urls/logs for both services
tools\agk-serve.bat                  one-click wrapper for the above

engine/lod_manager.py          vertex-clustering decimator (inspection-grade)
plugins/{blender,houdini,maya,unreal}/   DCC hooks
vcs_hooks/                     git pre-commit, git pre-push, P4 submit trigger
tools/                         upload_assets.py, agk.bat, upload.bat, houdini hook
tests/                         857 assertions across four suites + headless integration
.github/workflows/             ci, drop-in PR gate, nightly DCC export
demo/                          live_demo.py, blender_export_demo.py
dropbox/                       deliberately broken fixtures (each fails exactly one rule)
```
