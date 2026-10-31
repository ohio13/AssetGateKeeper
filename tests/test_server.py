"""Sanity checks for asset_health_server: ingest, auth, dashboard, storage.

Standalone like the rest of the suite: no pytest, no fixtures framework. Spins
the real server on an ephemeral port and talks to it over real sockets, because
the interesting failures here are HTTP-level (status codes, keep-alive, bodies).
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

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
expect("first launch seeds demo rows", ahs.seed_if_first_launch(seed_db) is True)
expect("first launch seeds in one call", ahs.totals(seed_db)["total"] == len(ahs._SEED))
expect("a second launch does not re-seed", ahs.seed_if_first_launch(seed_db) is False)
expect("still the same row count", ahs.totals(seed_db)["total"] == len(ahs._SEED))

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
expect("the new verdict is counted", ahs.totals(seed_db)["total"] == len(ahs._SEED) + 1)

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

print("8. CLI wiring")
parser = ahs.build_parser()
options = parser.parse_args(["--port", "9999", "--db", "x.db", "--token", "t", "--no-seed"])
expect("CLI accepts host/port/db/token/no-seed",
       (options.port, options.db, options.token, options.no_seed) == (9999, "x.db", "t", True))
expect("defaults are loopback-only", (ahs.DEFAULT_HOST, ahs.DEFAULT_PORT) == ("127.0.0.1", 8080))
expect("a bad database folder exits 2", ahs.main(["--db", str(WORK / "missing" / "x.db")]) == 2)

print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
