# AssetGatekeeper v1.0.0 - The Indie / Small-Studio Edition

**Released:** 2026-11-01
**Tag:** `v1.0.0` **Branch:** `release/v1.x`
**Requires:** Python 3.11+ (3.9+ works; `tomllib` is bundled from 3.11)
**Ships as:** wheel, source distribution, drop-in zip

Version 1 is the first release meant to be installed and lived with, not
demoed. It is one gate, two screens and a database you already have - no
service, no account, no cloud bill. A two-person studio can put it in front of
its export folder on day one and still be using the same files a year later.

---

## Highlights

### 1. One-click triage: AssetHealth to Art Workstation

The dashboard reads any failing asset and sends it straight to the 3D review
screen - one button per row, one asset at a time, with an in-flight cap of ten
so a large audit cannot swamp the reviewer's machine.

* The bridge is **server-to-server**: AssetHealth POSTs the bytes to
  `--workstation-url` (or `$AGK_WORKSTATION_URL`, or the `.agk/ports.json`
  written by `agk-serve`). No auth token ever reaches a browser, and no CORS
  hole is opened to make it work.
* The verdict travels **with** the asset. Status, problems, tag and source are
  stored in their own columns and rendered as a "Gate verdict" panel - the
  reviewer's notes are never overwritten by machine text, so provenance stays
  honest.
* Reviewers filter the library by **All / Needs attention / Passed** without
  leaving the viewport.

```
asset-health --workstation-url http://127.0.0.1:8100
```

### 2. Tag governance that catches typos instead of punishing them

A tag is a folder name or a filename prefix - `actors`, not `[actors.*]`.
Resolution walks filename prefix, then full filename, then the nearest ancestor
folder (four levels), and names are normalised (`environment_rock` and
`environment rock` are the same tag).

The point is what happens when someone mistypes. A folder called `charcter/`
does not silently fall back to the project default and ship unaudited: the gate
recognises it as a near miss of `character`, names the rule it probably meant,
and - in strict mode - refuses the guess outright with the fix in the message.

```
[tags.character]
max_triangles = 40000
max_material_slots = 4
```

Categories can only **tighten** a budget, never switch a check off, so a tag
cannot become a loophole.

### 3. Format handling that meets the pipeline where it is

* **`.gltf` sidecars** come along. When the dashboard sends a `.gltf`, the
  `.bin` and same-stem textures are copied beside it (20 files, 32 MB each), so
  the preview is not a blank box.
* **Unverified sources are quarantined, not refused.** `.blend`, `.max`, `.ma`,
  `.fbx`, `.usd` and friends cannot be measured without the DCC that made them.
  They are stored as `kind='unverified'` with the reason attached, reviewable
  and noteable - and they are **excluded from every triangle total** rather
  than counted as 0. A shelf of `.blend` files cannot make a project look cheap,
  and the report says "not measured" instead of printing a clean pass.
* **Executables are refused, hard.** `.exe`, `.dll`, `.ps1`, `.sh`, `.msi` and
  friends return HTTP 415. An asset store is not a file share.
* Every format the installed trimesh can open is measured for real; formats it
  cannot open degrade to unverified instead of being dropped.

### 4. Zero-dependency SQLite architecture

All state - verdicts, problems, projects, budgets, alerts, admin accounts,
sessions, the rule audit trail, the 3D library, LODs, collision proxies and
review notes - lives in two SQLite files created on first run.

* Standard library only for the server, workstation, healer, LOD engine and
  launcher. The two real dependencies are `trimesh` and `Pillow`, both needed
  for measurement itself.
* One operator can move the studio by copying a folder. Backups are `copy`.
* `sqlite3` connections are opened per request and closed, WAL is on, and writes
  are serialised - the boring correctness that keeps a dashboard up for months.

---

## In the box

```
asset-gatekeeper         the gate (single file, CLI + watcher)
art-workstation          the 3D review screen (:8100)
asset-health             the metrics dashboard    (:8080)
agk-serve                launcher: ports, logs, status, urls
art-workstation-upload   bulk-load a folder of exports

templates/rules.toml     ready-to-copy default rules (retro / mobile / pc)
asset_gatekeeper.example.toml   fully annotated reference
README.md  MANUAL.md  QUICKSTART.md  RELEASE_NOTES.md  LICENSE
```

## Install

```bash
pip install asset_gatekeeper-1.0.0-py3-none-any.whl
# or, from the source tree
pip install .
```

## Sixty seconds

```bash
asset-gatekeeper --list-presets            # ten targets, budget numbers, nothing hidden
asset-gatekeeper C:\Project\assets --preset retro
asset-gatekeeper C:\Project\assets --preset pc --texture-check pbr

agk-serve up                               # dashboard :8080, workstation :8100
```

Copy `templates/rules.toml` into the project, set `platform = "mobile"`, and
every artist's export is judged the same way from then on.

## Verification

* **857 assertions** across four suites: gate 396, servers 203, workstation 135,
  admin 123. They run over real sockets on ephemeral ports, never a live server.
* `pyflakes` clean across every Python file; `node --check` clean on all three
  front-end modules.
* CI runs the same four suites plus a synthetic-DCC headless integration pass.
* The build is reproducible from a clean tree: `python tools/build_release.py`.

## Fixed before this tag

* `--preset retro` and friends were silently dropped when the folder had no
  `rules.toml`: the budget stayed at the 50,000 default and said so in a banner
  nobody reads twice. The shipped profiles now apply to a bare folder, and a
  `--platform` name with no matching `[platforms.*]` table is refused instead of
  ignored. Found by building the release and running its first documented
  command, which is the only reason it was found at all.

## Known limits, stated plainly

* `.blend`, `.max`, `.ma`, `.fbx` and `.usd` cannot be triangle-counted here.
  They are stored, reviewable and excluded from budgets - export a `.glb`
  alongside if you want a number.
* Vertex caps are read but not enforced; the rules inspector shows them as `-`
  rather than inventing a limit.
* Servers hold their code in memory: pull a new version, then
  `agk-serve down` before relaunching, or you will review with the old build.
* Windows `SO_REUSEADDR` lets a second bind appear to succeed, so port
  availability is probed with `connect()` before binding. Port fallback is
  opt-in (`--port-auto`) on purpose: a dashboard that silently moves from 8080
  to 8081 leaves every artist's telemetry posting to the wrong place.

## Compatibility

* Rules files from 0.x load unchanged; unknown keys are still refused loudly.
* Databases are migrated in place - no export/import step.
* Python 3.9 and 3.10 need `tomli` for TOML rules; 3.11+ needs nothing.
