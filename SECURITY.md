# Security policy

AssetGatekeeper reads files that people hand it, and its optional servers
(AssetHealth and the Art Workstation) accept uploads and tokens. Please report
security problems privately, not in a public issue.

## How to report

Use GitHub's **Report a vulnerability** button on the repository's Security tab
(private vulnerability reporting). Include what an attacker could do, the steps
or file that show it, and the version (`asset-gatekeeper --version`).

This is a one-person, spare-time project, so I can't promise a reply time, but
security reports come before everything else. Fixes for confirmed problems are
released as soon as they are ready, and credited to you unless you prefer
otherwise.

## Supported versions

During the alpha, only the latest release on `main` gets security fixes.

## Running the servers safely

Both servers are built for a trusted studio network. On anything wider, create a
project or an admin token (so every request needs one), put them behind HTTPS,
and never expose them directly to the internet.
