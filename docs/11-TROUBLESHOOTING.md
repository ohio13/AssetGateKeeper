# 11 - Troubleshooting

Symptom, cause, fix. Everything here is a failure this project has actually
produced, either in development or in a real studio run - not a guess.

## The gate

| Symptom | Cause | Fix |
|---|---|---|
| `tomllib.TOMLDecodeError` on line 1 of a rules file | the file starts with a UTF-8 BOM (PowerShell writes one) | save as UTF-8 without BOM, or `Get-Content -Raw | Set-Content -Encoding utf8NoBOM` |
| Crash with `UnicodeEncodeError: 'charmap'` on an asset name | non-UTF-8 console (`cp1252`) and an accented filename | set `PYTHONIOENCODING=utf-8`; the gate reconfigures its own streams, but wrappers can undo that |
| `OSError: [WinError 1920] The file cannot be accessed by the system` | a dangling symlink (often a WSL `ln -s`) in the tree | delete the link; the walker skips them and reports a count in `--doctor` |
| Gate reports 0 assets | wrong folder, or everything excluded | `--verbose`, and check `--exclude` and the folder path |
| `--preset` seems ignored | fixed in 1.0.0 for folders without a rules file | upgrade; see `tests/test_gatekeeper.py` for the regression |
| Rules file silently ignored | it is not named `rules.toml`, or it is not next to the folder | pass `--rules <path>`; the startup banner prints which file it used |
| Every asset fails on a fresh install | budgets from a wiki, applied to existing content | run a week in warn mode and fix the budget first |
| `.blend`/`.max` report "unverified" | expected: no DCC can measure them here | export a `.glb` alongside if you need a number |
| Exit code 2 in CI | the gate could not run (bad flag, bad rules, missing folder) | read stderr; this is a pipeline bug, not an asset bug |

## Ports and servers

| Symptom | Cause | Fix |
|---|---|---|
| `port 8080 is already in use` | another dashboard, or a previous instance | `agk-serve status`; `agk-serve down`; or `--port 8081` |
| A second bind "succeeds" on Windows | `SO_REUSEADDR` semantics | the launcher probes with `connect()` before binding; do not rely on the bind error |
| `agk-serve up` reports a different port | fallback is opt-in | check `.agk/ports.json`; it is the source of truth |
| The dashboard shows stale behaviour after an upgrade | the server holds code in memory | `agk-serve down` then `up` |
| Everything suddenly 401s | the project token was rotated | update the gate command / CI secret |
| Logs | `.agk/logs/*.log`, one per server | `agk-serve logs` |

## DCC tools

| Symptom | Cause | Fix |
|---|---|---|
| Houdini gate always fails, no verdict | hython exports `PYTHONHOME`/`PYTHONPATH`; the child Python loads Houdini's stdlib (`SRE module mismatch`) | set `AGK_PYTHON`; 1.0.0 strips the variables in `clean_subprocess_env()` |
| Blender panel missing | add-on installed but not ticked | Preferences > Add-ons > tick Asset Gatekeeper |
| Maya window does not open | script run from an IDE context | run from the script editor or a shelf button |
| Max: `python is not defined` | you are in the MAXScript listener | `python.ExecuteFile @"..."` |
| Unreal: module not found | Python Editor Script Plugin disabled | enable it, restart |
| Unreal headless hangs | no project, or a splash/pause prompt | pass the `.uproject`, `-unattended -nopause -nosplash -NullRHI` |
| Unreal exit code is `-1` | the editor reports its own status | read the verdict from the JSON, not `$LASTEXITCODE` |
| Panel numbers differ from the gate | the panel measures the scene, the gate the export | expected; check export settings |

## Database

| Symptom | Cause | Fix |
|---|---|---|
| `database is locked` | two processes writing the same SQLite file | one dashboard per file; or move to PostgreSQL |
| Dashboard slow with many verdicts | SQLite + a large audit table | `/analytics?since=90d`, VACUUM, or PostgreSQL |
| PostgreSQL: `psycopg` missing | optional dependency | `pip install "asset-gatekeeper[postgres]"` |
| PostgreSQL: `SRE module mismatch` | a DCC's `PYTHONPATH` leaked into a script | strip `PYTHONHOME`/`PYTHONPATH` before spawning |
| Migration refused: "checksum changed" | an applied migration file was edited | never edit an applied migration; add a new one |
| Migration refused: "blocking DDL" | DROP COLUMN and friends | rewrite as add-new + backfill + drop later, or `--allow-blocking` with a maintenance window |

## Rules and tags

| Symptom | Cause | Fix |
|---|---|---|
| "unknown key" on a valid-looking key | typo, or a key that belongs in a different table | the error lists the known keys |
| Tag not applied | the folder is a level deeper than four, or the name differs | tags resolve filename first, then up to four ancestor folders |
| `tag 'charcter' is not a known tag` | it is a typo, and the tool is telling you | rename the folder; use `--tag-mode strict` in CI to make it fatal |
| `--platform x` error | no `[platforms.x]` table in that rules file | add the table, or use a preset of the same name |
| A tag made a budget *looser* than base | impossible by design; categories only tighten | check the base `[mesh]` values, the tag is relative to them |

## Packaging and environment

| Symptom | Cause | Fix |
|---|---|---|
| `pip install .` fails building a wheel | the build backend imported a module that reconfigures stdout | fixed in 1.0.0; upgrade |
| `%~dp0` in a `.bat` swallow the next argument | `%~dp0` ends in a backslash and escapes the closing quote | use `"%~dp0."` |
| `python` is 2.7 | old Windows image | `py -3` |
| `trimesh` installed but still "unverified" | installed into a different interpreter than the one running the gate | `asset-gatekeeper --doctor` prints the interpreter it uses |

## Getting help

1. `asset-gatekeeper --doctor` - interpreter, packages, ports, rules file.
2. `asset-gatekeeper <folder> --once --json` - the raw verdicts.
3. `agk-serve logs` - server-side errors.
4. `python tests/test_gatekeeper.py` - is the install itself healthy? (396 assertions, no network)

If you file an issue, those four outputs answer most of the questions before they
are asked.

Back to the [index](README.md).
