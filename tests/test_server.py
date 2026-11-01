"""Sanity checks for asset_health_server: ingest, auth, dashboard, storage.

Standalone like the rest of the suite: no pytest, no fixtures framework. Spins
the real server on an ephemeral port and talks to it over real sockets, because
the interesting failures here are HTTP-level (status codes, keep-alive, bodies).
"""
import json
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT))

import asset_health_server as ahs  # noqa: E402

WORK = Path("scratch") / "server_tests"
WORK.mkdir(parents=True, exist_ok=True)
failures = []


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not condition:
        failures.append(label)


def fresh_db(name):
    path = WORK / name
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(path) + suffix)
        if target.exists():
            target.unlink()
    ahs.init_db(path)
    return path


class FakeResponse:
    """What urllib.request.urlopen hands back, without a socket in sight."""

    status = 200

    def __init__(self, body: bytes = b"{}", headers=None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def request(port, path, method="GET", body=None, headers=None, timeout=10):
    url = f"http://127.0.0.1:{port}{path}"
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib follows 302s, which hides the redirect we want to assert on."""

    def redirect_request(self, *_args, **_kwargs):
        return None


def request_raw(port, path):
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
            return response.status, dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers)


VERDICT = {
    "schema": 1,
    "timestamp": "2026-10-31T12:00:00+00:00",
    "hostname": "WS-ART-014",
    "username": "jdoe",
    "platform": "Windows-11-10.0.26100-SP0",
    "name": "boss_statue.gltf",
    "path": "creatures/boss_statue.gltf",
    "size_bytes": 1160991,
    "passed": False,
    "metric": "72,500 tris / 4 mat",
    "stats": {"triangles": 72500, "material_slots": 4},
    "problems": [
        "72,500 triangles exceeds the 50,000 budget (+22,500)",
        "4 material slots exceeds the 3 slot budget (draw calls)",
    ],
}

print("1. schema and storage")
db = fresh_db("store.db")
with ahs.open_db(db) as connection:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
expect("verdicts table exists", "verdicts" in tables)
expect("problems table exists", "problems" in tables)
with ahs.open_db(db) as connection:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(verdicts)").fetchall()}
expect("verdicts has the required columns",
       {"id", "timestamp", "hostname", "username", "os", "filename", "filepath",
        "filesize", "passed"} <= columns, str(sorted(columns)))
with ahs.open_db(db) as connection:
    fk = connection.execute("PRAGMA foreign_key_list(problems)").fetchall()
expect("problems points at verdicts", fk and fk[0][2] == "verdicts", str(fk))

verdict_id = ahs.store_verdict(db, VERDICT)
expect("one verdict stores one row", verdict_id == 1, str(verdict_id))
with ahs.open_db(db) as connection:
    problem_rows = connection.execute(
        "SELECT message FROM problems WHERE verdict_id = ?", (verdict_id,)).fetchall()
expect("the whole problem tuple is stored, one row each", len(problem_rows) == 2)
with ahs.open_db(db) as connection:
    stored = connection.execute("SELECT * FROM verdicts WHERE id = 1").fetchone()
expect("platform maps to os, name maps to filename",
       stored["os"].startswith("Windows-11") and stored["filename"] == "boss_statue.gltf")
expect("filepath and filesize survive",
       stored["filepath"] == "creatures/boss_statue.gltf" and stored["filesize"] == 1160991)
expect("passed is stored as 0/1", stored["passed"] == 0)

print("2. payload validation")
for label, payload, expected_error in (
    ("missing filename", {"passed": True}, "name"),
    ("passed must be a bool", {"name": "x.obj", "passed": "no"}, "passed"),
    ("problems must be strings", {"name": "x.obj", "passed": False, "problems": [1, 2]}, "problems"),
):
    try:
        ahs.store_verdict(db, payload)
        expect(label, False, "no error raised")
    except ValueError as exc:
        expect(label, expected_error in str(exc), str(exc)[:48])
expect("unknown passed-through keys are ignored",
       ahs.store_verdict(db, {"name": "y.obj", "passed": True, "junk": 1}) == 2)
expect("short field names are accepted",
       ahs._normalise({"filename": "z.obj", "filepath": "a/z.obj", "filesize": 5,
                       "passed": True})["filesize"] == 5)

print("3. rule classification")
cases = (
    ("72,500 triangles exceeds the 50,000 budget (+22,500)", "Triangle budget"),
    ("4 material slots exceeds the 3 slot budget (draw calls)", "Material slots"),
    ("300x300 is not a power of two", "Non-power-of-two"),
    ("name does not end with a standard suffix (_Albedo)", "Texture naming"),
    ("4096x1024 exceeds the 2048x2048 maximum resolution", "Texture resolution"),
    ("6 bone weights on one vertex exceeds the 4 influence limit", "Bone influences"),
    ("5 UV channels exceeds the 4 channel limit", "UV channels"),
    ("1 degenerate triangles (zero-area)", "Degenerate triangles"),
    ("file has a .png extension but is actually JPEG", "Mislabeled format"),
    ("mesh contains no triangles (empty or corrupt export)", "Empty export"),
    ("something nobody predicted", "Other"),
)
for text, bucket in cases:
    expect(f"classified: {bucket}", ahs.classify(text) == bucket, ahs.classify(text))

print("4. dashboard queries + seeding")
seed_db = fresh_db("seed.db")
SEED_ROWS = len(ahs._SEED) + 1   # + the demo 'healed' row
expect("first launch seeds demo rows", ahs.seed_if_first_launch(seed_db) is True)
expect("first launch seeds in one call", ahs.totals(seed_db)["total"] == SEED_ROWS)
expect("a second launch does not re-seed", ahs.seed_if_first_launch(seed_db) is False)
expect("still the same row count", ahs.totals(seed_db)["total"] == SEED_ROWS)
expect("the demo set includes a healed asset", ahs.totals(seed_db)["healed"] == 1,
       str(ahs.totals(seed_db)))

stats = ahs.totals(seed_db)
expect("totals add up", stats["passed"] + stats["failed"] == stats["total"], str(stats))
expect("pass rate is a percentage", 0 <= stats["pass_rate"] <= 100, str(stats["pass_rate"]))
shame = ahs.wall_of_shame(seed_db)
expect("wall of shame lists failures only", all(entry["reasons"] for entry in shame))
expect("wall of shame is newest first",
       [e["timestamp"] for e in shame] == sorted((e["timestamp"] for e in shame), reverse=True))
expect("every failure keeps all its reasons", max(len(e["reasons"]) for e in shame) >= 2)
expect("wall of shame honours the limit", len(ahs.wall_of_shame(seed_db, limit=3)) == 3)
tops = ahs.bottlenecks(seed_db)
expect("bottlenecks are ranked", tops == sorted(tops, key=lambda i: (-i[1], i[0])), str(tops[:3]))
expect("triangle budget tops the chart", tops[0][0] == "Triangle budget", str(tops[0]))
expect("pass rate rounds sensibly",
       ahs.totals(seed_db)["pass_rate"] == round(stats["passed"] * 100 / stats["total"], 1))

print("5. HTTP: no auth configured")
server = ahs.build_server("127.0.0.1", 0, seed_db, quiet=True)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.15)

status, headers = request_raw(port, "/")
expect("/ redirects to the dashboard", status == 302 and headers.get("Location") == "/dashboard",
       f"{status} {headers.get('Location')}")
status, body, _ = request(port, "/dashboard")
page = body.decode("utf-8")
expect("dashboard renders", status == 200 and "AssetHealth" in page, str(status))
expect("dashboard shows global status", all(word in page for word in
                                            ("Assets scanned", "Pass rate", "Failed")))
expect("dashboard shows the wall of shame", "Wall of shame" in page and "boss_statue.gltf" in page)
expect("dashboard shows top bottlenecks", "Top bottlenecks" in page and "Triangle budget" in page)
expect("dashboard prints the pass rate", f"{stats['pass_rate']}%" in page)

status, body, _ = request(port, "/api/summary")
summary = json.loads(body)
expect("/api/summary is JSON", status == 200 and summary["totals"]["total"] == stats["total"])
expect("/api/summary ranks rules", summary["bottlenecks"][0]["rule"] == "Triangle budget")

status, body, _ = request(port, "/api/telemetry", method="POST", body=VERDICT)
expect("POST /api/telemetry stores a verdict", status == 201, str(status))
stored_id = json.loads(body)["id"]
expect("the response names the row", isinstance(stored_id, int) and stored_id > 0)
expect("the new verdict is counted", ahs.totals(seed_db)["total"] == SEED_ROWS + 1)

status, _, _ = request(port, "/api/telemetry", method="POST", body=b"{not json")
expect("invalid JSON is a 400", status == 400, str(status))
status, _, _ = request(port, "/api/telemetry", method="POST", body=json.dumps([1, 2, 3]))
expect("a JSON array is a 400", status == 400, str(status))
status, _, _ = request(port, "/api/telemetry", method="POST", body={"passed": True})
expect("a missing filename is a 400", status == 400, str(status))
status, _, _ = request(port, "/api/telemetry", method="POST", body=b"x" * (ahs.MAX_BODY_BYTES + 10))
expect("an oversized body is a 413", status == 413, str(status))
status, _, _ = request(port, "/nowhere")
expect("unknown routes are 404", status == 404, str(status))
status, _, _ = request(port, "/api/telemetry", method="PUT", body=VERDICT)
expect("wrong methods are 405", status == 405, str(status))

status, _, _ = request(port, "/dashboard")
expect("the server keeps serving after a bad request", status == 200)
server.shutdown()
server.server_close()

print("6. HTTP: bearer token required")
auth_db = fresh_db("auth.db")
ahs.store_verdict(auth_db, VERDICT)
secured = ahs.build_server("127.0.0.1", 0, auth_db, token="hunter2", quiet=True)
port2 = secured.server_address[1]
threading.Thread(target=secured.serve_forever, daemon=True).start()
time.sleep(0.15)

status, _, _ = request(port2, "/api/telemetry", method="POST", body=VERDICT)
expect("no header is a 401", status == 401, str(status))
status, _, _ = request(port2, "/api/telemetry", method="POST", body=VERDICT,
                       headers={"Authorization": "Bearer wrong"})
expect("a wrong token is a 401", status == 401, str(status))
status, _, _ = request(port2, "/api/telemetry", method="POST", body=VERDICT,
                       headers={"Authorization": "Basic hunter2"})
expect("the wrong scheme is a 401", status == 401, str(status))
status, _, _ = request(port2, "/api/telemetry", method="POST", body=VERDICT,
                       headers={"Authorization": "Bearer hunter2"})
expect("the right token is accepted", status == 201, str(status))
with ahs.open_db(auth_db) as connection:
    stored_row = dict(connection.execute("SELECT * FROM verdicts").fetchone())
expect("the token never reaches the database",
       not any("hunter2" in str(value) for value in stored_row.values()))
status, _, _ = request(port2, "/dashboard")
expect("the dashboard is protected too", status == 401, str(status))
status, body, _ = request(port2, "/dashboard", headers={"Authorization": "Bearer hunter2"})
expect("the dashboard opens with the token", status == 200 and b"AssetHealth" in body)
secured.shutdown()
secured.server_close()

print("7. unsafe input is escaped, and deletes cascade")
escape_db = fresh_db("escape.db")
ahs.store_verdict(escape_db, {
    "name": "<script>alert(1)</script>.gltf", "username": "<b>mallory</b>",
    "passed": False, "problems": ["<img src=x onerror=alert(1)>"],
})
page = ahs.render_dashboard(escape_db)
expect("script tags are escaped", "<script>alert(1)</script>" not in page)
expect("the escaped name is still readable", "&lt;script&gt;alert(1)&lt;/script&gt;.gltf" in page)
expect("problem text is escaped too", "<img src=x onerror=alert(1)>" not in page)

with ahs.open_db(escape_db) as connection:
    before = connection.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
    connection.execute("DELETE FROM verdicts")
    connection.commit()
    after = connection.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
expect("deleting a verdict removes its problems (foreign key)", before == 1 and after == 0,
       f"{before} -> {after}")

print("8. healing status + modifications")
heal_db = fresh_db("heal.db")
HEALED = {
    "name": "stone_wall_Albedo.png", "path": "env/stone_wall_Albedo.png",
    "hostname": "WS-ART-014", "username": "jdoe", "platform": "Windows-11",
    "size_bytes": 734003, "passed": True, "status": "HEALED", "metric": "1024x1024",
    "problems": [], "modifications": ["padded 1000x1000 -> 1024x1024"],
}
healed_id = ahs.store_verdict(heal_db, HEALED)
with ahs.open_db(heal_db) as connection:
    healed_row = connection.execute("SELECT * FROM verdicts WHERE id = ?", (healed_id,)).fetchone()
    mods = [r["action"] for r in connection.execute(
        "SELECT action FROM modifications WHERE verdict_id = ?", (healed_id,)).fetchall()]
expect("a healed verdict stores status HEALED", healed_row["status"] == "HEALED",
       str(healed_row["status"]))
expect("a healed verdict still counts as passed", healed_row["passed"] == 1)
expect("the modifications are stored", mods == ["padded 1000x1000 -> 1024x1024"], str(mods))
expect("healed assets are counted separately", ahs.totals(heal_db)["healed"] == 1)

expect("status is derived when the client omits it",
       ahs._normalise({"name": "a.png", "passed": True,
                       "modifications": ["padded"]})["status"] == "HEALED")
expect("a plain pass derives PASSED", ahs._normalise({"name": "a.png", "passed": True})["status"]
       == "PASSED")
expect("a failure derives FAILED", ahs._normalise({"name": "a.png", "passed": False})["status"]
       == "FAILED")
expect("an unknown status is re-derived rather than trusted",
       ahs._normalise({"name": "a.png", "passed": False, "status": "SORT_OF"})["status"] == "FAILED")
try:
    ahs._normalise({"name": "a.png", "passed": True, "modifications": [1]})
    expect("modifications must be strings", False, "no error raised")
except ValueError as exc:
    expect("modifications must be strings", "modifications" in str(exc), str(exc)[:48])

# A database created before healing shipped must gain the column, not the boot error.
legacy = WORK / "legacy.db"
for suffix in ("", "-wal", "-shm"):
    if Path(str(legacy) + suffix).exists():
        Path(str(legacy) + suffix).unlink()
with ahs.open_db(legacy) as connection:
    connection.execute(
        "CREATE TABLE verdicts (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,"
        " received_at TEXT NOT NULL, hostname TEXT, username TEXT, os TEXT, filename TEXT,"
        " filepath TEXT, filesize INTEGER, passed INTEGER NOT NULL, metric TEXT)")
    connection.execute("INSERT INTO verdicts (timestamp, received_at, passed) "
                       "VALUES ('2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 1)")
ahs.init_db(legacy)
with ahs.open_db(legacy) as connection:
    migrated = {row[1] for row in connection.execute("PRAGMA table_info(verdicts)").fetchall()}
    kept = connection.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
expect("an old database is migrated, not dropped", "status" in migrated and kept == 1,
       f"columns={sorted(migrated)} rows={kept}")

print("9. multi-tenant isolation")
ten_db = fresh_db("tenant.db")
pid_a, tok_a = ahs.create_project(ten_db, "Game A")
pid_b, tok_b = ahs.create_project(ten_db, "Game B")
pid_c, tok_c = ahs.create_project(ten_db, "Legacy", token="plain-token")

with ahs.open_db(ten_db) as connection:
    stored_tokens = [row["api_token"] for row in connection.execute(
        "SELECT api_token FROM projects").fetchall()]
expect("project tokens are hashed at rest", all(t.startswith("sha256:") for t in stored_tokens),
       str(stored_tokens[:1]))
expect("the plain token is not readable from the database",
       not any("plain-token" in t for t in stored_tokens))
expect("a token resolves to its own project",
       ahs.project_for_token(ten_db, tok_a)["id"] == pid_a)
expect("another project's token resolves separately",
       ahs.project_for_token(ten_db, tok_b)["id"] == pid_b)
expect("an unknown token resolves to nothing", ahs.project_for_token(ten_db, "nope") is None)
expect("a hand-written plain-text token still works",
       ahs.project_for_token(ten_db, "plain-token")["id"] == pid_c)
try:
    ahs.create_project(ten_db, "Game A")
    expect("duplicate project names are rejected", False, "no error raised")
except sqlite3.IntegrityError:
    expect("duplicate project names are rejected", True)
expect("list_projects hides the tokens",
       all(set(row) == {"id", "name", "created_at"} for row in ahs.list_projects(ten_db)))

ten_server = ahs.build_server("127.0.0.1", 0, ten_db, quiet=True)
tport = ten_server.server_address[1]
threading.Thread(target=ten_server.serve_forever, daemon=True).start()
time.sleep(0.15)

A_VERDICT = dict(VERDICT, name="game_a_hero.gltf", path="a/hero.gltf", username="alice")
B_VERDICT = dict(VERDICT, name="game_b_hero.gltf", path="b/hero.gltf", username="bob")
AUTH_A = {"Authorization": f"Bearer {tok_a}"}
AUTH_B = {"Authorization": f"Bearer {tok_b}"}

status, _, _ = request(tport, "/api/telemetry", method="POST", body=A_VERDICT)
expect("telemetry without a project token is refused in tenant mode", status == 401, str(status))
status, body, _ = request(tport, "/api/telemetry", method="POST", body=A_VERDICT, headers=AUTH_A)
expect("telemetry with a project token is accepted", status == 201, str(status))
expect("the row is tagged with that project", json.loads(body)["project_id"] == pid_a)
request(tport, "/api/telemetry", method="POST", body=B_VERDICT, headers=AUTH_B)

with ahs.open_db(ten_db) as connection:
    tagged = {(row["filename"], row["project_id"]) for row in connection.execute(
        "SELECT filename, project_id FROM verdicts").fetchall()}
expect("each verdict lands in its own project",
       ("game_a_hero.gltf", pid_a) in tagged and ("game_b_hero.gltf", pid_b) in tagged,
       str(sorted(tagged)))
expect("project totals do not leak across tenants",
       ahs.totals(ten_db, pid_a)["total"] == 1 and ahs.totals(ten_db, pid_b)["total"] == 1,
       f"{ahs.totals(ten_db, pid_a)['total']} / {ahs.totals(ten_db, pid_b)['total']}")

# A token in the body must never override the token's own scope.
spoof = dict(A_VERDICT, name="spoofed.gltf", project_id=pid_b)
request(tport, "/api/telemetry", method="POST", body=spoof, headers=AUTH_A)
with ahs.open_db(ten_db) as connection:
    spoof_row = connection.execute(
        "SELECT project_id FROM verdicts WHERE filename = 'spoofed.gltf'").fetchone()
expect("a body project_id cannot redirect a token's write", spoof_row["project_id"] == pid_a,
       str(spoof_row["project_id"]))

status, _, _ = request(tport, f"/dashboard?project_id={pid_a}")
expect("the dashboard refuses an anonymous request in tenant mode", status == 401, str(status))
status, _, _ = request(tport, f"/dashboard?project_id={pid_a}&token={tok_b}")
expect("one project's token cannot open another's dashboard", status == 403, str(status))
status, body, _ = request(tport, f"/dashboard?project_id={pid_a}&token={tok_a}")
page_a = body.decode("utf-8")
expect("a project token opens its own dashboard", status == 200, str(status))
expect("the dashboard shows its own assets", "game_a_hero.gltf" in page_a)
expect("the dashboard hides other tenants' assets", "game_b_hero.gltf" not in page_a)
expect("the dashboard is labelled with the project", "Game A" in page_a)

status, body, _ = request(tport, "/dashboard", headers=AUTH_A)
page_pinned = body.decode("utf-8")
expect("a project token without project_id stays in its own scope",
       status == 200 and "game_a_hero.gltf" in page_pinned
       and "game_b_hero.gltf" not in page_pinned)
status, _, _ = request(tport, "/dashboard?project_id=999", headers=AUTH_A)
expect("an unknown project is a 404 for an admin, 403 for a project token",
       status in (403, 404), str(status))

status, body, _ = request(tport, f"/api/summary?project_id={pid_a}&token={tok_a}")
summary_a = json.loads(body)
expect("/api/summary is scoped too", summary_a["totals"]["total"] == 2, str(summary_a["totals"]))
ten_server.shutdown()
ten_server.server_close()

# Admin token: sees every project, may pick one.
adm_server = ahs.build_server("127.0.0.1", 0, ten_db, token="studio-admin", quiet=True)
aport = adm_server.server_address[1]
threading.Thread(target=adm_server.serve_forever, daemon=True).start()
time.sleep(0.15)
ADMIN = {"Authorization": "Bearer studio-admin"}
status, body, _ = request(aport, "/dashboard", headers=ADMIN)
everything = body.decode("utf-8")
expect("the admin token sees every project",
       status == 200 and "game_a_hero.gltf" in everything and "game_b_hero.gltf" in everything)
status, body, _ = request(aport, f"/dashboard?project_id={pid_b}", headers=ADMIN)
expect("the admin token may scope to one project",
       status == 200 and "game_b_hero.gltf" in body.decode("utf-8")
       and "game_a_hero.gltf" not in body.decode("utf-8"))
status, _, _ = request(aport, "/dashboard", headers={"Authorization": "Bearer wrong"})
expect("a bad token is refused even when an admin token exists", status == 401, str(status))
adm_server.shutdown()
adm_server.server_close()

print("10. webhook alerting")
BUDGET = dict(ahs.DEFAULT_BUDGET_ROW)
heavy = {"name": "huge.gltf", "username": "alice", "hostname": "WS-1", "passed": False,
         "stats": {"triangles": 80_000},
         "problems": ["80,000 triangles exceeds the 50,000 budget (+30,000)"]}
mild = dict(heavy, stats={"triangles": 60_000},
            problems=["60,000 triangles exceeds the 50,000 budget (+10,000)"])
overs = ahs.overshoots(heavy, BUDGET)
expect("a 1.6x overshoot is critical", len(overs) == 1 and overs[0][0] == "triangles", str(overs))
expect("the ratio is reported", round(overs[0][3], 2) == 1.6, str(overs[0][3]))
expect("a 1.2x overshoot is not critical", ahs.overshoots(mild, BUDGET) == [])
expect("problem text alone can trigger an alert",
       len(ahs.overshoots({"problems": heavy["problems"]}, BUDGET)) == 1)
expect("an under-budget asset never alerts",
       ahs.overshoots({"stats": {"triangles": 40_000}, "problems": []}, BUDGET) == [])
expect("texture overshoot is measured too",
       len(ahs.overshoots({"stats": {"width": 4096, "height": 1024}, "problems": []}, BUDGET)) == 1)

text = ahs.alert_text(heavy, BUDGET, overs)
expect("the alert starts with the documented prefix", text.startswith("🔴 CRITICAL PIPELINE ALERT:"), text[:40])
expect("the alert names user, host and file",
       all(piece in text for piece in ("alice", "WS-1", "huge.gltf")))
expect("the alert carries the reasons", "80,000 triangles exceeds" in text)
expect("the alert states the overshoot", "1.6x" in text)
expect("slack uses a text envelope", json.loads(ahs.webhook_body("slack", text))["text"] == text)
expect("discord uses a content envelope",
       json.loads(ahs.webhook_body("discord", text))["content"] == text)
expect("teams uses a MessageCard",
       json.loads(ahs.webhook_body("teams", text))["@type"] == "MessageCard")

posted = []
_real_urlopen = urllib.request.urlopen


def fake_post(request, *args, **kwargs):
    """Intercept webhook traffic only â€” the test client's own calls must pass through.

    Patching urllib.request.urlopen globally also swallowed the requests this
    suite makes to the server under test, which looked like "three alerts fired".
    """
    url = getattr(request, "full_url", str(request))
    if not url.startswith("https://hooks.example"):
        return _real_urlopen(request, *args, **kwargs)
    posted.append((url, json.loads(request.data)))
    return FakeResponse(b"{}")


webhook = ahs.Notifier("https://hooks.example/T00/B00/xyz", "slack")
with patch("urllib.request.urlopen", side_effect=fake_post):
    webhook.submit(text)
    webhook.close(timeout=5.0)
expect("the notifier posts once", len(posted) == 1 and webhook.sent == 1, str(webhook.sent))
expect("the post is JSON with a text field", posted[0][1]["text"] == text)

silent = ahs.Notifier("https://hooks.example/dead", "slack")
with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
    try:
        silent.submit(text)
        silent.close(timeout=5.0)
        raised = False
    except Exception:  # noqa: BLE001 - the point is that it does not raise
        raised = True
expect("a dead webhook never raises into the ingest", not raised)
expect("the failure is counted", silent.failed == 1 and silent.sent == 0)

# End-to-end through the ingest route: only the critical failure alerts.
alert_db = fresh_db("alerts.db")
alert_pid, alert_tok = ahs.create_project(alert_db, "Alerting")
ahs.save_budget(alert_db, alert_pid, {"max_triangles": 50_000, "max_material_slots": 3})
alert_notifier = ahs.Notifier("https://hooks.example/T00/B00/xyz", "slack")
alert_server = ahs.build_server("127.0.0.1", 0, alert_db, quiet=True, notifier=alert_notifier)
alert_port = alert_server.server_address[1]
threading.Thread(target=alert_server.serve_forever, daemon=True).start()
time.sleep(0.15)
ALERT_AUTH = {"Authorization": f"Bearer {alert_tok}"}

with patch("urllib.request.urlopen", side_effect=fake_post):
    posted.clear()
    request(alert_port, "/api/telemetry", method="POST", headers=ALERT_AUTH, body=heavy)
    request(alert_port, "/api/telemetry", method="POST", headers=ALERT_AUTH, body=mild)
    request(alert_port, "/api/telemetry", method="POST", headers=ALERT_AUTH,
            body=dict(heavy, name="healed.gltf", passed=True, status="HEALED"))
    alert_notifier.close(timeout=5.0)
expect("one alert for three verdicts: only the critical failure", len(posted) == 1, str(len(posted)))
expect("the alert names the critical asset", "huge.gltf" in posted[0][1]["text"])
expect("healed and mild failures stay quiet",
       "healed.gltf" not in posted[0][1]["text"] and "60,000" not in posted[0][1]["text"])
alert_server.shutdown()
alert_server.server_close()

print("11. budget planner and remote-rules")
plan_db = fresh_db("planner.db")
plan_pid, plan_tok = ahs.create_project(plan_db, "Planner Game")
plan_server = ahs.build_server("127.0.0.1", 0, plan_db, quiet=True)
pport = plan_server.server_address[1]
threading.Thread(target=plan_server.serve_forever, daemon=True).start()
time.sleep(0.15)
PLAN_AUTH = {"Authorization": f"Bearer {plan_tok}"}

status, _, _ = request(pport, f"/budget-planner?project_id={plan_pid}")
expect("the planner needs a token", status == 401, str(status))
status, body, _ = request(pport, f"/budget-planner?project_id={plan_pid}&token={plan_tok}")
planner = body.decode("utf-8")
expect("the planner renders", status == 200 and "Budget planner" in planner, str(status))
expect("it has the triangle slider",
       'type="range"' in planner and 'id="max_triangles"' in planner and 'max="200000"' in planner)
expect("it has the material slider", 'id="max_material_slots"' in planner and 'max="16"' in planner)
expect("it has the edge dropdown", all(f'value="{edge}"' in planner for edge in (512, 1024, 2048, 4096)))
expect("it has the three checkboxes",
       all(f'id="{field}"' in planner for field in ("power_of_two", "naming", "auto_heal")))
expect("it posts to the save route", "/api/save-budget" in planner)

status, body, _ = request(
    pport, "/api/save-budget", method="POST", headers=PLAN_AUTH,
    body={"project_id": plan_pid, "max_triangles": 25_000, "max_material_slots": 2,
          "max_texture_edge": 1024, "power_of_two": True, "naming": False, "auto_heal": True})
saved = json.loads(body)["budget"]
expect("a budget can be published", status == 200, str(status))
expect("the published values stick",
       (saved["max_triangles"], saved["max_material_slots"], saved["max_texture_edge"],
        saved["naming"], saved["auto_heal"]) == (25_000, 2, 1024, False, True), str(saved))
expect("a partial form keeps the previous values",
       ahs.save_budget(plan_db, plan_pid, {"max_triangles": 30_000})["max_material_slots"] == 2)

status, _, _ = request(pport, "/api/save-budget", method="POST", headers=PLAN_AUTH,
                       body={"project_id": plan_pid, "max_triangles": -5})
expect("an out-of-range budget is a 400", status == 400, str(status))
status, _, _ = request(pport, "/api/save-budget", method="POST", headers=PLAN_AUTH,
                       body={"project_id": plan_pid, "max_triangles": "lots"})
expect("a non-numeric budget is a 400", status == 400, str(status))
status, _, _ = request(pport, "/api/save-budget", method="POST",
                       body={"project_id": plan_pid, "max_triangles": 1000})
expect("publishing without a token is refused", status == 401, str(status))

# The whole point: what the planner saves is what --remote-rules receives.
status, body, _ = request(pport, f"/api/rules?project_id={plan_pid}&token={plan_tok}")
document = json.loads(body)
expect("the rules endpoint serves the published budget", status == 200, str(status))
expect("it is shaped like rules.toml",
       document["mesh"]["max_triangles"] == 30_000
       and document["texture"]["max_edge"] == 1024
       and document["texture"]["naming"] is False
       and document["healer"]["enabled"] is True, str(document))

import asset_gatekeeper as agk  # noqa: E402 - proves the contract, not just the JSON

remote_budget = agk.Budget.from_mapping(document)
expect("the gate's own parser accepts it", remote_budget.max_triangles == 30_000,
       str(remote_budget.max_triangles))
expect("the planner can enable healing for clients",
       agk.HealSettings.from_mapping(document["healer"]).enabled is True)
expect("the remote document parses through the real text path",
       agk.parse_rules_text(json.dumps(document))["mesh"]["max_triangles"] == 30_000)

status, _, _ = request(pport, f"/api/rules?project_id={plan_pid}")
expect("the rules endpoint is not public", status == 401, str(status))
plan_server.shutdown()
plan_server.server_close()

print("12. alert throttling")
thr_db = fresh_db("throttle.db")
now = 1_000_000.0
expect("the first alert in a window is sent",
       ahs.claim_alert_slot(thr_db, "project:1", 300, now=now) is True)
expect("a second alert inside the cooldown is dropped",
       ahs.claim_alert_slot(thr_db, "project:1", 300, now=now + 10) is False)
expect("the cooldown expires on its own",
       ahs.claim_alert_slot(thr_db, "project:1", 300, now=now + 301) is True)
expect("another project keeps its own window",
       ahs.claim_alert_slot(thr_db, "project:2", 300, now=now + 302) is True)
expect("legacy scope has its own window too",
       ahs.claim_alert_slot(thr_db, "shared", 300, now=now + 303) is True)
expect("a cooldown of 0 disables throttling",
       all(ahs.claim_alert_slot(thr_db, "project:1", 0, now=now + 400) for _ in range(3)))
expect("scopes are named per project", ahs.alert_scope(7) == "project:7")
expect("legacy scope is explicit", ahs.alert_scope(None) == "shared")

# Through the real ingest route: a batch sweep must produce one message.
thr_pid, thr_tok = ahs.create_project(thr_db, "Batch Night")
ahs.save_budget(thr_db, thr_pid, {"max_triangles": 50_000})
batch_notifier = ahs.Notifier("https://hooks.example/T0/B0/x", "slack")
batch_server = ahs.build_server("127.0.0.1", 0, thr_db, quiet=True,
                                notifier=batch_notifier, alert_cooldown=300)
bport = batch_server.server_address[1]
threading.Thread(target=batch_server.serve_forever, daemon=True).start()
time.sleep(0.15)
BATCH_AUTH = {"Authorization": f"Bearer {thr_tok}"}

with patch("urllib.request.urlopen", side_effect=fake_post):
    posted.clear()
    for index in range(3):
        request(bport, "/api/telemetry", method="POST", headers=BATCH_AUTH,
                body=dict(heavy, name=f"batch_{index}.gltf"))
    batch_notifier.flush()
expect("a three-asset batch sends exactly one alert", len(posted) == 1, str(len(posted)))
expect("the suppressed count is tracked", batch_server.alerts_suppressed == 2,
       str(batch_server.alerts_suppressed))

status, body, _ = request(bport, f"/api/summary?project_id={thr_pid}&token={thr_tok}")
summary = json.loads(body)
expect("/api/summary exposes the alert state",
       summary["alerts"]["enabled"] is True and summary["alerts"]["cooldown"] == 300
       and summary["alerts"]["suppressed"] == 2, str(summary.get("alerts")))

# Rewind the window, as if five minutes had passed.
with ahs.open_db(thr_db) as connection:
    connection.execute("UPDATE alerts_state SET last_alert_at = 0 WHERE scope_key = ?",
                       (ahs.alert_scope(thr_pid),))
with patch("urllib.request.urlopen", side_effect=fake_post):
    posted.clear()
    request(bport, "/api/telemetry", method="POST", headers=BATCH_AUTH,
            body=dict(heavy, name="after_cooldown.gltf"))
    batch_notifier.flush()
expect("the next window alerts again", len(posted) == 1, str(len(posted)))
expect("the alert names the new asset", "after_cooldown.gltf" in posted[0][1]["text"])

# A different project is not muted by the first one's cooldown.
other_pid, other_tok = ahs.create_project(thr_db, "Other Vendor")
ahs.save_budget(thr_db, other_pid, {"max_triangles": 50_000})
with patch("urllib.request.urlopen", side_effect=fake_post):
    posted.clear()
    request(bport, "/api/telemetry", method="POST",
            headers={"Authorization": f"Bearer {other_tok}"},
            body=dict(heavy, name="vendor_fail.gltf"))
    batch_notifier.flush()
expect("a second tenant still gets its own alert", len(posted) == 1, str(len(posted)))
expect("the second alert belongs to the second tenant",
       "vendor_fail.gltf" in posted[0][1]["text"])
batch_server.shutdown()
batch_server.server_close()

print("13. live updates (Server-Sent Events)")
cast = ahs.Broadcaster(queue_max=2)
first, second = cast.subscribe(), cast.subscribe()
expect("subscribers are counted", cast.listeners == 2, str(cast.listeners))
cast.publish({"type": "verdict", "n": 1})
expect("every subscriber receives the event",
       first.get_nowait()["n"] == 1 and second.get_nowait()["n"] == 1)
for index in range(5):
    cast.publish({"type": "verdict", "n": 100 + index})
expect("a lagging tab keeps only the newest events",
       first.qsize() == 2 and first.get_nowait()["n"] == 103, str(first.qsize()))
while not first.empty():          # drain before asserting silence
    first.get_nowait()
cast.unsubscribe(first)
cast.publish({"type": "verdict", "n": 9})
expect("unsubscribing stops delivery", first.empty() and second.qsize() > 0)
cast.unsubscribe(second)
expect("no listeners left", cast.listeners == 0)

sse_db = fresh_db("sse.db")
sse_pid, sse_tok = ahs.create_project(sse_db, "Live")
sse_server = ahs.build_server("127.0.0.1", 0, sse_db, quiet=True)
sse_port = sse_server.server_address[1]
threading.Thread(target=sse_server.serve_forever, daemon=True).start()
time.sleep(0.15)

received_events = []


def read_stream():
    try:
        url = f"http://127.0.0.1:{sse_port}/api/events?token={sse_tok}"
        with urllib.request.urlopen(url, timeout=20) as stream:
            for raw in stream:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    received_events.append(json.loads(line[5:].strip()))
                    break
    except Exception as exc:                  # surfaced as a test failure below
        received_events.append({"error": str(exc)})


reader = threading.Thread(target=read_stream, daemon=True)
reader.start()
time.sleep(0.4)                              # let the subscription land
request(sse_port, "/api/telemetry", method="POST",
        headers={"Authorization": f"Bearer {sse_tok}"},
        body=dict(heavy, name="live_one.gltf"))
reader.join(10)

event = received_events[0] if received_events else {}
expect("the open stream receives the verdict", event.get("type") == "verdict", str(event))
expect("the event names the asset", event.get("name") == "live_one.gltf", str(event.get("name")))
expect("the event carries the verdict status", event.get("status") == "FAILED", str(event.get("status")))
expect("the event carries the project", event.get("project_id") == sse_pid)
expect("the stream closes when the client goes away", not reader.is_alive())

# One tab with no token must not be able to watch a tenant.
status, _, _ = request(sse_port, "/api/events")
expect("/api/events is not a hole in the auth", status == 401, str(status))
sse_server.shutdown()
sse_server.server_close()

print("14. CLI wiring")
parser = ahs.build_parser()
options = parser.parse_args(["--port", "9999", "--db", "x.db", "--token", "t", "--no-seed"])
expect("CLI accepts host/port/db/token/no-seed",
       (options.port, options.db, options.token, options.no_seed) == (9999, "x.db", "t", True))
expect("defaults are loopback-only", (ahs.DEFAULT_HOST, ahs.DEFAULT_PORT) == ("127.0.0.1", 8080))
expect("a bad database folder exits 2", ahs.main(["--db", str(WORK / "missing" / "x.db")]) == 2)

print("15. rules inspector (read-only)")
# The inspector exists so an artist can see the budget without opening a TOML
# file. It must agree with the gate exactly, so it runs the gate's resolvers.
ins_db = fresh_db("inspector.db")
ins_rules = WORK / "inspector_rules.toml"
ins_rules.write_text(
    'platform = "n64"\n'
    "\n[mesh]\n"
    "max_triangles = 2000\n"
    "max_material_slots = 4\n"
    "\n[texture]\n"
    "max_edge = 32\n"
    "power_of_two = true\n"
    "\n[tagging]\n"
    'mode = "strict"\n'
    "min_similarity = 0.8\n"
    "\n[platforms.n64]\n"
    "max_texture_edge = 64\n"
    "\n[tags.boss]\n"
    "max_triangles = 20000\n"
    "max_material_slots = 8\n"
    "\n[tags.environment_rock]\n"
    "max_triangles = 300\n"
    "max_texture_edge = 16\n",
    encoding="utf-8")

ins_server = ahs.build_server("127.0.0.1", 0, ins_db, rules_path=str(ins_rules), rules_root=str(WORK))
threading.Thread(target=ins_server.serve_forever, daemon=True).start()
ins_port = ins_server.server_address[1]

status, body, _ = request(ins_port, "/api/rules")
document = json.loads(body)
expect("GET /api/rules answers without a project in open mode", status == 200, str(status))
expect("the inspector declares itself read-only", document.get("read_only") is True, str(document.get("read_only")))
expect("it reports the platform from rules.toml",
       document.get("target_platform") == "n64", str(document.get("target_platform")))
expect("it reports the tag matching mode", document.get("tag_mode") == "strict", str(document.get("tag_mode")))
expect("it reports the tag similarity threshold", document.get("tag_similarity") == 0.8,
       str(document.get("tag_similarity")))
global_budget = document.get("global_budget") or {}
expect("the global budget is the effective one (platform overlay applied)",
       (global_budget.get("max_triangles"), global_budget.get("max_texture_edge"),
        global_budget.get("max_material_slots")) == (2000, 64, 4), str(global_budget)[:120])
expect("vertices are reported as unknown, not invented",
       global_budget.get("max_vertices") is None and document.get("vertex_budget_enforced") is False,
       str(global_budget.get("max_vertices")))
expect("the source names the file that was read",
       str(ins_rules) in str(document.get("source")), str(document.get("source")))
tags = {tag["tag"]: tag for tag in document.get("tag_budgets") or []}
expect("both tag budgets are listed", sorted(tags) == ["boss", "environment rock"], str(sorted(tags)))
expect("a tag keeps its own triangle limit", tags.get("boss", {}).get("max_triangles") == 20000,
       str(tags.get("boss")))
expect("a tag keeps its own texture limit",
       tags.get("environment rock", {}).get("max_texture_edge") == 16,
       str(tags.get("environment rock")))
expect("a tag inherits the effective platform limit it did not override",
       tags.get("boss", {}).get("max_texture_edge") == 64, str(tags.get("boss")))
expect("the project default leaves vertex caps empty",
       all(tag.get("max_vertices") is None for tag in tags.values()), str(tags))

# A broken file must explain itself, not 500: the inspector is where an artist
# goes to find out why their asset failed.
ins_bad = WORK / "inspector_broken.toml"
ins_bad.write_text('[mesh]\nmax_triangles = "lots"\n', encoding="utf-8")
ins_server.rules_path = str(ins_bad)
status, body, _ = request(ins_port, "/api/rules")
document = json.loads(body)
expect("a broken rules file is 200 with an error, not a 500", status == 200, str(status))
expect("the error is human readable", "cannot read rules" in str(document.get("error")),
       str(document.get("error"))[:80])
expect("a broken file yields no budget to render", document.get("global_budget") is None)
ins_server.shutdown()
ins_server.server_close()

# In a token-protected studio the inspector is not a hole.
locked = fresh_db("inspector_locked.db")
locked_server = ahs.build_server("127.0.0.1", 0, locked, token="s3cret")
threading.Thread(target=locked_server.serve_forever, daemon=True).start()
status, _, _ = request(locked_server.server_address[1], "/api/rules")
expect("/api/rules still needs a token when one is configured", status == 401, str(status))
status, _, _ = request(locked_server.server_address[1], "/api/rules?token=s3cret")
expect("the right token gets the inspector", status == 200, str(status))
locked_server.shutdown()
locked_server.server_close()

inspector_options = ahs.build_parser().parse_args(["--rules", "custom.toml"])
expect("the CLI can point the inspector at a rules file",
       inspector_options.rules == "custom.toml", inspector_options.rules)

print()
print("16. demo data is opt-in, and labelled when used")
# It used to be the default, and every new user asked why the dashboard already
# held verdicts they never ran. A pass rate that is not yours is a bug.
defaults = ahs.build_parser().parse_args([])
expect("demo data is off by default", defaults.demo is False)
expect("--demo asks for it", ahs.build_parser().parse_args(["--demo"]).demo is True)
expect("--no-seed is still accepted for old scripts",
       ahs.build_parser().parse_args(["--no-seed"]).no_seed is True)

optin_db = fresh_db("optin.db")
expect("a new database starts empty", ahs.totals(optin_db)["total"] == 0)
expect("an empty page carries no demo banner",
       "are demo data" not in ahs.render_dashboard(optin_db))
expect("nothing is labelled as demo yet", ahs.demo_row_count(optin_db) == 0)

expect("seeding inserts the sample set", ahs.seed_if_first_launch(optin_db) is True)
expect("it records how many rows it added",
       ahs.demo_row_count(optin_db) == len(ahs._SEED) + 1, str(ahs.demo_row_count(optin_db)))
expect("the dashboard labels those rows",
       "are demo data" in ahs.render_dashboard(optin_db))
expect("and names the way out", "--db myproject.db" in ahs.render_dashboard(optin_db))

print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
