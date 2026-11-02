# Upgrading to 1.1.0

1.1.0 is a correctness release, and a few of the fixes change behaviour on
purpose. Nothing needs a migration of stored data; the gate's database schema is
unchanged. This page is the list of things that may surprise you, in the order
they are likely to.

## 1. A wrong `Host` header is refused (both servers)

Both the dashboard and the Art Workstation now answer only when the `Host`
header names this machine: a loopback name, **any IP literal** (so
`http://192.168.1.10:8080` works), this machine's own hostname, the address the
server was bound to, or a name you add.

* **If you reach the dashboard by DNS name** (`http://artbox.studio:8080`), start
  the server with `--allow-host artbox.studio`. Without it you get a `403` with a
  message that says so.
* If your studio uses a reverse proxy, either pass `--allow-host <public name>`
  or bind to the name's address.
* This is the defence against a DNS-rebinding page driving a local dashboard. It
  is not optional and there is no flag to switch it off.

## 2. Telemetry carries the machine name, not the OS account

`--telemetry-identity` (or `[server] identity`) has three levels:

| Level | `hostname` | `username` | `path` |
|---|---|---|---|
| `full` | sent | sent | absolute |
| **`host`** (default) | sent | empty | absolute |
| `none` | empty | empty | relative to the watch folder |

An empty `hostname` groups every machine into one "unknown" bucket, so the
default keeps it: per-host dashboards keep working. The OS account - the part
that names a person - is opt-in.

If you were relying on `username` for Slack alerts or per-person filters, set
`--telemetry-identity full` on the artists' clients (or `identity = "full"` in
the shared `rules.toml`).

## 3. Stricter verdicts (these will turn some pipelines red)

All of these are deliberate, and each one is a bug that used to pass:

* **A reference that leaves the watch folder fails.** `../../shared/wood.png` or
  a hardcoded `C:/Users/...` path in a `.gltf`/`.mtl` is a `FAILED`, not a pass.
  If your studio really does keep a shared texture library up there, set
  `[mesh] allow_outside_references = true` and the finding becomes a warning.
* **An empty folder fails `--once`.** Previously "no tracked assets" exited 0,
  which is how CI went green on a typo'd path. Pass `--allow-empty` where an
  empty folder is legitimate (a fresh department folder, a tag-scoped run).
* **A file that cannot be read is an `ERROR`, not a pass.** New status word, same
  red run. Parsers should branch on `status`, not on `passed`.
* **`_BaseColor` and `_ORM` are now standard suffixes.** Textures following
  Unreal's convention pass naming where they used to fail. If your convention is
  different, list it: `[texture] suffixes = ["_Albedo", "_Normal", "_Roughness"]`.
* **`--triangles -1`, `nan`, and empty suffix lists are refused** at parse time
  or on load, instead of quietly failing every asset or disabling a rule.

* **FBX, TGA and JPG are now gated.** Folders full of `.fbx` that were silently
  skipped are now measured against the same budgets - expect new failures there
  first. Raise the budget per tag rather than excluding the format.
* **A truncated texture fails.** PNG/JPG/TGA are decoded in full; a half-written
  file used to pass on its header.
* **Unreal's `import_with_gatekeeper()` blocks when the gate cannot run** (not
  configured, engine missing, crash). Set `ASSETGATEKEEPER_PYTHON` and
  `ASSETGATEKEEPER_HOME` on every editor machine that uses it.
* **Unreal drag-and-drop imports over budget are moved out** to
  `/Game/_AssetGatekeeper_Rejected` (1.0 only warned). Set
  `ASSETGATEKEEPER_UNREAL_ON_FAIL=warn` on a machine that must keep the 1.0
  behaviour, or `delete` to discard instead of quarantining.
* **FBX triangle counts can go down.** `UCX_`/`UBX_`/`USP_`/`UCP_` collision
  meshes and LOD1+ of an LOD group are no longer counted, matching what Unreal
  imports. An FBX that failed by a small margin may now pass.
* **ASCII FBX is measured** instead of failing with "re-export as binary".

## 4. Healing is safer, and refuses more

A texture rename now repoints every `.mtl`/`.gltf` in the project that named the
old file, wherever it sits, including percent-encoded URIs and Windows
backslash paths. When the healer cannot prove a rename is safe it now leaves the
file alone rather than shipping a broken reference - so a few assets that used to
be "healed" will now fail with the real reason.

`[healer] texture_mode = "resize"` is still the default; `pad` is unchanged.

## 5. The viewer no longer needs the internet

Three.js is vendored under `server/static/vendor/`, so the Art Workstation works
on a machine with no route to unpkg.com. The dashboard sends a
`Content-Security-Policy` and refuses to be framed. If you had a browser
extension or a proxy rewriting those pages, check it still renders - then run
`python tools/browser_smoke.py`, which drives the real pages in headless
Chromium and fails on any console error.

## 6. Security headers changed `Referrer-Policy` to `same-origin`

`no-referrer` made Chromium post `Origin: null` on the admin sign-in form, which
the CSRF check refused: admin login was broken in every real browser. If you
depend on a `no-referrer` policy for something else, put it on the proxy.

## 7. What still is not different

* **Unverified assets** (`.blend`, `.max`, `.ma` and anything trimesh cannot
  open) are still stored, still excluded from every triangle-budget calculation,
  and still not gated. They are reported as unmeasured, not as passing. Do not
  read "no failures" on a folder of `.blend` files as "this folder is fine".
* The gate still never heals on its own: healing needs `--auto-heal` or
  `[healer] enabled = true`, and hooks pin it off.
* Budgets, presets and tag tables are unchanged in shape. 1.1.0 only refuses
  values that were never meaningful.

## Verification commands

```
python tests/test_gatekeeper.py      # gate, CLI, healing, telemetry
python tests/test_server.py          # dashboard, admin, security headers
python tests/test_workstation.py     # viewer API, uploads, LODs
python tests/test_enterprise.py      # storage, migrations, RBAC, runners
python tests/test_analytics.py       # analytics over both engines
python tools/browser_smoke.py        # the two UIs in a real browser
python tests/test_dcc_integration.py --deep    # Blender / Houdini / Unreal
```
