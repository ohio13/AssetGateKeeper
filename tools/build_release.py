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
import re
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
    "CONTRIBUTING.md",
    "SECURITY.md",
    "MANUAL.md",
    "QUICKSTART.md",
    "RELEASE_NOTES.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
    "MANIFEST.in",
    "setup.py",
    "Start AssetGatekeeper.bat",
    "Rules.bat",
    "Stop.bat",
    "Allow_Folder.bat",
    "Install_Extras.bat",
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
    # The string literal only: the line carries a comment since the 0.1.0a1
    # rename, and the whole rest of the line once ended up in the ZIP's name.
    found = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", text, re.M)
    if not found:
        raise SystemExit("could not find __version__ in asset_gatekeeper.py")
    return found.group(1)


_PRE = {"alpha": "a", "a": "a", "beta": "b", "b": "b", "rc": "rc", "c": "rc", "pre": "rc",
        "preview": "rc"}


def normalise_version(text: str) -> str:
    """PEP 440's normal form of a version or tag: 'v0.1.0-alpha.1' -> '0.1.0a1'.

    Covers what this project tags (release, alpha/beta/rc, .postN, .devN); the
    tag a person types and the string in the source must name the same release.
    """
    text = text.strip().lower()
    if text.startswith("v"):
        text = text[1:]
    found = re.fullmatch(
        r"(\d+(?:\.\d+)*)"
        r"(?:[-_.]?(alpha|beta|preview|pre|rc|a|b|c)[-_.]?(\d*))?"
        r"(?:[-_.]?post[-_.]?(\d*))?"
        r"(?:[-_.]?dev[-_.]?(\d*))?", text)
    if not found:
        raise ValueError(f"not a version: {text!r}")
    release, pre, pre_n, post, dev = found.groups()
    out = release
    if pre:
        out += _PRE[pre] + (pre_n or "0")
    if post is not None:
        out += ".post" + (post or "0")
    if dev is not None:
        out += ".dev" + (dev or "0")
    return out


def release_notes(version: str, text: str | None = None) -> tuple[str, str]:
    """(heading, body) of this version's section of RELEASE_NOTES.md.

    Sections start with '# v<version> ...'; the body runs to the next one. The
    GitHub release shows the body, so it must exist and be dated: a heading
    still saying 'unreleased' means the notes were not finished for the tag.
    """
    if text is None:
        text = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    sections = re.split(r"(?m)^(?=# v)", text)
    for section in sections:
        heading, _, body = section.partition("\n")
        found = re.match(r"# (v\S+)", heading)
        if not found:
            continue
        try:
            same = normalise_version(found.group(1)) == normalise_version(version)
        except ValueError:
            continue
        if same:
            return heading[2:].strip(), body.strip() + "\n"
    raise LookupError(f"RELEASE_NOTES.md has no '# v...' section for {version}")


def check_tag(tag: str, version: str | None = None, notes_text: str | None = None) -> list[str]:
    """Problems that should stop a release from this tag ([] = go)."""
    version = version or read_version()
    problems = []
    try:
        if normalise_version(tag) != normalise_version(version):
            problems.append(f"tag {tag} is not the version in asset_gatekeeper.py ({version}): "
                            "change one of them")
    except ValueError as exc:
        problems.append(str(exc))
    try:
        heading, body = release_notes(version, notes_text)
        if "unreleased" in heading.lower():
            problems.append(f"RELEASE_NOTES.md still says '{heading}': put the date in the "
                            "heading before tagging")
        if not body.strip():
            problems.append("the release notes section is empty")
    except LookupError as exc:
        problems.append(str(exc))
    return problems


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
    import setuptools
    major = int(str(getattr(setuptools, "__version__", "0")).split(".")[0] or 0)
    if major < 77:
        # pyproject.toml gives the licence as an SPDX string (license = "MIT"),
        # which older setuptools rejects with a confusing schema error.
        raise SystemExit(f"setuptools {setuptools.__version__} is too old to build this "
                         f"(77 or newer): {sys.executable} -m pip install -U setuptools")
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
    (stage / "BUILD-INFO.txt").write_bytes((
        f"{PROJECT} {version}\n"
        f"built {built}\n"
        f"commit {commit}\n"
        f"branch {git('rev-parse', '--abbrev-ref', 'HEAD') or 'unknown'}\n"
        "\n"
        "This tree runs without being installed:\n"
        "  python asset_gatekeeper.py <folder> --preset retro\n"
        "  python server\\art_workstation_server.py\n"
        "  python server\\asset_health_server.py\n").encode("utf-8"))
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
    sums.write_bytes(("\n".join(lines) + "\n").encode("ascii"))   # LF only, on 3.9 too
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
    parser.add_argument("--check-tag", metavar="TAG",
                        help="refuse unless TAG names this version and its release notes are dated")
    parser.add_argument("--notes", metavar="FILE",
                        help="write this version's section of RELEASE_NOTES.md to FILE")
    parser.add_argument("--no-build", action="store_true",
                        help="only --check-tag / --notes; build nothing")
    args = parser.parse_args(argv)

    version = read_version()
    if args.check_tag:
        problems = check_tag(args.check_tag, version)
        for problem in problems:
            print(f"release refused: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"tag {args.check_tag} matches {version}")
    if args.notes:
        _heading, body = release_notes(version)
        Path(args.notes).write_text(body, encoding="utf-8")
        print(f"release notes for {version} -> {args.notes}")
    if args.no_build:
        return 0
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
