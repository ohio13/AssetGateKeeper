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

### Admin console

First visit bootstraps the first account (the page is only reachable while zero
accounts exist). Then:

* **accounts** - PBKDF2 (120,000 rounds), sessions expire after 12 hours;
* **projects** - create/rotate tokens, set roots;
* **rules console** - edit the project's rules with a live diff, **Apply & Rescan**
  (the rescan runs as a subprocess and streams progress over SSE), presets as a
  dropdown, tags as a tree;
* **audit trail** - who changed which rule, when, and what the diff was.

The project picker is allow-listed at launch (`--allow-root`, `$AGK_PROJECT_ROOTS`,
`;`-separated). Paths are resolved before comparison, so `..` and symlinks cannot
escape the list, and a browser cannot widen it.

## Alerts

```toml
# rules.toml or the project settings
[alerts]
enabled = true
cooldown_minutes = 30
```

A new failure that repeats does not page anyone twice inside the cooldown. A
webhook is available for Slack/Teams:

```
AGK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

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
