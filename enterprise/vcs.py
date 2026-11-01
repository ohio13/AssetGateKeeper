"""Version control: Git, Git LFS pointer files, and Perforce Helix Core.

Why a gate needs to know about version control at all
    An artist's working copy is not what enters the depot. If the gate only ever
    sees the file on disk it validates something nobody will submit; if it sees
    the *pointer* it validates a 130-byte text file and calls a 40 MB texture
    clean. So this module knows how to reach the real bytes:

    * a Git LFS pointer in the working tree is resolved through ``git lfs smudge``
      when the binary is available, and reported as an error when it is not -
      never silently validated as text;
    * a Perforce changelist is materialised with ``p4 print`` straight from the
      depot, which means a submit trigger can validate content the artist has not
      synced, and a CI runner needs no workspace at all.

Safety
    Every external call is argv-only (never a shell string), bounded by a
    timeout, and decoded with an explicit error handler. A VCS tool that hangs is
    a CI outage, and a VCS tool that is invoked through a shell is an injection.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from .config import VCSConfig

LFS_VERSION = "https://git-lfs.github.com/spec/v1"
POINTER_MAX_BYTES = 1024                 # pointers are ~130 bytes; 1 KiB is generous
CONTENT_ACTIONS = {"add", "edit", "branch", "move/add", "integrate", "merge", "import"}

_LFS_OID_RE = re.compile(r"^oid sha256:([0-9a-f]{64})$")


class VCSError(Exception):
    """A version-control problem, with the command that caused it."""


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(argv: Sequence[str], timeout: float = 60.0,
        env: Optional[dict[str, str]] = None) -> CommandResult:
    """Run a command without a shell, with a timeout and a decoded stream."""
    argv = tuple(str(part) for part in argv)
    try:
        completed = subprocess.run(
            argv, capture_output=True, timeout=timeout, check=False,
            env={**(os.environ if env is None else env)})
    except FileNotFoundError as exc:
        raise VCSError(f"'{argv[0]}' is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise VCSError(f"'{argv[0]}' timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise VCSError(f"could not run '{argv[0]}': {exc}") from exc
    return CommandResult(
        argv=argv, returncode=completed.returncode,
        stdout=completed.stdout.decode("utf-8", "replace"),
        stderr=completed.stderr.decode("utf-8", "replace"))


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def detect_vcs(path: str | Path) -> str:
    """``"git"``, ``"perforce"`` or ``"none"`` for a working copy."""
    current = Path(path).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return "git"
        if (candidate / ".p4config").is_file():
            return "perforce"
    return "none"


# --------------------------------------------------------------------------- #
# Git LFS
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LfsPointer:
    oid: str
    size: int
    version: str = LFS_VERSION

    @property
    def short_oid(self) -> str:
        return self.oid[:12]

    def as_bytes(self) -> bytes:
        return (f"version {self.version}\noid sha256:{self.oid}\nsize {self.size}\n"
                ).encode("ascii")


def is_lfs_pointer(data: bytes) -> bool:
    """A pointer is a tiny text file that starts with the version line."""
    if not data or len(data) > POINTER_MAX_BYTES:
        return False
    return data.startswith(b"version https://git-lfs.github.com/spec/v1")


def parse_lfs_pointer(data: bytes) -> LfsPointer:
    """Read the three fields, refusing anything malformed.

    A half-understood pointer is dangerous: the whole point of parsing it is to
    prove the gate is looking at a 40 MB texture rather than 130 bytes of text.
    """
    if not is_lfs_pointer(data):
        raise VCSError("not a Git LFS pointer")
    text = data.decode("ascii", "replace")
    version = oid = ""
    size = -1
    for line in text.splitlines():
        if line.startswith("version "):
            version = line[len("version "):].strip()
        elif line.startswith("oid "):
            match = _LFS_OID_RE.match(line.strip())
            if not match:
                raise VCSError(f"unreadable LFS oid line: {line[:60]}")
            oid = match.group(1)
        elif line.startswith("size "):
            try:
                size = int(line.split(" ", 1)[1].strip())
            except ValueError as exc:
                raise VCSError(f"unreadable LFS size line: {line[:60]}") from exc
    if not oid or size < 0:
        raise VCSError("LFS pointer is missing its oid or size line")
    if version != LFS_VERSION:
        raise VCSError(f"unsupported LFS pointer version {version!r}")
    return LfsPointer(oid=oid, size=size, version=version)


def is_pointer_file(path: str | Path) -> bool:
    target = Path(path)
    try:
        if target.stat().st_size > POINTER_MAX_BYTES:
            return False
        return is_lfs_pointer(target.read_bytes())
    except OSError:
        return False


def resolve_lfs_pointer(path: str | Path, git_command: str = "git") -> bytes:
    """Ask Git LFS for the real content behind a pointer.

    Refuses to fall back to the pointer text: a gate that validates the pointer
    would report a 40 MB normal map as a 130-byte file with no image data, which
    is exactly the failure mode this function exists to prevent.
    """
    target = Path(path)
    if not is_pointer_file(target):
        return target.read_bytes()
    pointer = parse_lfs_pointer(target.read_bytes())
    try:
        completed = subprocess.run(
            [git_command, "lfs", "smudge"], input=target.read_bytes(),
            capture_output=True, timeout=120.0, check=False)
    except FileNotFoundError as exc:
        raise VCSError(
            f"'{target.name}' is a Git LFS pointer and '{git_command}' is not on "
            f"PATH, so its real ({pointer.size:,} byte) content cannot be read") from exc
    except subprocess.TimeoutExpired as exc:
        raise VCSError("git lfs smudge timed out") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise VCSError(
            f"git lfs could not resolve {target.name} "
            f"(oid {pointer.short_oid}): {detail[-1] if detail else 'no message'}")
    return completed.stdout


# --------------------------------------------------------------------------- #
# Perforce
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DepotFile:
    depot_file: str
    action: str
    rev: str = ""

    @property
    def has_content(self) -> bool:
        return self.action.lower() in CONTENT_ACTIONS


class PerforceClient:
    """A very small, very deliberate wrapper around the ``p4`` CLI.

    Only four commands are used, all read-only: ``info``, ``describe``,
    ``files`` and ``print``. Nothing here submits, syncs or edits a workspace,
    so pointing it at production is safe.
    """

    def __init__(self, config: VCSConfig,
                 executor: Callable[..., CommandResult] = run) -> None:
        self.config = config
        self._executor = executor

    # -- plumbing ---------------------------------------------------------- #

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.config.p4_port:
            env["P4PORT"] = self.config.p4_port
        if self.config.p4_user:
            env["P4USER"] = self.config.p4_user
        if self.config.p4_client:
            env["P4CLIENT"] = self.config.p4_client
        if self.config.p4_charset:
            env["P4CHARSET"] = self.config.p4_charset
        env["P4DIALOG"] = "1"
        return env

    def run(self, *args: str, timeout: float = 30.0) -> CommandResult:
        argv = [self.config.p4_executable, "-ztag", *args]
        return self._executor(argv, timeout=timeout, env=self._env())

    def available(self) -> tuple[bool, str]:
        try:
            result = self.run("info")
        except VCSError as exc:
            return False, str(exc)
        if not result.ok:
            return False, (result.stderr or result.stdout).strip()[:200]
        return True, ""

    def info(self) -> dict[str, str]:
        result = self.run("info")
        if not result.ok:
            raise VCSError(f"p4 info failed: {result.stderr.strip()[:200]}")
        return parse_tagged(result.stdout)

    # -- changelists ------------------------------------------------------- #

    def describe(self, changelist: int | str) -> list[DepotFile]:
        """The files in a pending or submitted changelist, with their actions."""
        result = self.run("describe", "-s", str(changelist))
        if not result.ok:
            raise VCSError(f"p4 describe {changelist} failed: "
                           f"{result.stderr.strip()[:200]}")
        files = []
        for record in split_records(result.stdout):
            depot = record.get("depotFile")
            if depot:
                files.append(DepotFile(depot_file=depot,
                                       action=record.get("action", ""),
                                       rev=record.get("rev", "")))
        return files

    def files(self, depot_path: str, revision: str = "") -> list[DepotFile]:
        spec = f"{depot_path}@{revision}" if revision else depot_path
        result = self.run("files", spec)
        if not result.ok:
            raise VCSError(f"p4 files {spec} failed: {result.stderr.strip()[:200]}")
        files = []
        for record in split_records(result.stdout):
            depot = record.get("depotFile")
            if depot:
                files.append(DepotFile(depot_file=depot,
                                       action=record.get("action", "edit"),
                                       rev=record.get("rev", "")))
        return files

    def print_to(self, depot_file: str, destination: Path,
                 revision: str = "") -> Path:
        """Write the depot content of one file to ``destination`` (no sync)."""
        spec = f"{depot_file}@{revision}" if revision else depot_file
        destination.parent.mkdir(parents=True, exist_ok=True)
        result = self._executor(
            [self.config.p4_executable, "print", "-q", "-o", str(destination), spec],
            timeout=300.0, env=self._env())
        if not result.ok:
            raise VCSError(f"p4 print {spec} failed: {result.stderr.strip()[:200]}")
        if not destination.is_file():
            raise VCSError(f"p4 print {spec} produced no file")
        return destination

    # -- materialisation --------------------------------------------------- #

    def materialise(self, changelist: int | str, destination: str | Path,
                    suffixes: Optional[Iterable[str]] = None) -> list[Path]:
        """Fetch the asset files of a changelist into a temp tree.

        This is what lets a submit trigger validate the *depot* content while the
        artist's shelf is still open, and what lets a CI runner validate a
        changelist without a workspace.
        """
        wanted = {s.lower() for s in (suffixes or ())}
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for entry in self.describe(changelist):
            if not entry.has_content:
                continue
            suffix = Path(entry.depot_file).suffix.lower()
            if wanted and suffix not in wanted:
                continue
            # Keep the depot's own structure so two files with the same name in
            # different folders cannot overwrite each other.
            relative = entry.depot_file.lstrip("/").replace(":", "_")
            target = destination / relative
            written.append(self.print_to(entry.depot_file, target, entry.rev))
        return written


def parse_tagged(output: str) -> dict[str, str]:
    """``-ztag`` output is ``... key value`` lines; later keys win."""
    values: dict[str, str] = {}
    for line in output.splitlines():
        if not line.startswith("..."):
            continue
        body = line[3:].strip()
        if " " not in body:
            continue
        key, value = body.split(" ", 1)
        values[key.strip()] = value.strip()
    return values


def split_records(output: str) -> list[dict[str, str]]:
    """Split ``-ztag`` output into records, one per ``depotFile``."""
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line.startswith("..."):
            continue
        body = line[3:].strip()
        if " " not in body:
            continue
        key, value = body.split(" ", 1)
        key, value = key.strip(), value.strip()
        if key == "depotFile" and current.get("depotFile"):
            records.append(current)
            current = {}
        current[key] = value
    if current:
        records.append(current)
    return records


def parse_p4_path_listing(output: str) -> list[str]:
    """Parse plain (non ``-ztag``) ``p4 files`` output: ``//depot/a.obj#3 - edit ...``."""
    paths = []
    for line in output.splitlines():
        if not line.startswith("//"):
            continue
        paths.append(line.split("#", 1)[0].strip())
    return paths


def quote_for_display(argv: Sequence[str]) -> str:
    """Only for log messages - never for execution."""
    return " ".join(shlex.quote(part) for part in argv)


def temp_directory(prefix: str = "agk-remote-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))
