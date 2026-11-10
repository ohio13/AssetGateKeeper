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
ASSET_SUFFIXES = {".obj", ".gltf", ".glb", ".fbx", ".png", ".tga", ".jpg", ".jpeg",
                  ".tif", ".tiff", ".webp", ".dds", ".exr", ".ktx2"}
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
    parser.add_argument("--rules", help="rules file (default: <asset folder>/rules.toml)")
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

    interpreter_problem = check_interpreter()
    if interpreter_problem:
        print(f"AssetGatekeeper: {interpreter_problem}", file=sys.stderr)
        return 2

    extra = ["--no-color", "--no-auto-heal"]
    if options.rules:
        extra += ["--rules", options.rules]

    failures: list[str] = []
    for path in paths:
        lfs = " [LFS]" if is_lfs_tracked(path) else ""
        completed = subprocess.run([*gatekeeper_command(), str(path), *extra],
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
        if completed.returncode == 0:
            if options.verbose:
                print(f"AssetGatekeeper: PASSED {path}{lfs}")
            continue
        if completed.returncode == 2:
            print(f"AssetGatekeeper: setup error for {path}:\n{completed.stderr.strip()}",
                  file=sys.stderr)
            return 2
        verdict = ""
        for line in (completed.stdout + completed.stderr).splitlines():
            if "FAILED" in line:
                verdict = line.strip()
        failures.append(f"{path}{lfs}\n    {verdict or completed.stdout.strip()[:300]}")

    if not failures:
        return 0

    print("Commit blocked by AssetGatekeeper:", file=sys.stderr)
    for item in failures:
        print(f"  - {item}", file=sys.stderr)
    print("\nNothing was healed or modified. Commit the fix, or `git commit --no-verify` "
          "if a technical lead agrees.", file=sys.stderr)
    return 1


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
