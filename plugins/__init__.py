"""Per-DCC panels and headless probes.

This file exists for the packaging: ``plugins/`` holds no installable code of its
own (Blender, Maya, Max, Houdini and Unreal each load one script out of their own
folder), but ``preflight_panel`` imports the shared checker as
``from plugins import preflight_core``. Without ``__init__.py`` setuptools treats
the folder as a namespace directory, ``namespaces = false`` skips it, and the
wheel ships without any DCC integration at all - which is how it shipped once.
"""
