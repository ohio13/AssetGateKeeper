"""Drive the Art Workstation viewer in a real browser (Chromium via Playwright).

The API suite (test_workstation.py) passed while the viewer showed the wrong
model, refused embedded glTF buffers and crashed on Geometry -> Textured: none
of that is visible without a browser. Every check here is a bug found by
driving the page on 2026-11-02.

Needs ``pip install playwright`` and ``python -m playwright install chromium``.
Without them the suite says so and exits 0 (CI installs both and runs it).
"""
import http.server
import json
import shutil
import socketserver
import struct
import sys
import threading
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
for extra in (str(ROOT), str(ROOT / "server"), str(ROOT / "engine")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIPPED: playwright is not installed (pip install playwright && "
          "python -m playwright install chromium)")
    raise SystemExit(0)

import art_workstation_server as aws  # noqa: E402
import lod_manager  # noqa: E402

WORK = Path("scratch") / "viewer_browser"
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)
failures = []


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not condition:
        failures.append(label)


# --- fixtures: every kind of asset the audit tripped over --------------------
CUBE = lod_manager.Mesh(
    [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)],
    [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
     (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)])


def sphere(rings=24, segments=48):
    import math
    points = [(0.0, 1.0, 0.0)]
    for ring in range(1, rings):
        phi = math.pi * ring / rings
        for segment in range(segments):
            theta = 2 * math.pi * segment / segments
            points.append((math.sin(phi) * math.cos(theta), math.cos(phi),
                           math.sin(phi) * math.sin(theta)))
    points.append((0.0, -1.0, 0.0))
    faces = []
    for segment in range(segments):
        faces.append((0, 1 + (segment + 1) % segments, 1 + segment))
    for ring in range(rings - 2):
        for segment in range(segments):
            a = 1 + ring * segments + segment
            b = 1 + ring * segments + (segment + 1) % segments
            faces += [(a, b, b + segments), (a, b + segments, a + segments)]
    last = len(points) - 1
    base = 1 + (rings - 2) * segments
    for segment in range(segments):
        faces.append((last, base + segment, base + (segment + 1) % segments))
    return lod_manager.Mesh(points, faces)


lod_manager.write_glb(WORK / "cube.glb", CUBE, "cube")
lod_manager.write_gltf(WORK / "embedded.gltf", sphere(), "embedded")   # data: URI buffer
lod_manager.write_obj(WORK / "ball.obj", sphere(), "ball")


def png(width, height):
    import zlib
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


(WORK / "rock_Albedo.png").write_bytes(png(64, 64))
(WORK / "tool.exe").write_bytes(b"MZ")
(WORK / "second.glb").write_bytes((WORK / "cube.glb").read_bytes())
FBX = ROOT / "tests" / "fixtures" / "fbx" / "cube_subd3.fbx"

db = WORK / "ws.db"
data = WORK / "data"
aws.init_db(db)
ids = {}
ids["cube"] = aws.create_asset(db, data, "cube.glb", (WORK / "cube.glb").read_bytes())["id"]
ids["embedded"] = aws.create_asset(db, data, "embedded.gltf",
                                   (WORK / "embedded.gltf").read_bytes())["id"]
ids["ball"] = aws.create_asset(db, data, "ball.obj", (WORK / "ball.obj").read_bytes(),
                               provenance={"verdict": "FAILED", "problems": "too heavy",
                                           "source": "cli"})["id"]
ids["fbx"] = aws.create_asset(db, data, "cube_subd3.fbx", FBX.read_bytes())["id"]
ids["png"] = aws.create_asset(db, data, "rock_Albedo.png",
                              (WORK / "rock_Albedo.png").read_bytes())["id"]
aws.generate_lods(db, data, ids["ball"], [0.5, 0.25])
aws.generate_lods(db, data, ids["embedded"], [0.5])

server = aws.build_server("127.0.0.1", 0, db, data, quiet=True)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{port}"


class Evil(http.server.SimpleHTTPRequestHandler):
    """Another site the reviewer happens to have open."""

    def do_GET(self):  # noqa: N802
        body = ("<!doctype html><script>"
                f"fetch('{BASE}/api/notes', {{method: 'POST', mode: 'no-cors',"
                " headers: {'Content-Type': 'text/plain'},"
                f" body: JSON.stringify({{asset_id: {ids['cube']}, category: 'Other',"
                " note_text: 'written by another website'})});"
                "</script>evil").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


evil = socketserver.TCPServer(("127.0.0.1", 0), Evil)
threading.Thread(target=evil.serve_forever, daemon=True).start()


def notes_of(asset_id):
    with urllib.request.urlopen(f"{BASE}/api/assets/{asset_id}/notes") as response:
        return [note["note_text"] for note in json.loads(response.read())["notes"]]


with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page_errors, console_errors = [], []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("console", lambda message: console_errors.append(message.text)
            if message.type == "error" else None)

    def status():
        return page.locator("#status").inner_text()

    def readout():
        return page.locator("#readout").inner_text().replace("\n", " ")

    def hint_visible():
        return page.locator("#viewport-hint").is_visible()

    def pick(asset_id, settle="loaded"):
        page.locator(f'#asset-list .asset[data-id="{asset_id}"]').click()
        page.wait_for_function(
            "() => !document.getElementById('status').textContent.startsWith('loading')",
            timeout=15000)

    page.goto(f"{BASE}/viewer?live=0")
    page.wait_for_selector("#asset-list .asset")

    print("1. every previewable format loads")
    pick(ids["cube"])
    expect("a .glb loads", status() == "cube.glb" and not hint_visible(), status())
    expect("its readout is its own (12 tris)", "12 tris" in readout(), readout())
    pick(ids["embedded"])
    expect("a .gltf with an embedded data: buffer loads (CSP refused it)",
           status() == "embedded.gltf" and not hint_visible(), status())
    page.locator("#lod-buttons button[data-level='LOD1']").click()
    page.wait_for_timeout(800)
    expect("a .gltf LOD loads", not status().startswith("error"), status())
    pick(ids["ball"])
    page.locator("#lod-buttons button[data-level='LOD1']").click()
    page.wait_for_timeout(800)
    expect("an .obj LOD loads (was parsed as glTF)", not status().startswith("error")
           and "LOD1" in readout(), (status(), readout()))

    print("2. an asset that cannot be drawn never shows the previous one")
    pick(ids["cube"])
    pick(ids["fbx"])
    expect("selecting an FBX shows the no-preview panel", hint_visible()
           and "no 3D preview" in page.locator("#hint-title").inner_text(),
           page.locator("#hint-title").inner_text())
    expect("the readout no longer shows the cube's 12 tris", "12 tris" not in readout(), readout())
    expect("the header names the FBX", "cube_subd3.fbx" in status(), status())
    expect("the cube's material sliders are gone",
           "material" not in page.locator("#materials").inner_text().lower()
           or "no 3d model" in page.locator("#materials").inner_text().lower(),
           page.locator("#materials").inner_text())
    expect("a note can be added to an unmeasured asset", page.locator("#note-submit").is_enabled())
    page.fill("#note-text", "needs a re-export")
    page.locator("#note-submit").click()
    page.wait_for_timeout(600)
    expect("... and it is saved", "needs a re-export" in notes_of(ids["fbx"]), notes_of(ids["fbx"]))
    pick(ids["png"])
    expect("a texture says what it is instead of failing as glTF",
           hint_visible() and "64x64" in page.locator("#hint-body").inner_text()
           and "64x64" in readout(), (page.locator("#hint-body").inner_text(), readout()))
    expect("its library card shows its size, not '0 tris'",
           "0 tris" not in page.locator(f'#asset-list .asset[data-id="{ids["png"]}"]').inner_text())

    print("3. materials")
    pick(ids["cube"])
    page.click("#mode-geometry")
    page.click("#mode-textured")
    page.click("#mode-geometry")
    page.click("#mode-textured")
    expect("Geometry -> Textured does not throw", not page_errors, page_errors[:1])

    print("4. verdicts")
    page.goto(f"{BASE}/viewer?live=0&asset={ids['ball']}")
    page.wait_for_function(
        "() => !document.getElementById('status').textContent.startsWith('loading')"
        " && document.getElementById('status').textContent !== 'loading…'", timeout=15000)
    page.wait_for_timeout(500)
    verdict = page.locator("#verdict-panel").inner_text()
    expect("a deep link shows the asset's verdict (was 'No asset selected')",
           "FAILED" in verdict and "too heavy" in verdict, verdict.replace("\n", " | "))
    page.click("button.filter[data-filter='attention']")
    visible = page.locator("#asset-list .asset:visible").count()
    expect("an upper-case FAILED counts as needing attention", visible == 1, visible)
    page.click("button.filter[data-filter='all']")

    print("5. uploads")
    page.set_input_files("#upload", str(WORK / "tool.exe"))
    page.wait_for_function("() => !document.getElementById('status').textContent.startsWith('uploading')",
                           timeout=15000)
    expect("a refused file is reported, not left on 'uploading'",
           "refused" in status() and "tool.exe" in status(), status())
    page.set_input_files("#upload", [str(WORK / "second.glb"), str(WORK / "rock_Albedo.png")])
    page.wait_for_function("() => !document.getElementById('status').textContent.startsWith('uploading')",
                           timeout=15000)
    page.wait_for_timeout(500)
    names = page.locator("#asset-list").inner_text()
    expect("several files upload in one go", "second.glb" in names and names.count("rock_Albedo.png") == 2,
           status())

    print("6. collision")
    pick(ids["cube"])
    page.select_option("#collision-kind", "hull")
    page.click("#collision-derive")
    page.wait_for_timeout(800)
    with urllib.request.urlopen(f"{BASE}/api/assets/{ids['cube']}/collision.obj") as response:
        hull_obj = response.read().decode()
    expect("a hull rebuilt from the mesh exports (was 413)",
           sum(line.startswith("f ") for line in hull_obj.splitlines()) == 12,
           sum(line.startswith("f ") for line in hull_obj.splitlines()))

    print("7. deleted while open")
    page.goto(f"{BASE}/viewer?asset={ids['embedded']}")
    page.wait_for_function("() => document.getElementById('status').textContent === 'embedded.gltf'",
                           timeout=15000)
    request = urllib.request.Request(f"{BASE}/api/assets/{ids['embedded']}", method="DELETE")
    urllib.request.urlopen(request).read()
    page.wait_for_timeout(2500)
    expect("deleting the open asset clears the screen",
           hint_visible() and "No asset selected" in page.locator("#collision-info").inner_text()
           and page.locator("#lod-buttons button").count() == 0,
           page.locator("#collision-info").inner_text())

    print("8. another website")
    other = browser.new_page()
    other.goto(f"http://127.0.0.1:{evil.server_address[1]}/")
    other.wait_for_timeout(1500)
    expect("a page on another origin cannot write a note",
           "written by another website" not in notes_of(ids["cube"]), notes_of(ids["cube"]))

    blocked = [line for line in console_errors if "Content Security Policy" in line
               or "Fetch API cannot load" in line]
    expect("no load was blocked by the page's security policy", not blocked, blocked[:1])
    expect("no uncaught page errors", not page_errors, page_errors[:2])
    browser.close()

server.shutdown()
server.server_close()
evil.shutdown()
evil.server_close()
print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
