"""Enterprise layer for AssetGatekeeper.

Everything in this package is optional and lazy: the gate, the dashboard and the
workstation keep working with zero configuration and zero extra dependencies.
Importing this package costs nothing; importing its modules only imports the
third-party packages they actually need (psycopg3 for PostgreSQL, cryptography
for RS256 tokens), and each of those failures is reported as a clear config
error instead of an ImportError traceback in a studio's CI log.

Modules
    config      typed configuration, secrets via environment only
    store       storage abstraction: SQLite (default) or pooled PostgreSQL
    analytics   historical trends: compliance, polygon growth, error frequency
    rbac        path-based role grants with deny-wins evaluation
    auth        OIDC / SAML service provider, signed sessions
    vcs         Git, Git LFS pointers and Perforce Helix Core
    remote      validate depot paths and URLs without a working copy
    cli         the ``agk-enterprise`` command
"""

__version__ = "1.0.0"
