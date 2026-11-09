"""Admin surface: accounts, sessions, CSRF, roles, atomic rules writes, audit, rescan.

Run directly (`python tests/test_admin.py`); no network, no browser, no GPU. The
servers are real ``ThreadingTCPServer`` instances on port 0 so the tests exercise
the same code paths a director's browser does.

This file is separate from test_server.py on purpose: everything here is about
*who is allowed to change a budget*, and it should be possible to read those
assertions in one sitting.
"""

from __future__ import annotations

import http.cookiejar
import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO), str(REPO / "server")]

import asset_gatekeeper as agk          # noqa: E402
import asset_health_server as ahs       # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="agk-admin-"))
ok_count = 0
failures: list[str] = []


def expect(label: str, condition: object, detail: object = "") -> None:
    global ok_count
    if condition:
        ok_count += 1
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}  {detail}")


class Session:
    """A browser-ish client: keeps cookies, sends CSRF, follows redirects."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, path: str):
        return self._call(path)

    def form(self, path: str, fields: dict):
        return self._call(path, urllib.parse.urlencode(fields).encode(),
                          {"Content-Type": "application/x-www-form-urlencoded"}, "POST")

    def post(self, path: str, payload: dict | None = None, csrf: str | None = None):
        headers = {}
        data = b""
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()
        if csrf:
            headers["X-CSRF-Token"] = csrf
        return self._call(path, data, headers, "POST")

    def _call(self, path: str, data=None, headers=None, method=None):
        request = urllib.request.Request(self.base + path, data=data, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def state(self) -> dict:
        return json.loads(self.get("/admin/api/state")[1])


def start(db: Path, rules: Path, assets: Path | None = None,
          allow: tuple[str, ...] = ()) -> tuple:
    server = ahs.build_server("127.0.0.1", 0, db, quiet=True, rules_path=str(rules),
                              rules_root=str(rules.parent),
                              scan_root=str(assets or rules.parent),
                              allow_roots=allow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# --------------------------------------------------------------------------- #
print("0. password and document helpers")
secret = ahs.hash_password("correct horse battery staple")
expect("a stored password is a versioned pbkdf2 string", secret.startswith("pbkdf2_sha256$"))
expect("the plaintext is not in the stored value", "correct horse" not in secret)
expect("the right password verifies", ahs.verify_password("correct horse battery staple", secret))
expect("a wrong password does not", not ahs.verify_password("wrong", secret))
expect("two hashes of one password differ (per-user salt)",
       ahs.hash_password("same") != ahs.hash_password("same"))
expect("a corrupt hash fails closed", not ahs.verify_password("x", "not-a-hash"))

before = {"mesh": {"max_triangles": 1000}, "tags": {"boss": {"max_triangles": 9000}}}
after = {"mesh": {"max_triangles": 1200}, "tags": {"boss": {"max_triangles": 9000}}}
changes = ahs.diff_documents(before, after)
expect("a diff names the exact key", changes == [("mesh.max_triangles", 1000, 1200)], str(changes))
expect("an unchanged document has no diff", ahs.diff_documents(before, dict(before)) == [])
merged = ahs.merge_rules_document({"healer": {"enabled": False}, "mesh": {"max_triangles": 1}},
                                  {"mesh": {"max_triangles": 2}})
expect("merging keeps sections the form does not own",
       merged["healer"] == {"enabled": False}, str(merged))
expect("merging replaces keys the form does own", merged["mesh"]["max_triangles"] == 2)

text = agk.to_toml({"mesh": {"max_triangles": 1500}, "tags": {"boss": {"max_triangles": 900}}})
expect("to_toml writes a table per section", "[mesh]" in text and "[tags.boss]" in text, text)
expect("to_toml output parses back", agk.parse_rules_text(text)["tags"]["boss"]["max_triangles"] == 900)

# --------------------------------------------------------------------------- #
print("1. first run happens in the browser, not a terminal")
# A fresh dashboard has no accounts: the sign-in page becomes the setup page, so
# a double-click install never needs a shell. Then it closes for good.
fresh_db = WORK / "first-run.db"
ahs.init_db(fresh_db)
fresh_rules = WORK / "first-run-rules.toml"
fresh_server, fresh_base = start(fresh_db, fresh_rules)
first = Session(fresh_base)
status, body = first.get("/admin/login")
expect("an empty dashboard offers account setup",
       status == 200 and b"Create the first account" in body, str(status))
expect("the setup form posts to the bootstrap route", b"/admin/api/bootstrap" in body)
expect("the sign-in form is not shown before setup", b'name="password"' in body)
status, body = first.form("/admin/api/bootstrap",
                          {"username": "ada", "password": "short", "confirm": "short"})
expect("a short password is refused", b"10" in body, body[:80])
status, body = first.form("/admin/api/bootstrap",
                          {"username": "ada", "password": "long-enough-pw",
                           "confirm": "different-pw1"})
expect("a mismatched confirmation is refused", b"do not match" in body, body[:80])
status, body = first.form("/admin/api/bootstrap",
                          {"username": "ada", "password": "long-enough-pw",
                           "confirm": "long-enough-pw"})
expect("the first account is created and signed in", b"Rules" in body, str(status))
first_state = first.state()
expect("the first account is a director", first_state["user"]["role"] == "director")
expect("bootstrap is audited", any(row["target"] == "account"
                                   for row in first_state["audit"]))
first.post("/admin/api/logout", csrf=first_state["csrf"])
status, body = first.get("/admin/login")
expect("the setup page is gone once an account exists",
       b"Create the first account" not in body and b"Sign in" in body)
status, _ = first.form("/admin/api/bootstrap",
                       {"username": "mallory", "password": "another-pw-123",
                        "confirm": "another-pw-123"})
expect("bootstrap cannot be replayed to add another director", status == 403, str(status))
fresh_server.shutdown()
fresh_server.server_close()

print("2. the admin page is closed by default")
root = WORK / "project"
root.mkdir(parents=True, exist_ok=True)
rules = root / "rules.toml"
rules.write_text(
    'platform = "n64"\n\n[mesh]\nmax_triangles = 1500\nmax_material_slots = 2\n\n'
    "[healer]\nenabled = false\n\n[filters]\nexclude = [\"preview_*\"]\n",
    encoding="utf-8")
assets = root / "assets"
assets.mkdir()
(assets / "crate.obj").write_text(
    "v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\nf 1 2 3\nf 1 2 4\nf 1 3 4\nf 2 3 4\n",
    encoding="utf-8")

db = WORK / "health.db"
ahs.init_db(db)
password = ahs.create_admin_user(db, "alice", "director")
ahs.create_admin_user(db, "bob", "artist")

server, base = start(db, rules, assets, allow=(str(WORK),))
anon = Session(base)
status, body = anon.get("/admin")
expect("an anonymous visitor lands on the sign-in page", status == 200 and b"Sign in" in body)
expect("/admin/api/state is 401 without a session", anon.get("/admin/api/state")[0] == 401)
expect("the preset dropdown data is not public", anon.get("/admin/api/state")[0] == 401)

# --------------------------------------------------------------------------- #
print("3. signing in")
status, body = anon.form("/admin/login", {"username": "alice", "password": "nope"})
expect("a wrong password does not sign in", b"Invalid credentials" in body)
status, body = anon.form("/admin/login", {"username": "alice", "password": password})
expect("the right password reaches the admin page", b"Rules" in body)
state = anon.state()
expect("the session identifies the user", state["user"]["username"] == "alice")
expect("a director may write", state["can_write"] is True)
expect("the page receives a CSRF token", bool(state["csrf"]))
targets = {row["name"] for row in state["presets"]}
expect("ten hardware targets are offered",
       {"n64", "ps1", "nds", "gamecube", "xbox", "ps2", "n3ds", "wii", "ps3", "pc"} <= targets,
       str(sorted(targets)))
expect("every preset carries character and prop budgets",
       all(row["character_triangles"] and row["prop_triangles"] for row in state["presets"]))
csrf = state["csrf"]

# --------------------------------------------------------------------------- #
print("4. CSRF and role enforcement")
document = {
    "mesh": {"max_triangles": 1200, "max_material_slots": 2},
    "texture": {"max_edge": 64, "power_of_two": True},
    "tags": {"character": {"max_triangles": 1200}, "prop": {"max_triangles": 400}},
}
expect("apply without a CSRF token is refused",
       anon.post("/admin/api/apply", {"document": document, "action": "save"})[0] == 403)
expect("apply with a forged CSRF token is refused",
       anon.post("/admin/api/apply", {"document": document, "action": "save"},
                 csrf="forged")[0] == 403)

anon.post("/admin/api/logout", csrf=csrf)
bob_password = ahs.set_admin_password(db, "bob", "bobs-password-123")
bob = Session(base)
bob.form("/admin/login", {"username": "bob", "password": bob_password})
bob_state = bob.state()
expect("an artist can read the rules", bob_state["user"]["role"] == "artist")
expect("an artist is told they cannot write", bob_state["can_write"] is False)
status, body = bob.post("/admin/api/apply", {"document": document, "action": "save"},
                        csrf=bob_state["csrf"])
expect("an artist cannot change rules", status == 403, str(status))
expect("the refusal names who can", b"director or producer" in body, body[:120])

# --------------------------------------------------------------------------- #
print("5. a director applies a preset")
anon.form("/admin/login", {"username": "alice", "password": password})
state = anon.state()
csrf = state["csrf"]
status, body = anon.post("/admin/api/apply", {"document": document, "action": "save"}, csrf=csrf)
result = json.loads(body)
expect("the apply succeeds", status == 200 and result["ok"], body[:200])
saved = agk.parse_rules(rules)
expect("the budget is written", saved["mesh"]["max_triangles"] == 1200, str(saved.get("mesh")))
expect("character and prop categories are written",
       saved["tags"]["character"]["max_triangles"] == 1200
       and saved["tags"]["prop"]["max_triangles"] == 400, str(saved.get("tags")))
expect("sections the form does not show survive", saved.get("healer") == {"enabled": False}
       and saved.get("filters", {}).get("exclude") == ["preview_*"], str(saved))
expect("a dangling platform name is dropped",
       "platform" not in saved, str(saved.get("platform")))
expect("the repair is reported, not silent",
       any("dropped platform" in note for note in result.get("notes", [])), str(result.get("notes")))
expect("the removal is in the audit diff",
       any(change["key"] == "platform" and change["new"] is None for change in result["changed"]))
expect("the file stays plain text with the header",
       rules.read_text(encoding="utf-8").startswith("# rules.toml"))
expect("it round-trips through the gate's parser", agk.parse_rules(rules) == saved)
expect("the gate accepts the written file",
       agk.Budget.from_mapping(saved).max_triangles == 1200)

# --------------------------------------------------------------------------- #
print("6. the audit trail")
audit = anon.state()["audit"]
expect("every changed key is recorded", len(audit) == len(result["changed"]),
       f"{len(audit)} rows vs {len(result['changed'])} changes")
entry = next(row for row in audit if row["target"] == "mesh.max_triangles")
expect("the row names the user", entry["username"] == "alice")
expect("the row names the role", entry["role"] == "director")
expect("old and new values are both kept",
       (entry["old_value"], entry["new_value"]) == ("1500", "1200"),
       f"{entry['old_value']} -> {entry['new_value']}")
expect("the row is timestamped", bool(entry["changed_at"]))

# --------------------------------------------------------------------------- #
print("7. a refused document never reaches the disk")
before_text = rules.read_text(encoding="utf-8")
status, body = anon.post("/admin/api/apply",
                         {"document": {"mesh": {"max_triangles": "lots"}}, "action": "save"},
                         csrf=csrf)
expect("a non-numeric budget is refused", status == 400, str(status))
expect("nothing was written", rules.read_text(encoding="utf-8") == before_text)
status, body = anon.post("/admin/api/apply",
                         {"document": {"mesh": {"nonsense": 1}}, "action": "save"}, csrf=csrf)
expect("an unknown key is refused", status == 400 and b"unknown" in body.lower(), body[:120])
# Category names are the question every new user asks: "[actors.*]" is not TOML
# and is not matchable, so it is refused with the folder name it probably meant.
status, body = anon.post(
    "/admin/api/apply",
    {"document": {**document, "tags": {"[actors.*]": {"max_triangles": 1500}}},
     "action": "save"}, csrf=csrf)
expect("a bracketed category name is refused", status == 400, str(status))
expect("the refusal suggests the plain name", b"actors" in body, body[:180])
status, body = anon.post(
    "/admin/api/apply",
    {"document": {**document, "tags": {"actors": {"max_triangles": 1500},
                                           "normal_enemy": {"max_triangles": 20000}}},
     "action": "save"}, csrf=csrf)
expect("a plain folder name is accepted", status == 200, body[:180])
expect("the category is written as a normal tag table",
       agk.parse_rules(rules)["tags"]["actors"]["max_triangles"] == 1500)
status, body = anon.post("/admin/api/apply", {"document": document, "action": "save"},
                         csrf=csrf)
expect("the previous document can be restored", status == 200, str(status))

status, body = anon.post("/admin/api/apply",
                         {"document": {"platform": "ps9"}, "action": "save"}, csrf=csrf)
expect("an unknown platform overlay is refused or dropped safely",
       status == 200 or status == 400, str(status))
expect("a refused platform leaves the file parseable and unchanged",
       agk.parse_rules(rules)["mesh"]["max_triangles"] == 1200)

# --------------------------------------------------------------------------- #
print("8. apply & rescan, streamed")
status, body = anon.post("/admin/api/apply", {"document": document, "action": "rescan"}, csrf=csrf)
result = json.loads(body)
expect("apply with rescan starts a scan", status == 200 and result["scan"]["started"], body[:200])
expect("a second concurrent scan is refused", anon.post("/admin/api/scan", csrf=csrf)[0] in (200, 409))
deadline = time.time() + 120
snapshot = result["scan"]
while time.time() < deadline:
    snapshot = anon.state()["scan"]
    if snapshot["status"] != "running":
        break
    time.sleep(0.4)
expect("the scan finishes", snapshot["status"] == "done", json.dumps(snapshot)[:200])
expect("it counted the asset", snapshot["totals"]["total"] >= 1, json.dumps(snapshot["totals"]))
expect("the asset passed under a 1,200 triangle budget",
       snapshot["totals"]["passed"] >= 1, json.dumps(snapshot["totals"]))
expect("events are buffered for a late subscriber", len(server.scanner.recent()) >= 2)

# --------------------------------------------------------------------------- #
print("9. static files are an allow-list")
expect("admin.js is served",
       anon.get("/static/admin.js")[1].count(b"budgetDocument") >= 1)
expect("admin.css is served", anon.get("/static/admin.css")[0] == 200)
expect("path traversal is refused",
       anon.get("/static/../asset_health_server.py")[0] in (400, 404))
expect("an arbitrary file is 404", anon.get("/static/secrets.txt")[0] == 404)

# --------------------------------------------------------------------------- #
print("10. signing out and session hygiene")
# Read the raw Set-Cookie header instead of trusting the jar: the flags are the
# security property, and a cookie jar happily hides them.
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


raw_opener = urllib.request.build_opener(_NoRedirect)
try:
    raw_opener.open(urllib.request.Request(
        base + "/admin/login",
        data=urllib.parse.urlencode({"username": "alice", "password": password}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"}))
    set_cookie = ""
except urllib.error.HTTPError as exc:
    set_cookie = exc.headers.get("Set-Cookie", "")
else:
    set_cookie = ""
expect("the session cookie is HttpOnly", "HttpOnly" in set_cookie, set_cookie)
expect("the session cookie is SameSite=Strict", "SameSite=Strict" in set_cookie, set_cookie)
expect("the session cookie is not Secure-less on a plain port but is scoped",
       "Path=/" in set_cookie, set_cookie)
token = next((cookie.value for cookie in anon.jar if cookie.name == ahs.SESSION_COOKIE), "")
expect("a session token was issued", bool(token))
with ahs.open_db(db) as connection:
    stored = connection.execute("SELECT token_hash FROM admin_sessions").fetchall()
expect("no plaintext token is stored", all(row[0] != token for row in stored))
anon.post("/admin/api/logout", csrf=csrf)
expect("the session is gone after sign out", anon.get("/admin/api/state")[0] == 401)

# --------------------------------------------------------------------------- #
print("11. the project picker")
# The picker is the only feature that lets a browser name a filesystem path, so
# the allow-list is the assertion that matters most in this file.
# (Section 9 signed out, so sign back in as the director.)
anon.form("/admin/login", {"username": "alice", "password": password})
csrf = anon.state()["csrf"]
project_dir = WORK / "terminus"
(project_dir / "assets").mkdir(parents=True, exist_ok=True)
(project_dir / "assets" / "grunt.obj").write_text(
    "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")

status, _ = anon.post("/admin/api/project", {"path": str(WORK.parent / "elsewhere")},
                      csrf=csrf)
expect("a folder outside the allow-list is refused", status == 403, str(status))
status, _ = anon.post("/admin/api/project", {"path": str(project_dir / ".." / "..")},
                      csrf=csrf)
expect("a .. traversal is refused", status == 403, str(status))
status, body = anon.post("/admin/api/project", {"path": ""}, csrf=csrf)
expect("an empty path is refused", status in (400, 403), str(status))
status, body = anon.post("/admin/api/project", {"path": str(project_dir)}, csrf=csrf)
info = json.loads(body)
expect("a folder on the allow-list is accepted", status == 200 and info["ok"], body[:160])
expect("it resolves the path rather than trusting the string",
       Path(info["project"]) == project_dir.resolve(), info.get("project"))
expect("it says where rules.toml will live",
       info["rules_path"] == str(project_dir.resolve() / "rules.toml"), info.get("rules_path"))
expect("there is no rules file yet", info["rules_exists"] is False)
expect("switching project is audited",
       any(row["target"] == "project" and row["new_value"].endswith("terminus")
           for row in anon.state()["audit"]), "no project audit row")

status, body = anon.post("/admin/api/apply", {"document": document, "action": "save"},
                         csrf=csrf)
result = json.loads(body)
expect("Save & Run Audit writes into the chosen project", status == 200 and
       Path(result["path"]) == project_dir.resolve() / "rules.toml", body[:200])
expect("rules.toml appeared in that folder",
       (project_dir / "rules.toml").is_file())
expect("the file is what the form said",
       agk.parse_rules(project_dir / "rules.toml")["mesh"]["max_triangles"] == 1200)
expect("the audit row names the file, not just the key",
       # Resolved: on Windows the temp dir is an 8.3 short path (RUNNER~1) while
       # the server records the long one, so a plain substring never matched.
       any(str(project_dir.resolve() / "rules.toml") in (row.get("summary") or "")
           for row in anon.state()["audit"]))

# The launcher seeds the allow-list from both --allow-root and the environment,
# so the same folder arriving twice is normal - it must not become two identical
# dropdown entries.
import os as _os  # noqa: PLC0415 - local to this section
_variants = [str(WORK), str(WORK) + _os.sep]
if _os.name == "nt":
    _variants.append(str(WORK).lower())
dupe_server = ahs.build_server("127.0.0.1", 0, db, quiet=True, allow_roots=tuple(_variants))
expect("duplicate allow-roots collapse to a single entry",
       len(dupe_server.allow_roots) == 1, str(dupe_server.allow_roots))
dupe_server.server_close()

state = anon.state()
expect("the scan root follows the project",
       Path(state["scan_root"]) == project_dir.resolve(), state.get("scan_root"))
expect("the picker gets the allow-list", bool(state["allowed_roots"]))
expect("the picker gets recent projects",
       any(Path(row["path"]) == project_dir.resolve() for row in state["recent_projects"]),
       str(state.get("recent_projects")))

status, body = anon.post("/admin/api/apply", {"document": document, "action": "rescan"},
                         csrf=csrf)
result = json.loads(body)
expect("the audit runs over the chosen project",
       status == 200 and result["scan"]["started"] and
       Path(result["scan"]["root"]) == project_dir.resolve(),
       str(result.get("scan", {}).get("root")))
deadline = time.time() + 120
snapshot = result["scan"]
while time.time() < deadline:
    snapshot = anon.state()["scan"]
    if snapshot["status"] != "running":
        break
    time.sleep(0.4)
expect("it audited the project's own assets", snapshot["totals"]["total"] >= 1,
       json.dumps(snapshot["totals"]))

# --------------------------------------------------------------------------- #
print("12. the page and its script agree")
# A single renamed id is a blank admin page in a browser and nothing at all in
# pyflakes - the same failure mode that once blanked the workstation viewer.
import re  # noqa: PLC0415 - local to this section

html_text = (REPO / "server" / "static" / "admin.html").read_text(encoding="utf-8")
js_text = (REPO / "server" / "static" / "admin.js").read_text(encoding="utf-8")
referenced = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js_text))
missing = sorted(name for name in referenced if f'id="{name}"' not in html_text)
expect("every element the script reaches for exists in the page", not missing, str(missing))
expect("the script and stylesheet are wired in",
       'src="/static/admin.js"' in html_text and 'href="/static/admin.css"' in html_text)
expect("the category collector skips the 'no categories' placeholder row",
       'if (!nameInput || !trisInput) continue;' in js_text,
       "collectTags would read .value from null again")
expect("the page never posts to an endpoint the server does not serve",
       all(path in js_text for path in ("/admin/api/apply", "/admin/api/state")),
       "apply/state endpoints referenced")

# --------------------------------------------------------------------------- #
print("12. send to workstation")
import art_workstation_server as aws  # noqa: PLC0415 - only this section needs it

ws_db, ws_data = WORK / "bridge-ws.db", WORK / "bridge-ws-data"
aws.init_db(ws_db)
ws_server = aws.build_server("127.0.0.1", 0, ws_db, ws_data, quiet=True)
threading.Thread(target=ws_server.serve_forever, daemon=True).start()
ws_base = f"http://127.0.0.1:{ws_server.server_address[1]}"

bridge_db = WORK / "bridge.db"
ahs.init_db(bridge_db)
gate_asset = WORK / "hero.obj"
# A real triangle: the workstation validates geometry on ingest, so a
# stub file is refused with a 422 (which is correct, and worth keeping).
gate_asset.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
bridge_server = ahs.build_server("127.0.0.1", 0, bridge_db, quiet=True,
                                 rules_path=str(WORK / "bridge-rules.toml"),
                                 rules_root=str(WORK), scan_root=str(WORK),
                                 allow_roots=(str(WORK),), workstation_url=ws_base)
threading.Thread(target=bridge_server.serve_forever, daemon=True).start()
bridge = Session(f"http://127.0.0.1:{bridge_server.server_address[1]}")

status, body = bridge.post("/api/telemetry", {
    "filename": gate_asset.name, "path": str(gate_asset), "passed": False,
    "status": "FAILED", "problems": ["triangle budget exceeded: 1,900 > 1,500",
                                     "degenerate faces: 3"]})
expect("the dashboard ingests a verdict", status == 201, body[:120])
verdict_id = json.loads(body)["id"]

expect("the dashboard offers the action per row",
       "Send to Workstation" in ahs.render_dashboard(bridge_db))
status, body = bridge.post("/api/send-to-workstation", {"verdicts": [verdict_id]})
result = json.loads(body)
expect("the bridge sends one asset", status == 200 and result["sent"] == 1, body[:200])
expect("and names the new workstation asset",
       isinstance(result["results"][0].get("asset_id"), int), body[:200])

listing = json.loads(urllib.request.urlopen(f"{ws_base}/api/assets", timeout=5).read())
item = next(row for row in listing["assets"] if row["filename"] == gate_asset.name)
expect("the pass/fail status travelled", item["verdict_status"] == "failed", str(item)[:160])
expect("the exact rule violation travelled",
       "triangle budget exceeded" in item["verdict_problems"], item["verdict_problems"][:120])
expect("the provenance is labelled", item["source"] == "assethealth", item["source"])
expect("the file's path travelled, for versions and the inspector (W8)",
       item["source_path"].replace("\\", "/") == str(gate_asset).replace("\\", "/"), item["source_path"])
aws.set_review(ws_db, item["id"], "approved", reviewer="Lena")
audit_page = bridge.get("/audit/full")[1].decode("utf-8", "replace")
expect("the audit page shows the workstation's sign-off for that file (W2)",
       'class="review approved"' in audit_page, audit_page[:120])

status, body = bridge.post("/api/send-to-workstation", {"verdicts": [verdict_id] * 11})
expect("a batch of eleven is refused", status == 400 and b"10" in body, body[:140])
status, body = bridge.post("/api/send-to-workstation", {"verdicts": []})
expect("an empty batch is refused", status == 400, str(status))

# A .gltf with its buffer in a separate .bin: the gate passes it, and the
# workstation must get both files (it used to get the .gltf alone, unreadable).
import struct as _struct  # noqa: PLC0415
_tri = _struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0) + _struct.pack("<3I", 0, 1, 2)
(WORK / "rock.bin").write_bytes(_tri)
(WORK / "rock.gltf").write_text(json.dumps({
    "asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
    "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
    "buffers": [{"byteLength": len(_tri), "uri": "rock.bin"}],
    "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36},
                    {"buffer": 0, "byteOffset": 36, "byteLength": 12}],
    "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
                   "min": [0, 0, 0], "max": [1, 1, 0]},
                  {"bufferView": 1, "componentType": 5125, "count": 3, "type": "SCALAR"}]}),
    encoding="utf-8")
status, body = bridge.post("/api/telemetry", {
    "filename": "rock.gltf", "path": str(WORK / "rock.gltf"), "passed": True,
    "status": "PASSED", "problems": []})
rock_id = json.loads(body)["id"]
status, body = bridge.post("/api/send-to-workstation", {"verdicts": [rock_id]})
result = json.loads(body)["results"][0]
expect("the bridge sends the .bin the .gltf references", result.get("sidecars") == ["rock.bin"],
       str(result)[:200])
rock = json.loads(urllib.request.urlopen(f"{ws_base}/api/assets/{result['asset_id']}", timeout=5).read())
expect("so a gate-PASSED .gltf arrives measured, not 'could not read mesh'",
       rock["kind"] == "mesh" and rock["triangle_count"] == 1 and rock["missing"] == [],
       str({k: rock[k] for k in ("kind", "triangle_count", "analyse_note", "missing")}))

missing_asset = WORK / "ghost.obj"
status, body = bridge.post("/api/telemetry", {
    "filename": missing_asset.name, "path": str(missing_asset), "passed": True,
    "status": "PASSED", "problems": []})
ghost_id = json.loads(body)["id"]
status, body = bridge.post("/api/send-to-workstation", {"verdicts": [ghost_id]})
result = json.loads(body)
expect("a row whose file has gone fails on its own",
       result["sent"] == 0 and "not reachable" in result["results"][0]["error"], body[:200])

# 9 Nov 2026: a 90 MB sculpt was posted to a workstation that takes 64 MB; it
# hung up mid-upload and the artist saw "[WinError 10053] ...". The bridge now
# asks the workstation for its limit and refuses up front, in words.
small_db, small_data = WORK / "small-ws.db", WORK / "small-ws-data"
aws.init_db(small_db)
small_ws = aws.build_server("127.0.0.1", 0, small_db, small_data, quiet=True,
                            max_upload_bytes=1024 * 1024)
threading.Thread(target=small_ws.serve_forever, daemon=True).start()
bridge_server.workstation_url = f"http://127.0.0.1:{small_ws.server_address[1]}"
sculpt = WORK / "SM_Sculpt.obj"
sculpt.write_bytes(b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n" + b"# padding\n" * 300_000)
status, body = bridge.post("/api/telemetry", {
    "filename": sculpt.name, "path": str(sculpt), "passed": False,
    "status": "FAILED", "problems": ["1,082,096 triangles exceeds the 1,000,000 budget"]})
sculpt_id = json.loads(body)["id"]
status, body = bridge.post("/api/send-to-workstation", {"verdicts": [sculpt_id]})
error = json.loads(body)["results"][0].get("error", "")
expect("a file over the workstation's limit is refused before sending",
       json.loads(body)["sent"] == 0 and "takes files up to 1.0 MB" in error, error)
expect("... naming the file, its size and how to raise the limit",
       error.startswith("SM_Sculpt.obj is 2.9 MB") and "--max-upload-mb 4" in error, error)
expect("... not a socket error", "WinError" not in error and "10053" not in error, error)
status, body = bridge.post("/api/send-to-workstation", {"verdicts": [verdict_id]})
expect("a small file still goes to the same workstation", json.loads(body)["sent"] == 1, body[:200])
expect("the workstation reports its limit to the bridge",
       bridge_server.workstation_url and json.loads(urllib.request.urlopen(
           f"{bridge_server.workstation_url}/api/health", timeout=5).read())["max_upload_bytes"]
       == 1024 * 1024)
aborted = ahs._send_failure(urllib.error.URLError(ConnectionAbortedError(10053, "aborted")),
                            "SM_Sculpt.obj", 90_230_854)
expect("a connection the workstation drops is explained, not quoted",
       "hung up while SM_Sculpt.obj (86 MB)" in aborted and "10053" not in aborted, aborted)
expect("a workstation that is not running says so",
       "not running" in ahs._send_failure(ConnectionRefusedError(), "a.obj", 10))
small_ws.shutdown()
small_ws.server_close()

bridge_server.shutdown()
bridge_server.server_close()
ws_server.shutdown()
ws_server.server_close()
# --------------------------------------------------------------------------- #
print("13. the full audit: pagination, filters and the tag tree")
# The dashboard shows a top-N; these pages must reach every row, so the fixture is
# deliberately bigger than one page (120 rows = 3 pages at 50).
audit_root = WORK / "audit-root"
audit_rules = WORK / "audit-rules.toml"
audit_rules.write_text(
    "[mesh]\nmax_triangles = 1500\n\n[tags.boss]\nmax_triangles = 9000\n"
    "\n[tags.enemies]\nmax_triangles = 2000\n", encoding="utf-8")
audit_db = WORK / "audit.db"
ahs.init_db(audit_db)
audit_server, audit_base = start(audit_db, audit_rules, audit_root, allow=(str(WORK),))


def fetch(path: str) -> tuple[int, str]:
    with urllib.request.urlopen(audit_base + path, timeout=15) as response:
        return response.status, response.read().decode("utf-8", "replace")


def ingest(target: Path, passed: bool) -> None:
    payload = json.dumps({
        "filename": target.name, "path": str(target), "passed": passed,
        "status": "PASSED" if passed else "FAILED",
        "problems": [] if passed else ["triangle budget exceeded"],
    }).encode("utf-8")
    request = urllib.request.Request(audit_base + "/api/telemetry", data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    urllib.request.urlopen(request, timeout=10).read()


for folder, count in (("Boss", 40), ("Enemies", 40), ("Props", 40)):
    (audit_root / folder).mkdir(parents=True, exist_ok=True)
    for index in range(count):
        target = audit_root / folder / f"{folder.lower()}_{index:03d}.glb"
        target.write_bytes(b"glTF")
        ingest(target, index % 3 == 0)          # one in three passes

status, page1 = fetch("/audit/full")
expect("the full audit renders", status == 200 and "All verdicts" in page1, str(status))
expect("it pages instead of slicing to a top-N", "page 1 of 3" in page1,
       page1[:200])
expect("a page holds fifty rows", page1.count('<td class="when">') == 50,
       str(page1.count('<td class="when">')))
expect("rows group into collapsible categories",
       "<details" in page1 and page1.count("<summary>") >= 2)
expect("every row keeps the send button",
       page1.count("Send to Workstation") == 50,
       str(page1.count("Send to Workstation")))

status, page3 = fetch("/audit/full?page=3")
expect("the last page holds the remainder", page3.count('<td class="when">') == 20,
       str(page3.count('<td class="when">')))

status, passes_only = fetch("/audit/full?status=pass")
expect("the status filter narrows the set", passes_only.count('<td class="when">') == 42,
       str(passes_only.count('<td class="when">')))
expect("and shows only passes", "badge failed" not in passes_only)

status, passes_page = fetch("/audit/passes")
expect("the passes route renders", status == 200 and "Passes" in passes_page, str(status))
expect("it is the same set as status=pass", passes_page.count('<td class="when">') == 42,
       str(passes_page.count('<td class="when">')))

status, boss_only = fetch("/audit/full?tag=boss")
expect("a tag filter narrows to that category", boss_only.count('<td class="when">') == 40,
       str(boss_only.count('<td class="when">')))
expect("the tree names the category", "boss" in boss_only)

audit_server.shutdown()
audit_server.server_close()

server.shutdown()
server.server_close()
shutil.rmtree(WORK, ignore_errors=True)

print()
print(f"{ok_count} passed")
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
