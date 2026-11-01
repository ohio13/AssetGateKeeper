#!/usr/bin/env bash
# pre-push gate for Git: refuse to publish commits that contain assets over budget.
#
# Install:
#     cp vcs_hooks/git_pre_push.sh .git/hooks/pre-push
#     chmod +x .git/hooks/pre-push
#
# Or chain it from an existing pre-push hook with `. "$(git rev-parse --show-toplevel)/vcs_hooks/git_pre_push.sh"`.
#
# What it does
#   1. Works out the commit range that is actually being pushed: the upstream
#      branch if one exists, otherwise everything since the empty tree (a new
#      branch has no upstream to diff against).
#   2. Keeps changes whose extension is an asset we gate. Git LFS is detected
#      through `git check-attr` (the .gitattributes clean filter) rather than by
#      requiring the `git-lfs` binary — a pointer file is still a file the artist
#      changed, and the working copy is what gets gated.
#   3. Runs the gate on the working-tree copy of each changed asset, one
#      process per file, and blocks the push on the first exit code 1.
#
# Healing is force-disabled (`--no-auto-heal`): a hook that rewrites an asset
# during `git push` would be indistinguishable from corruption to the artist.
#
# Exit codes: 0 allow the push, 1 block it, 2 the hook itself is misconfigured.
set -u

ASSET_EXTENSIONS=" obj gltf glb png "

repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "AssetGatekeeper: not inside a git repository" >&2
  exit 0   # not our business; never break an unrelated push
}
cd "$repo_root" || exit 0

# --- locate the gate ---------------------------------------------------------
AGK_PYTHON=${AGK_PYTHON:-python3}
command -v "$AGK_PYTHON" >/dev/null 2>&1 || AGK_PYTHON=python
AGK_GATEKEEPER=${AGK_GATEKEEPER:-"$(dirname "$0")/../asset_gatekeeper.py"}
# The hook may be a copy, so fall back to the checkout that owns this script.
if [ ! -f "$AGK_GATEKEEPER" ]; then
  AGK_GATEKEEPER="$repo_root/vcs_hooks/../asset_gatekeeper.py"
fi
if [ ! -f "$AGK_GATEKEEPER" ]; then
  echo "AssetGatekeeper: cannot find asset_gatekeeper.py; set AGK_GATEKEEPER" >&2
  exit 2
fi
# Refuse to guess: on Windows `python` is still 2.7 on plenty of machines.
if ! "$AGK_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  echo "AssetGatekeeper: $AGK_PYTHON is not Python 3.9+; set AGK_PYTHON to a modern interpreter" >&2
  exit 2
fi

# --- work out the range ------------------------------------------------------
upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)
if [ -n "${upstream}" ]; then
  range="${upstream}..HEAD"
  echo "AssetGatekeeper: gating ${range}"
else
  # `git mktree` on empty stdin is the portable empty-tree hash; reading
  # /dev/null into hash-object is not (it silently yields an empty string on
  # some Windows Git builds, which turned this hook into a no-op).
  empty_tree=$(printf '' | git mktree 2>/dev/null)
  if [ -z "$empty_tree" ]; then
    echo "AssetGatekeeper: cannot determine the commit range; skipping gate" >&2
    exit 0
  fi
  range="${empty_tree}..HEAD"
  echo "AssetGatekeeper: no upstream; gating every commit on this branch"
fi

changed=$(git diff --name-only --diff-filter=ACMR "${range}" 2>/dev/null || true)
if [ -z "${changed}" ]; then
  exit 0
fi

# --- filter to the assets we gate -------------------------------------------
assets=""
while IFS= read -r path; do
  [ -n "${path}" ] || continue
  ext=${path##*.}
  case "$ASSET_EXTENSIONS" in
    *" ${ext} "*) ;;
    *) continue ;;
  esac
  # LFS-tracked files are still gated: the pointer is what lands in history, but
  # the working copy is what the artist will hand to the engine.
  lfs_note=""
  if git check-attr filter -- "${path}" 2>/dev/null | grep -q "filter: lfs"; then
    lfs_note=" [LFS]"
  fi
  [ -f "${path}" ] || continue   # deleted in the working tree: nothing to inspect
  assets="${assets}${path}${lfs_note}"$'\n'
done <<EOF
${changed}
EOF

if [ -z "${assets}" ]; then
  exit 0
fi

# --- gate --------------------------------------------------------------------
failed=0
report=""
while IFS= read -r entry; do
  [ -n "${entry}" ] || continue
  path=${entry%% \[LFS\]*}
  verdict=$("$AGK_PYTHON" "$AGK_GATEKEEPER" "$path" --no-color --no-auto-heal 2>&1)
  status=$?
  if [ $status -eq 0 ]; then
    continue
  elif [ $status -eq 2 ]; then
    echo "AssetGatekeeper: setup error while gating ${path}" >&2
    echo "$verdict" >&2
    exit 2
  fi
  failed=$((failed + 1))
  line=$(printf '%s\n' "$verdict" | grep -m1 'FAILED' || printf '%s' "$verdict")
  report="${report}  - ${entry}
    ${line}
"
done <<EOF
${assets}
EOF

if [ $failed -eq 0 ]; then
  exit 0
fi

{
  echo "Push blocked by AssetGatekeeper: ${failed} asset(s) exceed the studio budget."
  printf '%s' "$report"
  echo ""
  echo "Nothing was healed or modified. Fix the assets, or push with --no-verify if a"
  echo "technical lead agrees to take the hit on the build machine instead."
} >&2
exit 1
