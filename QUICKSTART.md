# AssetGatekeeper — Setup manual

You are installing three things that share one config file:

| Piece | What it is | Opened in |
|---|---|---|
| **Admin page** | pick the project, choose a platform budget, save it, run an audit | `http://127.0.0.1:8080/admin` |
| **AssetHealth** | pass rate, wall of shame, bottlenecks, history | `http://127.0.0.1:8080/` |
| **Art Workstation** | 3D review: LODs, collision, notes, reports | `http://127.0.0.1:8100/viewer` |

`asset-gatekeeper` (the command-line gate) is installed too — the web pages are a
front end for exactly the same checks.

**Two ways to work.** §2–§8 is the no-terminal path: double-click, click, done.
§9–§10 is the terminal path, for CI, VCS hooks and scripted runs. Everything in
§10 is also what the double-click does under the hood.

Total time: about ten minutes, most of it installing Python.

---

## 1. Before you start

| Need | Detail |
|---|---|
| Windows 10/11 | macOS/Linux work too, via the terminal path (§9) |
| **Python 3.9 or newer** | python.org installer, and tick **"Add python.exe to PATH"** |
| Disk | a few MB, plus whatever the workstation stores if you upload assets |
| Admin rights | not needed |

Check Python once, in any terminal:

```bat
py -3 --version
```

Update to something like `Python 3.11.x`. If the command is not found, reinstall
Python and tick the PATH box — nothing else in this manual can work without it.

> On Windows, `python` alone is often a 2.7 stub or a Store alias. Use `py -3`
> everywhere, or the venv's `Scripts\python.exe`.

---

## 2. Install

### A. From the release zip (recommended)

1. Unzip `asset-gatekeeper-1.0.0.zip` to a stable place, e.g.
   `C:\Tools\asset-gatekeeper`. Avoid a folder that syncs while it runs
   (OneDrive/GDrive) — the databases live in there.
2. Create a private environment so the two required packages don't touch your
   system Python:

   ```bat
   cd C:\Tools\asset-gatekeeper
   py -3 -m venv .venv
   .venv\Scripts\python.exe -m pip install trimesh pillow
   ```

   `Rules.bat` prefers `.venv` automatically when it exists.
3. Optional, if you want the `asset-gatekeeper` command on your PATH:

   ```bat
   .venv\Scripts\python.exe -m pip install dist\asset_gatekeeper-1.0.0-py3-none-any.whl
   ```

That is the whole install. There is no service to register, no installer, no
registry key.

### B. From the wheel only (CI, servers, or a developer)

```bat
py -3 -m venv .venv
.venv\Scripts\activate
pip install asset_gatekeeper-1.0.0-py3-none-any.whl
```

Gives you five console scripts and both web UIs:

| Command | What |
|---|---|
| `asset-gatekeeper` | the gate |
| `asset-health` | the dashboard / admin server |
| `art-workstation` | the 3D review server |
| `art-workstation-upload` | bulk-upload a folder into the review screen |
| `agk-serve` | start/stop/status both services at once |

`trimesh` and `Pillow` are installed as dependencies. They are the only two
third-party packages in the system.

---

## 3. First run: double-click `Rules.bat`

In the install folder, double-click **`Rules.bat`**.

It does this, in order:

1. finds an interpreter (`.venv`, then `py -3`);
2. checks `trimesh` and `Pillow` are importable, and if not tells you the exact
   `pip install` line;
3. starts **both** servers as detached background processes — free ports are
   chosen automatically (8080/8100, else the next free one), output goes to
   `.agk\logs\`, the chosen ports to `.agk\ports.json`;
4. opens your browser on the admin page;
5. prints the URLs, the ready-to-paste gate command, and how to stop everything.

Two console windows do **not** open, and you do not have to keep any window
alive. The console window `Rules.bat` itself opened can be closed; the services
keep running.

| I want to… | Do |
|---|---|
| re-open the page later | double-click `Rules.bat` again (it reuses the running services) |
| stop everything | `agk-serve.bat down` |
| see if they are running | `agk-serve.bat status` |
| read the server output | `agk-serve.bat logs --tail 60` |
| start with the review screen too | `agk-serve.bat up --open both` |

### Create the first account

On a brand-new install the browser lands on **"Create the first account"** —
because a fresh dashboard has no users, and a zero-CLI install should not need a
terminal for its first step.

- The first account is always a **director** (whoever sets up the box is
  accountable for the budgets).
- Password: **at least 10 characters**; only a PBKDF2-SHA256 hash (120,000
  rounds, per-user salt) is stored, so it cannot be recovered — write it down.
- Setup works **only from this machine** (loopback), and only while no account
  exists. Once you have signed in, the page never appears again.

Later accounts (producers, other directors) are added with one command:

```bat
agk-serve.bat down
.venv\Scripts\python.exe server\asset_health_server.py --add-admin sam --admin-role producer
agk-serve.bat up
```

The password is printed once. `--admin-role artist` gives a read-only account.

---

## 4. Point it at your project

The admin page can write `rules.toml` into a project folder — so it can only
open folders you allow **at launch**. Set this once:

```bat
setx AGK_PROJECT_ROOTS "D:\Projects;C:\Work\MyGame"
```

(`;`-separated. On macOS/Linux use `:` and `export`.) The install folder is
always allowed. Alternatively pass them at launch:
`Rules.bat --allow-root "D:\Projects"`.

Close and re-open `Rules.bat` after `setx` so the new environment is picked up.

Then, in the page:

1. **Project** panel → type or paste the folder, or pick from the dropdown /
   recent chips;
2. press **Use this folder**.

What you get:

- the page shows `rules.toml: <folder>\rules.toml — found` (an existing file is
  loaded into the form) or `— will be created on save`;
- from then on, saves write into **that** folder and audits scan **that** folder;
- the choice is remembered in the recent chips and recorded in the audit trail.

If you get **"that folder is outside the allowed roots"**, the page is telling
you the truth: add the folder (or one of its parents) to `AGK_PROJECT_ROOTS`.
A `..` in a path is not a way around it — paths are resolved before they are
compared.

---

## 5. Choose the budget

Pick a platform target from the **Preset** dropdown. The form fills with that
machine's starting numbers; nothing is saved until you press a button.

| Preset | Character polys | Prop polys | Texture edge | Material slots | POT | PBR |
|---|---|---|---|---|---|---|
| N64 | 1,200 | 400 | 64 | 2 | yes | no |
| PS1 | 1,100 | 350 | 256 | 1 | yes | no |
| Nintendo DS | 900 | 300 | 128 | 2 | yes | no |
| GameCube | 6,000 | 2,000 | 512 | 4 | yes | no |
| Xbox (2001) | 9,000 | 3,000 | 1,024 | 4 | yes | no |
| PS2 | 5,000 | 1,800 | 256 | 3 | yes | no |
| 3DS | 3,000 | 1,200 | 256 | 2 | yes | no |
| Wii | 5,000 | 2,000 | 512 | 3 | yes | no |
| PS3 | 20,000 | 8,000 | 1,024 | 4 | no | no |
| Modern PC | 120,000 | 30,000 | 4,096 | 8 | no | yes |

These are defensible starting points for each machine, not measurements of your
content — adjust to your frame budget.

- **Character / Prop polygons** become real categories
  (`[tags.character]` / `[tags.prop]`). Anything under a `Boss\` folder or
  named `Boss_x.glb` is matched that way, so "characters are bigger than props"
  is enforced per asset, not just documented.
- **Max texture edge**, **Material slots**, **UV channels**, **Bone influences**:
  see the tooltips; the checkerbox flags (power-of-two, PBR albedo range,
  texture naming, reference checks, degenerate triangles) match the platform's
  era.
- **Categories**: the table lists the tags the file already has; *Add category*
  appends one (name + polygon budget).
- **Target** is read-only: the preset is a *formula that fills the numbers*. The
  file stores the numbers, not the preset's name — so a later hand-edit to a
  budget is never silently overridden on the next run.

The same catalogue is available without a browser:

```bat
.venv\Scripts\asset-gatekeeper --list-presets
```

---

## 6. Save & Run Audit

Two buttons, both explicit (the page never rescans while you type):

| Button | What happens |
|---|---|
| **Save only** | validate → atomically write `rules.toml` into the project |
| **Save & Run Audit** | the above, then scan the project and stream the verdicts in |

On save the server:

1. **validates first** — unknown keys, bad numbers, or a platform name with no
   matching `[platforms.<name>]` table are refused *before* anything touches the
   disk (a broken `rules.toml` stops the whole gate, so it must never be written);
2. **writes atomically** — temp file + `fsync` + rename, so the gate, CI and your
   editor only ever see the old file or the new one;
3. **keeps sections the form does not show** (`[filters]`, `[platforms]`,
   `[server]`, `[healer]`) — an apply never deletes your hand-written config;
4. **records one audit row per changed key** — timestamp, user, role, the dotted
   key (`tags.boss.max_triangles`), and both old and new values, plus the file;
5. **starts one background scan** over the project, posting telemetry back to the
   dashboard that launched it.

The **Scan** panel fills live: `PASS`/`FAIL` per asset, running counts, and a
final `done: 466 passed, 0 failed` line. One scan at a time — a second request
gets "a scan is already running" instead of competing for CPU.

Everything you saved is plain text and belongs in version control:

```bat
cd D:\Projects\MyGame
git add rules.toml && git commit -m "N64 budgets: 1200 char / 400 prop"
```

---

## 7. Who may change what

| Role | Read the rules | Change budgets | Typical holder |
|---|---|---|---|
| `director` | yes | yes | you, the first account |
| `producer` | yes | yes | production |
| `artist` | yes | **no** — and the page says why | the team |

Sessions last 12 hours (`HttpOnly; SameSite=Strict` cookies). Every change is in
the **Audit trail** panel and in `git log`; both survive restarts, the audit table
is the short version.

Security, stated plainly: this is authentication for a **local** dashboard. If
you ever bind it to the network (`--host 0.0.0.0`), put TLS in front of it —
roles cannot protect a password sent in clear text.

---

## 8. The other two surfaces

**AssetHealth** — `http://127.0.0.1:8080/`: pass rate, wall of shame, which rule
everybody is failing, and history. `/?refresh=5` makes it a wallboard.

Optional Slack/Discord/Teams alerts — set the webhook once, then restart:

```bat
setx AGK_WEBHOOK_URL "https://hooks.slack.com/services/…"
:: then close Rules.bat's window and double-click it again
agk-serve.bat status
```

`Rules.bat` and `agk-serve.bat up` both read `AGK_WEBHOOK_URL` (or `--webhook-url`
/ `--webhook-provider slack|discord|teams`). Only *critical* failures alert, and
at most one message per project per 5 minutes — a nightly sweep that breaks a
hundred files is one notification, not a hundred.

**Art Workstation** — `http://127.0.0.1:8100/viewer`: drag a `.glb`/`.obj`/`.gltf`
onto the page, or load a folder:

```bat
.venv\Scripts\art-workstation-upload "D:\Projects\MyGame\assets" --recursive --skip-uploaded
```

Per asset: textured / geometry / wireframe / collision views, LODs, a collision
proxy you can export as `.obj`, per-material roughness, notes with categories,
and the **📋 Rules & Budgets** tab showing the same numbers the gate enforces.
Uploads are *copied* into `workstation_data\` — your source art is never touched.

---

## 9. The terminal path

For CI, VCS hooks, or when you want to see exactly what a button does.

### One scan, read-only

```bat
.venv\Scripts\asset-gatekeeper "D:\Projects\MyGame\assets" --recursive --once
```

One line per asset, every violation on it, exit code `0` pass / `1` fail / `2` bad
setup. Add `--json` for machine output.

### Write a starter config by hand

```bat
.venv\Scripts\asset-gatekeeper "D:\Projects\MyGame\Assets" --init
```

Five questions with defaults; every answer is also a flag (`--triangles`,
`--max-texture`, `--max-materials`, `--platform`), plus `--yes` for scripts.

### Watch mode while people work

```bat
.venv\Scripts\asset-gatekeeper "…\Assets" --recursive --notify
```

New and changed files are inspected after they settle; failures raise a toast.

### Feed the dashboard

```bat
.venv\Scripts\asset-gatekeeper "…\Assets" --recursive --once ^
    --telemetry-url http://127.0.0.1:8080/api/telemetry
```

or set it once per machine: `setx ASSET_GATEKEEPER_SERVER_URL http://127.0.0.1:8080/api/telemetry`.

### Block bad commits

```bat
copy vcs_hooks\git_pre_commit.py .git\hooks\pre-commit
```

Perforce users: `vcs_hooks\p4_pre_submit.py` as a submit trigger. Hooks always
pass `--no-auto-heal` and fail closed.

### CI

`.github/workflows/asset_gatekeeper.yml` is a drop-in PR gate. Keep the exit code;
never `|| true` it.

---

## 10. Verify the install

```bat
.venv\Scripts\asset-gatekeeper --doctor
```

Checks Python, `trimesh`, `Pillow`, optional `watchdog`, the target folder, the
`rules.toml` it would actually use (printing the parsed budget and categories),
and whether 8080/8100 are free — every failing line shows the fix. Exit code `1`
means a real problem; a busy port is only a warning.

Run the project's own tests if you want proof the checkout is healthy:

```bat
.venv\Scripts\python.exe tests\test_gatekeeper.py
.venv\Scripts\python.exe tests\test_admin.py
```

Each prints `ok` per assertion and `FAILURES: none` at the end (801 assertions in
total across four suites).

---

## 11. Everyday operations

| Task | Command |
|---|---|
| start / re-open | `Rules.bat` |
| stop | `agk-serve.bat down` |
| status | `agk-serve.bat status` |
| URLs + gate command | `agk-serve.bat urls` |
| tail the logs | `agk-serve.bat logs --tail 60` |
| start without a browser | `agk-serve.bat up` |
| different ports | `agk-serve.bat up --health-port 9000 --workstation-port 9100` |

### Where things live

| What | Where |
|---|---|
| budgets the gate enforces | `<project>\rules.toml` (plain text, in your git) |
| verdict history | `asset_health.db` in the install folder |
| accounts, sessions, audit trail | same database |
| review library, notes, LODs | `art_workstation.db` + `workstation_data\` |
| logs, chosen ports, pids | `.agk\logs\`, `.agk\ports.json` |
| venv | `.venv\` |

Back up by copying the `.db` files while the services are stopped. Deleting
`.agk\` is always safe — it is regenerated on the next start.

---

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Rules.bat` closes instantly | Python not on PATH, or `trimesh`/`Pillow` missing — run it from `cmd` once; it prints which, with the exact fix |
| "Python was not found" | reinstall Python and tick **Add python.exe to PATH**, then `py -3 --version` |
| `trimesh`/`Pillow` missing | `.venv\Scripts\python.exe -m pip install trimesh pillow` |
| browser opens a dead page | the services took longer than the browser; wait 2 s and refresh, or run `agk-serve.bat status` |
| `cannot bind … in use` | another process owns the port; the message prints the find/stop commands. `--port-auto` moves instead of failing |
| "that folder is outside the allowed roots" | add it to `AGK_PROJECT_ROOTS` and restart (`setx` needs a new console) |
| page says "sign in" and I have no password | the account exists but the password is lost. Stop the services and create another: `.venv\Scripts\python.exe server\asset_health_server.py --add-admin <name> --admin-role director` (or `asset-health …` if the wheel is installed) |
| admin page is blank | a JS error blanks the module: open the browser console (F12). `tests\test_admin.py` checks every element id the script uses exists |
| saves say "no changes" | the form already matches the file — or you are editing a different project than you think; check the `rules.toml:` line under the picker |
| a budget I set has no effect | precedence: CLI flag > remote rules > `[platforms.*]` > `rules.toml` > defaults. `--doctor` shows what is in force |
| PowerShell blocks a script | `Set-ExecutionPolicy -Scope Process RemoteSigned` in that window (the `.bat` files are unaffected) |
| OneDrive keeps locking the database | move the install folder out of a synced directory |

---

## 13. Uninstall

Nothing was installed system-wide, so removal is deletion:

```bat
agk-serve.bat down
```

then delete the folder (or keep it and delete only `.agk\`, `*.db`,
`workstation_data\`, `.venv\` to reset state while keeping the tool). Project
`rules.toml` files stay where you put them — they are yours, and they are in git.
