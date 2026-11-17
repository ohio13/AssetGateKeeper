#!/usr/bin/env python3
r"""Pre-commit hook: block a local Git commit that contains a failing asset.

Installed as ``.git/hooks/pre-commit`` (or called from an existing hook):

    python vcs_hooks/git_pre_commit.py --install

Only staged assets are inspected, and only the ones that exist on disk (a
``git rm --cached`` should not be gated). Healing is force-disabled here: a hook
must never rewrite an artist's file as a side effect of them typing ``git commit``.

Exit codes: 0 allow, 1 block, 2 hook misconfiguration.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Every format the gate checks (asset_gatekeeper.TRACKED_SUFFIXES): a hook that
# skipped .dds or .tif let an 8K texture through that CI then failed.
ASSET_SUFFIXES = {".obj", ".gltf", ".glb", ".fbx", ".ply", ".stl", ".off", ".3mf", ".dae",
                  ".usd", ".usda", ".usdc", ".usdz",
                  ".png", ".tga", ".jpg", ".jpeg", ".jfif", ".jpe", ".tif", ".tiff", ".webp",
                  ".dds", ".bmp", ".gif", ".psd", ".exr", ".ktx2", ".ktx", ".hdr"}
HOOK_NAME = "pre-commit"


def git(*args: str, cwd: str | None = None) -> str:
    completed = subprocess.run(["git", "-c", "core.quotepath=off", *args], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", cwd=cwd)
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()[:300]}")
    return completed.stdout


def staged_assets(cwd: str | None = None) -> list[Path]:
    """Staged additions/modifications/renames that are real asset files.

    ``cwd`` exists for tests and for hooks that run outside the work tree root.
    """
    output = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", cwd=cwd)
    root = Path(cwd) if cwd else Path(".")
    paths = []
    for line in output.splitlines():
        path = root / line.strip()
        if path.suffix.lower() in ASSET_SUFFIXES and path.is_file():
            paths.append(path)
    return paths


def is_lfs_tracked(path: Path, cwd: str | None = None) -> bool:
    """True when .gitattributes routes this path through the LFS clean filter."""
    try:
        return "filter: lfs" in git("check-attr", "filter", "--", str(path), cwd=cwd)
    except RuntimeError:
        return False  # no attributes file, or an old git: not our problem


def nearest_rules(path: Path) -> list[str]:
    """["--rules", <file>] for the nearest rules.toml at or above ``path``'s folder,
    stopping at the repository root (the hook's working folder), or [].

    The gate reads only the asset's own folder by default: a repository with one
    rules.toml at the top had every asset in a subfolder checked against the
    shipped defaults while CI, running --once at the top, used the project's.
    """
    top = Path.cwd().resolve()
    folder = path.resolve().parent
    while True:
        candidate = folder / "rules.toml"
        if candidate.is_file():
            return ["--rules", str(candidate)]
        if folder == top or folder.parent == folder or top not in folder.parents:
            return []
        folder = folder.parent


def gatekeeper_command() -> list[str]:
    import os

    python = os.environ.get("AGK_PYTHON") or sys.executable
    script = os.environ.get("AGK_GATEKEEPER") or str(
        Path(__file__).resolve().parent.parent / "asset_gatekeeper.py")
    if not Path(script).is_file():
        raise RuntimeError(
            f"asset_gatekeeper.py not found at {script}; set AGK_GATEKEEPER to its path")
    return [python, script]


def check_interpreter() -> str:
    """Return an error message if the target interpreter cannot run the gate.

    Worth the round trip: on Windows the bare `python` on PATH is still 2.7 on
    plenty of workstations, and a hook that dies on a syntax error looks like a
    broken repository rather than a configuration problem.
    """
    python = gatekeeper_command()[0]
    probe = subprocess.run(
        [python, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"],
        capture_output=True, text=True)
    if probe.returncode != 0:
        return (f"{python} is not Python 3.9+ (or cannot run); "
                f"set AGK_PYTHON to a modern interpreter")
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="git_pre_commit.py",
        description="Block commits containing assets that exceed the budget.")
    parser.add_argument("--rules", help="rules file (default: the nearest rules.toml at or above the asset's folder, up to the repository root, as a --once run of that folder uses)")
    parser.add_argument("--install", action="store_true",
                        help="write this script into .git/hooks/pre-commit and exit")
    parser.add_argument("--verbose", action="store_true")
    options = parser.parse_args(argv)

    if options.install:
        return install()

    try:
        paths = staged_assets()
    except RuntimeError as exc:
        print(f"AssetGatekeeper: {exc}", file=sys.stderr)
        return 2

    if not paths:
        if options.verbose:
            print("AssetGatekeeper: no staged assets")
        return 0

    try:
        interpreter_problem = check_interpreter()
    except RuntimeError as exc:
        # The script copied into .git/hooks finds no asset_gatekeeper.py beside
        # it: say how to fix that instead of printing a traceback.
        print(f"AssetGatekeeper: {exc}, or install the hook with "
              "`python <AssetGatekeeper>/vcs_hooks/git_pre_commit.py --install`",
              file=sys.stderr)
        return 2
    if interpreter_problem:
        print(f"AssetGatekeeper: {interpreter_problem}", file=sys.stderr)
        return 2

    extra = ["--no-color", "--no-auto-heal"]
    if options.rules:
        extra += ["--rules", options.rules]

    failures: list[str] = []
    # The gate reads the file on disk, but the commit holds the staged version:
    # stage a failing mesh, fix it on disk without `git add`, and the fixed copy
    # passed while the failing one was committed. Such a file is not checked.
    try:
        unstaged = {Path(line.strip()).as_posix() for line in
                    git("diff", "--name-only", "--", *[str(path) for path in paths]).splitlines()
                    if line.strip()}
    except RuntimeError as exc:
        print(f"AssetGatekeeper: {exc}", file=sys.stderr)
        return 2
    for path in list(paths):
        if path.as_posix() in unstaged:
            failures.append(f"{path}\n    has changes that are not staged, so the commit would "
                            "hold a version this hook cannot check: `git add` it to commit what is on "
                            "disk, or `git stash --keep-index` to check the staged version")
            paths.remove(path)
    # One gate run per rules file, not one per asset: each run is a Python start
    # (about 0.6 s), so a 200-texture commit took two minutes; batched it takes
    # seconds. Each file is still checked exactly as it would be on its own.
    groups: dict[tuple[str, ...], list[Path]] = {}
    for path in paths:
        rules = () if options.rules else tuple(nearest_rules(path))
        groups.setdefault(rules, []).append(path)
    for rules, members in groups.items():
        for batch in _batches(members):
            completed = subprocess.run([*gatekeeper_command(), *map(str, batch), "--once",
                                        "--json", *extra, *rules],
                                       capture_output=True, text=True,
                                       encoding="utf-8", errors="replace")
            if completed.returncode == 2:
                print(f"AssetGatekeeper: setup error for {batch[0]}"
                      f"{f' and {len(batch) - 1} more' if len(batch) > 1 else ''}:\n"
                      f"{completed.stderr.strip()[-1500:]}", file=sys.stderr)
                return 2
            verdicts = _verdicts(completed.stdout)
            for path in batch:
                lfs = " [LFS]" if is_lfs_tracked(path) else ""
                verdict = verdicts.get(_key(path))
                if verdict is None:
                    # No verdict is fine only when the gate said why: the project's
                    # .agkignore or [filters] include leaves the file out, as a scan
                    # of the folder would. Anything else (a crash) blocks: a hook
                    # that waves files through when the gate stops is not a gate.
                    if f"{path.name} not checked:" in completed.stderr:
                        if options.verbose:
                            print(f"AssetGatekeeper: not checked (left out by the project) "
                                  f"{path}{lfs}")
                        continue
                    last = (completed.stderr.strip().splitlines() or ["no output"])[-1]
                    failures.append(f"{path}{lfs}\n    not checked: the gate stopped "
                                    f"(exit {completed.returncode}): {last[:300]}")
                    continue
                if verdict.get("passed"):
                    if options.verbose:
                        print(f"AssetGatekeeper: PASSED {path}{lfs}")
                    continue
                reasons = "; ".join(verdict.get("problems") or []) or str(verdict.get("status"))
                failures.append(f"{path}{lfs}\n    {verdict.get('status', 'FAILED')}: {reasons}")

    if not failures:
        return 0

    print("Commit blocked by AssetGatekeeper:", file=sys.stderr)
    for item in failures:
        print(f"  - {item}", file=sys.stderr)
    print("\nNothing was healed or modified. Commit the fix, or `git commit --no-verify` "
          "if a technical lead agrees.", file=sys.stderr)
    return 1


def _key(path: Path) -> str:
    import os
    return os.path.normcase(str(Path(path).resolve()))


def _batches(paths: list[Path], max_files: int = 150, max_chars: int = 20000):
    """Groups small enough for one command line (Windows allows 32,767 characters)."""
    batch: list[Path] = []
    length = 0
    for path in paths:
        size = len(str(path)) + 3
        if batch and (len(batch) >= max_files or length + size > max_chars):
            yield batch
            batch, length = [], 0
        batch.append(path)
        length += size
    if batch:
        yield batch


def _verdicts(stdout: str) -> dict:
    """The gate's --json verdict lines, by file."""
    import json
    found = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("file"):
            found[_key(Path(row["file"]))] = row
    return found


def install() -> int:
    """Drop a shim into .git/hooks so the checkout is protected immediately."""
    try:
        hooks_dir = Path(git("rev-parse", "--git-dir").strip()) / "hooks"
    except RuntimeError as exc:
        print(f"AssetGatekeeper: {exc}", file=sys.stderr)
        return 2
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / HOOK_NAME
    if target.exists():
        backup = target.with_suffix(target.suffix + ".bak")
        target.replace(backup)
        print(f"existing hook backed up to {backup}")
    interpreter = sys.executable
    script = Path(__file__).resolve()
    # Bytes, not write_text(newline=...): that argument is Python 3.10+, and on
    # 3.9 --install crashed (9 Nov 2026). A shell script must keep LF endings.
    target.write_bytes((
        "#!/bin/sh\n"
        "# Installed by AssetGatekeeper. Re-run git_pre_commit.py --install to refresh.\n"
        f'exec "{interpreter}" "{script}" "$@"\n').encode("utf-8"))
    try:
        target.chmod(0o755)
    except OSError:
        pass  # Windows: the shim is invoked through Git Bash, which ignores the bit
    print(f"installed {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
