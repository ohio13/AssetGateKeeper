"""Which files under a project folder are checked at all.

Three layers, the way version control and engines already do it:

* ``[filters] include`` (or ``--include``): glob patterns relative to the folder
  being scanned. When there are any, a file is checked only if it, or one of
  the folders it sits in, matches one. ``MM_*`` matches a file or folder of that
  name at any depth; ``Art/MM_*/**`` is anchored to the scanned folder.
* ``.agkignore`` files: ``.gitignore`` syntax (``#`` comments, ``!`` to bring
  something back, a trailing ``/`` for folders only, a leading ``/`` to anchor
  to the file's own folder, ``**`` for any depth). One may sit in any folder and
  covers that folder and everything below it; a deeper one is read after the
  ones above it, and the last matching line wins. An ignored folder is not
  walked at all, so a project with thousands of folders stays fast.
* ``[filters] exclude`` (or ``--exclude``): unchanged, matched against the file
  name or full path.

Matching ignores letter case everywhere: Git for Windows does the same
(core.ignorecase), and a rule must not pass on an artist's PC and fail on the
Linux build machine.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Optional, Sequence

IGNORE_FILE = ".agkignore"


@lru_cache(maxsize=512)
def _pattern(glob: str, anchored: bool) -> "re.Pattern[str]":
    """A ``.gitignore`` glob as a regular expression over a POSIX relative path."""
    out = []
    i = 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("/**", i) and i + 3 == len(glob):
            out.append("(?:/.*)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            end = glob.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = glob[i + 1:end].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = end + 1
        elif c == "\\" and i + 1 < len(glob):
            out.append(re.escape(glob[i + 1]))
            i += 2
        else:
            out.append(re.escape(c))
            i += 1
    body = "".join(out)
    if not anchored:
        body = "(?:.*/)?" + body
    return re.compile(body, re.IGNORECASE | re.DOTALL)


class _Rule:
    __slots__ = ("negate", "folders_only", "regex", "text")

    def __init__(self, line: str):
        self.text = line
        self.negate = line.startswith("!")
        if self.negate:
            line = line[1:]
        self.folders_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = "/" in line
        self.regex = _pattern(line.lstrip("/"), anchored)

    def matches(self, relative: str, is_folder: bool) -> bool:
        if self.folders_only and not is_folder:
            return False
        return bool(self.regex.fullmatch(relative))


def parse_ignore(text: str) -> list:
    rules = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        # Trailing spaces are dropped unless escaped, as in Git.
        while line.endswith(" ") and not line.endswith("\\ "):
            line = line[:-1]
        if not line or line.startswith("#"):
            continue
        if line.startswith("\\#") or line.startswith("\\!"):
            line = line[1:]
        rules.append(_Rule(line))
    return rules


def glob_matches(pattern: str, relative: str) -> bool:
    """Does a ``.gitignore``-style pattern match a POSIX path relative to its base
    (the path itself, or a folder it is in)?"""
    rule = _Rule(pattern)
    parts = PurePosixPath(relative).parts
    for depth in range(1, len(parts) + 1):
        if rule.matches("/".join(parts[:depth]), depth < len(parts)):
            return True
    return False


class Scope:
    """What a scan of ``root`` leaves out, and why."""

    def __init__(self, root: Path, include: Sequence[str] = (), exclude: Sequence[str] = (),
                 ignore_files: bool = True):
        self.root = Path(root)
        self.include = tuple(item for item in include if item and item.strip())
        self.exclude = tuple(exclude)
        self.ignore_files = ignore_files
        self._ignore_cache: dict[str, list] = {}
        #: Folders the walk did not enter (an .agkignore said so).
        self.pruned_folders: list[Path] = []

    # - .agkignore - #

    def _rules_in(self, folder: Path) -> list:
        key = os.path.normcase(str(folder))
        rules = self._ignore_cache.get(key)
        if rules is None:
            try:
                text = (folder / IGNORE_FILE).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            rules = parse_ignore(text)
            self._ignore_cache[key] = rules
        return rules

    def _relative(self, path: Path) -> Optional[PurePosixPath]:
        try:
            return PurePosixPath(Path(path).relative_to(self.root).as_posix())
        except ValueError:
            return None

    def ignored(self, path: Path, is_folder: bool = False) -> bool:
        """Is ``path`` ignored by the .agkignore files above it (inside ``root``)?

        As in Git, a path inside an ignored folder is ignored whatever a deeper
        line says: the folder is never read."""
        if not self.ignore_files:
            return False
        relative = self._relative(path)
        if relative is None or not relative.parts:
            return False
        parts = relative.parts
        # Each prefix (each folder above, then the path itself) is judged in turn;
        # once a folder is ignored, everything in it is.
        for depth in range(1, len(parts) + 1):
            here_is_folder = depth < len(parts) or is_folder
            verdict = False
            for base_depth in range(0, depth):
                base = self.root.joinpath(*parts[:base_depth])
                below = "/".join(parts[base_depth:depth])
                for rule in self._rules_in(base):
                    if rule.matches(below, here_is_folder):
                        verdict = not rule.negate
            if verdict:
                return True
        return False

    # - include - #

    def included(self, path: Path) -> bool:
        if not self.include:
            return True
        relative = self._relative(path)
        if relative is None:
            return True            # a file named on its own, outside the scanned folder
        text = relative.as_posix()
        return any(glob_matches(pattern, text) for pattern in self.include)

    # - together - #

    def reason(self, path: Path) -> str:
        """'' when the file is in scope, else 'ignored' (.agkignore) or 'outside'
        ([filters] include). [filters] exclude is judged by is_candidate()."""
        if self.ignored(path):
            return "ignored"
        if not self.included(path):
            return "outside"
        return ""

    def walk_folder(self, folder: Path) -> bool:
        """Should the walk enter ``folder``? False for an ignored one (recorded)."""
        if self.ignored(folder, is_folder=True):
            self.pruned_folders.append(Path(folder))
            return False
        return True


#: What marks the top of a project when one file is checked on its own (a
#: version-control hook, a DCC button): its rules, or the repository.
PROJECT_MARKERS = ("rules.toml", "asset_gatekeeper.toml", ".git", ".p4config", ".p4ignore")


def project_root_for(path: Path) -> Path:
    """The nearest folder above ``path`` that holds rules or a repository; else
    the highest one with an .agkignore; else the file's own folder. Scope
    patterns are read from there."""
    folder = Path(path).resolve().parent
    highest_ignore = None
    for candidate in (folder, *folder.parents):
        if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
        if (candidate / IGNORE_FILE).is_file():
            highest_ignore = candidate
    # No rules and no repository: start from the highest .agkignore, so every
    # one above the file is read.
    return highest_ignore or folder


def scope_for(root: Path, budget) -> Scope:
    """The scope a budget asks for, over ``root``."""
    return Scope(Path(root), getattr(budget, "include", ()) or (),
                 getattr(budget, "exclude", ()) or ())
