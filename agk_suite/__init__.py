"""Home of AssetGatekeeper's servers, tools and plug-ins in an installed copy.

In this repository server/, engine/, tools/, enterprise/ and plugins/ sit at the
top level. A wheel installs them as ``agk_suite.server`` and so on instead:
installed at the top of site-packages, packages with names as common as
``server`` or ``tools`` would overwrite, or be overwritten by, any other package
using the same name (9 Nov 2026 audit, B3). The modules add this folder to
``sys.path`` themselves, so their own imports (``from server import netguard``)
work unchanged in both layouts.
"""
