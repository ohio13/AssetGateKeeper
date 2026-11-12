# Unreleased (on `main`)

* **Polypaint bake: no more black triangles (12 Nov).** Reducing a thin part
  (hair against the cheek, a ponytail) could fold it into two triangles on
  the same three points, facing opposite ways, and one normal per vertex
  pointed wrong wherever the low-poly mesh folded sharply: a Nintendo 64 bake
  of a character showed a black triangle on each side of the head, and much
  of the face was shaded as if lit from behind. Folds are now removed, and
  shading normals are smoothed only between triangles within 60 degrees of
  each other (hard edges elsewhere, as Blender's auto smooth does). The mesh
  has a few more vertices where its edges are hard; the triangle count is
  unchanged.

* **The gate's code is split into parts in `agk_gate/` (11 Nov).**
  `asset_gatekeeper.py` had grown to about 8,500 lines; it is now a short front
  door, and the code lives in seven parts in `agk_gate/` (settings,
  readers, the gate, the audit, reports, the command line, and what they
  share). Nothing changes for a user: the same command, the same flags, the
  same verdicts (checked file by file against the old version, with and
  without a `rules.toml`), and every name is still on `asset_gatekeeper` for
  scripts, plug-ins and tests. One thing to know: copying the gate by hand
  now means copying `asset_gatekeeper.py` *and* the `agk_gate/` folder; the
  download ZIP and `pip install` already include both.

* **The DCC panels call the gate's skeleton and lightmap checks directly
  (11 Nov).** `skeleton_problems()`, `lightmap_facts_of()` and
  `lightmap_problems()` are now plain functions in the gate; the panels used
  to run copies of the gate's methods with swapped-in data, which a small
  change in the gate could have broken. Same words, same thresholds.

* **Unity and Godot check an import in batches; the CLI takes several files
  (11 Nov).** Both plug-ins started Python once per imported file (about
  0.7 s each, over two minutes for 200 assets). They now send up to 50 files
  that share a `rules.toml` to one run, at most 8,000 characters of command
  line, and match each verdict back to its file by path; labels, Console and
  Output messages, NOT checked, the unchanged-file guard and cancelling work
  as before, and Unity's timeout scales with the batch. The gate accepts
  `asset-gatekeeper a.fbx b.png c.glb --once --json`: each file checked as it
  would be alone, one line each in order, then a summary line; a missing file
  is an `ERROR` line instead of ending the run. 50 small files: 37-39 s one
  process each, under 1 s in one run (docs/06, docs/09).

* **glTF collision and LODs read as in an FBX; bakes stay inside the folder's
  limits (11 Nov).** A glTF's `UCX_`/`UBX_`/`USP_`/`UCP_` meshes counted as part
  of the model: a 12-triangle crate with its hull read as 24 triangles and two
  materials, failed a budget it passed as FBX, and a glTF holding only a hull
  passed. They now add nothing to triangles, materials, UV sets, broken
  triangles, memory or texel density; only the `[collision]` checks see them.
  Materials, UV sets, `[lightmap]` and `[skeleton]` read a glTF's LOD0 only
  (not `MSFT_lod` levels or meshes no node places). `--bake-texture` or
  `--bake-triangles` above the folder's limits is refused before the bake,
  instead of baking for minutes and then failing the gate.
* **`--json` explains a failing `[[totals]]`; names cannot drive the terminal
  (11 Nov).** A folder total over budget exited 1 with `"failed": 0` and nothing
  else in the JSON; the summary line now carries `totals_failed`, and the
  admin page's Scan log shows them. Control characters in file and node names
  (an ESC that would recolour or clear the terminal, a newline that could
  fake a PASSED line) are printed as `\x1b`. docs/10's CI example installs
  from PyPI instead of a 1.0.0 wheel.
* **Faster FBX checks with memory, texel, skeleton or lightmap rules
  (11 Nov).** Run as a script, the gate loaded itself a second time whenever
  it measured an FBX, and the FBX was parsed up to three times per check. It
  now loads once and parses each file once: a small FBX with `[mesh] max_mb`
  went from 1.1 s to 0.7 s.
* **Wizard asks about the new checks; Switch, VR and mid-range presets
  (11 Nov).** A seventh question picks extra checks (LODs, Unreal collision,
  skeleton limits, lightmap UVs; none by default) and writes their tables:
  collision only as a comment unless Unreal is chosen, 75 bones per material
  for mobile and VR with a per-platform `[platforms.<name>.skeleton]` block.
  `--preset`/the admin page gain `switch`, `vr` (from Meta's Quest guidance:
  750k-1M triangles and 80-200 draw calls a frame) and `midrange` (PS4 / Xbox
  One generation), which the wizard now uses instead of borrowing `ps3`,
  `mobile` and the defaults. `[platforms.<name>]` and `[tags.<name>]` can set
  `[lod] min_levels`/`max_ratio` and `[collision] max_hulls`/
  `max_hull_vertices` (a folder of small props: `[tags.props.lod] min_levels
  = 1`). The docs' `[platforms.ps2.mesh] allow_degenerate` example, which
  the parser refuses, is replaced.
* **The DCC panels check LODs, collision, skeletons and lightmap UVs.** With a
  `[lod]`, `[collision]`, `[skeleton]` or `[lightmap]` table in `rules.toml`,
  the Maya, 3ds Max and Blender pre-flight panels and the Houdini SOP read the
  LOD groups or `_LOD<n>` names, the UCX_/UBX_/USP_/UCP_ meshes beside the
  selection, the skin's bones and the lightmap UV set from the scene and judge
  them with the gate's own code, so the panel says what the gate will say on
  export, in the same words. What a host cannot read (bones per material
  section in Maya and Max, Houdini skeletons) is shown as "not checked in the
  panel; the gate checks it on export", never as a pass. Collision meshes, and
  LOD1+ in a Maya LOD group, no longer count towards the panels' triangle
  totals (and material totals in Maya, Max and Blender), as they never did in
  the gate. Without the tables nothing
  changes. Tested against stand-ins for each host's API, not yet inside the
  hosts. See docs/09-DCC-PLUGINS.md.
* **Fixes from a full sweep (10 Nov).**
  * *Hooks let failing assets through.* The Git pre-commit and pre-push hooks
    and the Perforce trigger skipped .tif, .tiff, .webp, .dds, .exr and .ktx2,
    which the gate checks; the Git hooks judged an asset in a subfolder by the
    shipped defaults instead of the repository's `rules.toml` above it; the
    pre-commit hook checked the file on disk when a different version was
    staged; and one file checked with `--rules` from a folder above it (the
    Perforce trigger, `--heal`) never got that project's folder tags. All now
    judge as a `--once` run of the project does. A hook copied into
    `.git/hooks`, as docs/10 used to say, exits 2 with a fix instead of a
    traceback; docs/10 gives `git_pre_commit.py --install`.
  * *AssetHealth solo mode behind a reverse proxy on the same PC* treated
    every intranet user as "this PC" (a director, no password). A request
    with `X-Forwarded-For`/`Forwarded` now counts as from elsewhere.
  * *Rules that checked nothing or crashed:* `nan`/`inf` in `max_mb`,
    `texel_density`, `[[totals]]` and exceptions are refused; an
    `[[exceptions]]` value no budget can hold (`max_texture_edge = 0`) is
    refused at start instead of a traceback mid-run; a rules.toml saved as
    Windows-1252 says to save it as UTF-8.
  * *New checks:* `[lightmap] require` no longer asks a glTF `UCX_` hull for a
    lightmap; `[lod] max_ratio` names a count that passes (29, not 28, for
    0.29 of 100).
  * *Reports:* JUnit stays valid XML when a node or file name holds a control
    character; a file name that is not UTF-8 no longer stops `--report`;
    docs/06 shows the real `--json` shape (JSON Lines, `file`, not `path`).
  * *Bake:* below 16 px says so (not "not a power of two"); running out of
    memory (`--bake-texture 65536`) is a message, not a traceback.
  * *Speed:* a .glb's JSON is read without loading its binary chunk; a check
    read a large .glb in full about nine times.

* **A rules wizard for projects that use several tools.** `asset-gatekeeper
  <folder> --wizard` (or double-click `tools\rules_wizard.bat`) asks six
  questions - engines, art programs, platforms, kinds of asset, art style,
  broken geometry; the first four take several answers - and writes one
  commented `rules.toml` that works for all of them: the engines' and tools'
  naming presets combined, the tightest platform as the base budget with a
  `[platforms.<name>]` block for each platform (`--platform <name>`), and
  per-folder budgets for characters, props and environment. The numbers come
  from the shipped presets; the file says which answers made it. It asks
  before replacing a `rules.toml` and keeps a copy. `--wizard-answers
  answers.json` gives the same answers from a file for scripts; without a
  terminal it refuses instead of waiting. Plain `--init` is unchanged.

* **LOD and collision checks (off by default).** A `[lod]` table can require a
  number of LOD levels (`min_levels`, LOD0 included) and how much each level
  must save (`max_ratio`: LOD2 at most 0.6 of LOD1's triangles, say). Levels
  are read from FBX LOD groups, glTF `MSFT_lod`, or objects named `_LOD0`,
  `_LOD1`, ...; an asset without LODs fails saying it has LOD0 only. A
  `[collision]` table checks Unreal's UCX_/UBX_/USP_/UCP_ meshes in FBX and
  glTF: `require` (skinned meshes exempt), names that match a render mesh
  (Unreal drops a hull that does not; on by default once the table exists),
  convex UCX_ hulls (Unreal silently fills a dent; also on by default),
  `max_hulls` and `max_hull_vertices`. Every failure names the mesh or level
  and the number that broke the rule; `--json` gets `lod_levels`,
  `lodN_triangles`, `collision_hulls` and `max_hull_vertices`. Without the
  tables nothing changes. See docs/05-RULES-REFERENCE.md.
* **Install from PyPI.** `pip install "asset-gatekeeper[full]"` now works:
  v0.1.0-alpha.1 is on PyPI, published by the release workflow. The README
  and the installation guide use it instead of the git URL.
* **Unity and Godot plug-ins (new, early).** `plugins/unity` (Unity 2021.3+,
  added with Package Manager > Add package from disk) and
  `plugins/godot/addons/asset_gatekeeper` (Godot 4.2+) check models and
  textures when the engine imports them, in the background: failures go to
  the Console (Unity, plus an `AGK_Failed` label) or the Output panel and a
  dock (Godot), with a Check All / Check project command and settings for the
  Python, the gate folder and the rules file. They report, they do not block
  the import. The Godot add-on is tested in Godot 4.2.2 (headless, in CI);
  the Unity package is compiled and run against stand-ins for Unity's API but
  has not yet been tried in Unity itself. See docs/09-DCC-PLUGINS.md.

* **Skeleton and lightmap UV checks.** Two new rule tables, both off unless
  written. `[skeleton]` budgets the skeleton a skinned FBX or glTF uses:
  `max_bones`, `max_bones_per_section` (bones weighting one material; Unreal
  caps mobile at 75 and splits a larger section into extra draw calls),
  `single_root` (Unreal's FBX importer refuses "multiple roots") and
  `max_depth`; `[platforms.<name>.skeleton]` can tighten the numbers per
  target. `[lightmap]` checks an authored lightmap UV set on static meshes
  (`uv_channel`, 1 by default): `require` it, keep it `inside_unit_square`,
  and cap `max_overlap`, measured by rasterising the lightmap triangles on a
  `resolution` grid (256 by default) so touching islands never count as
  overlapping; a 100,000-triangle mesh takes a fraction of a second. Each
  failure names the section, bone, mesh or percentage and says what to change.
  See docs/05-RULES-REFERENCE.md.

# v0.1.0-alpha.1 (10 Nov 2026)

The first public alpha. The internal builds 1.0.0 to 1.1.1 (private testing,
October-November 2026) are folded into it; their notes follow this section in
RELEASE_NOTES.md, for the record. The gate, the GitHub check and the editor plug-ins are the product;
AssetHealth, the Art Workstation, auto-healing and PostgreSQL are experimental.

* **Going public (10 Nov).** The README and the setup manual open with a
  plain note: an early alpha, built first as a test tool for private projects;
  try it on a copy or under version control; which parts write to your files;
  no warranty; and that it was built with AI (nearly all of the code, tests and
  docs written by AI assistants, mostly Claude, directed and tested by one
  developer). Test fixtures no longer use the developer's PC and folder names.
  The README, CONTRIBUTING.md, SECURITY.md and the GitHub issue and pull
  request forms say how the project is run: one person, spare time, guided by
  their own projects; development continues, feature requests aren't taken,
  bug-report replies can be slow, pull requests probably won't be merged, and
  forks are welcome under the MIT licence.
  THIRD_PARTY_NOTICES.md lists the two libraries copied in (three.js and
  tomli, both MIT) and three.js's licence now sits beside it in
  `server/static/vendor/`. Personal notes and AI-assistant files are
  ignored by Git, and the public history was rebuilt without them.
* **FBX mesh memory and texel density (10 Nov).** `[mesh] max_mb`,
  `texel_density` and `[[totals]]` now measure FBX files too; before, an FBX
  only said "not measured for FBX yet" and failed every total it was in. The
  vertex count is what an engine builds (a UV seam or a hard edge splits a
  vertex: a flat-shaded cube is 24, not 8), the model is placed with its own
  transforms and unit scale, and the base-colour texture beside the FBX sets
  the density when there is one. Checked against the glTF path on every FBX
  test file (same density) and on hand-counted cases.
* **Release workflow (10 Nov).** Pushing a tag such as `v0.1.0-alpha.1`
  builds the wheel, source archive and drop-in ZIP, checks the wheel installs
  and serves, and publishes a GitHub release with that version's notes. It
  refuses first if the tag is not the version in `asset_gatekeeper.py` or the
  notes heading still says "unreleased" (`tools/build_release.py
  --check-tag`). PyPI is opt-in (repository variable `PUBLISH_TO_PYPI`) and uses
  trusted publishing, so no token is stored anywhere.
* **Long LOD jobs can be watched and stopped (10 Nov).** The developer left a Shippable
  LOD of the test sculpt running for 25 minutes and gave up: the page said "generating
  LOD1 (1 of 3)" the whole time, closing the page did not stop it, and a second
  request waited behind it without a word. Measured on his PC, the job takes
  3-4 minutes (the plain decimator 13 s), so something on the PC slowed it
  further; the likeliest is memory (1.7 GB for the test sculpt). Now a job carries a
  fraction, a line saying what it is doing and the time left, shown as a bar;
  **Cancel** (`POST /api/jobs/<id>/cancel`) stops a queued job before it starts
  and a running one at its next progress call; a queued job names the job it is
  waiting for; a Shippable job on a PC with too little free memory warns before
  it starts. The tooltip's "about a minute per million triangles" was wrong and
  now gives the test sculpt's real figure.
* **Ready for PyPI (10 Nov).** `twine check` passes on the wheel and sdist and
  the build prints no warnings: the licence is an SPDX string (`license =
  "MIT"`, setuptools 77+; the TOML table and the licence classifier stop
  building in 2027), the data-only folders (`server/static`, the DCC plug-ins)
  are listed so setuptools keeps shipping them, and every README link is
  absolute, because PyPI shows the README as the project page and relative
  links lead nowhere there. `asset-gatekeeper` is free on PyPI. Publishing
  needs the repository public and a PyPI account.
* **One front door (10 Nov).** Six `.bat` files sat at the top of the folder.
  Now there is one to start with, **`Start AssetGatekeeper.bat`** (the old
  `Rules.bat`), with `Stop.bat`, `Allow_Folder.bat` and `Install_Extras.bat`
  beside it. `agk-serve.bat`, `start_workstation.bat` and
  `start_asset_health.bat` moved to `tools\` and find the install folder from
  there. **`Rules.bat` still works**: it is a two-line file that runs the new
  one, so shortcuts and pinned icons keep working. Messages that named
  `Rules.bat` or `agk-serve.bat` now name the new files.
* **A first-run wizard (10 Nov).** A first start offered a page of budget
  fields, and Scan with no project picked used to check the tool's own test
  files. The Rules & Presets page now opens with **Get started**: your project
  folder, your engine (Unreal, Unity, Godot or Other, written as `[naming]
  preset`) and what it has to run on (the hardware budgets); **Save and check
  my project** writes the project's `rules.toml` and runs the first scan. A
  folder that already has rules is asked about before its numbers are
  replaced. Covered by a browser test from an empty install: Just me on this
  PC, the wizard, the scan with its skipped line.
* **No account for one person (10 Nov).** A solo install opened on "Create
  the first account", a password nobody else would ever need. The first-run
  page now offers **Just me on this PC** first: no account, no password, rules
  changed from this PC only (any other machine is shown a page saying so and
  gets nothing). **Turn on accounts** gives you a name and password when a second
  person joins; you stay the director and the change history moves to your name.
  `--add-admin` refuses while solo mode is on, so the PC's owner cannot be
  locked out. Rule changes still need the page's CSRF token, so another website
  cannot make them.
* **Big models open quickly on the review page (10 Nov).** The test sculpt (1.08 million
  triangles) took about 70 seconds to appear, with nothing on screen. A model
  over 500,000 triangles now gets a lighter copy at upload (about 200,000
  triangles, quadric reduction, Polypaint colours averaged onto it): 3 MB
  instead of 20 MB for the test sculpt, about 4 seconds more at upload. The viewer shows
  it first with a "Lighter preview" banner and a **Show full detail** button,
  and every download has a progress bar, then "Building the 3D view (N
  triangles)". Needs fast-simplification (Install_Extras.bat); without it the
  full model loads with the bar. `GET /api/assets/<id>/preview?light=1`.
  `engine/light_preview.py`.
* **Say what was skipped (10 Nov).** A run used to ignore files it does not
  read without a word: the six `.jfif` reference images in the developer's test folder
  were never mentioned, so the folder looked checked. Each run now ends with
  `6 file(s) skipped: unsupported type .jfif x6 (a JPEG: rename it to .jpg to
  check it)`, with a hint for common source files (.psd, .blend, .ztl, Maya, 3ds
  Max ...); `--exclude` matches are counted too, and `--json`'s summary line
  carries the same as `skipped`. "No tracked assets found" now says what was
  there instead. The admin page's scan prints it as well.
* **The admin page's scan reported failures as a failed scan (10 Nov).** A
  scan that found a failing asset ended "scan failed: <the last log line>" (the
  gate exits 1 when something fails), and the gate's closing summary line was
  counted as one more failed asset, so two assets showed as three. Both fixed;
  the test scans a folder with one pass, one fail and a `.jfif`.
* **N64 face texture (10 Nov).** At 64 px the whole test figure shared one
  texture and her face got about 6 x 6 texels: the eyes were dark blobs. The
  bake can now give the face a texture of its own, the same size, as a second
  material (`T_<name>_Face_BaseColor.png`; `_Face_Normal.png` with a normal
  map), packed separately. The face is found where the sculpt holds the most
  different colours in a small space (eyes: white, iris, pupil, lashes, skin)
  and is the reduced surface nearest it, a tenth of the area - on a full figure,
  about the head. Checked on the developer's test sculpt: the face texture runs from the
  hairline to the neck, and both eyes, the brows and the lips show at 64 px,
  where one shared texture had blobs and no lips (13 s, gate passes under her
  N64 rules). Two simpler detectors were tried on the test sculpt first and are kept out
  by a test: counting colour-changing edges put the face on her chest (her head
  has far more polygons, so each face edge changes colour only a little), and
  colour spread per cell put it on her chin (the suit collar's ring). On with `--bake-preset n64` and the review page's **N64 style**
  (the **Face texture** box), off otherwise (`--bake-face-texture`,
  `--bake-no-face-texture`). Rules that allow one material slot keep one
  texture and the log says why; a sculpt with no colour detail keeps one too.
  On a test figure the eyes get 27 dark texels instead of none.
  A bake now also names any texture an earlier bake left beside it that the
  new `.glb` does not use (a `_Normal.png` before an N64 bake, say); nothing is
  deleted.
* **The bake meets its triangle budget on sculpts with repeated points (10 Nov).**
  Vertices on top of each other (UV seams, pole fans, separately exported
  pieces) are open edges to the reducer, which keeps them: a 1,200-triangle N64
  bake of such a figure came out at 4,628 triangles and passed only because the
  folder had no rules. The reducer now works on a welded copy, and a bake that
  still misses its budget says so.
* **Install_Extras.bat (10 Nov).** One double-click installs the libraries
  into the same Python `Rules.bat` uses: what the gate needs (trimesh, Pillow,
  charset-normalizer, DracoPy) if anything is missing, plus the Polypaint bake
  (fast-simplification, xatlas, scipy; Python 3.10+) and watchdog. Only missing
  or broken packages are installed, each one is then imported to prove it
  loads, and `--check` only reports. Before this, the review page printed a pip
  command and a Windows user had to know which Python it meant and type it in
  cmd, not inside Python. `Rules.bat` and the bake's "needs ..." message now
  point at it; on macOS and Linux it is `python tools/install_extras.py`.
* **A first-time user's install, in CI (10 Nov).** A new `fresh-install` job
  unzips GitHub's "Download ZIP" into an empty folder on a Python with nothing
  installed (Windows 3.12, Ubuntu 3.12 and 3.9) and does what the README says:
  Install_Extras.bat, Rules.bat, a scan, a Polypaint bake (and the N64 preset),
  Stop.bat. On Windows the `.bat` files themselves run. `tests/fresh_install.py`.
* **Polypaint bake, second pass (10 Nov).** The first bake of the 1.08 M
  triangle test sculpt at 15,000 triangles was "pretty good, needs fine tuning
  around the face": eyebrows came out as hollow outlines and the eyeliner
  wings were missing, because each texel took the average colour of the four
  nearest sculpt vertices, and the eyebrows and eyeliner are separate shells
  floating just above the skin. Now:
  * Colour comes from the sculpt's surface, not its vertices: the sculpt's own
    triangles are drawn into the texture, the outermost one wins (as a ray from
    a cage outside the model would see it), and colours are blended across each
    triangle. Every texel is sampled 4 times at 2048 px, 16 at 256-512 px and 64
    at 64-128 px, so small textures are an average of the surface, not a guess.
    Islands are packed to the exact texture size with a few texels of gap, and
    the empty background takes the nearest island's colour (no black seams).
    From 512 px up, the face and other fine colour detail get up to twice the
    texels along each side.
  * A tangent-space **normal map** (`T_<name>_Normal.png`, also inside the
    `.glb`): glTF/OpenGL convention, green up, with MikkTSpace-style tangents
    written into the file. On by default; `--bake-no-normal` or the **Normal
    map** box on the review page turns it off.
  * **`--bake-preset n64`** (and **N64 style** on the review page): about 1,200
    triangles, a 64 px texture in 16 colours (a 4-bit palette texture fits the
    N64's 4 KB texture memory), no normal map, nearest-texel filtering - and
    never more than the folder's `rules.toml` allows. `--bake-colours 16|256`
    reduces any bake's colours on its own.
  * The bake writes its own `.glb` (one mesh, one material, metallic 0,
    roughness 0.8); the test sculpt bakes at 15,000 triangles in about 35-50 s
    and under 2 GB of memory, the N64 preset in about 15-25 s.
* **First outside test (9 Nov).** A fresh download from GitHub, used on a real
  90 MB ZBrush sculpt (1,082,096 triangles):
  * The review screen refused any file over 64 MB with "body must be at most
    67108864 bytes" in small grey text, and Send to Workstation failed with
    "[WinError 10053] An established connection was aborted". The limit is now
    512 MB (`--max-upload-mb`, `$AGK_MAX_UPLOAD_MB`, `Rules.bat --max-upload-mb`);
    a larger file is refused before it is sent, in red, with its size, the
    limit and how to raise it, and the dashboard asks the review screen for its
    limit instead of finding out mid-upload. The bulk uploader does the same.
  * Double-clicking `agk-serve.bat` stopped nothing (it flashed its help and
    closed), so the hidden servers kept the folder locked and it could not be
    deleted. New **`Stop.bat`** stops both servers and any other server running
    from that copy; `agk-serve.bat` on its own now shows what is running and
    waits; no launcher window keeps the folder open any more.
  * Opening a project outside the install folder needed `setx`. New
    **`Allow_Folder.bat`** picks a folder in a dialog (or takes one dragged onto
    it), keeps it in `.agk\allowed_folders.txt` and restarts the page.
  * `tools\agk.bat` and `tools\upload.bat` pointed at a developer's own
    sandbox; they now find Python like the other launchers.
* **ZBrush Polypaint (9 Nov).** A ZBrush sculpt looked blue in Houdini and grey
  in Maya, 3ds Max, Blender and the review page: its colour was `#MRGB` comment
  lines, one colour per vertex. The gate now says so in its verdict; the review
  page draws such files in colour; and `--bake-polypaint` (or **Bake into a game
  asset** on the review page) turns the sculpt into a game asset: a reduced
  `.glb` with UVs and the colours baked into a BaseColor texture, sized to the
  rules (30,000 triangles and 2048 px from a 1.08 M triangle sculpt in about 15 s).
  Optional extra: `pip install "asset-gatekeeper[bake]"`.
* **Full audit (9 Nov).** Fixed what a first-time user would meet:
  * With no project picked, a scan checked the AssetGatekeeper folder itself, so
    a new dashboard filled with the tool's own test files and screenshots marked
    as failures. The install folder is never scanned; the page asks for a
    project first. The dashboard's "Wall of shame" is now "Recent failures", and
    a row with no user name shows the machine name instead of `unknown (...)`.
  * `pip install`: the servers kept their databases and logs inside
    site-packages, and the wheel installed folders called `server`, `tools`,
    `engine`, `enterprise` and `plugins` at the top level. Data now goes to the
    user's data folder (`%LOCALAPPDATA%\AssetGatekeeper`, `AGK_HOME` to move
    it) and the packages install under `agk_suite/`. The launcher names the
    command pip users have (`agk-serve down`).
  * Python 3.9, 3.10 and 3.13 and macOS are now tested; the package says
    "Alpha" and links to the right repository; broken links in the reference
    manual are fixed and its screenshots and design notes moved into `docs/`.
  * `--jobs` lowers itself when the largest files would not fit in free memory
    (a 90 MB sculpt needs about 750 MB to check), and trimesh and Pillow are
    capped below their next major version; meshes load with
    `trimesh.load_scene`.
* **Real-world model test (6 Nov).** 460 public models (Khronos glTF samples,
  three.js and assimp test files) were run through the gate and compared with
  an independent reader. Fixed from what it found:
  * One damaged file stopped a whole `--once` folder check with a traceback and
    no report. Any unexpected error is now an `ERROR` verdict for that file and
    the run carries on; corrupt GLB text and out-of-range indices say "damaged".
  * Draco-compressed glTF read every vertex as zero, so every triangle was
    "degenerate" (duck.glb: 4,212 of 4,212). DracoPy is now a dependency, and
    when compression cannot be decoded the shape checks are skipped with a
    warning instead of failing.
  * Textures packed inside `.glb`, `data:` URIs and FBX embedded media were
    never checked; a `.glb` with 4096x4096 maps passed a 2048 budget.
  * The degenerate test used a fixed area, so real-scale small props failed
    (BoomBox 1,774 triangles, a 1 mm sphere set 1,040,409). It now judges shape:
    flat (`degenerate_ratio`, default 1e-6 x longest edge squared) or collapsed.
    `degenerate_epsilon` is an optional extra floor, 0 by default.
  * glTF triangles are counted from the file: strips and fans included,
    compressed meshes exact, each mesh once (`drawn_triangles` in stats).
  * ASCII FBX strings ending in `\` and binary `c` arrays no longer read as corrupt.
  * 28 default texture map names, matched ignoring case and separators; an
    optional `[mesh] name_prefixes` rule shared by the gate and every DCC panel
    (Maya's own underscore check is gone); `.tif .webp .dds .exr .ktx2` checked;
    a missing file reported once; glTF 1.0 and meshopt explained in words.
  * 3ds Max: a warning icon when blocked, and an installable AGK button.
* **Calibrated against Unreal Engine 5.3 (6 Nov evening).** Importing the nine
  test models headlessly gave the same triangle count as the gate for every
  static mesh, once glTF parts placed several times are counted every time:
  Unreal bakes the copies in (CesiumMilkTruck 3,624, not 2,856). The gate now
  budgets that count (`unique_triangles` is in the stats). Unreal kept every
  triangle the old degenerate test had flagged in BoomBox and Corset, confirming
  the shape-based rule; `[mesh] degenerate = "block" | "warn" | "allow"` chooses
  what a genuinely flat triangle does. Unreal 5.3 imported nothing from a
  Draco-compressed .glb, so such files now carry a warning saying so.
* **Naming conventions (6 Nov evening).** `[naming]` takes one or more presets
  (`default`, `unreal`, `unity`, `gltf`, `substance`, `godot`) plus a custom
  convention (prefixes, endings, or a regular expression for textures and
  meshes); a name passes when it follows any of them. `--suggest-naming` reads a
  folder's existing names and prints a block that fits them. The same rules run
  in the gate, Maya, 3ds Max, Blender and the Unreal validator.
* **AssetHealth and PostgreSQL (found 6 Nov).** `--adopt-legacy` and
  `--remove-project`; a clearer 401 page. `migrate --sqlite` lost budget
  exceptions (now copied, with migration `0003_exceptions`); `--init-schema`
  records its migrations; `--plan` says "would apply".
* **GitHub.** `--report github:FILE` marks failing assets on a pull request in
  any repository; the workflow uses `codeql-action` v4 and uploads SARIF only
  where code scanning exists.
* **Repository.** Short README (the full manual is `docs/REFERENCE.md`),
  `CONTRIBUTING.md`, `SECURITY.md`, issue forms, and `tools/corpus_check.py`.

# Internal builds (before the public alpha)

## Since v1.1.1

* **Found by the developer's retest on 5 Nov, fixed and tested.**
  * Perforce `--require-approval` refused an approved `.obj` and `.mtl`:
    Perforce stores them as text and printed them to the trigger with CRLF
    line endings, while the approved copy in the Workstation had LF, so the
    content hashes differed. For `.obj`, `.mtl` and `.gltf` the trigger now
    also asks about the same text with LF and CRLF endings; any other change
    is still refused, and binary files are matched byte for byte.
  * The Workstation showed `#1` for a file that became `#2`: while the change
    is pending, `p4 describe` lists the revision being replaced. The trigger
    asks `p4 files <path>@=<change>` and shows no number rather than a wrong one.

* **Found by the developer's hands-on tests on 4 Nov (UE 5.3.2, P4V, Chrome), all fixed
  and tested.**
  * Perforce: an `.obj` submitted with its `.mtl` and texture was refused for a
    "missing reference" to the texture in the same changelist - each file was
    gated as it was fetched, the `.obj` before its texture, and the `.mtl` was
    not fetched at all without `--require-approval`. The whole changelist is
    now fetched first, laid out as in the depot, and referenced files already
    in the depot are fetched at `#head`.
  * Perforce to the Workstation (`--workstation`): the depot path now carries
    its revision (`//depot/...#3 @18`), and a mesh arrives with its `.mtl` and
    textures instead of each becoming an asset of its own.
  * Workstation: dropping an `.obj`, `.mtl` and texture together from Windows
    Explorer (alphabetical: `.mtl` first) made the `.mtl` and texture assets
    of their own. The mesh had no texture, its shippable LODs were exported
    with a 2x2 placeholder (black in Unreal), and approving it did not cover
    its `.mtl` at submit. Meshes now go first, and a texture shared by several
    dropped meshes is attached to each.
  * Mesh memory (G2) was 37-53% under Unreal's Size Map. It now counts the
    lightmap UV set and depth-only index buffer Unreal adds to every static
    mesh (`[mesh] lightmap_uv`, `depth_index_buffer`, both on by default):
    within 7% on all five test meshes.
  * Check for fixes said the fixed copy "is added as a new asset"; it becomes
    the next version, as the panel now says.
  * `tests/unreal/validate_in_editor.py`: UE 5.3 imports OBJ through
    Interchange, which ignores the import task's destination name, so the
    script could not find its own test mesh. It now loads what was imported.

* **Check for fixes searched the system temp folder (found 3 Nov).** The
  Workstation heals a throwaway copy of the asset, and the healer looked for
  other files referencing the texture in that copy's parent - the system temp
  folder. With more than 5,000 folders there it refused the fix ("could not
  finish checking"); with fewer it could take a minute, or refuse because some
  other program's FBX named the texture. It now searches only the copy (0.02 s).
  Since 1.1.1 (b957b56).

* **Independent audit of this round (2 Nov), all fixed and tested.**
  * The verdict cache lived in the folder being checked, so a committed
    `.agk-cache.json` could pass an over-budget file in CI. It now lives in the
    user's cache directory (CI: the runner's temp folder), and a `--cache`
    inside the checked folder is refused.
  * A changed `.bin` sent for an approved `.gltf` overwrote the approved
    version's file, approval intact. Reviewed or superseded versions are now
    frozen; the change becomes a new version. `--require-approval` checks
    `.bin`/`.mtl`/texture edits too.
  * `GET /api/approvals` answered with the first approval found; the newest
    decision across every version with those bytes now wins, and with accounts
    on only signed-in decisions count.
  * Perforce: `[[exceptions]]` never matched at submit (the temp copy's layout);
    the trigger now passes the depot path (`--exception-path`).
    `AGK_REQUIRE_APPROVAL=True` turned approval off; any case works now.
  * `[[totals]]` counted an FBX as 0 MB; it now fails the total, naming it.
  * Dropping a different file with a name already in the library merged it as a
    version; the viewer now asks (New version / Separate asset).
  * The budget planner's Save and its exception buttons always sent `Bearer
    undefined` (`data--token`): every save with a token was a 401. This bug
    predates 1.1.
  * Unreal: a mesh whose triangle count could not be read passed as 0
    triangles, in the new validator and in the older import hook; it now fails
    as not checked.
  * The alert digest walked timestamps to the second and could lose or repeat a
    failure; it walks verdict ids now. SARIF paths are percent-encoded URIs and
    a folder total points at `rules.toml`.

* **Big repositories (roadmap phase 4).**
  * `--once` keeps verdicts in a cache outside the folder (refused inside it):
    a file that has not changed -
    nor anything it references, the rules or the gate - is not read again (600
    heavy glTF/GLB: 16 s, then 0.7 s). `--no-cache`, `--cache FILE`; the GitHub
    workflow keeps it between runs with `actions/cache`.
  * `--jobs N` checks files in parallel processes (`0` = one per CPU), with the
    same verdicts in the same order.
  * Art Workstation: notes pinned to the model - a numbered pin, the camera and a
    picture of the view; clicking it (or a `?note=` link on another machine)
    flies back to that view; the printable report includes the pictures.
  * Unreal: `plugins/unreal/agk_validator.py` runs the rules through Data
    Validation - on save, Validate Assets and Validate Changelist - on
    Unreal's own numbers (static meshes: LOD0 triangles, slots, UV channels;
    textures: size, power of two, naming), with `[[exceptions]]` matched on
    the `/Game` path. `tests/unreal/validate_in_editor.py` checks it in an
    editor.
  * Art Workstation: **shippable LODs** (tick Shippable before Generate): a
    quadric edge-collapse decimator that keeps UVs, normals and materials, locks
    UV seams and material borders, records each level's surface error, and
    writes `.glb` levels with their textures to download and import. Also
    `python engine/lod_shippable.py model.glb --ratios 0.5,0.25`. No new
    dependency (meshoptimizer has no Windows wheels; pyfqmr and
    fast-simplification move vertices and drop UVs).
  * Fixed: the header said "1 notes" / "1 LODs", and its note count lagged one
    note behind.

* **Inspection views and textured FBX (roadmap phase 3).**
  * Viewer: **UV check** (numbered checker; pink where a mesh has no UVs), **Texel**
    (density heat map against the mesh's median), **Normals** and **Scale** (a
    1.8 m person and metre grid) views, with the size and median texel density in
    the readout.
  * FBX previews now carry UVs, the file's normals and each material's
    base-colour texture (when it was uploaded with the FBX); before they were
    flat colours with recomputed normals.
  * Gate: `max_texture_mb` / `max_mesh_mb` GPU memory budgets (BC7/BC5 by
    default, mips included), per-folder and per-platform, and texel density
    limits (`texel_density_min` / `max`); `[[totals]]` caps a folder's combined
    memory.
  * Fixed: a version with no depot path showed compare buttons and an empty Copy
    button (the `hidden` attribute lost to the stylesheet); the toolbar overflowed
    a phone screen once the new buttons were in.

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

## v1.1.1 (2026-11-02)

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

## v1.1.0

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

## v1.0.0 - The Indie / Small-Studio Edition

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
