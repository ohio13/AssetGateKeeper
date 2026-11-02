#!/usr/bin/env python3
"""Build the AssetGatekeeper release artefacts.

One command, four outputs, no network:

    python tools/build_release.py

    dist/asset_gatekeeper-<version>-py3-none-any.whl   pip install this
    dist/asset_gatekeeper-<version>.tar.gz             sdist (PEP 517 source)
    dist/asset-gatekeeper-<version>.zip                drop-in tree, unzip and run
    dist/SHA256SUMS.txt                                checksums for the three

The drop-in tree is the one an indie studio actually uses: no install step, no
virtualenv, `python asset_gatekeeper.py <folder>` and it works. It carries the
default `templates/rules.toml`, the core modules, both web UIs and the docs.

Nothing here is discovered by accident: the wheel and sdist come from
`pyproject.toml` + `MANIFEST.in`, and the drop-in tree is an explicit list, so a
stray database or a scratch folder can never end up in a release.

The build refuses to run on a dirty working tree unless --allow-dirty is passed:
a release built from uncommitted code cannot be reproduced from its own tag.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
PROJECT = "asset-gatekeeper"

#: Files copied as-is into the drop-in tree.
DROP_IN_FILES = (
    "asset_gatekeeper.py",
    "healer.py",
    "notifier.py",
    "asset_gatekeeper.example.toml",
    "README.md",
    "MANUAL.md",
    "QUICKSTART.md",
    "RELEASE_NOTES.md",
    "LICENSE",
    "pyproject.toml",
    "MANIFEST.in",
    "setup.py",
    "Rules.bat",
    "agk-serve.bat",
    "start_workstation.bat",
    "start_asset_health.bat",
)

#: Directories copied whole into the drop-in tree (code + assets, no tests).
#: enterprise/ and plugins/ are here because a studio that unzips this gets the
#: console scripts and the DCC panels too; leaving them out meant the drop-in tree
#: could not do half of what the README promised.
DROP_IN_DIRS = ("server", "engine", "tools", "templates", "plugins", "enterprise",
                "vcs_hooks", "docs", "agk_vendor")

#: Never copied, whatever folder they appear in.
SKIP_DIRS = {"__pycache__", ".git", ".agk", "build", "dist", "scratch", "dropbox",
             "workstation_data", ".venv", "venv", ".idea", ".vscode"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".log", ".tmp")


def read_version() -> str:
    """Version straight from the source, without importing it.

    Importing the gate would pull in optional dependencies and touch stdout -
    neither belongs in a packaging script.
    """
    text = (ROOT / "asset_gatekeeper.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("could not find __version__ in asset_gatekeeper.py")


def git(*args: str) -> str:
    try:
        result = subprocess.run(("git",) + args, cwd=str(ROOT), capture_output=True,
                                text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def tree_is_clean() -> bool:
    return not git("status", "--porcelain")


def clean_dist() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)


def _build(kind: str) -> Path:
    """Run the PEP 517 backend in-process.

    stdout and stderr are swapped for buffers first: setuptools logs through
    handlers bound to whatever stream is current, and a host that detaches that
    stream mid-build aborts the wheel with "underlying buffer has been
    detached". A build script that dies because of its own logging is not a
    build script.
    """
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        from setuptools import build_meta
        builder = build_meta.build_wheel if kind == "wheel" else build_meta.build_sdist
        name = builder(str(DIST))
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    artefact = DIST / name
    if not artefact.is_file():
        raise SystemExit(f"{kind} build reported {name} but produced no file")
    return artefact


def _copy_file(source: Path, target: Path) -> None:
    if not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _copy_dir(source: Path, target: Path) -> None:
    for item in source.rglob("*"):
        if item.is_dir():
            continue
        relative = item.relative_to(source)
        # Compare the path *inside* the project: a checkout that happens to live
        # under a folder called "build" must still be releasable.
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if item.suffix.lower() in SKIP_SUFFIXES:
            continue
        _copy_file(item, target / relative)


def stage_drop_in(version: str) -> Path:
    """Assemble the unzip-and-run tree under dist/."""
    stage = DIST / f"{PROJECT}-{version}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    for name in DROP_IN_FILES:
        _copy_file(ROOT / name, stage / name)
    for name in DROP_IN_DIRS:
        if (ROOT / name).is_dir():
            _copy_dir(ROOT / name, stage / name)

    commit = git("rev-parse", "--short", "HEAD") or "unknown"
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    (stage / "BUILD-INFO.txt").write_text(
        f"{PROJECT} {version}\n"
        f"built {built}\n"
        f"commit {commit}\n"
        f"branch {git('rev-parse', '--abbrev-ref', 'HEAD') or 'unknown'}\n"
        "\n"
        "This tree runs without being installed:\n"
        "  python asset_gatekeeper.py <folder> --preset retro\n"
        "  python server\\art_workstation_server.py\n"
        "  python server\\asset_health_server.py\n",
        encoding="utf-8", newline="\n")
    return stage


def zip_stage(stage: Path) -> Path:
    archive = DIST / f"{stage.name}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for item in sorted(stage.rglob("*")):
            if item.is_file():
                bundle.write(item, item.relative_to(stage.parent).as_posix())
    return archive


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sums(artefacts: list[Path]) -> Path:
    """Checksums in the format ``sha256sum -c`` expects, on every platform.

    ``newline="\n"`` is the whole point: Path.write_text translates to
    ``os.linesep`` by default, so on Windows every line ended with CRLF and the
    filename picked up a trailing carriage return. ``sha256sum -c SHA256SUMS.txt``
    on the Linux box that downloads the release then reports the file as missing.
    """
    sums = DIST / "SHA256SUMS.txt"
    lines = [f"{sha256(item)}  {item.name}" for item in artefacts]
    sums.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return sums


def summarise(artefacts: list[Path], version: str) -> None:
    print(f"\n{PROJECT} {version}")
    print("-" * 66)
    for item in artefacts:
        size = item.stat().st_size
        print(f"{item.name:<46}{size / 1024:>9,.0f} KB")
    print("-" * 66)
    print(f"{len(artefacts)} artefact(s) in {DIST}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the release artefacts.")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="build even though the working tree has changes")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    args = parser.parse_args(argv)

    version = read_version()
    if not tree_is_clean() and not args.allow_dirty:
        print("working tree is dirty - commit first, or pass --allow-dirty", file=sys.stderr)
        return 2

    clean_dist()
    wheel = _build("wheel")
    sdist = _build("sdist")
    stage = stage_drop_in(version)
    drop_in = zip_stage(stage)
    artefacts = [wheel, sdist, drop_in]
    sums = write_sums(artefacts)

    if args.json:
        print(json.dumps({
            "version": version,
            "arts": [{"name": p.name, "bytes": p.stat().st_size, "sha256": sha256(p)}
                     for p in artefacts + [sums]],
        }, indent=2))
    else:
        summarise(artefacts + [sums], version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
