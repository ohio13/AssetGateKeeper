"""AssetGatekeeper's gate, in parts (split out of asset_gatekeeper.py, 11 Nov 2026).

``import asset_gatekeeper`` is still the way in: it re-exports every name the
parts define, as the single file did. The parts, in import order:

* ``common``  - imports, optional dependencies, shared constants, the engine/ loader
* ``config``  - Budget, rules.toml, platform presets, tags, exceptions
* ``readers`` - glTF, OBJ, textures, the FBX parser, skin weights, UV sets,
  skeleton and lightmap facts
* ``gate``    - the Gatekeeper and its checks (references, PBR, LODs, collision)
* ``audit``   - naming suggestions, the repository audit, enterprise helpers
* ``report``  - the Reporter, telemetry, notifications, the skipped-files line
* ``cli``     - watching, the verdict cache, CI reports, build_parser() and main()

They are one namespace on purpose, so behaviour is exactly the single file's:
each part starts with ``from agk_gate.<previous part> import *``, the few names
a part uses from a later one are linked in at the end of ``cli``, and an
assignment to ``asset_gatekeeper.<name>`` (``patch.object`` in the tests)
reaches every part. Add new code to the part it belongs to; a name it needs
from a later part is found at run time.

Standard library only here: setuptools reads the version from this file.
"""

# Single source of truth for the version: pyproject.toml reads it from here
# (setuptools parses this literal statically), so it cannot drift.
__version__ = "0.1.0a1"   # shown as 0.1.0-alpha.1; internal 1.0-1.1.1 builds preceded it
