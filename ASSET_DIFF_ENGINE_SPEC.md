# Asset Diff Engine — specification (memory bank)

**Status:** design only. No code in this repository implements it yet.
**Scope:** the next subsystem *after* the gate, the healer and AssetHealth.
**Owner:** pipeline engineering.

This document is the memory bank for that work: what we intend to build, the data
model we intend to store it in, the algorithms we intend to use, and — just as
importantly — what we have decided *not* to promise.

---

## 1. The problem

The gate answers "is this asset within budget?" and nothing else. It cannot answer
the next question every lead asks in a review:

> "What actually changed since the artist's last submit, and did it change the way
> the asset *looks*?"

Yesterday's answers are manual: open two files in two DCC sessions, scrub, squint.
That does not scale to a changelist, cannot run on a build machine, and leaves no
record for the next review.

Three concrete failures this engine should prevent:

1. **Silent visual regressions.** A texture is re-exported with a colour profile
   change; every budget passes; the model ships slightly pink.
2. **Unreviewed geometry churn.** A "minor" fix rewrites 40% of the vertex buffer,
   invalidating every skinned pose the animators tuned.
3. **Unbounded growth.** Nobody notices a prop creeping from 1,800 to 9,400 triangles
   across eleven commits, because each individual commit passed the 50,000 cap.

---

## 2. What we will build (and not)

| Deliverable | In scope | Out of scope |
| :--- | :--- | :--- |
| Binary change detection per commit | yes | — |
| Per-asset structural signature (topology, materials, UVs, vertex normals) | yes | full semantic diffing of rigs |
| "Did it look different?" verdict per texture | yes | perceptual quality judgement |
| Geometry drift metrics (added/removed/moved vertices, Hausdorff distance) | yes | automatic re-topology |
| Side-by-side + overlay images for review | yes | interactive 3D viewer server |
| Author attribution and change narrative | partial (from VCS metadata) | intent inference |

**Non-goal, stated loudly:** this engine will not tell you whether a change is
*good*. It reports *what* changed, with numbers and pictures. Approval stays human.

---

## 3. Data model

New tables in `asset_health.db`, all keyed by project so tenancy carries over:

```sql
-- One row per (asset, commit): the immutable facts of a submitted binary.
CREATE TABLE asset_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    asset_path    TEXT    NOT NULL,          -- repo-relative, normalized
    rev           TEXT    NOT NULL,          -- git sha / p4 change number
    author        TEXT    NOT NULL DEFAULT '',
    captured_at   TEXT    NOT NULL,
    size_bytes    INTEGER NOT NULL,
    sha256        TEXT    NOT NULL,          -- exact bytes
    content_hash  TEXT    NOT NULL,          -- normalized payload (see §4)
    metrics_json  TEXT    NOT NULL,          -- triangles, verts, materials, uv sets...
    UNIQUE (project_id, asset_path, rev)
);

-- Derived, expensive facts, cached so a diff never re-parses the same file twice.
CREATE TABLE asset_signatures (
    snapshot_id     INTEGER PRIMARY KEY REFERENCES asset_snapshots(id) ON DELETE CASCADE,
    signature_json  TEXT NOT NULL,           -- quantized geometry/material digest
    thumbnail_png   BLOB                     -- small render or downsampled texture
);

-- One row per comparison, so a review can be replayed and linked from chat.
CREATE TABLE asset_diffs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    asset_path    TEXT    NOT NULL,
    base_snapshot INTEGER NOT NULL REFERENCES asset_snapshots(id),
    head_snapshot INTEGER NOT NULL REFERENCES asset_snapshots(id),
    verdict       TEXT    NOT NULL,          -- IDENTICAL / COSMETIC / STRUCTURAL / VISUAL
    summary_json  TEXT    NOT NULL,          -- the numbers behind the verdict
    created_at    TEXT    NOT NULL
);
```

`sha256` answers "are the bytes the same?"; `content_hash` answers the more useful
"is the *asset* the same?" — the same mesh re-exported with a different order of
vertices in the buffer is byte-different and content-identical, and that distinction
is most of the value.

---

## 4. Signatures: the cheap half

Every ingest computes a signature. It must be deterministic, cheap, and stable
across exporter versions — so it is built from *decoded* structure, never from
file layout.

**Meshes (`.obj`, `.gltf`, `.glb`)**

- Vertex positions quantized to a fixed grid (default 1e-4 of the bounding box
  diagonal), sorted into a canonical order, then hashed.
- Triangle set as sorted, vertex-index-remapped triples (so index reordering is
  invisible), hashed.
- Per-attribute digests: material slot names + base colours, UV set count and
  per-set checksum, vertex-normal checksum, bone-weight histogram.
- Aggregate `signature_json` = `{topology: h, positions: h, materials: h, uvs: h,
  norms: h, bbox: [...], triangles: n, vertices: n}`.

**Textures (`.png`)**

- Downsample to 64×64 (LANCZOS), quantize to 5 bits per channel, hash. Catches
  "re-exported with a different profile" while ignoring a 2-pixel metadata change.
- Store the full-size SHA-256 separately for byte-exactness.

**Cost ceiling:** a signature must compute in under 250 ms for a 50k-triangle mesh.
That is the budget the gate already proves is affordable, and it keeps the engine
inside the per-asset time the pipeline can absorb.

---

## 5. The verdict ladder

A diff returns the **highest** rung it can prove, never a guess:

| Verdict | Test | Typical cause |
| :--- | :--- | :--- |
| `IDENTICAL` | `sha256` equal | nothing happened |
| `REPACKED` | `sha256` differs, `content_hash` equal | re-export, different buffer order |
| `STRUCTURAL` | topology hash differs | faces added/removed, retopo, decimation |
| `COSMETIC` | topology equal, materials/UVs/normals differ | repaint, UV move, normal recalc |
| `VISUAL` | texture pixel digest differs | the image looks different |

The ladder is deliberate: a `REPACKED` diff should never wake an artist, and a
`STRUCTURAL` one always should.

---

## 6. Geometry diff (the expensive half)

Structural comparison runs only for `STRUCTURAL`, and only on explicit request
(CI job, review click) — never on every submit.

1. **Match** base and head vertices by quantized position (spatial hash on the
   quantization grid, O(n)).
2. **Classify** matched vertices as *moved* (position delta > ε) or *unchanged*.
3. **Unmatched** vertices are *added* on the head side, *removed* on the base side.
4. **Faces** are compared as vertex-id sets after matching: same set = same face.
5. **Drift metrics**: max and mean vertex displacement, added/removed face counts,
   bounding-box delta, and a two-sided Hausdorff-ish sample (k random head vertices
   → nearest base vertex, and vice versa) to catch gross silhouette changes cheaply.

Reported as numbers first, pictures second:

```json
{
  "verdict": "STRUCTURAL",
  "triangles": {"base": 1800, "head": 9400, "delta": 7600},
  "vertices": {"matched": 1100, "moved": 40, "added": 7300, "removed": 12},
  "drift": {"max": 0.041, "mean": 0.008, "units": "m"},
  "materials": {"base": ["wood", "metal"], "head": ["wood", "metal", "glass"]}
}
```

**Cost control:** the sample count and the vertex cap are configurable; above the
cap the engine reports "too large to diff precisely" rather than lying or hanging.

---

## 7. Visual diff

Textures are the common case and the easiest win:

- Emit a three-panel PNG: base | head | amplified difference (per-channel delta,
  gamma-corrected so small shifts are visible without amplifying noise).
- Report `changed_pixel_ratio` and the maximum per-channel delta. A 0.4% change is
  a profile fix; 40% is a re-paint.

For meshes, render a silhouette from three fixed orthographic views using a minimal
software rasterizer (standard library + numpy, no OpenGL), and diff the coverage
masks. This is coarse on purpose: it answers "did the shape change enough to see?",
which is the question a reviewer actually asks.

---

## 8. Integration points

| Surface | Behaviour |
| :--- | :--- |
| `asset_gatekeeper.py` | `--signature` writes a signature JSON next to the verdict; telemetry gains `signature` and `sha256` |
| Ingest (`POST /api/telemetry`) | stores `asset_snapshots` rows when the payload carries a signature |
| VCS hooks | `vcs_hooks/*` pass the commit id, so the snapshot is attributable |
| AssetHealth server | `GET /api/diff?asset_path=&base=&head=` returns the diff JSON; `/diff?…` renders the three-panel page |
| CI | a nightly job diffs `HEAD` against the release tag and posts one digest, not one message per asset |

---

## 9. Phasing

**Phase 1 — signatures only.** Snapshot writing, `IDENTICAL`/`REPACKED`/`VISUAL`
for textures, storage and retention. No geometry matching. Delivers the "did the
image change?" answer and the growth history, and is small enough to ship safely.

**Phase 2 — structural geometry diff.** Vertex matching, drift metrics, the
`STRUCTURAL`/`COSMETIC` split. Needs the quantization and matching work in §6 and a
solid corpus of real exports to tune ε.

**Phase 3 — visual review surface.** Three-panel texture images, mesh silhouettes,
a `/diff` page, and a Slack digest that links to it.

Each phase ships independently and is useful on its own. Nothing here requires the
gate to become slower: signatures are opt-in, and diffs run out-of-band.

---

## 10. Risks and decisions to revisit

- **Quantization ε is a policy, not a fact.** Too tight and a re-export looks
  structural; too loose and real edits vanish. It must be per-project and tuned
  against real data before Phase 2 ships.
- **Storage growth.** Two revisions of every asset is a lot of blobs. Phase 1 should
  store metrics always, thumbnails only on change, and bytes not at all — the VCS
  is the blob store; we keep hashes and derived facts.
- **Diff of a diff.** Comparing a snapshot to its immediate predecessor is easy; the
  interesting comparison is "this branch vs the release tag", which needs a
  migration path for snapshots taken before the tag existed.
- **Rigged meshes** add skinning weights and bone order to the signature. Decided:
  include the weight histogram in Phase 1, defer per-bone analysis.
- **Never guess.** If a file cannot be parsed, the diff is `UNKNOWN` with the parse
  error attached. A false "no change" is worse than an explicit failure.

---

## 11. Open questions for the team

1. Is "did it look different?" sufficient for art approval, or is a per-region
   comparison required in Phase 3?
2. Which is the review unit: a commit, a changelist, or a named milestone?
3. Do we need per-vertex attribution (which vertices did *this* artist move), or is
   asset-level attribution enough?
4. Where should the diff run — build machine only, or also on an artist's machine
   before submit?
