# Since v1.1.1 (unreleased, on `main`)

* **Studio rules (roadmap phase 2).**
  * Expiring exceptions: `[[exceptions]]` in `rules.toml` (path, rule, allow,
    reason, owner, expires) relax one rule for some paths until a date; the day
    after, a still-failing asset says the exception expired. The budget planner
    publishes them with the budget; the gate reports active / expired / ending
    soon at start-up.
  * `--report junit:FILE` / `sarif:FILE` for CI; the shipped GitHub workflow
    uploads SARIF so failures are annotations on the pull request.
  * AssetHealth alerts mention the artist: `--people people.toml` maps user and
    machine names to Slack/Discord IDs or Teams emails (Teams gets an Adaptive
    Card mention), with a per-person cooldown, a `?find=` link into the Art
    Workstation, and `--alert-digest HH:MM` for one message a day.
  * Healer: normal maps are renormalised after a resize; TGA heals like PNG;
    JPG only with `[healer] jpg = true`.
  * Fixed: a verdict that carried a note lost its ERROR status (reported as
    FAILED); `--telemetry-identity` help and docs named a `path` mode that does
    not exist (the default is `host`); docs/08 described an `[alerts]` table and
    `AGK_WEBHOOK_URL` that never existed.

* **Art Workstation review workflow (roadmap phase 1).**
  * Versions: the same name (or depot path) uploaded again becomes the next
    version; the inspector lists versions with what changed and can overlay or
    put side by side the version before. Identical re-uploads add nothing.
  * Sign-off: In review / Changes requested / Approved per version, with
    history, library filters, and `GET /api/approvals/<sha256>`.
  * Named reviewers: local accounts with roles (viewer, reviewer, lead), created
    with `art-workstation --add-user`; the shared token keeps working for
    scripts but can never sign off or delete.
  * Where a file came from: depot path and changelist from the bulk uploader
    (`--depot-root`, `--changelist`), AssetHealth and the Perforce trigger.
  * Perforce trigger: `--workstation` queues passing submits for review;
    `--require-approval` refuses unapproved content (fails closed).
* **Certification:** `python tools/certify.py` runs every suite and writes a
  dated report.

* **Healing, made safe to use (audit 2026-11-02).**
  * Non-power-of-two textures are now **resized** by default, as the docs always
    said; the code padded, which shifted UVs (a marker at UV 0.5 moved 12 px on
    1000 -> 1024). `texture_mode = "pad"` still pads, if you ask for it.
  * Every adopted heal keeps the original - and every `.mtl`/`.gltf` it repointed -
    in `.agk-backups/`, and `asset-gatekeeper <file> --undo-heal` puts it back
    (refusing if anything was edited since, and naming the later heal to undo
    first). `--list-heals` shows what was healed in a folder.
  * Heal **one** asset: `asset-gatekeeper <file> --heal [--dry-run]`, against the
    project's rules (found above the file), with healing otherwise off. When it
    changes nothing it says why; before, that was only in `-v` output.
  * Art Workstation: **Check for fixes** on `.png`/`.obj` assets makes a verified
    fixed copy as a new asset; the original is untouched.
  * Folder-wide `--auto-heal` says at startup what it may change and who turned it
    on; the budget planner's switch spells out that it turns healing on for every
    machine reading the rules.
  * Fixed: a recursive watch could inspect the healer's own staged copies (a file
    check looked only at the name, not the folder); the healer's backup folder is
    skipped by scans, the audit and the reference repointing (a later rename would
    otherwise have edited the backed-up `.mtl`).
  * Fixed: the healer rewrote files in text mode. Repointing a reference turned an
    artist's LF `.mtl`/`.gltf` into CRLF on Windows (and CRLF into LF elsewhere), and
    stripping `.obj` faces also replaced any non-UTF-8 byte (a Latin-1 material
    name) with U+FFFD. Every kept line is now written back byte for byte.
  * Fixed (gate): an `.obj`/`.mtl` that is not UTF-8 - a cp1252 material name from
    an older Windows exporter - failed as "could not read mesh
    (ModuleNotFoundError ...)" unless `charset-normalizer` happened to be
    installed. It is now a declared dependency, `--doctor` checks it, and the
    failure says how to fix it.
  * Docs: README, docs/03, 05, 06, 07 and 12 corrected - `--heal-dry-run` did not
    exist, the healer never "resizes oversized" textures, and `pad` was the real
    default in 1.1.0.

# v1.1.1 (2026-11-02)

* **Art Workstation, phase 1 (audit 2026-11-02).** The viewer no longer shows
  the previous asset when a load fails; embedded glTF buffers, every LOD and
  Geometry -> Textured work; other websites can no longer write to it; collision
  export, filenames and API errors fixed. A Chromium test suite runs in CI.
* **Art Workstation, phase 4.** Generated LODs are about 17x faster (1.3M
  triangles: 12 s, was 201 s), land within 2 % of their target, never exceed a
  triangle cap the mesh can get under, and sit where the source does (glTF node
  transforms applied - also to the seeded collision box). A level that cannot
  reach its target is kept and flagged. Started on the network with no token, the
  server prints a warning, the page shows a red banner, and verdicts that arrive
  are marked "not authenticated". Phone-width layout. The inspector links the
  printable report and the original file. Non-English names no longer break the
  collision export. docs/07 rewritten to match the product.
* **Art Workstation, phase 3.** LODs can be generated from the viewer (presets,
  background job with progress), artists can upload their own LOD files (also by
  naming them `<asset>_LOD1`...), and levels can be deleted one at a time; generation
  never overwrites an uploaded level. A LOD that saves nothing over the level above
  or the source is flagged. Assets can be deleted from the viewer. The gate verdict
  panel, which was drawn underneath the collision panel and never visible, is now
  on screen.
* **Art Workstation, phase 2.** FBX is measured by the gate's reader and drawn
  through a converted `.glb`, under an on-screen warning that it is a conversion
  (`engine/fbx_preview.py`). Textures are previewed (TGA/TIF/DDS via PNG). Each
  asset has its own folder, and the files it references travel with it on every
  path - drag-and-drop, the bulk uploader (`--workstation`/`--recursive` now work
  as documented) and AssetHealth's "Send to workstation" - so a `.gltf` with a
  `.bin` arrives measured. Unreadable files are "broken", not "unverified".
* **Unreal: one message per rejected drag-and-drop.** UE 5.3.2 reports a single
  FBX drag-and-drop to the post-import hook twice. The first report moved the
  mesh to `/Game/_AssetGatekeeper_Rejected`; the second queued the same move and
  told the artist it "could NOT be moved". Repeat reports of the same asset
  within 2 seconds are now ignored, a path is queued once per tick, and an asset
  that is already gone is skipped quietly. Found in a hand test on 2026-11-02.

# AssetGatekeeper v1.1.0

**Released:** 2026-11-01  **Tag:** `v1.1.0`
**Upgrading from 1.0:** read [docs/12-UPGRADING-TO-1.1.md](docs/12-UPGRADING-TO-1.1.md) first -
several verdicts are deliberately stricter and some pipelines will turn red.

## New

* **FBX is measured.** Triangles, material slots, UV sets, bone influences,
  degenerate faces and texture references, read straight from binary **and
  ASCII** FBX (7.1+) with no Autodesk SDK. Verified against Blender's own counts
  on 13 exported scenes (`tests/fixtures/fbx`) and Unreal's on files Unreal
  exported as FBX 7.1, 7.3 and 7.7, ASCII and binary (`tests/fixtures/fbx_ue`);
  hostile files (truncated, decompression bombs, 3,000-deep nesting, broken
  ASCII arrays) fail cleanly. Pre-2011 FBX is refused with a re-export hint.
* **FBX counts what the engine imports.** Unreal collision hulls (`UCX_`,
  `UBX_`, `USP_`, `UCP_`) are not render triangles, and only LOD0 of an LOD group
  counts. Before this, Unreal's own export of a 768-triangle mesh read as 828.
* **Unreal drag-and-drop imports are rejected, not just warned about.** An
  over-budget mesh is moved to `/Game/_AssetGatekeeper_Rejected` (or deleted, or
  left with a warning: `ASSETGATEKEEPER_UNREAL_ON_FAIL`). An asset other assets
  already reference is never moved.
* **TGA and JPG textures** get the same checks as PNG (size, power of two,
  naming, PBR range, real format matches the extension).
* **Textures are fully decoded.** A truncated PNG/JPG/TGA used to pass on its
  header; it now fails. About 70 ms per 2048² texture.

## Fixed

* The three audit rounds of 1 November 2026: CLI overrides, the viewer, admin
  sign-in in real browsers, request smuggling, the Host guard, heal repointing,
  telemetry identity, the PostgreSQL backend (migrations, `--dsn`, restarts),
  `--workstation-url`/`--workstation-token` (ignored since 1.0), remote rules
  that are invalid vs unreachable (exit 2), telemetry failures shown once per run,
  and heal refusing renames that would orphan a reference outside the watched
  folder or inside a binary `.fbx`/`.glb`.
* Unreal's `import_with_gatekeeper()` fails closed instead of importing what it
  could not check.
* **Plugins tested in the real editors** (Houdini 22.0.459, Unreal 5.3.2), which
  found three bugs the stubs could not:
  * Houdini SOP counted open polylines (hair guides, Line SOP) as faces: 100
    guides of 1,000 points read as 99,800 triangles and 100 "degenerate" prims.
    Open curves are now skipped.
  * Houdini SOP's verdict now posts a yellow **warning badge** (`addWarning`)
    instead of a blue info message that was easy to miss.
  * Unreal `init_unreal.py`: the post-import check never bound in UE 5.3
    ("expected 2, got 5" arguments), so drag-and-drop imports were never checked
    while the log still said "import guard active". Fixed, and the log now says
    "NOT active" if the bind ever fails again.
  * Unreal `import_gatekeeper.py`: an editor launched from another DCC (or a
    pipeline launcher) inherited its `PYTHONHOME`, and every gated import -
    good assets included - failed "SRE module mismatch". The gate's interpreter
    now gets a cleaned environment.
* **Second real-editor round** (Houdini 22, Unreal 5.3.2, Blender 4.0.2):
  * Unreal `init_unreal.py`: both import hooks stopped firing after the first
    asset was deleted or moved, so drag-and-drop went unchecked within minutes.
    The hooks are now held and re-attached by a watchdog if the engine drops them.
    Reimports fire a separate delegate and were never checked; they are now.
  * Unreal `init_unreal.py` on an unconfigured machine logged "engine not found"
    and then called every mesh, 240k triangles included, "within budget". It now
    says "NOT checked" and tells the artist once, on screen.
  * Blender `preflight_panel.py` could not be installed as documented: no
    `bl_info` (never listed in Add-ons) and, once copied into Blender's add-ons
    folder, "No module named 'preflight_core'". It now installs, finds the
    engine through `ASSETGATEKEEPER_HOME`, and measures only when the scene
    changes (a 159k-triangle selection took 0.86 s on every sidebar redraw).
  * Blender `addon.py`: the same inherited-`PYTHONHOME` failure as Unreal; and
    the sidebar cut every reason to "159,200 tri... (+109,200)". Text now wraps.
  * Houdini SOP: a curves-only node (hair guides) no longer warns "no polygons";
    only a stream with nothing in it does.
* **Perforce trigger applies the project's budget.** It gated a temp copy of each
  file, where no `rules.toml` exists, so every submit was judged against the
  shipped defaults. It now fetches the nearest depot `rules.toml` (at `#head`).
* **Perforce trigger works on a real server.** The trigger runs inside the P4
  service, which has no `P4PORT` or login, so the documented trigger line
  refused every submit ("TCP connect to perforce:1666 failed"). New
  `--port %serverport% --p4user --tickets` (or `AGK_P4PORT`/`AGK_P4USER`/
  `AGK_P4TICKETS`) are passed to every `p4` call; README and docs/10 show the
  working line, which also corrects the README's `change-submit` to
  `change-content`. Verified on P4 Server 2026.1: 6/6 cases plus fail-closed.
* **`rules.toml` is read inside every DCC.** The engine needs `tomllib`
  (Python 3.11+); Unreal 5.3 (3.9), Blender 4.0 (3.10) and Maya 2022-2024 have
  neither it nor `tomli`, so a studio budget could not be read in-editor - in
  Unreal the error was swallowed and imports went unchecked. `tomli` 2.0.1 (MIT)
  now ships in `agk_vendor/` as the fallback; verified in Unreal 5.3.2.
* **SIEM forwarding speaks the receivers' formats.** Splunk HEC rejects anything
  but its own `{"event": ...}` envelope; the webhook sent a generic body to the
  HEC URL its own docs used. Syslog lines named no host (`-`); they now carry
  the machine name.
* **3ds Max pre-flight:** unconverted primitives were skipped and only
  Standard/Multi materials counted. Verified in 3ds Max 2027.2: the old plugin
  read 0 triangles for every primitive (an unconverted 180,000-triangle plane
  passed); the fixed one matches Max's own counts and counts Physical Material.
* **Maya material slots:** both Maya tools counted every shading group connected
  to a mesh, including the empty `initialShadingGroup` left behind by per-face
  assignment. Verified in Maya 2027: 5 face materials read as 6.
* **Maya shelf button and menu never appeared.** Maya runs `userSetup.py`
  without `__file__`, so its setup raised NameError and was skipped (a warning
  in the Script Editor was the only sign). It now finds its folder on
  `sys.path` and builds the UI once Maya is idle; verified in Maya 2027.
* **Maya install docs:** the documented `sys.path` pointed one folder too high;
  `import maya_preflight` failed.

---

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

* **1,205 assertions** across six suites: gate 444, dashboard 223, workstation 138,
  admin 123, enterprise 218, analytics 59. They run over real sockets on
  ephemeral ports, never a live server. The gate suite calls `main([...])` once
  per CLI flag, because argparse accepting a flag used to be the whole test.
* `python tools/browser_smoke.py` drives both UIs in headless Chromium over the
  DevTools protocol and fails on any console error, CSP refusal or uncaught
  exception. Round 2 shipped a dead viewer and a broken admin login with every
  unit test green; this is the test that sees that.
* `pyflakes` clean across every Python file **except** `plugins/blender/addon.py`,
  where the `bpy.props` annotation idiom Blender requires
  (`name: bpy.props.StringProperty(name=...)`) trips pyflakes 4.1's
  forward-annotation check. It is a tool false positive, not a defect: 12 findings,
  all on three property declarations, and the add-on registers in Blender 4.0.2.
  `node --check` clean on all three front-end modules.
* CI runs the same six suites plus a synthetic-DCC headless integration pass.
  The DCC sweep itself was run against Blender 4.0.2, Houdini 22.0.459 and
  Unreal 5.3 on this machine: 0 failures.
* The build is reproducible from a clean tree: `python tools/build_release.py`.

## Fixed before this tag (audit round 2)

* **Auto-heal no longer breaks references.** Renaming `wood.png` to
  `wood_Albedo.png` used to leave every `.mtl`/`.gltf` pointing at a file that no
  longer existed, and the break only surfaced on the *next* scan. The rename now
  carries a pending-edit list and repoints the references at the moment the heal
  is adopted - never earlier, so a discarded heal leaves the original untouched.
* **`.glb` LODs are real GLB.** They were glTF *text* under a `.glb` name, which
  every viewer sniffs and rejects after the LOD ladder has been recorded as
  generated. There is now a real GLB writer (header, JSON chunk, BIN chunk), and
  an empty LOD is refused instead of written.
* **An unreadable file is an `ERROR`, not an empty repository.** A locked mesh
  produced "no tracked assets found" and exit 0, so CI stayed green on a folder
  nobody had gated. There is a fourth verdict status, it counts in the exit code,
  and `--allow-empty` exists for folders that are legitimately empty.
* **Telemetry stopped volunteering identity.** Hostname, OS account and absolute
  paths went to the studio endpoint by default. `--telemetry-identity` now picks
  `full`, `path` (default) or `none`.
* **References cannot walk out of the project.** `../../shared/wood.png` or a
  hardcoded `C:/Users/...` path failed nothing before; it fails now, with
  `[mesh] allow_outside_references` for studios that really do share a library.
* **Houdini packed prims are counted.** The SOP called `prim.vertexCount()`, which
  Houdini 19 renamed to `numVertices()` - every cook raised AttributeError, the
  error was swallowed as "check skipped", and packed prims counted as one vertex.
  A packed 4,000,000-triangle asset now fails the budget it should always have.
* **Blender reads the project budget.** The add-on ran the engine on a temporary
  export with no rules unless someone had typed a path in, so the viewport said
  PASSED and the export failed a minute later; and the pre-flight panel counted
  material *slots* where the gate counts distinct materials.
* **The dashboard and the workstation send security headers** (nosniff, DENY
  framing, a `default-src 'none'` CSP) and refuse a `Host` header that is not
  their own - the one signal a DNS-rebinding page cannot forge. First-admin setup
  keeps its loopback guard, which that Host check now actually enforces.
* **`--triangles -1` is refused** instead of failing every mesh in the repository,
  and a `Budget` refuses impossible numbers wherever it is built (rules file,
  preset, tag budget or flag).
* **Small ones:** `_BaseColor`/`_ORM` are accepted and inferred (a suffix hint is
  now matched at a word boundary, so `wall_normal` no longer becomes
  `wall_normal_ORM`); the Perforce trigger is `change-content` with
  `p4 print //depot/...@=<change>`; `--once` no longer spends 0.35 s proving a
  five-day-old file is stable; a symlink loop cannot walk a scan forever;
  `SHA256SUMS.txt` is LF-only so `sha256sum -c` works on the box that downloads
  it; the README flag table lists every flag (and a test keeps it that way); and
  the wheel finally ships `plugins/` instead of leaving the DCC panels out.

## Fixed before this tag

* `--preset retro` and friends were silently dropped when the folder had no
  `rules.toml`: the budget stayed at the 50,000 default and said so in a banner
  nobody reads twice. The shipped profiles now apply to a bare folder, and a
  `--platform` name with no matching `[platforms.*]` table is refused instead of
  ignored. Found by building the release and running its first documented
  command, which is the only reason it was found at all.

## Known limits, stated plainly

* `.blend`, `.max`, `.ma` and `.usd` cannot be triangle-counted here (FBX can, from 1.1).
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
