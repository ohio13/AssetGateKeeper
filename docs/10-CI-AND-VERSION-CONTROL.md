# 10 - CI and version control

The gate is most valuable where work crosses a boundary: from a working copy into
a depot, or from a branch into main. This page covers every place that can happen.

## The strategy that works

Per discipline, per boundary, in this order:

1. **warn only** for two weeks - collect the false positives;
2. fix the budgets, not the assets, for everything that was a false positive;
3. **block** for that discipline;
4. only then block for the next one.

Blocking everything on day one creates a shadow pipeline in week two, and you
find out in month three.

## Git

### Pre-commit hook

```
vcs_hooks/git_pre_commit.py
```

Install once per clone, from inside it:

```
python "<AGK>\vcs_hooks\git_pre_commit.py" --install
```

That writes a small `.git/hooks/pre-commit` that runs the script where it is
(an existing hook is kept as `pre-commit.bak`). A plain copy of the script
into `.git\hooks` cannot find `asset_gatekeeper.py` unless `AGK_GATEKEEPER`
names it; call the script from your own hook if you already have one. It gates
the staged files only, exits non-zero on problems, and prints one line per
violation - which is what makes people keep it enabled rather than `--no-verify`-ing
it forever.

### GitHub Actions / any CI

```yaml
- name: Asset gate
  run: |
    pip install "asset-gatekeeper[full]"     # or a wheel from the GitHub release
    asset-gatekeeper assets --once --json --no-color --no-notify > gate.json

- name: Upload the verdicts
  if: always()
  uses: actions/upload-artifact@v4
  with:
    name: gate-verdicts
    path: gate.json
```

Exit codes are the contract: `0` clean, `1` budget failure, `2` the gate could
not run (bad rules, missing folder). Give code 2 a different colour in your
dashboard; it usually means a bad merged `rules.toml`, not a bad asset.

### Reports your CI server shows per asset

```
asset-gatekeeper Assets --once --recursive --report junit:gate.xml --report sarif:gate.sarif
```

* **JUnit** (`junit:<file>`): one test case per asset, named by its path, with
  the failure reason as the message; an unreadable file is an *error*, not a
  *failure*. Jenkins (JUnit plugin), TeamCity (XML report processing), Horde,
  GitLab (`artifacts:reports:junit`) and Azure DevOps (Publish Test Results)
  show each failing asset as its own red line.
* **SARIF** (`sarif:<file>`): one result per problem of each failing asset. The
  shipped GitHub workflow uploads it with `github/codeql-action/upload-sarif@v4`
  where code scanning is available (public repositories, or GitHub Advanced
  Security; set the repository variable `AGK_CODE_SCANNING=true` for a private
  repository that has it).
* **GitHub annotations** (`github:<file>`): one `::error file=...` workflow
  command per problem. The shipped workflow prints the file, so each failing
  asset is marked on the pull request's Files tab in any repository, free or
  paid.

Reports are written by `--once` runs and single-file runs; a watch ignores the
flag.

### Big repositories: keep the cache between runs

A `--once` run keeps each verdict in a cache file outside the asset folder.
Unchanged files - with the files they reference, under the same rules and the
same gate version - are not read again, so a nightly audit of a large depot
checks only what changed since last night. `--jobs N` checks the rest in
parallel.

```
asset-gatekeeper Assets --once --recursive --jobs 0 --cache "$RUNNER_TEMP/agk-cache/verdicts.json"
```

On GitHub Actions, keep it with `actions/cache/restore` and `actions/cache/save`
(`if: always()`, so a failing run is cached too), keyed on the commit with a
fallback to the last cache (the shipped workflow does this): an old entry is harmless, it is re-checked when its
file changed). On Jenkins or TeamCity, keep it in the agent's workspace or the
build cache. What invalidates an entry: the file's size and time (when its bytes
really changed - a checkout that only touches times is hashed and kept), a
referenced `.mtl`, texture or `.bin` that changed or appeared, any rule, the
gate's code or version, and an exception reaching its last 14 days. With
healing on, cached failures are always re-checked.

Keep the cache out of the checkout (the runner's temp folder, or the agent's own
cache): the gate refuses a `--cache` inside the folder it checks, because a cache
file committed with the assets could otherwise pass anything.

### Post the verdicts while you are there

```
asset-gatekeeper assets --once --json --telemetry-url "$AGK_TELEMETRY"
```

Now the CI run feeds the same `/analytics` the artists see, and a producer can
watch a branch's compliance without asking anyone.

## Git LFS

A pointer file is 130 bytes of text. Validating it as if it were art is the
classic silent failure, so the gate detects pointers by **content** and resolves
them through `git lfs smudge`:

```
normal_Albedo.png is a Git LFS pointer and 'git' is not on PATH, so its real
(41,943,040 byte) content cannot be read
```

That message is the feature. If the LFS client is missing, the gate refuses to
judge the file rather than reporting a 130-byte texture as clean.

## Perforce Helix Core

### Pre-submit trigger

```
vcs_hooks/p4_pre_submit.py
```

Add it as a `change-content` trigger. `change-submit` is the wrong hook: it fires
before the content exists in the depot, so `p4 print //depot/...@=<change>` has
nothing to resolve and the trigger fails for the wrong reason. `change-content`
runs after the content has been received and before the commit lands, which is
exactly the window a gate needs. It:

* resolves each file in the changelist to its **depot** revision (`p4 print -q
  //depot/...@=<change>`), not the artist's workspace, so what is validated is
  what will be stored;
* gates those revisions with the project's rules: the nearest `rules.toml`
  above each file in the depot, at its submitted `#head` (so a `rules.toml`
  edited in the same changelist does not loosen its own gate), or the file
  given with `--rules` on the trigger line;
* exits non-zero to refuse the submit, or prints warnings for advisory mode.

Every file of the changelist is fetched **before** any is gated, laid out as in
the depot, so an `.obj` submitted with its `.mtl` and texture finds them. A file
a mesh references that the changelist does not carry (a texture submitted
earlier) is fetched at its depot `#head`; one that is in neither is a real
broken link and fails. (Until Nov 2026 each file was gated as soon as it was
fetched, so an `.obj` went before its own texture and was refused for a
"missing reference".)

With `--require-approval`, the file's content hash is looked up in the Art
Workstation. Perforce stores `.obj`, `.mtl` and `.gltf` as text and hands them
out with the server's line endings, so for those three the same text with LF and
with CRLF endings counts as the approved content; every other change, and every
byte of a binary file, must match exactly.

Validation happens in a temp folder that is deleted afterwards. No sync, no
workspace, nothing left behind.

#### Installing it on the server

The trigger runs **inside the Perforce service**, not as an artist. The service
has no `P4PORT`, no `P4USER` and no login, so the trigger's own `p4 describe` /
`p4 print` calls must be told where the server is and who to read the depot as.
Without that, `p4` dials its default `perforce:1666` and every submit is refused
with "TCP connect to perforce:1666 failed" (found on a real P4 Server 2026.1).

1. Pick (or create) a Perforce user the trigger reads the depot as, e.g.
   `agk-trigger`, with read access to the gated paths.
2. Give it a ticket that works from any host, in a file the service can read:

   ```
   set P4TICKETS=C:\p4\hooks\p4tickets.txt
   p4 -u agk-trigger login -a
   ```

   For an unattended server, put that user in a group with a long or unlimited
   `Timeout`, otherwise the ticket expires (12 hours by default) and the gate
   fails closed until someone logs in again.
3. Install the trigger (`p4 triggers`), one line, tabs between fields:

   ```
   AssetGatekeeper change-content //depot/... "python C:\p4\hooks\p4_pre_submit.py %changelist% %user% --port %serverport% --p4user agk-trigger --tickets C:\p4\hooks\p4tickets.txt"
   ```

   Use the full path to a Python that has `trimesh`, `pillow` and `numpy`
   installed - the service does not see a per-user `PATH`. `p4` itself must be
   on the system `PATH`, or pass `--p4 <path to p4.exe>` (no spaces) or set
   `AGK_P4`. `AGK_P4PORT`, `AGK_P4USER` and `AGK_P4TICKETS` work in place of
   the flags.

Verified on a real P4 Server 2026.1 (Windows): a clean FBX submits; a 240k-
triangle FBX is refused with the reason in the submit output; a depot
`rules.toml` raising the budget lets it through; a `rules.toml` lowered in the
same changelist does not loosen its own gate; a 6-influence skin is refused;
and a trigger pointing at a missing script refuses the submit (fails closed).

### Sign-off before submit, and a review queue

Two optional trigger flags connect the submit to the
[Art Workstation](07-ART-WORKSTATION.md#versions-sign-off-and-where-a-file-came-from):

* `--workstation http://review-pc:8100 --workstation-token <its --token>` sends
  every asset of a submit that passes to the workstation for review, with its
  depot path and changelist. It never blocks a submit on its own.
* `--require-approval` (with `--workstation`) also refuses any asset whose exact
  content has not been **Approved** there. The rejection names the file and its
  review state. If the workstation cannot be reached, the submit is refused
  (fails closed). Artists upload the file and ask a reviewer before submitting.
  The files an asset carries (`.bin`, `.mtl`, `.tif`, `.dds`, `.exr`, `.hdr`)
  are checked too: a changelist that only edits `rock.bin` needs a reviewed,
  approved version holding that exact `rock.bin`. Use it with accounts on: a
  typed-name approval stops counting once accounts exist.

Both can come from the environment instead: `AGK_WORKSTATION`,
`AGK_WORKSTATION_TOKEN`, `AGK_REQUIRE_APPROVAL=1` (`true`, `yes` and `on` work too, in any case).

### Validating a depot path from anywhere

```
agk-enterprise validate //depot/proj/main/env/cliff.obj@head
agk-enterprise validate 123456                       # a whole changelist
agk-enterprise validate https://art.internal/x.glb   # a URL
```

That command is the building block for the trigger, for a runner, and for a
one-off "is this file in the depot actually clean?" question from a lead. It is
read-only with respect to Perforce: `info`, `describe`, `files`, `print` and
nothing else.

## Runner farms

When validation must happen on a machine that is not the artist's:

```
# on a spare box
agk-enterprise hub --port 9000 --token $AGK_RUNNER_TOKEN
# submit work
agk-enterprise hub --submit //depot/proj/main/env/...
# workers
agk-enterprise runner --hub http://hub:9000 --token $AGK_RUNNER_TOKEN --jobs 4
```

| Endpoint | Purpose |
|---|---|
| `POST /claim` | a worker takes a job (204 when idle) |
| `POST /result` | the verdicts come back |
| `GET /status` | queue depth, for a dashboard |

Auth is one bearer token: runners are machines, and a machine that needs a
session cookie is a machine that will paste one into a CI variable. Every fetch is
size-capped and timed out; a bad asset produces an error report, not a stuck
worker.

## Choosing the boundary

| Studio size | Where to gate | Why |
|---|---|---|
| 1 | CLI, before export | nothing else is worth the setup |
| 2-10 | Git pre-commit + CI | the repo is already the source of truth |
| 10-50 | + DCC panels | catch it before the artist wastes an export |
| 50+ | + Perforce trigger + runners | the submit is the only boundary everyone crosses |
| 200+ | + SIEM + RBAC + replicas | see [Enterprise](ENTERPRISE.md) |

## CI checklist

```
[ ] the gate runs with --once --json --no-color
[ ] big repositories keep the verdict cache between runs (--cache outside the checkout)
[ ] exit code 2 is treated differently from 1
[ ] verdicts are uploaded as an artifact
[ ] telemetry is posted (so /analytics has CI data too)
[ ] rules.toml is reviewed like code
[ ] the job pins a version (no "latest" in a build)
[ ] a failing gate prints the problems, not just "1 test failed"
```

Next: [11 Troubleshooting](11-TROUBLESHOOTING.md).
