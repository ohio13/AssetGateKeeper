# 01 - What AssetGatekeeper is, and what it refuses to be

## The problem, stated plainly

Every game project has a memory of what an asset "should" cost: 4,000 triangles
for a prop, 2,048 pixels for an albedo, four material slots, no missing
textures. That knowledge lives in three places that do not talk to each other:

* in a wiki page nobody reads after month three;
* in the head of the technical artist who wrote it;
* in the build that fails at 2 a.m. when the memory of the content is already 40%
  over the platform's budget.

AssetGatekeeper moves that knowledge into a file next to the assets, and then
applies it in four places: the terminal, the DCC, the submit trigger and CI.

## The model: one asset, one verdict

Everything in this tool is built on a single record:

```
verdict
  timestamp      when the gate ran
  filename       what was checked
  passed         did it fit the budget
  status         PASSED | FAILED | HEALED
  metric         "12,345 tris / 3 mat / 2048px"   (human-readable summary)
  problems[]     one line per violation, with numbers
  project_id     which project it belongs to
```

A verdict is **evidence**, not an opinion. It carries the numbers it was based
on, the budget it was compared against, and the exact problems.

Three consequences follow, and they explain most of the design:

1. **The same verdict can be produced anywhere.** The CLI, the Blender panel,
   the Perforce trigger and CI all call the same `Budget` and the same checks.
   They cannot drift, because they are the same code path.
2. **A verdict can be aggregated.** Compliance trends, polygon growth and error
   clusters are all derived from verdicts - no separate analytics pipeline.
3. **A verdict is not a note.** The reviewer's opinion lives in the workstation
   in its own table. Machine findings and human findings are stored and displayed
   separately so nobody ever has to guess which is which.

### The three statuses

| Status | Meaning | What happens next |
|---|---|---|
| `PASSED` | every active check passed | exported, filed into the depot |
| `FAILED` | at least one check failed | the artist fixes it, or a lead overrides the budget for that path |
| `HEALED` | it failed, the healer repaired it, and the repaired file verified as passing | the repaired file is adopted; the original problem is still recorded |

`HEALED` exists because "it passed" and "it passed after we rewrote your file"
are not the same fact. Both count as acceptable in compliance numbers, and both
remain visible.

## The pipeline

```
   artist's DCC                gate                 review              metrics
+----------------+      +----------------+    +--------------+    +-------------+
| Blender/Maya/  | ---> | asset-gatekeeper| -> | Art          | -> | AssetHealth |
| Max/Houdini/UE |      |  (budget check)|    | Workstation  |    |  dashboard  |
| pre-flight     |      |  verdict       |    | 3D + notes   |    |  trends     |
+----------------+      +----------------+    +--------------+    +-------------+
        |                       |                    ^                    |
        | export folder         | telemetry POST     | server-to-server   |
        v                       v                    | bridge             |
    rules.toml  <---------  SQLite / PostgreSQL  -----+--------------------+
```

Nothing in that diagram is mandatory. A solo developer can use the first box and
the second one and ignore the rest forever.

## What it is deliberately not

These are decisions, not gaps. Each one has a reason, and each one is enforced.

**Not a style guide.** It checks what an engine can measure: counts, sizes,
references. It will not tell you the silhouette is wrong or the topology is ugly.
It has opinions about *cost*, not *quality*.

**Not a file converter.** It never silently changes an asset's format. The
healer resizes and pads textures, and only adopts the result if the repaired
file passes under the same budget - otherwise the original is untouched.

**Not a cloud service.** There is no account, no upload, no telemetry to us. The
two databases are files. Deleting the folder deletes the product.

**Not a policy engine that can be fooled by naming.** A tag is a folder name or a
filename prefix - never a pattern. A category can only *tighten* a budget, never
switch a check off. A misspelled folder is reported with the name you probably
meant, and it still applies the budget it resolved to, visibly.

**Not a silent pass.** If it cannot measure a format (`.blend`, `.max`, `.ma`,
`.usd`), it says so, stores the asset as `unverified`, excludes it from triangle
totals, and prints "not measured" rather than "0". A dashboard that reports 0
triangles for a 4-million-triangle `.blend` is worse than no dashboard.

**Not a lock-in.** Every number it produces comes from a text file you can read
and a SQLite file you can open with any tool.

## Who each surface is for

| Surface | Used by | Runs as |
|---|---|---|
| `asset-gatekeeper` | tech artists, CI, submit triggers | CLI, one process per run |
| DCC panels | artists, before export | add-on inside the DCC |
| Art Workstation | reviewers, art directors, outsourcing partners | local web app, port 8100 |
| AssetHealth | producers, technical directors | local web app, port 8080 |
| `agk-enterprise` | pipeline engineers | CLI against a central database |

## Scale characteristics, measured

| Thing | Number | Where it comes from |
|---|---|---|
| Walk speed | 13,796 files in 1.97 s | a real 42k-file project folder, warm NTFS cache |
| Full gate run | 869 assets, 108 passed / 761 failed | a real retro profile applied to PC-era content |
| Verdict write | one row + N problem rows, ~1 ms local | SQLite WAL on the same disk |
| Dashboard memory | tens of MB | stdlib HTTP server, no framework |
| Workstation upload | 32 MB per file, 20 sidecars | `DEFAULT_SIDECARS` |
| Test suite | 1,134 assertions | `tests/` |

The largest thing on the list is the file walk, which is why it is written as a
single pass with a skip callback rather than trying to be clever.

## The vocabulary

| Term | Means |
|---|---|
| **budget** | the numbers a check compares against: triangles, slots, edge size |
| **rules.toml** | the project file that supplies those numbers and the policy flags |
| **preset** | a shipped budget for a hardware era (`retro`, `ps2`, `pc`, ...) |
| **platform profile** | a `[platforms.*]` table a project defines itself |
| **tag** | a folder name or filename prefix that selects a tighter budget |
| **verdict** | the recorded result of one gate run over one asset |
| **problem** | one violation line inside a verdict |
| **kind** | what the gate thinks the file is: `mesh`, `texture`, `unverified`, ... |
| **healed** | repaired by the healer and re-verified as passing |

Next: [02 Installation](02-INSTALLATION.md), or jump straight to the path that
matches you in [the index](README.md#start-here).
