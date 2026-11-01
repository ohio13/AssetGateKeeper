# 04 - Studio rollout: from a pilot team to 200 people

This is the path for anything bigger than one person. It is written as phases
because that is how it survives contact with production: every phase leaves the
studio in a working state, and you can stop after any of them.

| Phase | Who | Duration | You get |
|---|---|---|---|
| 0. Pilot | one team, one discipline | 2 weeks | proof the budgets are right |
| 1. Rules as code | the art leads | 1 week | reviewable policy in the repo |
| 2. Review and metrics | producers, directors | 2-3 weeks | 3D review + trends |
| 3. Gates at the door | pipeline | 2 weeks | DCC panels, submit triggers, CI |
| 4. Enterprise | IT + pipeline | 3-6 weeks | central DB, SSO, RBAC, SIEM |

Do not skip phase 0. The most common way a studio rejects a tool like this is by
rolling it out with budgets nobody validated, so every artist's first experience
is a false positive.

## Phase 0 - Pilot

Pick **one** discipline with a lead who has opinions: characters, or props.
Nothing else.

1. Install on the lead's machine only ([02](02-INSTALLATION.md)).
2. Run the gate over their current export folder with a preset that matches the
   hardware, `--json --out` to a file, and read the top twenty failures together.
3. For each one, decide: real bug, or wrong budget? Fix the rules file, not the
   asset, for the second category.
4. Repeat until the failure list is mostly real. This usually takes two passes,
   and the second pass reveals that your "prop budget" was really two budgets.

Success criterion: **the lead would be annoyed if you took it away**. Not "the
numbers are green" - that can be achieved by turning the checks off.

Keep that validated `rules.toml`. It becomes the base for the whole studio.

## Phase 1 - Rules as code

Rules are the studio's technical contracts, so treat them like code:

```
<PROJECT>/
  rules.toml            the effective budget for this repo
  assets/
    characters/         [tags.character]
    environments/       [tags.environment]
    props/              [tags.prop]
    fx/                 [tags.fx]
```

* **One `rules.toml` per repository**, owned by the technical art leads, changed
  by pull request. A rules change is reviewed like a shader change, because that
  is what it is.
* **Directories are the taxonomy.** A tag is a folder name; there are no
  patterns, no regex, and no per-file overrides by design. If a folder's budget
  is wrong, the folder is wrong.
* **Department budgets are tags.** `[tags.environment]` with 40k triangles and
  `[tags.character]` with 15k is the whole mechanism; see
  [05](05-RULES-REFERENCE.md#tags-and-precedence) for precedence and the
  typo protection.
* **A category can only tighten.** There is no key that turns a check off for a
  tag. If someone needs a bigger budget, they need a budget change that everyone
  can see in the diff.

Check the whole tree before you trust it:

```
asset-gatekeeper <PROJECT>/assets --json > baseline.json
asset-gatekeeper <PROJECT>/assets --summary
```

Freeze that baseline number somewhere visible. It is your starting point for
"are we getting worse", which phase 2 turns into a chart.

## Phase 2 - Review and metrics

Now put the two web surfaces behind your existing intranet/reverse proxy.

```
agk-serve up          # AssetHealth :8080, Art Workstation :8100
agk-serve status
agk-serve urls
```

* **Art Workstation** ([:8100](07-ART-WORKSTATION.md)) is for reviewers. They open
  a failing asset in 3D, read the gate verdict panel, and leave a note. No DCC, no
  licence, no download.
* **AssetHealth** ([:8080](08-ASSETHEALTH.md)) is for producers and TDs. Create one
  project per repo, give the repo's CI its token, and point every artist's gate at
  it.
* **Budget planner** turns a platform target into numbers, so the conversation is
  "the PS5 can afford this" rather than "the lead said 40k".
* **`/analytics`** is the honest trend: compliance per bucket, polygon growth,
  clustered errors.

Sequencing that matters: give artists the telemetry *before* the enforcement. A
week of "here is what would fail" removes most of the argument that follows.

## Phase 3 - Gates at the door

Enforcement belongs at the three places work actually crosses a boundary:

| Boundary | Mechanism | Document |
|---|---|---|
| DCC -> export | pre-flight panel | [09](09-DCC-PLUGINS.md) |
| working copy -> depot | Perforce trigger or Git hook | [10](10-CI-AND-VERSION-CONTROL.md) |
| branch -> main | CI job | [10](10-CI-AND-VERSION-CONTROL.md#ci) |

Start with **warn-only** everywhere. A door that blocks on day one creates a
shadow pipeline in week two.

```
# Perforce: warn first
p4 triggers -> add a change-content trigger that exits 0 but posts the verdict
# CI: warn first
asset-gatekeeper assets --json --out gate.json || echo "gate failed (not blocking yet)"
```

Flip to blocking per discipline, not globally, and only after that discipline's
false-positive rate is near zero.

## Phase 4 - Enterprise

Needed when one of these becomes true:

* more than one person needs the numbers, and they must agree;
* the studio has a security team that asks where the accounts are;
* a dashboard query is competing with ingest;
* someone needs to prove who loosened a budget, when, and for what path.

Then, in this order (each step is independent and reversible):

1. **PostgreSQL** - one database for all clients. See
   [Enterprise: database](ENTERPRISE.md#1-postgresql-backend-and-historical-telemetry).
   Migrate with `agk-enterprise migrate --sqlite ... --dsn ...`, then point every
   server at the DSN.
2. **Versioned migrations** - stop relying on `init_db` for production schema
   changes. `agk-enterprise migrate --dsn ... --plan|--apply|--verify`.
3. **SSO** - OIDC (Okta, Entra ID/Entra, Keycloak) or SAML (ADFS). Secrets from
   the environment only.
4. **Path RBAC** - lock `/environments/` to the environment leads. This is the
   feature that makes a 200-person studio possible: one lead can loosen a budget
   for their own department without loosening it for everyone.
5. **Read replicas** - when `/analytics` starts showing up in slow-query logs.
6. **SIEM** - when security needs the audit trail in Splunk or Datadog.

## The topology for a real studio

```
                       +-------------------+
                       |  PostgreSQL        |   primary: writes
                       |  (one cluster)     |   replicas: analytics, audit
                       +---------+---------+
                                 |
        +------------------------+------------------------+
        |                        |                        |
  +-----+------+          +------+------+          +------+------+
  | AssetHealth|          | Workstation |          |  CI runner  |
  |  :8080     |          |   :8100     |          |  (per job)  |
  +-----+------+          +------+------+          +------+------+
        |                        |                        |
   producers, TDs           reviewers              gate + telemetry
        |                        |
   browsers on the intranet (no plugin, no licence)

  artists' machines: asset-gatekeeper + a DCC panel, telemetry POSTing to :8080
```

Rules of thumb:

* the two web servers are **stateless** once PostgreSQL is in place - run two if
  you want, behind a load balancer, no sticky sessions needed;
* artists never talk to PostgreSQL directly; they talk to AssetHealth, which
  talks to the database;
* CI needs a token, not a person: one project per repo.

## Roles and what each one may do

| Role | Needs | Permissions (RBAC verbs) |
|---|---|---|
| Artist | gate + DCC panel | `assets.validate`, `workstation.review` |
| Department lead | override budgets for their paths | `rules.override`, `rules.edit` (scoped to `/department/**`) |
| Technical director | publish budgets | `rules.edit` (all paths), `audit.read` |
| Producer | trends, no overrides | `audit.read` |
| Pipeline engineer | everything | `admin` |
| External partner | review only, one project | `workstation.review` on one project token |

The RBAC config that expresses this is in
[Enterprise: authorization](ENTERPRISE.md#authorization). A deny always wins, and
a more specific rule beats a broader one - so "artists may not override anything
under /environments/rock" is expressible and cannot be talked around by argument.

## Overrides: the thing that kills these tools

An override is a lead saying "this asset is allowed to break the budget". It is
fine, it is normal, and it must leave a trace.

* Overrides are **path-scoped** and **permission-gated** (RBAC). Only a lead can
  do it, only in their department.
* Every override emits an audit event (`rbac.override`, and `budget.violation`
  for the asset that caused it), forwarded to the SIEM if configured.
* The verdict keeps both facts: what the gate measured, and who allowed it.
* Review override counts monthly, per department. A lead with forty overrides a
  month is not policing a budget, they are ignoring one - and now you can see it.

## Onboarding checklist for a new artist (10 minutes)

```
[ ] python + trimesh + pillow installed (or the studio share on PYTHONPATH)
[ ] asset-gatekeeper --version prints the studio version
[ ] the shared rules.toml is reachable from their working copy
[ ] DCC panel installed and shows a verdict on a test asset
[ ] their project token is in the gate command or the .bat they run
[ ] they have opened Art Workstation once and left a note on an asset
```

Keep this in the onboarding doc, not in someone's head. The first week is when
people decide whether a tool is trustworthy.

## Day-one pitfalls, from experience

| Symptom | Cause | Fix |
|---|---|---|
| Everyone's first run fails | budgets from a wiki, never validated against real assets | phase 0 |
| The gate is "too slow" | it is walking a 40k-file tree with network storage | point it at the export folder, not the repo |
| Numbers disagree between two people | they run different `rules.toml` | one file per repo, in version control |
| Reviews stall | reviewers must open a DCC | workstation + one URL |
| A lead overrides everything | overrides are free | count them monthly, publish the number |
| Security asks "where are the accounts" | no SSO | phase 4.3 |

Next: [05 Rules reference](05-RULES-REFERENCE.md) for the policy itself.
