"""Compatibility shim for tooling that still expects a setup.py.

Everything real lives in ``pyproject.toml`` (PEP 621 metadata, version read from
``asset_gatekeeper.__version__``). This file exists so ``pip install -e .``,
``python setup.py --version`` and old CI images keep working; it must stay empty
of configuration so the two files cannot drift apart.
"""

from setuptools import setup

setup()
