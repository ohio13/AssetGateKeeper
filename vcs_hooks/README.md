# Version control hooks

Gateway scripts that keep an over-budget asset out of the depot. Every one of them
passes `--no-auto-heal`: a hook that rewrites an artist's file during a submit is
not a gate, it is a liability.

| File | Host | Install |
| :--- | :--- | :--- |
| `p4_pre_submit.py` | Perforce (server trigger) | `p4 triggers` entry, below |
| `git_pre_commit.py` | Git (local) | `python vcs_hooks/git_pre_commit.py --install` |
| `git_pre_push.sh` | Git (local) | `cp vcs_hooks/git_pre_push.sh .git/hooks/pre-push && chmod +x .git/hooks/pre-push` |

## Perforce

Submit the trigger to the server's `triggers` table (one line, **tabs** between
fields — `p4 triggers -i < triggers.txt`):

```
AssetGatekeeper  change-content  //depot/art/...  "python C:\p4\hooks\p4_pre_submit.py %changelist% %user%"
```

What it does, in order:

1. `p4 describe -s <changelist>` — parses added/edited/branched revisions;
   `delete` and `move/delete` carry no bytes and are skipped.
2. Filters to `.obj .gltf .glb .png`.
3. `p4 print -o <temp>` each rev into a temp tree that keeps the depot's folder
   shape, so a `.gltf` with relative texture/buffer references still resolves.
4. Runs `asset_gatekeeper.py <file> --no-color --no-auto-heal` per asset, single
   file mode, and prints the tool's own FAILED line.

Exit `1` rejects the submit and P4V shows the message to the artist. Exit `2`
means the hook itself is misconfigured (path, interpreter). An unreadable
changelist also rejects: failing closed is the whole point.

## Git

```bash
python vcs_hooks/git_pre_commit.py --install     # writes .git/hooks/pre-commit
```

The shim calls this script with the current interpreter, so it keeps working when
the repo moves. `--install` backs up an existing hook to `pre-commit.bak`.

`git_pre_commit.py` gates `git diff --cached --diff-filter=ACMR`; `git_pre_push.sh`
gates the whole branch (upstream if there is one, otherwise every commit since the
empty tree) and reports LFS-tracked files with an `[LFS]` marker via
`git check-attr filter`, so it does not need the `git-lfs` binary.

Both refuse to run on an interpreter older than 3.9 instead of dying on a syntax
error, and both honour:

| Variable | Meaning |
| :--- | :--- |
| `AGK_GATEKEEPER` | path to `asset_gatekeeper.py` (default: two levels up) |
| `AGK_PYTHON` | interpreter to use (default: `sys.executable`, or `python3`/`python`) |

## Escape hatches

`git commit --no-verify` / `git push --no-verify` skip the local hooks — that is
Git's rule, not ours, and the message says so. Perforce has no client-side bypass
for a server trigger, which is why it belongs on the server.

## Testing the hooks without a server

`tests/test_gatekeeper.py` (group 15) drives the real thing: a throwaway repository
is created, the hook is installed, a failing asset is staged, and the commit is
asserted to be blocked with the asset byte-identical afterwards. The P4 trigger is
covered by replacing `p4 describe`/`p4 print` with canned output, which exercises
the parser, the candidate filter, the fail-closed paths and the no-heal argv.
