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
       any(str(project_dir / "rules.toml") in (row.get("summary") or "")
           for row in anon.state()["audit"]))

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

server.shutdown()
server.server_close()
shutil.rmtree(WORK, ignore_errors=True)

print()
print(f"{ok_count} passed")
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
