"""The two web front ends: AssetHealth (metrics) and the Art Workstation (review).

Both are stdlib-only scripts that also work as installed modules - their own
``HERE``/``REPO_ROOT`` bootstrapping keeps ``python server/<name>.py`` working
from a checkout, which is why ``STATIC_DIR`` is resolved from ``__file__`` rather
than from the current directory.

Installed, they are reachable as console scripts (``asset-health`` and
``art-workstation``) or as ``python -m server.asset_health_server``.
"""
