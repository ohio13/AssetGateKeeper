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

Install once per clone:

```
copy "<AGK>\vcs_hooks\git_pre_commit.py" .git\hooks\pre-commit
```

(or call it from `.git/hooks/pre-commit` if you already have a hook). It gates
the staged files only, exits non-zero on problems, and prints one line per
violation - which is what makes people keep it enabled rather than `--no-verify`-ing
it forever.

### GitHub Actions / any CI

```yaml
- name: Asset gate
  run: |
    pip install asset_gatekeeper-1.0.0-py3-none-any.whl "trimesh>=4" "pillow>=10"
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
* gates those revisions with the project's rules;
* exits non-zero to refuse the submit, or prints warnings for advisory mode.

Validation happens in a temp folder that is deleted afterwards. No sync, no
workspace, nothing left behind.

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
[ ] exit code 2 is treated differently from 1
[ ] verdicts are uploaded as an artifact
[ ] telemetry is posted (so /analytics has CI data too)
[ ] rules.toml is reviewed like code
[ ] the job pins a version (no "latest" in a build)
[ ] a failing gate prints the problems, not just "1 test failed"
```

Next: [11 Troubleshooting](11-TROUBLESHOOTING.md).
