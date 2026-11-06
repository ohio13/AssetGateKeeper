# AssetGatekeeper documentation

This folder is the manual. It is split by **who you are** and **what you are
doing**, because a solo developer and a 200-person studio need different words
for the same flag.

## Start here

| If you are... | Read, in this order |
|---|---|
| Trying it once, alone | [02 Installation](02-INSTALLATION.md) -> [03 Solo developer](03-SOLO-DEVELOPER.md) |
| Setting up a small studio (2-15) | 02 -> [04 Studio rollout](04-STUDIO-ROLLOUT.md) (Phase 0-2) -> [09 DCC plugins](09-DCC-PLUGINS.md) |
| Rolling out to 50-200 people | 04 (all phases) -> [Enterprise layer](ENTERPRISE.md) -> [10 CI and version control](10-CI-AND-VERSION-CONTROL.md) |
| Writing or reviewing budgets | [05 Rules reference](05-RULES-REFERENCE.md) |
| Living in the terminal / CI | [06 CLI reference](06-CLI-REFERENCE.md) |
| Reviewing art | [07 Art Workstation](07-ART-WORKSTATION.md) |
| Watching the numbers | [08 AssetHealth](08-ASSETHEALTH.md) |
| Something is broken | [11 Troubleshooting](11-TROUBLESHOOTING.md) |
| Wanting everything on one page | [Reference](REFERENCE.md) (the old long README) |

## The whole map

| Document | Answers |
|---|---|
| [01 What it is](01-WHAT-IT-IS.md) | What problem this solves, the verdict model, and what it deliberately refuses to do |
| [02 Installation](02-INSTALLATION.md) | Every way to install it: zip, wheel, source, venv, offline, CI. Requirements, ports, upgrades |
| [03 Solo developer](03-SOLO-DEVELOPER.md) | One person, one machine, twenty minutes, no server |
| [04 Studio rollout](04-STUDIO-ROLLOUT.md) | Phased adoption, topology, roles, department budgets, SSO, replicas, SIEM |
| [05 Rules reference](05-RULES-REFERENCE.md) | Every `rules.toml` key, precedence, presets, tags, fuzzy matching, healing |
| [06 CLI reference](06-CLI-REFERENCE.md) | Every command and flag, exit codes, telemetry payload, hooks, watcher behaviour |
| [07 Art Workstation](07-ART-WORKSTATION.md) | The 3D review screen: uploads, LODs, collision, notes, the gate verdict panel |
| [08 AssetHealth](08-ASSETHEALTH.md) | Telemetry, projects and tokens, dashboard, alerts, budget planner, admin, `/analytics` |
| [09 DCC plugins](09-DCC-PLUGINS.md) | Blender, Maya, 3ds Max, Houdini, Unreal: install, behaviour, headless, troubleshooting |
| [10 CI and version control](10-CI-AND-VERSION-CONTROL.md) | Git hooks, Perforce triggers, LFS, remote validation, runner farms |
| [11 Troubleshooting](11-TROUBLESHOOTING.md) | Symptom -> cause -> fix, including the bugs this project has actually hit |
| [Enterprise layer](ENTERPRISE.md) | PostgreSQL, OIDC/SAML, path RBAC, migrations, SIEM, read replicas |
| [../MANUAL.md](../MANUAL.md) | The original long-form manual, kept for reference |
| [../QUICKSTART.md](../QUICKSTART.md) | The one-page version |
| [../RELEASE_NOTES.md](../RELEASE_NOTES.md) | What changed, and why |

## Conventions used in this folder

* `AGK` is shorthand for the folder containing `asset_gatekeeper.py`. Paths are
  written for Windows because that is where DCC tools live; on macOS or Linux use
  the same paths with `/`.
* `<PROJECT>` is the folder holding the assets you export (usually the repo you
  open in your DCC).
* `rules.toml` always means the *effective* rules: the file found next to the
  assets, plus any platform profile and CLI override on top of it.
* Commands that start with `asset-gatekeeper` assume the package is installed.
  From an unzipped copy, `python asset_gatekeeper.py` is the same program.
* Exit codes are a contract in this tool. `0` means the assets are clean, `1`
  means the gate ran and found problems, `2` means the gate could not run. See
  [06](06-CLI-REFERENCE.md#exit-codes).

## The four things this software does

1. **Gates assets** against technical budgets - polygons, texture memory, draw
   calls, PBR sanity, broken references - in the terminal, in CI, in Perforce,
   and before the artist exports.
2. **Shows the art in 3D** so a reviewer can decide without opening a DCC.
3. **Collects the numbers** so a producer can see whether the project is getting
   healthier or more expensive over time.
4. **Stays out of the way**: two SQLite files, no service to babysit, and every
   enterprise feature is opt-in.

If you only read one more page, read [01 What it is](01-WHAT-IT-IS.md): the rest
is configuration, and configuration only makes sense once the model does.
