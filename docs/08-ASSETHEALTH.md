# 08 - AssetHealth: the metrics dashboard

The screen a producer opens. It answers "is the project getting healthier or
more expensive", not "is this asset good".

* **URL:** `http://127.0.0.1:8080`
* **Start:** `asset-health` (or `agk-serve up`)
* **Database:** `asset_health.db` (or a PostgreSQL DSN - see
  [Enterprise](ENTERPRISE.md))
* **Default port:** 8080

## What it ingests

Every gate run can post a verdict:

```
asset-gatekeeper "C:\Proj\assets" --once ^
  --telemetry-url http://127.0.0.1:8080/api/telemetry
```

The payload carries the file, the numbers, the problems, the host, the user and
the timestamp. The dashboard never re-runs the gate: it stores what happened.

Telemetry is fire and forget. If the dashboard is down, the gate still exits with
the right code and the verdict is simply not recorded - a metrics outage must
never become a pipeline outage.

## Projects and tokens

Everything is scoped to a project. A project is one repository (or one game), and
it owns:

* an **API token** the gate presents (`?token=` or `Authorization: Bearer`);
* its **verdicts**, **budgets** and **root paths**;
* its **alerts** settings.

Create one on first run (the bootstrap page), or from `/admin`. With no projects
at all, the dashboard runs in open mode - that is deliberate for a solo
developer, and the reason a real studio creates a project on day one.

Creating the first project switches tokens on: from then on the plain
`/dashboard` answers 401 and every client needs a token. Results recorded before
that belong to no project; only the admin token sees them, and the start-up
window counts them. Two commands manage the switch:

```
asset_health_server.py --db studio.db --adopt-legacy "Nova Survival"   # move earlier results in
asset_health_server.py --db studio.db --remove-project "Trial" --yes   # undo a trial project
```

`--remove-project` deletes the project's token, budget and exceptions and keeps
its results as project-less history. Without `--yes` it only says what it would
do. With no projects left, the server is back in open mode.

```
asset-gatekeeper <folder> --telemetry-url "http://health:8080/api/telemetry?token=<TOKEN>"
```

## Pages

| Route | What it shows |
|---|---|
| `/` | the wall: recent verdicts, pass rate, biggest offenders |
| `/dashboard` | per-project detail, bottlenecks, problem categories |
| `/analytics` | compliance over time, polygon growth, clustered errors (below) |
| `/budget-planner` | turn a platform target into numbers you can paste into rules |
| `/audit/full` | every verdict, paged (50 per page) |
| `/audit/passes` | only the passes - "show me what is working" |
| `/admin` | accounts, projects, the rules console |

Everything has a JSON twin under `/api/` (`/api/summary`, `/api/rules`,
`/api/events`, `/api/analytics`) so a wallboard or another tool can read the same
numbers without scraping.

### `/analytics` - the three questions

```
GET /analytics?since=90d&bucket=week&project_id=1
```

| Panel | Question it answers | How to read it |
|---|---|---|
| **Compliance** | are we shipping to budget? | a heal counts once as acceptable; the denominator is shown |
| **Polygon growth** | is the content getting more expensive? | average vs peak: rising average with flat count = heavier assets; flat average with rising count = more assets |
| **Error clusters** | which mistake do we keep making? | problems folded by signature (`72,500 triangles exceeds...` and `120,400...` are one row) with first/last sighting |

Windows are `7d`, `30d`, `90d`, `12m`, `3y`; buckets are `day`, `week`, `month`.
An empty panel explains itself: a growth chart with no data says the verdicts
carried no triangle metric, because an empty chart and "everything is zero" look
identical otherwise.

### Budget planner

It answers: "we are shipping on PS2 - what should the numbers be?" Choose the
target, adjust the two or three knobs that matter (characters vs props, texture
memory), and it prints the exact `rules.toml` block. It writes numbers, never a
platform name, so a preset can never silently override a director's edit.

Under the budget, **Exceptions** lists the project's waivers (path, what they
allow, owner, reason, end date - amber in the last 14 days, red once expired)
and adds or removes one. They are checked by the gate's own parser before they
are stored, and served with the budget on `/api/rules`, so every
`--remote-rules` client applies them (see
[05 rules reference](05-RULES-REFERENCE.md#exceptions)). API:
`POST /api/save-exception` with `{"project_id", "exception": {...}}`,
`POST /api/remove-exception` with `{"project_id", "id"}`, same token as
`/api/save-budget`.

### Admin console

First visit offers **Just me on this PC** (solo mode: no account, no password,
this machine only; requests from elsewhere get a page saying so) or bootstraps the
first account (both only while zero accounts exist, only from loopback; a request
that came through a reverse proxy, with `X-Forwarded-For` or `Forwarded`, counts as
from elsewhere even when the proxy runs on the same PC). Solo mode
is one director account named `me` with a random password nobody knows, plus a
per-run CSRF token; **Turn on accounts** (`/admin/accounts`) renames it and sets a
real password. `--add-admin` refuses while solo mode is on. On a first start
(no project set, none recent; or `/admin?setup=1`) the page opens with a
**Get started** wizard: project folder, engine (`[naming] preset` unreal, unity,
godot or none), hardware budget; then it saves and runs the first scan. Then:

* **accounts** - PBKDF2 (120,000 rounds), sessions expire after 12 hours;
* **projects** - create/rotate tokens, set roots;
* **rules console** - edit the project's rules with a live diff, **Apply & Rescan**
  (the rescan runs as a subprocess and streams progress over SSE), presets as a
  dropdown, tags as a tree;
* **audit trail** - who changed which rule, when, and what the diff was.

The project picker is allow-listed at launch: the install folder, folders added
with `Allow_Folder.bat` (kept in `.agk/allowed_folders.txt`; it restarts the
dashboard for you), `--allow-root` and `$AGK_PROJECT_ROOTS` (`;`-separated). Paths
are resolved before comparison, so `..` and symlinks cannot escape the list, and a
browser cannot widen it.

**Send to Workstation** asks the review screen for its upload limit first (512 MB
by default). A file over it is not sent; the row says how big it is and how to
raise the limit, instead of a connection error.

## Alerts

Every row carries a status word: `PASSED`, `HEALED`, `FAILED` or `ERROR`. `ERROR`
means the gate could not read the file at all - a locked handle, a permission the
account does not have, a reparse point gone stale. It is not a verdict on the
art, it is counted as a failure so the run goes red, and the audit page shows it
as `ERROR` rather than `FAILED` so nobody goes looking for a modelling problem
that does not exist.

A failure that blows a budget by **more than 50%** (80,000 triangles against a
50,000 budget) posts to a Slack, Discord or Teams webhook. Milder failures and
healed assets stay on the dashboard only.

```
python server/asset_health_server.py --webhook-url https://hooks.slack.com/services/...
```

The URL can also come from `SLACK_WEBHOOK_URL`, `DISCORD_WEBHOOK_URL` or
`TEAMS_WEBHOOK_URL`, or a `[webhooks]` table (`url`, `provider`, `cooldown`) in
`--webhooks-config`. `--alert-cooldown SECONDS` (default 300) stops a nightly
sweep failing 300 assets from sending 300 messages.

### Telling the artist

With a `people.toml`, the alert mentions the person whose machine sent the
failure, with a link that opens the asset in the Art Workstation:

```toml
[people.lena]
name = "Lena"
chat = "U012ABCDEF"            # Slack member ID / Discord user ID / Teams email
users = ["lwalker"]            # OS user names (sent with --telemetry-identity full)
hosts = ["WS-ART-014"]         # machine names (sent by default)
```

```
python server/asset_health_server.py --webhook-url ... --people people.toml \
    --workstation-url http://review-pc:8100
```

* The user name wins; the machine name is the fallback; nobody is guessed.
* Slack and Discord get `<@ID>`; Teams gets an Adaptive Card with a real
  mention (a plain Teams message cannot mention anyone).
* The cooldown is per person, so one artist's broken batch does not silence
  another's alert.
* The link is `<workstation>/viewer?find=<path>`: the workstation opens the
  newest version with that depot path or name. Use a `--workstation-url` the
  artists can reach.
* `--alert-digest 09:00` sends one message per person each day at that local
  time instead of one per failure.

## Demo data

```
asset-health --demo
```

Seeds 14 rows (13 assets and 1 healed) with a red "DEMO DATA" banner. It exists so
a producer can see the shape of the product before the pipeline is connected; the
banner exists so nobody mistakes it for a real project. `--no-seed` still parses,
for scripts that pass every flag.

## Running it in a studio

* **One instance**, behind the intranet proxy, pointed at PostgreSQL when more
  than one person needs the numbers (see
  [Enterprise](ENTERPRISE.md#1-postgresql-backend-and-historical-telemetry)).
* **Read replicas** for `/analytics` when heavy trend queries start competing with
  ingest (`read_dsns` in the enterprise config). Reads route automatically and
  fall back to the primary.
* **Circuit breakers** mean a database blip shows a fast error, not a 30-second
  hang per request: one breaker per endpoint, half-open probe after the cooldown.
* **SIEM**: audit events (refused tokens, RBAC denials, budget violations) can be
  forwarded to Splunk/Datadog - see
  [Enterprise](ENTERPRISE.md#siem-forwarding).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 401 from the gate | wrong or missing project token | rotate the token in `/admin`, update the gate command |
| Verdicts land in the wrong project | shared token across repos | one token per repo |
| Dashboard is empty after an upgrade | the server holds old code in memory | `agk-serve down` then `up` |
| `/analytics` shows a gap | no verdicts in that window | widen `since`; the panel says "no verdicts in this window" |
| Numbers disagree with the gate | the gate resolved a different `rules.toml` | the verdict's own file path is in `/audit/full` |

Next: [09 DCC plugins](09-DCC-PLUGINS.md), or [10 CI](10-CI-AND-VERSION-CONTROL.md).
