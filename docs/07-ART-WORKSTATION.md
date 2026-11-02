# 07 - Art Workstation: the 3D review screen

A local web app for looking at art without a DCC, a licence, or a download. A
reviewer opens a URL, orbits the asset, reads the gate's verdict and leaves a
note. It runs on one machine and stores everything in two places you can copy.

* **URL:** `http://127.0.0.1:8100`
* **Start:** `art-workstation` (or `agk-serve up`, which starts both servers)
* **Database:** `art_workstation.db`
* **Files:** `workstation_data/assets/<id>/` (one folder per asset: the file, the
  files it references, and the server's previews in `.agk/`); LODs in
  `workstation_data/lods/<id>/`

## What it is for

| Question | Answer here |
|---|---|
| "Is this asset broken, or just ugly?" | the gate verdict panel, next to the model |
| "Does it look right from behind?" | orbit, zoom, wireframe-free realtime view |
| "Is the LOD chain sane?" | a level selector that shows each LOD's cost |
| "Does it have collision?" | the collision proxy is stored and viewable |
| "What did review say?" | notes, categorised, timestamped, per asset |
| "Can I look at thirty exports at once?" | bulk upload, one folder |

## Loading assets

Three ways in:

1. **Drag files** onto the page, or use **Upload asset** (several at once).
2. **Bulk upload** a folder:
   ```
   art-workstation-upload "C:\Proj\export\env" --workstation http://127.0.0.1:8100
   ```
   This is the path for a review pass: point it at the export folder, walk away.
   (`--url` is the same flag; sub-folders are always walked.)
3. **From AssetHealth** - the dashboard's "Send to workstation" button posts the
   asset server-to-server, so a producer can triage failures without touching a
   file system. The token never reaches the browser: AssetHealth reads the file
   and posts the bytes.

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
| Referenced files | `.bin`, `.mtl`, textures an asset names | stored in the asset's folder (64 files, 64 MB each) |
| Source files | `.blend`, `.max`, `.ma`, `.mb`, `.usd*` | stored as **unverified**: reviewable, noteable, **excluded from triangle totals** |
| Executables | `.exe`, `.dll`, `.ps1`, `.sh`, `.msi`, `.bat`, `.cmd` | **refused with HTTP 415** |

A file in a measurable format that cannot be read - a truncated `.fbx`, a `.glb`
of random bytes, an image that will not open - is stored as **broken**: shown in
red, listed under **Needs attention**, kept out of the triangle total, with the
reason. Broken is a problem to fix; unverified is a format nothing here opens.

### FBX previews are conversions, and the viewer says so

The browser cannot draw FBX, so the server converts its geometry to a `.glb`
(`engine/fbx_preview.py`) and the viewer draws that, under a banner that stays on
screen for the FBX and its LODs:

> **Converted preview** - You are viewing a .glb converted from this FBX, not the
> FBX itself. Geometry only: one flat colour per material slot, no textures,
> animation or skinning; pivots are not applied, so placement can differ from
> your DCC or engine. The numbers on the right are measured from the FBX.

What is drawn is what the gate counts: Unreal collision meshes (`UCX_` and
friends) and LOD1+ levels of an LOD group are left out, axes and units are
converted to Y-up metres, and each material slot gets its own colour, named
after the FBX material.

The unverified category is the important one. A `.blend` cannot be measured
without Blender, so the tool says so instead of reporting `0 triangles`. The
asset appears in the library as `unmeasured .blend`, its report says "not
measured", and no budget is applied to it - because a zero that means "unknown"
is how a project ships a four-million-triangle boss with a green dashboard.

## The library

The landing view lists every asset with three filters:

* **All**
* **Needs attention** - a gate verdict of FAILED, or a broken file
* **Passed**

Each card shows the geometry summary, the LOD count, the note count and the gate
badge. Click a card to open the viewer.

## The viewer

| Area | What it does |
|---|---|
| viewport | orbit (drag), pan (right-drag), zoom (wheel); no wireframe clutter by default |
| readout | triangles and vertices at the current LOD, as a percentage of the source |
| LOD selector | one entry per stored level; the readout shows that level's cost and share |
| gate verdict panel | status, problems, tag, and where the verdict came from (CLI / CI / dashboard) |
| notes | add, categorise, read; machine text is never mixed into them |
| collision | the stored proxy, if any |

### The gate verdict panel, and why it exists

A reviewer needs to know whether the numbers are an opinion or a measurement. So
the panel is separate from the notes, and it is honest about provenance:

```
Gate verdict            FAILED
problems                72,500 triangles exceeds the 50,000 budget (+22,500)
                        4 material slots exceeds the 3 slot budget
tag                     environment rock
source                  cli (ws-ana, 2026-10-31 14:02)
```

`source` is `cli`, `ci` or `dashboard`. A verdict with no source is a verdict a
human typed, and the panel says so.

## LODs

Upload a source mesh and then upload LODs: each subsequent upload of the same
name at a lower level is stored as a level, not as a new asset. The viewer's
selector then shows each level's triangles and its share of the source, which is
the number that decides whether the chain is worth keeping.

Two rules the viewer enforces quietly:

* a LOD heavier than its parent is flagged - a "LOD" that costs more is a bug;
* the source mesh is the denominator, so percentages stay meaningful even if the
  artist re-uploads.

## Collision

A collision proxy is stored per asset and viewable. It is not validated by the
gate (collision budgets are engine-specific), but it is captured next to the
asset so a reviewer can see whether it exists at all - which is the question that
actually gets asked.

## Notes

Notes are per asset, categorised, and attributed. They are deliberately not free
text in the verdict: the whole point of the verdict panel is that a machine's
finding and a human's opinion stay distinguishable six months later.

## Formats and the 415 rule

Executables are refused because an asset store is not a file share. Everything
else is either measured or stored-and-labelled. If a format is refused that you
believe should be stored, the suffix list is a constant (`REFUSED_SUFFIXES`) and
the fix is one line plus a test.

## Running it in a studio

* **Start it where the reviewers are**, not on an artist workstation: one
  workstation per review room is enough, and it keeps the files on one disk.
* **Put it behind your intranet proxy.** It has no TLS of its own; terminate TLS
  in front of it, exactly like the other internal dashboards.
* **Back it up** by copying `art_workstation.db` and `workstation_data/`. Those
  two things are the entire state.
* **Prune it**: a review room accumulates gigabytes. The library has a delete
  action per asset; a quarterly prune is normal.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 415 on upload | the file is an executable | that is intentional; store the export, not the tool |
| "unmeasured" on a `.glb` | `trimesh` not installed | `pip install trimesh` |
| "waiting for its files" | the `.gltf`/`.obj` was uploaded without what it references | select it and drop the named files onto the page |
| Model loads, textures black | the textures it references were not uploaded | drop them onto the page with the asset selected |
| "broken" | the file cannot be read in a format that should be measurable | re-export it; the reason is in the tooltip and the report |
| Verdict panel empty | the asset was uploaded directly, not sent from AssetHealth | run the gate with `--telemetry-url`, or use "Send to workstation" |
| Port already in use | another instance | `agk-serve status`, or `--port 8101` |

Next: [08 AssetHealth](08-ASSETHEALTH.md) for the metrics side.
