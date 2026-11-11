# 02 - Installation

Five ways to install it. Pick the one that matches how you work; they all run the
same code, and you can switch later without losing data (the databases are just
files).

| Path | Use when | Needs admin | Takes |
|---|---|---|---|
| A. Drop-in zip | you want to try it, or the studio forbids installs | no | 1 min |
| B. Wheel into a venv | one machine, clean isolation | no | 3 min |
| C. From source | you are changing it, or pinning to a commit | no | 5 min |
| D. Shared install on a network share | a studio with 200 machines | yes (once) | 20 min |
| E. Air-gapped | no internet, no PyPI | no | 10 min |

## Requirements

* **Python 3.11+** recommended (3.9+ works; on 3.9/3.10 TOML rules need `tomli`).
  Verify with `python --version`. On Windows, `python` may be Python 2 on old
  images: use `py -3 --version`.
* **Nothing else for the core.** The gate, both servers, the healer and the tests
  are standard library only.
* **Two optional packages make measurement real:**
  `trimesh` (mesh analysis for `.glb`/`.gltf`/`.obj`) and `Pillow` (texture
  analysis). Without them, meshes and textures are stored but reported as
  `unverified` - the tool still runs, it just cannot see inside those formats.
* **`watchdog`** is optional: without it the file watcher falls back to polling
  (works, uses more CPU).
* **Ports:** 8080 (AssetHealth) and 8100 (Art Workstation) by default. Both are
  changeable.

## Path A - drop-in zip (no install)

```
1. Unzip asset-gatekeeper-1.0.0.zip somewhere you own, e.g. C:\Tools\AssetGatekeeper
2. Open a terminal in that folder
3. python asset_gatekeeper.py --version
4. python asset_gatekeeper.py "C:\Projects\MyGame\assets" --preset ps2
```

That is the whole setup. The `.bat` launchers next to it do the same things with
a double-click:

| File | Runs |
|---|---|
| `Install_Extras.bat` | installs the libraries below into the Python `Start AssetGatekeeper.bat` uses, then checks they load |
| `Start AssetGatekeeper.bat` | starts AssetHealth + Art Workstation in the background and opens the admin page |
| `Stop.bat` | stops everything `Start AssetGatekeeper.bat` started |
| `Allow_Folder.bat` | lets the project picker open another folder |
| `tools\agk-serve.bat` | the same launcher with a menu (status, logs, urls) |
| `Rules.bat` | the start file's old name, kept so old shortcuts work; it runs `Start AssetGatekeeper.bat` |
| `tools\start_workstation.bat`, `tools\start_asset_health.bat` | one server in its own window, for reading its errors |
| `tools\start_workstation.bat` | Art Workstation only (port 8100) |
| `tools\start_asset_health.bat` | AssetHealth only (port 8080) |

`Install_Extras.bat` (or `python tools/install_extras.py` on macOS and Linux)
does this for you. By hand: install `trimesh` and `Pillow` into the same Python for mesh and texture
measurement, and `charset-normalizer`, without which trimesh cannot read an `.obj`
or `.mtl` that is not UTF-8 (a cp1252 material name from an older Windows exporter):

```
python -m pip install "trimesh>=4" "pillow>=10" charset-normalizer DracoPy
```

## Path B - from PyPI (or a wheel) into a virtual environment

```
python -m venv C:\Tools\agk-venv
C:\Tools\agk-venv\Scripts\activate
pip install "asset-gatekeeper[full]"
asset-gatekeeper --version
```

Each tagged release is published to PyPI as `asset-gatekeeper`. Offline, or to
pin an exact build, install the wheel from the
[GitHub release](https://github.com/ohio13/AssetGateKeeper/releases) instead:
`pip install asset_gatekeeper-<version>-py3-none-any.whl`.

Optional extras, installed only if you need them:

| Extra | Adds | For |
|---|---|---|
| `asset-gatekeeper[full]` | `watchdog` | instant file watching |
| `asset-gatekeeper[enterprise]` | `psycopg`, `cryptography` | PostgreSQL + RS256 SSO |
| `asset-gatekeeper[postgres]` | `psycopg` | central database only |
| `asset-gatekeeper[saml]` | `signxml` | SAML assertion signatures |

This gives you five commands on `PATH`:

```
asset-gatekeeper          the gate
art-workstation           the 3D review screen        (port 8100)
asset-health              the metrics dashboard       (port 8080)
art-workstation-upload    bulk-load a folder of exports
agk-serve                 launcher: ports, logs, status
agk-enterprise            trends, RBAC checks, migrations, runners
```

## Path C - from source

```
git clone <your fork> C:\Tools\AssetGatekeeper
cd C:\Tools\AssetGatekeeper
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[full]"
python tests\test_gatekeeper.py     # 396 assertions, no network
```

`pip install -e` keeps the source live: edit the gate's code in `agk_gate/`, rerun, done.
This is the path to use if you are writing checks for your studio.

## Path D - one install for the whole studio

Install into a folder on a network share every machine can read, then point each
machine at it:

```
# once, as an administrator, into a share
python -m pip install --target \\studio\share\agk\lib ^
    asset_gatekeeper-1.0.0-py3-none-any.whl watchdog

# on every machine, in the user environment
setx PYTHONPATH "\\studio\share\agk\lib"
setx AGK_HOME   "\\studio\share\agk"
```

Every artist then runs `python -m asset_gatekeeper ...` or a small wrapper `.bat`
that the pipeline team ships. Two rules for this path:

* the share must be **read-only** to artists, or someone will edit
  `asset_gatekeeper.py` in place and the studio will debug a fork for a week;
* pin the version in the folder name (`agk\1.0.0\lib`), and switch the
  `PYTHONPATH` when you upgrade. Rolling back is then one `setx`.

## Path E - air-gapped / no PyPI

On a machine that has internet:

```
python -m pip download --dest C:\agk-bits ^
    "asset_gatekeeper-1.0.0-py3-none-any.whl" "trimesh>=4" "pillow>=10"
```

Copy `C:\agk-bits` to the offline machine, then:

```
python -m pip install --no-index --find-links C:\agk-bits ^
    asset_gatekeeper-1.0.0-py3-none-any.whl trimesh pillow
```

## Verify the installation

```
asset-gatekeeper --version            # 1.0.0
asset-gatekeeper --list-presets       # ten targets, budget numbers in a table
asset-gatekeeper --doctor             # environment report: python, packages, ports
python -c "import trimesh, PIL; print('measurement OK')"
```

`--doctor` is the one to run when something is odd: it reports the interpreter,
whether `trimesh`/`Pillow`/`watchdog` are importable, which rules file would be
used, and whether the default ports are free.

## Where it writes

Nothing writes to `Program Files` or the registry. The servers started by
`agk-serve` / `Start AssetGatekeeper.bat` keep their data in one folder:

| Installed as | Data folder |
|---|---|
| a downloaded or cloned copy | that copy's own folder (deleting it removes everything) |
| `pip install`, Windows | `%LOCALAPPDATA%\AssetGatekeeper` |
| `pip install`, macOS | `~/Library/Application Support/AssetGatekeeper` |
| `pip install`, Linux | `$XDG_DATA_HOME/asset-gatekeeper` (`~/.local/share/asset-gatekeeper`) |
| any, with `AGK_HOME` set | `$AGK_HOME` |

Inside it:

| Path | Written by | Contains |
|---|---|---|
| `asset_health.db` | AssetHealth | verdicts, problems, projects, budgets, admin |
| `art_workstation.db` | Art Workstation | assets, LODs, collision, notes |
| `workstation_data/` | Art Workstation | uploaded files, by asset id |
| `.agk/ports.json` | `agk-serve` | which port each server actually took |
| `.agk/logs/*.log` | `agk-serve` | one log per server |
| `.agk/allowed_folders.txt` | `Allow_Folder.bat` / `agk-serve allow` | extra project folders the picker may open |

Started by hand (`asset-health`, `art-workstation`), a server writes its
database to the folder it is run from instead. The gate's verdict cache is in
`%LOCALAPPDATA%\AssetGatekeeper\cache` (`~/.cache/asset-gatekeeper` elsewhere).

Back up a studio by copying the two databases and `workstation_data/`.
Restore by copying them back.

## Upgrading

```
agk-serve down                     # stop the servers: they hold code in memory
pip install --force-reinstall asset_gatekeeper-<version>-py3-none-any.whl
agk-serve up
```

The databases migrate themselves on first start of the new version. If you are
on the enterprise layer with PostgreSQL, run the versioned migrations instead:

```
agk-enterprise migrate --dsn $AGK_DB_URL --plan
agk-enterprise migrate --dsn $AGK_DB_URL --apply
```

**Uninstalling** is `pip uninstall asset-gatekeeper`, then delete the four items
in the table above if you also want the history gone.

Next: [03 Solo developer](03-SOLO-DEVELOPER.md) or
[04 Studio rollout](04-STUDIO-ROLLOUT.md).
