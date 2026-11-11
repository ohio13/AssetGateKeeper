#!/usr/bin/env python3
"""
AssetGatekeeper
===============

A tiny, dependency-light "budget gate" that sits on an artist's export folder and
fails fast on assets that would blow a project's technical budget. Written to be
readable and hackable, not clever.

Rules enforced
-
    .obj / .gltf / .glb   triangles     <= 50,000
                          material slots <= 3          (draw-call proxy)
                          at least 1 triangle          (catches truncated writes)
    .png                  max edge      <= 2,048 px
                          both edges are a power of two
                          filename ends with _Albedo / _Normal / _Roughness

Usage
-
    # watch forever (Ctrl+C to stop)
    python asset_gatekeeper.py "D:\\Exports\\Incoming"

    # one-shot sweep (handy in CI or on a pre-existing drop folder)
    python asset_gatekeeper.py "D:\\Exports\\Incoming" --once --recursive

    # override the shipped budgets for a different project
    python asset_gatekeeper.py . --triangles 30000 --max-texture 1024 --max-materials 2

    # relax individual rules
    python asset_gatekeeper.py . --allow-npot --no-naming

    # use a shared rules file instead of <folder>/rules.toml
    python asset_gatekeeper.py . --rules studio/rules.toml

Configuration
-
If ``rules.toml`` sits in the watched folder it supplies the baseline budget;
CLI flags still win, so CI can tighten one value without editing a file the
artists own. Precedence: explicit CLI flag > rules.toml > built-in defaults.
See ``asset_gatekeeper.example.toml`` for the schema.

Exit codes (--once): 0 = every asset passed, 1 = at least one failed, 2 = bad setup.

Install
-
    python -m pip install watchdog trimesh pillow DracoPy

Notes for extending
-
* Every budget value lives in one dataclass (``Budget``); add a field, a matching
  ``_*_problems`` check, and an entry in ``Budget.from_mapping`` to make it
  configurable from ``rules.toml``.
* One asset, one log line: all violations for a file are collected and reported
  together, so an artist fixes everything in a single pass instead of playing
  whack-a-mole across five re-exports.
* Material slots are counted from the file itself (glTF material indices, OBJ
  ``usemtl`` names), not from trimesh's object graph, so primitives that share a
  material are not double-counted.
* Triangle counts are topology counts, not render cost. Instanced scenes count
  once per unique geometry; a draw-call budget would be a different gate.
"""

from __future__ import annotations

import sys as _sys
import types as _types

# Run as a script this file is __main__; engine modules (fbx_preview,
# polypaint, rules_wizard, ...) say "import asset_gatekeeper", which must find
# this module rather than load it a second time.
_sys.modules.setdefault("asset_gatekeeper", _sys.modules[__name__])

# The code is in agk_gate/ (split into parts on 11 Nov 2026, no change in
# behaviour; agk_gate/__init__.py lists them). Every name is here as before.
import agk_gate.cli as _cli  # noqa: E402 - imports every part

#: The parts in import order (agk_gate/__init__.py says what each holds).
_PARTS = ("common", "config", "readers", "gate", "audit", "report", "cli")
for _part in _PARTS:
    globals().update({_name: _value for _name, _value in
                      vars(_sys.modules[f"agk_gate.{_part}"]).items()
                      if not _name.startswith("__") or _name == "__version__"})
del _part


class _OneNamespace(_types.ModuleType):
    """``asset_gatekeeper.trimesh = None`` (or ``patch.object(agk, ...)``) is
    seen by every part, as it was by the one file's functions."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for part in _PARTS:
            module = _sys.modules.get(f"agk_gate.{part}")
            if module is not None and name in module.__dict__:
                module.__dict__[name] = value

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        for part in _PARTS:
            module = _sys.modules.get(f"agk_gate.{part}")
            if module is not None:
                module.__dict__.pop(name, None)


_sys.modules[__name__].__class__ = _OneNamespace


if __name__ == "__main__":
    _sys.exit(_cli.main())
