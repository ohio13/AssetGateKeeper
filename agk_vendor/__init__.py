"""Third-party code bundled so the engine runs inside DCC Pythons unchanged.

``tomli`` 2.0.1 (MIT, Taneli Hukkinen; licence in ``tomli/LICENSE``): the TOML
reader that became the standard library's ``tomllib`` in Python 3.11. Unreal
5.3 (Python 3.9), Blender 4.0 (3.10) and Maya 2022-2024 (3.7-3.10) have no
``tomllib`` and no ``tomli``, so a project ``rules.toml`` could not be read
inside them - the in-editor checks failed on the first import. Used only when
neither ``tomllib`` nor an installed ``tomli`` is available. Unmodified.
"""
