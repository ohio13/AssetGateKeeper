# 07 - Art Workstation: the 3D review screen

A local web app for looking at art without a DCC, a licence, or a download. A
reviewer opens a URL, orbits the asset, reads the gate's verdict, checks the LOD
chain and the collision proxy, and leaves a note. It runs on one machine and
stores everything in two places you can copy.

* **URL:** `http://127.0.0.1:8100`
* **Start:** `art-workstation` (or `agk-serve up`, which starts both servers)
* **Database:** `art_workstation.db`
* **Files:** `workstation_data/assets/<id>/` (one folder per asset: the file, the
  files it references, and the server's previews in `.agk/`); LODs in
  `workstation_data/lods/<id>/`

## What it is for

| Question | Where it is answered |
|---|---|
| "Is this asset broken, or just over budget?" | the **Gate verdict** at the top of the inspector |
| "Does it look right from behind?" | the viewport: orbit, textured / geometry / wireframe |
| "Is the LOD chain sane?" | the **LODs** section and the LOD buttons above the viewport |
| "Does the collision contain the mesh?" | the **Collision inspector**: box, sphere or hull, drawn over the model |
| "What did review say?" | **Review notes**, categorised and attributed, per asset |
| "What are this project's budgets?" | the **Rules & Budgets** tab |
| "Can I attach this to a ticket?" | **Printable report** - one self-contained HTML page |
| "Can I look at thirty exports at once?" | bulk upload, one folder |

## Loading assets

Three ways in:

1. **Drag files** onto the page, or use **Upload asset** (several at once).
2. **Bulk upload** a folder:
   ```
   art-workstation-upload "C:\Proj\export\env" --workstation http://127.0.0.1:8100
   ```
   This is the path for a review pass: point it at the export folder, walk away.
   (`--url` is the same flag; sub-folders are always walked; `--no-sidecars`
   sends only the assets themselves.)
3. **From AssetHealth** - the dashboard's "Send to workstation" button posts the
   asset server-to-server, with the gate's verdict, so a producer can triage
   failures without touching a file system. The token never reaches the browser:
   AssetHealth reads the file and posts the bytes.

On every path, the files an asset references travel with it: a `.gltf`'s `.bin`
and textures, an `.obj`'s `.mtl` and maps, an FBX's external textures. They go
into the asset's own folder under the relative path the asset uses, so it
measures and renders as exported, and they do not appear as assets of their own.
A `.gltf` uploaded alone shows **waiting for its files** and names them; with it
selected, drop the missing files onto the page to complete it.

### What it accepts

| Category | Extensions | Behaviour |
|---|---|---|
| Meshes | `.glb`, `.gltf`, `.obj` | measured and drawn in 3D; LODs can be generated |
| FBX | `.fbx` (binary or ASCII 7.1+) | measured by the gate's own reader (same numbers as the verdict); drawn through a **converted `.glb` preview**, with a warning on screen (below) |
| Other meshes | `.ply`, `.stl`, `.dae`, ... (whatever trimesh reads) | measured; no 3D preview in the browser |
| Textures | `.png`, `.jpg`, `.bmp`, `.webp` shown as-is; `.tga`, `.tif`, `.dds` shown through a PNG conversion | sized, power-of-two noted, previewed as an image |
| Referenced files | `.bin`, `.mtl`, textures an asset names | stored in the asset's folder (64 files, each within the upload limit) |
| Source files | `.blend`, `.max`, `.ma`, `.mb`, `.usd*` | stored as **unverified**: reviewable, noteable, **excluded from triangle totals** |
| Executables | `.exe`, `.dll`, `.ps1`, `.sh`, `.msi`, `.bat`, `.cmd` | **refused with HTTP 415** |

Any upload is capped at 512 MB per file; `--max-upload-mb` (or `Rules.bat
--max-upload-mb`, or `$AGK_MAX_UPLOAD_MB`) moves it. A file over the limit is
refused in words before it is sent, naming its size and the limit. Names in any script are kept (`岩石_rock.fbx`
stays `岩石_rock.fbx`, and downloads under that name); characters a file system
or a header cannot carry are replaced.

A file in a measurable format that cannot be read - a truncated `.fbx`, a `.glb`
of random bytes, an image that will not open - is stored as **broken**: shown in
red, listed under **Needs attention**, kept out of the triangle total, with the
reason. Broken is a problem to fix; unverified is a format nothing here opens.

The unverified category is the important one. A `.blend` cannot be measured
without Blender, so the tool says so instead of reporting `0 triangles`. The
asset appears in the library as `unmeasured .blend`, its report says "not
measured", and no budget is applied to it - because a zero that means "unknown"
is how a project ships a four-million-triangle boss with a green dashboard.

### FBX previews are conversions, and the viewer says so

The browser cannot draw FBX, so the server converts its geometry to a `.glb`
(`engine/fbx_preview.py`) and the viewer draws that, under a banner that stays on
screen for the FBX and its LODs:

> **Converted preview** - You are viewing a .glb converted from this FBX, not the
> FBX itself. Geometry, UVs, normals and base-colour textures (those uploaded
> with it); a slot with no texture gets a flat colour. No animation, skinning or
> other material inputs; pivots are not applied, so placement can differ from
> your DCC or engine. The numbers on the right are measured from the FBX.

What is drawn is what the gate counts: Unreal collision meshes (`UCX_` and
friends) and LOD1+ levels of an LOD group are left out, and axes and units are
converted to Y-up metres. The conversion keeps the first UV set and the FBX's
own normals (so hard edges and UV seams look as authored) and each material's
base-colour (diffuse) texture. That texture shows only if it was uploaded with
the FBX - drop it in the same batch, or let the bulk uploader or AssetHealth
send it; otherwise the slot is a flat colour named after the FBX material.
Normal, roughness and other maps are not applied in the FBX preview; the UV and
texel-density views (below) work on it like on any other mesh.

### Inspection views

The toolbar's view buttons switch how the mesh is drawn, without changing it:

| Button | Shows |
| --- | --- |
| Textured / Geometry | the materials as authored, or plain grey shading |
| **UV check** | a numbered checker through the first UV set - stretching, flipped or mirrored islands and seams show at a glance; a mesh with no UVs turns pink |
| **Texel** | texel density per face, against the mesh's median: green is near the median, blue is lower (blurry), red is higher (wasted memory); the readout gives the median in pixels per metre |
| **Normals** | short lines along the vertex normals (up to 20,000), to spot flipped or unwelded normals |
| **Scale** | a 1.8 m person and a metre grid beside the asset; the readout gives its size in metres |

Texel density uses the asset's own base-colour texture size where it has one,
otherwise 1024 px (the gate's `texel_texture` default).

## The screen

Four columns on a desktop; on a screen narrower than 820 px (a phone, a split
window) they stack - viewport first, then the inspector, library and notes - so
nothing is squeezed off the edge.

| Area | What it does |
|---|---|
| **Library** (left, *Assets* tab) | every asset, filtered by **All**, **Needs attention** (a FAILED verdict, or a broken file) and **Passed**; each card shows the geometry summary, LOD count, note count and verdict badge |
| **Rules & Budgets** (left, second tab) | the project's `rules.toml` as a table: the default budget and each tag's overrides. Read-only - change it in `rules.toml` or the AssetHealth budget planner. A broken `rules.toml` is shown as an error, not an empty table |
| **Toolbar** | **Textured** / **Geometry** (flat normals) / **Wireframe** / **Collision** (show the proxy), an **Exposure** slider, and one button per LOD level |
| **Viewport** | orbit (drag), pan (right-drag), zoom (wheel). Textures show as an image with their size; an asset that cannot be drawn says why and never leaves the previous model on screen |
| **Readout** (under the viewport) | triangles and vertices at the current level, as a share of the source, with where the level came from and any warning |
| **Inspector** (right) | Gate verdict, LODs, Collision inspector, Materials, Asset - one scrolling column |
| **Review notes** (far right) | add, categorise, read |

The page keeps itself current: an upload, a new note or a verdict from another
reviewer's tab appears without a reload (a live event stream).

### Links you can paste

| Link | Opens |
|---|---|
| `/viewer?asset=12` | asset 12, selected, with its verdict |
| `/viewer?asset=12&lod=LOD2` | ... viewed at LOD2 |
| `/viewer?asset=12&note=40` | ... from the camera of pinned note 40, that note highlighted |
| `/viewer?tab=rules` (or `#rules`) | the Rules & Budgets tab |
| `?live=0` (with any of the above) | no live stream - for headless screenshots and kiosk screens, which otherwise never finish loading |

In token mode add `&token=...` to the link, or log in on the page.

## The gate verdict

A reviewer needs to know whether the numbers are a measurement or an opinion. So
the verdict sits apart from the notes, and it says where it came from:

```
GATE VERDICT
FAILED
  72,500 triangles exceeds the 50,000 budget (+22,500)
  4 material slots exceeds the 3 slot budget
Category: environment rock
sent from cli
```

* The status is whatever the gate reported - **PASSED** and **FAILED** drive
  the library filters (case does not matter on the way in).
* *Category* is the tag the gate judged it under.
* *sent from* is whatever the sender said it was (AssetHealth sends its own
  source; the gate sends `cli` or `ci`). An asset uploaded directly has no
  verdict, and the panel says so rather than showing a green one.
* If the verdict arrived while the workstation was **open to the network with no
  token**, the panel adds: *Not authenticated: ... anyone could have sent it.* The
  server records how each asset arrived (`token`, `local` for loopback, `open`),
  so this note survives restarts and later configuration changes.

API: `POST /api/assets?filename=rock.fbx&verdict=failed&problems=...&tags=...&source=ci`
with the file as the body.

## LODs

The **LODs** section lists each level with its triangles, its share of the source,
where it came from (preview, shippable or uploaded) and a download link. Three
ways to fill it:

* **Generate LODs** - pick a preset (Standard 50 / 25 / 12.5 %,
  Aggressive 40 / 15 / 5 %, Mobile 25 / 10 / 3 %, or one level at 50 %) and press
  **Generate**. It runs in the background and says which level it is on; the page
  stays usable. These are *preview* LODs from a fast clustering decimator:
  positions only, no UVs, normals or materials - the readout says so - for
  judging the cost of a chain, not for shipping.
  * **Accurate:** each level lands within about 2 % of its target (a 25 % level
    of a 100,000-triangle mesh is 24,500-25,500 triangles), and a triangle cap
    is never exceeded when the mesh can get under it.
  * **Fast:** about 12 seconds for a 1.3-million-triangle mesh, all three levels
    (numpy; without numpy it falls back to pure Python, which is much slower).
  * **Placed:** a glTF's node transforms (position, rotation, scale) are applied,
    so a LOD sits exactly where the source does.
  * If a level cannot reach its target - asking for 4 triangles of a 12-triangle
    cube - it is kept, and flagged *over its N-triangle target*.
* **Generate LODs with Shippable ticked** - the same presets through a second
  decimator (`engine/lod_shippable.py`) whose levels you can put in the game:
  * **UVs, normals and materials are kept.** It collapses edges by quadric error
    (flat areas first, silhouettes last), and only ever moves a vertex onto a
    neighbour, so every vertex left is an original one with its exact UV and
    normal. On a textured test sphere, LOD2 (25 %) samples its texture within
    0.7 texels of 512 of where the source does.
  * **UV seams, hard edges and material borders are locked**, so the texture
    cannot tear and slots cannot pull apart. A mesh that is mostly seams cannot
    get as low; the level then says where it stopped and why.
  * No flipped faces, slivers or non-manifold edges; each level is within 2 %
    of its target when the mesh allows it, and is a subset of the level above.
  * Each level shows its **surface error** (`shippable +/-1.23 mm`): the
    largest distance, as a quadric RMS, that a collapse moved the surface.
  * The levels are `.glb` files with the materials and textures embedded,
    placed in world space: download one with the arrow on its row and import it
    into Unreal, Unity or Blender. In the viewer they are drawn with their
    materials, not the grey preview.
  * Slower: about 4 seconds for 65,000 triangles and 80 seconds for a million,
    all levels, in the background.
* **Upload your own LOD** - choose the level and a `.glb`, self-contained `.gltf`,
  `.obj` or `.fbx`. It is measured like the source (an FBX by the gate's reader,
  viewed through a converted `.glb`). A file named `<asset>_LOD1` ... `_LOD4`
  dropped on the page or picked with **Upload asset** becomes that level of the
  asset with the same name, not a new asset.

Generation never overwrites or removes a level you uploaded: it is refused until
you delete that level (the **x** on its row).

What the viewer flags, on the level's button, its row and the readout:

* a LOD with the same triangle count as the source or the level above, or more
  than either - a "LOD" that saves nothing, or costs more, is a bug in the chain;
* a generated level over its target;
* percentages always use the source as the denominator.

API: `POST /api/assets/<id>/lods?async=1` with `{"ratios": [0.5, 0.25]}` or
`{"max_tris": [5000, 1000]}` (or both), plus `"shippable": true` for the second
decimator, returns a job (`GET /api/jobs/<job id>` for its
state and current level); without `async` it answers when done. Up to four
levels; ratios must get smaller level by level. One job per asset at a time
(another request gets 409). `POST /api/assets/<id>/lods/LOD2?filename=x.fbx`
uploads a level; `DELETE /api/assets/<id>/lods/LOD2` removes one.

## Collision

Every measured mesh gets a collision proxy when it is uploaded: its bounding box,
computed from the file (with node transforms applied). The **Collision inspector**
shows the proxy's kind and size, draws it over the model (toggle **Collision** in
the toolbar), and lets you change it:

* **Rebuild from mesh** with a kind:
  * **box** - the bounds of the model as drawn;
  * **sphere** - the sphere through the box's corners, so the whole mesh is
    inside it;
  * **hull** - a convex hull of the points that reach furthest in 64 directions
    (at most 64 points), in world space.
* **Export .obj** downloads the proxy as a mesh you can open in a DCC or engine.

Collision is not budgeted by the gate (collision budgets are engine-specific);
it is stored next to the asset so a reviewer can see whether it exists and
whether it contains the mesh - the questions that actually get asked.

API: `POST /api/assets/<id>/collision` with `{"kind": "box", "data": {"min": [...],
"max": [...]}}`, `{"kind": "sphere", "data": {"center": [...], "radius": r}}` or
`{"kind": "hull", "data": {"points": [[x, y, z], ...]}}`. Non-finite numbers, a
`min` above its `max` and a radius that is not positive are refused (400).

## Materials

The **Materials** section lists each material on the model as drawn, by name,
with an **exposure** and a **roughness** slider. They change only what you see -
for checking how a surface reads under light - and are reset when the asset is
reloaded; nothing is written back to the file. A texture the asset references but
that was not uploaded is named under **waiting for its files**.

## Notes

Notes are per asset, with a category (Geometry, Texture, LOD, Collision,
Shading, Other), an author and a time. The text takes light formatting:
`**bold**`, `` `code` `` and `- ` list items. Everything else is shown as typed;
nothing in a note is run as HTML.

They are deliberately not free text in the verdict: a machine's finding and a
human's opinion stay distinguishable six months later.

### Notes pinned to the model

"The seam on the left" depends on where the camera was. Press **Pin to model**,
click the spot on the model, write the note and **Add note**. The note keeps:

* the point you clicked (and which level - source or an LOD - was on screen);
* the camera: where it stood, what it looked at, its field of view;
* a picture of the view (a JPEG up to 800 px wide), shown under the note.

Pinned notes are numbered on the model (#1 is the oldest and keeps its number as
notes are added). Click a number, the picture or **view** to fly the camera back
to that exact view; **link** copies `/viewer?asset=12&note=40`, which opens that
view on any machine. The printable report carries each pin's number and picture,
so it still makes sense on paper. Deleting a note deletes its picture. A click
beside the model places nothing; press **Pin to model** again to cancel.

## Versions, sign-off and where a file came from

### Versions

Uploading a file with the same depot path (the bulk uploader, AssetHealth and
the Perforce trigger send one) adds the **next version** of that asset instead
of a second card. Dropping a *different* file whose name is already in the
library asks first: **New version**, **Separate asset** or **Cancel** - two
unrelated `rock.obj` files are never merged silently.
The library shows the newest version (with a `v3` mark); **Versions & review**
in the inspector lists every version with its triangles and time, and what
changed from the one before (triangles, vertices, material slots, texture size,
file size).

* **Overlay previous** draws the version before as an orange wireframe over this
  one; **Side by side** puts it, solid orange, to the left. A second click turns
  it off.
* Sending the exact bytes of the newest version again adds nothing: the upload
  says *unchanged* (the bulk uploader prints it per file).
* A version that has been reviewed, or that a newer version has replaced, is
  **frozen**: its files never change. A changed `.bin`, `.mtl` or texture sent
  for it goes into a new version (with the same primary file and the other
  files copied), and the rest of that batch follows into the same new version.
* A fixed copy from **Check for fixes** becomes the next version, marked
  *healed*.
* Each version keeps its own verdict, LODs, collision, notes and sign-off. Links
  (`?asset=<id>`) always open that exact version.
* Totals on the stats line count the newest version of each asset only.
* API: `POST /api/assets?filename=` takes `version_of=<id>` (add a version under
  another name), `new_asset=1` (force a separate asset) and `ask=1` (a 409 with
  `choice` and `existing` instead of versioning a different file by name);
  `GET /api/assets?all_versions=1` lists every version.

### Sign-off

Each version is *In review*, *Changes requested* or *Approved*. **Approve**,
**Request changes** and **Back to review** take an optional note; every decision
is kept, newest first. The library's **To review** and **Approved** filters use
it.

* With accounts (below), the decision carries the signed-in reviewer's name and
  is *verified*. Without accounts, a name must be typed and the panel says
  *name typed, not signed in* - a sign-off anyone could have made.
* The Perforce trigger can refuse any asset whose **exact content** has not been
  approved: `--require-approval --workstation <url>` (see
  [10 CI and version control](10-CI-AND-VERSION-CONTROL.md#perforce-helix-core)).
  It asks `GET /api/approvals/<sha256>`, and refuses the submit if the
  workstation cannot be reached. The answer covers the files an asset carries
  too (a `.bin`, `.mtl` or texture of a reviewed version), so a changelist that
  only edits `rock.bin` is checked.
* When the same bytes have several versions (sent under two names), the
  **newest decision** among them wins: approved once and sent back later is not
  approved. Once accounts exist, only signed-in decisions count; earlier
  typed-name approvals have to be given again by a reviewer who signs in.

### Named reviewers (accounts)

With no accounts the workstation works as before. The first account turns
accounts on: every reviewer signs in on the page, and notes, uploads and
sign-offs carry the account's name instead of a typed one. You create accounts
on the server's machine; each password is typed at a hidden prompt:

```
art-workstation --add-user lena --role lead
art-workstation --add-user rita --role reviewer
art-workstation --list-users
art-workstation --set-password rita          # also signs rita out everywhere
art-workstation --set-role rita --role lead
art-workstation --remove-user rita           # the last one turns accounts off
```

| Role | Can |
|---|---|
| viewer | see everything, write notes |
| reviewer | + upload, LODs, collision, fixes, sign-off |
| lead | + delete assets |
| the `--token` (scripts) | read, upload, LODs, collision, fixes - never sign off or delete |

Passwords are at least 10 characters and stored as salted PBKDF2 hashes. A
session lasts 7 days in an HttpOnly, SameSite=Strict cookie. Ten wrong
passwords from one address pause sign-in for five minutes. Keep `--token` set
once accounts are on: the bulk uploader, AssetHealth's **Send to workstation**
and the Perforce trigger use it. Single sign-on (OIDC/SAML) is in AssetHealth's
`enterprise/` module and is not wired into the workstation yet.

### Where a file came from

An asset sent with a depot path shows it in the inspector with its changelist -
`//depot/Art/Props/vase.obj @502` - and a **Copy** button. The bulk uploader
takes `--depot-root //depot/Art/Props --changelist 502` (each file is recorded
as the depot root plus its path below the uploaded folder; without
`--depot-root` the relative path is recorded). AssetHealth sends the path the
gate reported; the Perforce trigger, with `--workstation`, sends each passing
submit for review with its depot path and changelist. A depot path also decides
versions: two `rock.obj` files in different folders are different assets.

## The Asset section: report, download, delete

* **Printable report** opens one self-contained HTML page - facts, a gate check,
  references, LODs, collision, notes - with no external CSS, script or image, so
  it can be attached to a ticket or printed and still look the same in five
  years. Its gate check runs the gate over the stored file with the project's
  default budget, and says so; it is not a copy of the verdict that was sent.
  `GET /api/assets/<id>/report?download=1` saves it as a file instead.
* **Download file** - the asset as uploaded, under its original name.
* **Check for fixes** (on `.png`, `.tga`, `.jpg` and `.obj` assets) asks the healer what it could
  fix - a missing name suffix, a size that is not a power of two (resized, so UVs
  stay put), zero-area `.obj` faces - and which budget it checked against: the
  `rules.toml` the workstation was started with (`--rules`, or one in its working
  folder), else the gate's defaults. Nothing is stored until you press **Make fixed
  copy**: that adds the fixed file as a **new asset**, verified by the gate, marked
  *Fixed copy of #N* with what changed and a link back. The original asset is not
  changed, and nothing is written to anyone's disk - download the copy, or fix the
  source with `asset-gatekeeper <file> --heal`. When there is nothing it can fix, it
  says why (the copy would still be over the edge budget, the format is not one it
  edits, ...).
* **Delete asset** removes the asset, its files, LODs, collision and notes. It
  asks for a second click.

`GET /api/assets/<id>/references` returns the asset's broken links and orphaned
textures, worked out from what is on disk at the time of the request.

## Security

* **Solo mode** (no token) is for one person on one machine: it binds
  `127.0.0.1` by default and nothing else can reach it.
* **Token mode** (`--token`, or `AUTH_TOKEN`) is for a shared machine: every
  `/api` call needs `Authorization: Bearer <token>` or `?token=`.
* **Accounts** (above) are for a team: each reviewer signs in, with a role.
* **Open to the network with no token** - `--host 0.0.0.0` without a token -
  works, but it is the one configuration where anyone on the network can upload,
  set verdicts and delete. The server prints a `WARNING` at start-up, the page
  shows a red banner saying so, and every verdict that arrives is marked *Not
  authenticated* (above).
* Other websites cannot drive it: a request from another site is refused
  (Origin / `Sec-Fetch-Site` check), there is no CORS, and a `Host` header that is
  not this machine is refused, which stops DNS rebinding (`--allow-host` adds a
  name, for a proxy).

## Formats and the 415 rule

Executables are refused because an asset store is not a file share. Everything
else is either measured or stored-and-labelled. If a format is refused that you
believe should be stored, the suffix list is a constant (`REFUSED_SUFFIXES`) and
the fix is one line plus a test.

## Running it in a studio

* **Start it where the reviewers are**, not on an artist workstation: one
  workstation per review room is enough, and it keeps the files on one disk.
* **Use a token** if anyone but you can reach it, and **put it behind your
  intranet proxy** (`--allow-host` with the proxy's name). It has no TLS of its
  own; terminate TLS in front of it, like the other internal dashboards.
* **Back it up** by copying `art_workstation.db` and `workstation_data/`. Those
  two things are the entire state.
* **Prune it**: a review room accumulates gigabytes. Delete per asset; a
  quarterly prune is normal.

Options: `--host`, `--port` (`--port-auto` moves to the next free port),
`--db`, `--data`, `--token`, `--rules PATH` (the `rules.toml` the Rules & Budgets
tab shows), `--allow-host NAME`, `--quiet`.

## API summary

| Method and path | What it does |
|---|---|
| `GET /api/health` | liveness, counts, `auth`, `exposed` |
| `GET /api/stats` | repository totals |
| `GET /api/rules` | the rules snapshot behind the Rules & Budgets tab |
| `GET /api/events` | the live event stream |
| `GET /api/assets` · `POST /api/assets?filename=` | list · upload (raw body; optional verdict fields) |
| `GET` · `DELETE /api/assets/<id>` | detail (LODs, collision, files, missing files) · delete |
| `GET /api/assets/<id>/file` · `/preview` | the bytes · the converted preview (FBX `.glb`, texture `.png`) |
| `POST /api/assets/<id>/files?filename=` · `GET .../files/<path>` | add · serve a file the asset references |
| `GET` · `POST /api/assets/<id>/lods` | list · generate (`?async=1` for a job) |
| `POST` · `DELETE /api/assets/<id>/lods/<LODn>` · `GET .../file` | upload · delete · download a level |
| `GET /api/jobs/<id>` | a background job's state and progress |
| `GET` · `POST /api/assets/<id>/collision` · `GET .../collision.obj` | read · set · export the proxy |
| `GET /api/assets/<id>/references` · `/report` | reference check · printable report |
| `GET` · `POST /api/assets/<id>/heal` | what a fix would change (nothing stored) · make the fixed copy as a new asset |
| `GET /api/assets/<id>/notes` · `POST /api/notes` · `DELETE /api/notes/<id>` | notes; `POST` takes an optional `pin` (`point`, `normal`, `camera` {`position`, `target`, `fov`}, `level`) and `screenshot` (a PNG or JPEG `data:` URL, 600 KB max) |
| `GET /api/notes/<id>/screenshot` | the view a pinned note was written in |
| `GET` · `POST /api/assets/<id>/review` | sign-off history · a decision (`status`, `note`, `reviewer` without accounts) |
| `GET /api/approvals/<sha256>` | is this exact file approved? (the Perforce trigger) |
| `GET /api/me` · `POST` · `DELETE /api/session` | who is signed in · sign in · sign out |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 415 on upload | the file is an executable | that is intentional; store the export, not the tool |
| "unmeasured" on a `.glb` | `trimesh` not installed | `pip install trimesh` |
| "waiting for its files" | the `.gltf`/`.obj` was uploaded without what it references | select it and drop the named files onto the page |
| Model loads, textures black | the textures it references were not uploaded | drop them onto the page with the asset selected |
| "broken" | the file cannot be read in a format that should be measurable | re-export it; the reason is in the tooltip and the report |
| Verdict panel says "No gate verdict" | the asset was uploaded directly, not sent with one | run the gate with `--telemetry-url`, or use "Send to workstation" |
| Red banner "Open to the network with no token" | started with `--host 0.0.0.0` and no token | restart with `--token`, or bind `127.0.0.1` |
| LOD generation very slow | numpy is not installed | `pip install numpy` |
| "over its N-triangle target" on a LOD | the mesh is too small to reach that level | use a gentler preset, or upload that level |
| Port already in use | another instance | `agk-serve status`, or `--port 8101` / `--port-auto` |

Next: [08 AssetHealth](08-ASSETHEALTH.md) for the metrics side.
