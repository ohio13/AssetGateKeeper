/* Art Workstation viewer.
 *
 * Three.js is vendored under /static/vendor (no CDN, no network at runtime);
 * everything else - routing, auth, notes, collision - is the stdlib server.
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import { ConvexGeometry } from "three/addons/geometries/ConvexGeometry.js";

const $ = (id) => document.getElementById(id);

const state = {
  assets: [],
  asset: null,          // the full /api/assets/<id> payload
  level: "source",
  geometryMode: false,
  wireframe: false,
  collision: true,
  token: localStorage.getItem("aw_token") || "",
  sceneObjects: { model: null, collision: null },
  originals: new Map(), // material -> {color, map, ...} for the exposure sliders
  meshMaterials: new Map(), // mesh -> the material(s) it was loaded with
  selectSeq: 0,         // the newest selectAsset call wins; older ones stop
};

// Formats the browser can draw. Everything else the server stores is measured
// (or labelled unverified) but gets the "no 3D preview" panel instead of being
// fed to the glTF loader, which used to fail with "not valid JSON" and leave
// the previous asset on screen.
const PREVIEW_SUFFIXES = [".glb", ".gltf", ".obj"];
const suffixOf = (name) => {
  const lower = String(name || "").toLowerCase();
  return lower.includes(".") ? lower.slice(lower.lastIndexOf(".")) : "";
};
const previewable = (asset) => !!asset && asset.kind === "mesh"
  && PREVIEW_SUFFIXES.includes(suffixOf(asset.filename));

/* - auth */

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
  const response = await fetch(path, Object.assign({}, options, { headers }));

  if (response.status === 401) {
    const supplied = window.prompt("This workstation needs an access token:");
    if (supplied) {
      state.token = supplied.trim();
      localStorage.setItem("aw_token", state.token);
      return api(path, options);           // retry once, with the new token
    }
    throw new Error("unauthorised");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).error || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return response;
}

const json = (path, options) => api(path, options).then((r) => r.json());

/* - three.js */

const host = $("canvas-host");
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0d1016);

const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 5000);
camera.position.set(2.4, 1.8, 3.2);

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.0;
host.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;

scene.add(new THREE.HemisphereLight(0xbcd0ff, 0x22262e, 1.6));
const key = new THREE.DirectionalLight(0xffffff, 2.4);
key.position.set(4, 8, 6);
scene.add(key);
const rim = new THREE.DirectionalLight(0x7aa2f7, 1.2);
rim.position.set(-6, 2, -4);
scene.add(rim);

const grid = new THREE.GridHelper(20, 40, 0x2a3444, 0x1a212c);
grid.material.opacity = 0.55;
grid.material.transparent = true;
scene.add(grid);

function resize() {
  const width = host.clientWidth || 1;
  const height = host.clientHeight || 1;
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", resize);
new ResizeObserver(resize).observe(host);

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}

/* - loading */

const gltfLoader = new GLTFLoader();
const objLoader = new OBJLoader();

function disposeObject(object) {
  if (!object) return;
  object.traverse((node) => {
    // In Geometry mode a mesh wears the shared normal material; dispose the
    // material it was loaded with instead (and keep the shared one alive).
    if (state.meshMaterials.has(node)) node.material = state.meshMaterials.get(node);
    if (node.geometry) node.geometry.dispose();
    if (node.material) {
      const materials = Array.isArray(node.material) ? node.material : [node.material];
      materials.forEach((material) => {
        for (const value of Object.values(material)) {
          if (value && value.isTexture) value.dispose();
        }
        if (material !== normalMaterial) material.dispose();
      });
    }
  });
  scene.remove(object);
}

/* The one way to empty the viewport. Called on every selection, on delete and
 * when a load fails, so nothing of the previous asset - model, collision box,
 * material sliders - can be left on screen under another asset's name. */
function clearModel() {
  disposeObject(state.sceneObjects.model);
  state.sceneObjects.model = null;
  disposeObject(state.sceneObjects.collision);
  state.sceneObjects.collision = null;
  state.meshMaterials.clear();
  state.originals.clear();
  $("materials").innerHTML = '<div class="empty">No 3D model loaded.</div>';
}

function showHint(title, body) {
  $("hint-title").textContent = title;
  $("hint-body").textContent = body;
  $("viewport-hint").style.display = "";
}

function frameObject(object) {
  const box = new THREE.Box3().setFromObject(object);
  if (box.isEmpty()) return;
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z) || 1;

  controls.target.copy(center);
  const distance = radius * 2.4;
  camera.position.set(center.x + distance * 0.7, center.y + distance * 0.5,
                      center.z + distance * 0.8);
  camera.near = Math.max(radius / 1000, 0.001);
  camera.far = radius * 100;
  camera.updateProjectionMatrix();
  controls.update();

  grid.position.set(center.x, box.min.y, center.z);
  grid.scale.setScalar(Math.max(radius / 4, 0.4));
}

async function loadModel(assetId, level) {
  const lod = level === "source" ? null
    : ((state.asset && state.asset.lods) || []).find((item) => item.level === level);
  const path = level === "source"
    ? `/api/assets/${assetId}/file`
    : `/api/assets/${assetId}/lods/${level}/file`;
  const response = await api(path);
  const buffer = await response.arrayBuffer();
  // The loader follows the stored file's real type. LODs used to be named
  // "<level>.gltf" whatever they were, so every LOD of an .obj failed with
  // "Unexpected token '#' ... is not valid JSON".
  const name = level === "source"
    ? (state.asset ? state.asset.filename : "")
    : (lod ? lod.file_path : `${level}.glb`);

  let object;
  if (suffixOf(name) === ".obj") {
    object = objLoader.parse(new TextDecoder().decode(buffer));
  } else {
    const gltf = await new Promise((resolve, reject) =>
      gltfLoader.parse(buffer, "", resolve, reject));
    object = gltf.scene;
  }
  if (!state.asset || state.asset.id !== assetId) {
    disposeObject(object);                 // the user moved on while this loaded
    return;
  }

  clearModel();
  $("viewport-hint").style.display = "none";
  const grey = level === "source" ? null
    : new THREE.MeshStandardMaterial({ color: 0x9ea3ad, metalness: 0.05, roughness: 0.7,
                                       name: "LOD preview (no materials)" });
  object.traverse((node) => {
    if (!node.isMesh) return;
    // Generated LODs carry positions only: no UVs, normals or materials.
    if (grey) {
      if (node.geometry && !node.geometry.attributes.normal) node.geometry.computeVertexNormals();
      node.material = grey;
    }
    state.meshMaterials.set(node, node.material);
    const materials = Array.isArray(node.material) ? node.material : [node.material];
    materials.forEach((material) => {
      if (!state.originals.has(material)) {
        state.originals.set(material, {
          color: material.color ? material.color.clone() : null,
          map: material.map || null,
          metalness: material.metalness,
          roughness: material.roughness,
        });
      }
    });
  });

  state.sceneObjects.model = object;
  scene.add(object);
  frameObject(object);
  applyMaterialMode();
  buildMaterialControls();
  refreshCollisionExport();
  updateReadout();
  await refreshCollisionOverlay();
  $("collision-derive").disabled = false;
}

/* - materials */

// One shared normal material for Geometry mode. Swapping whole materials keeps
// the originals untouched: the old code wrote a MeshNormalMaterial over each
// mesh, then tried to "restore" colour on it (it has none) - Textured threw
// "Cannot read properties of undefined (reading 'set')" and never came back.
const normalMaterial = new THREE.MeshNormalMaterial({ flatShading: false });

function applyMaterialMode() {
  const object = state.sceneObjects.model;
  if (!object) return;
  object.traverse((node) => {
    if (!node.isMesh || !state.meshMaterials.has(node)) return;
    node.material = state.geometryMode ? normalMaterial : state.meshMaterials.get(node);
  });
  applyWireframe();
}

function applyWireframe() {
  const object = state.sceneObjects.model;
  if (!object) return;
  object.traverse((node) => {
    if (!node.isMesh) return;
    const materials = Array.isArray(node.material) ? node.material : [node.material];
    materials.forEach((material) => {
      if ("wireframe" in material) material.wireframe = state.wireframe;
    });
  });
  normalMaterial.wireframe = state.wireframe;
}

/* - collision */

function collisionLines(kind, data) {
  const group = new THREE.Group();
  const style = { color: 0xe0af68, transparent: true, opacity: 0.9 };

  if (kind === "box" && data.min && data.max) {
    const box = new THREE.Box3(
      new THREE.Vector3().fromArray(data.min),
      new THREE.Vector3().fromArray(data.max));
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const helper = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.BoxGeometry(size.x, size.y, size.z)),
      new THREE.LineBasicMaterial(style));
    helper.position.copy(center);
    group.add(helper);
  } else if (kind === "sphere" && data.center && data.radius) {
    const helper = new THREE.Mesh(
      new THREE.SphereGeometry(data.radius, 24, 16),
      new THREE.MeshBasicMaterial(Object.assign({ wireframe: true }, style)));
    helper.position.fromArray(data.center);
    group.add(helper);
  } else if (kind === "hull" && Array.isArray(data.points)) {
    try {
      const points = data.points.map((p) => new THREE.Vector3().fromArray(p));
      const geometry = new ConvexGeometry(points);
      group.add(new THREE.Mesh(geometry,
        new THREE.MeshBasicMaterial(Object.assign({ wireframe: true }, style))));
    } catch (error) {
      console.warn("cannot build hull preview:", error);
    }
  }
  return group;
}

async function refreshCollisionOverlay() {
  disposeObject(state.sceneObjects.collision);
  state.sceneObjects.collision = null;
  if (!state.asset) {
    $("collision-info").innerHTML = '<div class="empty">No asset selected.</div>';
    return;
  }
  $("collision-info").innerHTML = "";
  const collision = state.asset.collision;
  if (!collision) {
    $("collision-info").innerHTML =
      '<div class="empty">No proxy yet. Pick a type and rebuild.</div>';
    return;
  }
  renderCollisionInfo(collision);
  if (!state.collision || !state.sceneObjects.model) return;
  const group = collisionLines(collision.kind, collision.data);
  state.sceneObjects.collision = group;
  scene.add(group);
}

function renderCollisionInfo(collision) {
  const rows = [];
  const push = (k, v) => rows.push(
    `<div class="row"><span class="k">${k}</span><span class="v">${v}</span></div>`);
  push("type", collision.kind);
  if (collision.kind === "box") {
    const size = collision.data.max.map((v, i) => (v - collision.data.min[i]).toFixed(3));
    const volume = size.reduce((a, b) => a * Number(b), 1);
    push("size", size.join(" - "));
    push("volume", volume.toFixed(3));
  } else if (collision.kind === "sphere") {
    push("radius", Number(collision.data.radius).toFixed(3));
    push("volume", (4 / 3 * Math.PI * Math.pow(collision.data.radius, 3)).toFixed(3));
  } else {
    push("points", collision.data.points.length);
  }
  push("created", collision.created_at.replace("T", " "));
  $("collision-info").innerHTML = `<div class="kv">${rows.join("")}</div>`;
}

/* - assets */

function fmt(n) { return Number(n || 0).toLocaleString(); }

// "FAILED" from the CLI and "failed" from AssetHealth are the same verdict.
const verdictOf = (asset) => String((asset && asset.verdict_status) || "").trim().toLowerCase();

async function refreshAssets() {
  const data = await json("/api/assets");
  state.assets = data.assets || [];
  const list = $("asset-list");
  if (!state.assets.length) {
    list.innerHTML = '<div class="empty">Nothing uploaded yet.<br>'
      + "Drop meshes, textures or source files anywhere.</div>";
    return;
  }
  list.innerHTML = "";
  const wanted = state.filter || "all";
  const visible = state.assets.filter((asset) => wanted === "all"
    ? true
    : wanted === "attention" ? verdictOf(asset) === "failed"
      : verdictOf(asset) === "passed");
  visible.forEach((asset) => {
    const element = document.createElement("div");
    element.className = "asset" + (state.asset && state.asset.id === asset.id ? " active" : "");
    element.dataset.id = asset.id;
    element.dataset.verdict = verdictOf(asset);
    // An unverified format (.blend, .max, .ma) has no triangle count at all.
    // Printing "0 tris" would read as a cheap mesh rather than an unmeasured
    // one, so it is named as unmeasured and its reason moved into the tooltip.
    const unverified = asset.kind === "unverified";
    const heavy = unverified ? ""
      : (asset.triangle_count > 50000 ? ' <span class="bad">!</span>' : "");
    const suffix = asset.filename.includes(".")
      ? asset.filename.slice(asset.filename.lastIndexOf(".")) : "";
    const geometry = unverified
      ? `<span title="${escapeHtml(asset.analyse_note || "needs a DCC to measure")}">`
        + `unmeasured ${escapeHtml(suffix)}</span>`
      : asset.kind === "texture"
        ? `texture  ${escapeHtml((asset.analyse_note || "").replace(/ texture$/, ""))}`
        : `${fmt(asset.triangle_count)} tris  ${fmt(asset.vertex_count)} verts  `
          + `${asset.material_slots} mat${heavy}`;
    // Straight from AssetHealth: reviewers should never have to open the
    // dashboard in another tab to know what they are looking at.
    const verdict = verdictOf(asset) === "failed"
      ? '<span class="verdict failed" title="AssetHealth: failed">FAILED</span>'
      : verdictOf(asset) === "passed"
        ? '<span class="verdict passed" title="AssetHealth: passed">PASSED</span>' : "";
    element.innerHTML =
      `<div class="name">${verdict}${escapeHtml(asset.filename)}</div>`
      + `<div class="meta">${geometry}</div>`
      + `<div class="meta">${asset.lod_count} LOD  ${asset.note_count} note`
      + `${asset.note_count === 1 ? "" : "s"}</div>`;
    element.addEventListener("click", () => selectAsset(asset.id));
    list.appendChild(element);
  });
  const stats = await json("/api/stats");
  // Measured and unverified are reported apart: the triangle total only covers
  // assets something actually counted, so it can never look artificially cheap.
  const unmeasured = stats.unverified
    ? `  ${stats.unverified} unmeasured (not in the tri total)` : "";
  $("stats").textContent =
    `${stats.assets} assets  ${fmt(stats.triangles)} tris  ${stats.lods} LODs  `
    + `${stats.notes} notes${unmeasured}`;
}

async function selectAsset(id) {
  const seq = ++state.selectSeq;
  let asset;
  try {
    asset = await json(`/api/assets/${id}`);
  } catch (error) {
    if (seq === state.selectSeq) $("status").textContent = `error: ${error.message}`;
    return;
  }
  if (seq !== state.selectSeq) return;     // a newer click already took over

  state.asset = asset;
  state.level = "source";
  clearModel();
  buildLodButtons();
  renderNotes();
  showVerdict(asset);
  updateReadout();
  refreshCollisionExport();
  await refreshCollisionOverlay();
  // Notes work for every asset: an unmeasured .blend is exactly the one a
  // reviewer needs to comment on.
  $("note-submit").disabled = false;
  $("note-text").disabled = false;
  $("collision-derive").disabled = true;    // needs a model on screen
  refreshAssets().catch(() => {});

  if (!previewable(asset)) {
    const what = asset.kind === "texture"
      ? `${asset.analyse_note || "texture"} - the 3D view shows meshes only.`
      : asset.kind === "unverified"
        ? (asset.analyse_note || "This format cannot be measured or drawn here.")
        : `${suffixOf(asset.filename)} files are measured but not drawn in the browser.`;
    showHint(`${asset.filename}: no 3D preview`,
             `${what} Its numbers, collision and notes are on the right.`);
    $("status").textContent = `${asset.filename} (no 3D preview)`;
    return;
  }
  showHint(`Loading ${asset.filename}`, "");
  $("status").textContent = `loading ${asset.filename}`;
  try {
    await loadModel(asset.id, "source");
    if (seq === state.selectSeq) $("status").textContent = asset.filename;
  } catch (error) {
    if (seq !== state.selectSeq) return;
    clearModel();
    showHint(`${asset.filename} could not be displayed`, error.message);
    $("status").textContent = `${asset.filename}: could not display it`;
  }
}

/* Re-read the selected asset without reloading its model (notes, collision,
 * LOD list): reloading reset the camera every time a note was added. */
async function refreshSelected() {
  if (!state.asset) return;
  const id = state.asset.id;
  const asset = await json(`/api/assets/${id}`);
  if (!state.asset || state.asset.id !== id) return;
  state.asset = asset;
  renderNotes();
  updateReadout();
  showVerdict(asset);
  await refreshCollisionOverlay();
  refreshAssets().catch(() => {});
}

function clearSelection() {
  state.selectSeq += 1;
  state.asset = null;
  state.level = "source";
  clearModel();
  $("lod-buttons").innerHTML = "";
  renderNotes();
  showVerdict(null);
  updateReadout();
  refreshCollisionExport();
  refreshCollisionOverlay().catch(() => {});
  $("note-submit").disabled = true;
  $("collision-derive").disabled = true;
  showHint("No asset loaded", "Pick one from the library, or drop a file here to upload it.");
}

function buildLodButtons() {
  const container = $("lod-buttons");
  container.innerHTML = "";
  const levels = [{ level: "source", label: "Source" }].concat(
    (state.asset.lods || []).map((lod) => ({
      level: lod.level,
      label: `${lod.level}  ${fmt(lod.triangle_count)}`,
    })));
  levels.forEach((item) => {
    const button = document.createElement("button");
    button.className = "chip" + (item.level === state.level ? " active" : "");
    button.dataset.level = item.level;
    button.textContent = item.label;
    button.addEventListener("click", async () => {
      state.level = item.level;
      Array.from(container.children).forEach((child) =>
        child.classList.toggle("active", child === button));
      try {
        await loadModel(state.asset.id, item.level);
        $("status").textContent = `${state.asset.filename}  ${item.level === "source" ? "" : item.level}`;
      } catch (error) {
        clearModel();
        showHint(`${item.level} could not be displayed`, error.message);
        $("status").textContent = `error: ${error.message}`;
      }
    });
    container.appendChild(button);
  });
}

function updateReadout() {
  const asset = state.asset;
  if (!asset) { $("readout").textContent = ""; return; }
  const lod = (asset.lods || []).find((item) => item.level === state.level);
  const tris = lod ? lod.triangle_count : asset.triangle_count;
  const verts = lod ? lod.vertex_count : asset.vertex_count;
  const share = lod && asset.triangle_count
    ? `${(tris / asset.triangle_count * 100).toFixed(1)}% of source` : "source";
  const geometry = asset.kind === "unverified"
    ? "<span>unmeasured - no triangle count</span>"
    : asset.kind === "texture"
      ? `<span>${escapeHtml(asset.analyse_note || "texture")}</span>`
    : `<span>${fmt(tris)} tris</span><span>${fmt(verts)} verts</span>`
      + `<span>${share}</span>`;
  $("readout").innerHTML =
    `<span>${state.level === "source" ? "source" : state.level}</span>`
    + geometry
    + `<span>uploaded ${asset.uploaded_at.replace("T", " ")}</span>`
    + `<span>by ${escapeHtml(asset.uploader || "unknown")}</span>`;
}

/* - notes */

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

/* Deliberately tiny: escape everything first, then re-introduce only the
 * constructs a note needs. No raw HTML ever reaches the DOM from a note. */
function renderMarkdown(text) {
  const escaped = escapeHtml(text);
  const lines = escaped.split(/\r?\n/);
  const out = [];
  let listOpen = false;
  for (const line of lines) {
    const item = line.match(/^\s*[-*]\s+(.*)$/);
    if (item) {
      if (!listOpen) { out.push("<ul>"); listOpen = true; }
      out.push(`<li>${inline(item[1])}</li>`);
      continue;
    }
    if (listOpen) { out.push("</ul>"); listOpen = false; }
    out.push(line.trim() ? `<div>${inline(line)}</div>` : "");
  }
  if (listOpen) out.push("</ul>");
  return out.join("");
}

function inline(text) {
  return text
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

/* - per-material instruments */

function buildMaterialControls() {
  const host = $("materials");
  host.innerHTML = "";
  const entries = [];
  if (state.sceneObjects.model) {
    state.sceneObjects.model.traverse((child) => {
      if (!child.isMesh || !child.material) return;
      const list = Array.isArray(child.material) ? child.material : [child.material];
      for (const material of list) {
        if (!material || entries.some((entry) => entry.material === material)) continue;
        entries.push({ material, name: material.name || `material ${entries.length}` });
      }
    });
  }
  if (!entries.length) {
    host.innerHTML = '<div class="empty">No materials on this asset.</div>';
    return;
  }

  for (const entry of entries) {
    const base = state.originals.get(entry.material) || {};
    const roughness = Number(base.roughness ?? entry.material.roughness ?? 0.5);
    const row = document.createElement("div");
    row.className = "mat";
    row.innerHTML =
      `<div class="mat-name" title="${escapeHtml(entry.name)}">${escapeHtml(entry.name)}</div>
       <label class="mat-row">exposure
         <input type="range" min="0.1" max="3" step="0.05" value="1" data-kind="exposure">
         <span class="val">1.00</span></label>
       <label class="mat-row">roughness
         <input type="range" min="0" max="1" step="0.01" value="${roughness.toFixed(2)}" data-kind="roughness">
         <span class="val">${roughness.toFixed(2)}</span></label>`;

    for (const input of row.querySelectorAll("input")) {
      input.addEventListener("input", () => {
        const value = Number(input.value);
        input.parentElement.querySelector(".val").textContent = value.toFixed(2);
        if (input.dataset.kind === "exposure") {
          // Exposure here is a per-material colour multiplier against the
          // pristine colour captured at load, so the slider is always reversible.
          if (base.color && entry.material.color) {
            entry.material.color.copy(base.color).multiplyScalar(value);
          }
        } else {
          entry.material.roughness = value;
        }
        entry.material.needsUpdate = true;
      });
    }
    host.appendChild(row);
  }
}

function refreshCollisionExport() {
  const link = $("collision-export");
  if (!state.asset) {
    link.style.display = "none";
    return;
  }
  link.style.display = "";
  link.href = `/api/assets/${state.asset.id}/collision.obj`
    + (state.token ? `?token=${encodeURIComponent(state.token)}` : "");
}

function renderNotes() {
  const container = $("note-list");
  const notes = (state.asset && state.asset.notes) || [];
  if (!notes.length) {
    container.innerHTML = '<div class="empty">No notes yet.</div>';
    return;
  }
  container.innerHTML = "";
  notes.forEach((note) => {
    const element = document.createElement("div");
    element.className = "note";
    element.innerHTML =
      `<div class="head"><span class="cat">${escapeHtml(note.category)}</span>`
      + `<span>${escapeHtml(note.author || "anonymous")}</span></div>`
      + `<div class="body">${renderMarkdown(note.note_text)}</div>`
      + `<div class="head" style="margin-top:6px"><span>`
      + `${escapeHtml(note.created_at.replace("T", " "))}</span>`
      + `<button class="chip" data-note="${note.id}" style="padding:2px 8px">`
      + `delete</button></div>`;
    element.querySelector("[data-note]").addEventListener("click", async () => {
      try {
        await api(`/api/notes/${note.id}`, { method: "DELETE" });
        await refreshSelected();
      } catch (error) { $("status").textContent = `error: ${error.message}`; }
    });
    container.appendChild(element);
  });
}

/* - upload */

async function uploadFile(file) {
  const query = new URLSearchParams({
    filename: file.name,
    uploader: $("note-author").value.trim() || "anonymous",
  });
  const response = await api(`/api/assets?${query}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: file,
  });
  return response.json();
}

/* Upload one or more files, one after another, and say what happened to each.
 * A refused file (an .exe, an oversized export) used to leave the status bar on
 * "uploading ..." with an uncaught error and nothing for the user to read. */
async function uploadFiles(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  const done = [];
  const refused = [];
  for (const [index, file] of list.entries()) {
    $("status").textContent = list.length > 1
      ? `uploading ${index + 1}/${list.length}: ${file.name}` : `uploading ${file.name}`;
    try {
      done.push(await uploadFile(file));
    } catch (error) {
      refused.push(`${file.name}: ${error.message}`);
    }
  }
  await refreshAssets().catch(() => {});
  if (done.length) await selectAsset(done[done.length - 1].id);
  const parts = [];
  if (done.length === 1) {
    const asset = done[0];
    parts.push(`${asset.filename} uploaded`
      + (asset.kind === "mesh" ? ` (${fmt(asset.triangle_count)} tris)` : ""));
  } else if (done.length) {
    parts.push(`${done.length} files uploaded`);
  }
  if (refused.length) parts.push(`refused: ${refused.join("; ")}`);
  $("status").textContent = parts.join(" - ");
}

/* - wiring */

$("upload").addEventListener("change", (event) => {
  const files = Array.from(event.target.files || []);
  event.target.value = "";
  uploadFiles(files);
});

["dragenter", "dragover"].forEach((name) =>
  host.addEventListener(name, (event) => {
    event.preventDefault();
    $("dropzone").classList.add("visible");
  }));
["dragleave", "drop"].forEach((name) =>
  host.addEventListener(name, (event) => {
    event.preventDefault();
    $("dropzone").classList.remove("visible");
  }));
host.addEventListener("drop", (event) => {
  uploadFiles(event.dataTransfer.files);
});

$("mode-textured").addEventListener("click", () => {
  state.geometryMode = false;
  $("mode-textured").classList.add("active");
  $("mode-geometry").classList.remove("active");
  applyMaterialMode();
});
$("mode-geometry").addEventListener("click", () => {
  state.geometryMode = true;
  $("mode-geometry").classList.add("active");
  $("mode-textured").classList.remove("active");
  applyMaterialMode();
});
$("wireframe").addEventListener("click", () => {
  state.wireframe = !state.wireframe;
  $("wireframe").classList.toggle("active", state.wireframe);
  applyWireframe();
});
$("collision").addEventListener("click", async () => {
  state.collision = !state.collision;
  $("collision").classList.toggle("active", state.collision);
  await refreshCollisionOverlay();
});
$("brightness").addEventListener("input", (event) => {
  const value = Number(event.target.value);
  renderer.toneMappingExposure = value;
  key.intensity = 2.4 * value;
  rim.intensity = 1.2 * value;
  $("brightness-out").textContent = value.toFixed(2);
});

$("collision-derive").addEventListener("click", async () => {
  if (!state.asset) return;
  const kind = $("collision-kind").value;
  try {
    let data;
    const model = state.sceneObjects.model;
    if (!model) return;
    model.updateMatrixWorld(true);
    if (kind === "box") {
      const box = new THREE.Box3().setFromObject(model);
      data = { min: box.min.toArray(), max: box.max.toArray() };
    } else if (kind === "sphere") {
      // The bounding sphere of the box - half its diagonal - so the proxy
      // contains the mesh. Half the largest side left a cube's corners outside.
      const sphere = new THREE.Box3().setFromObject(model).getBoundingSphere(new THREE.Sphere());
      data = { center: sphere.center.toArray(), radius: sphere.radius };
    } else {
      // Send the points that reach furthest in 64 directions, in world space.
      // Sampling every Nth vertex missed the extremes, ignored node transforms,
      // and sent up to 4,000 points that export then refused.
      const directions = [];
      const golden = Math.PI * (3 - Math.sqrt(5));
      for (let n = 0; n < 64; n += 1) {
        const y = 1 - 2 * (n + 0.5) / 64;
        const r = Math.sqrt(Math.max(0, 1 - y * y));
        directions.push(new THREE.Vector3(Math.cos(golden * n) * r, y, Math.sin(golden * n) * r));
      }
      const best = directions.map(() => ({ score: -Infinity, point: null }));
      const point = new THREE.Vector3();
      model.traverse((node) => {
        if (!node.isMesh || !node.geometry || !node.geometry.attributes.position) return;
        const position = node.geometry.attributes.position;
        for (let i = 0; i < position.count; i += 1) {
          point.fromBufferAttribute(position, i).applyMatrix4(node.matrixWorld);
          directions.forEach((direction, d) => {
            const score = point.dot(direction);
            if (score > best[d].score) best[d] = { score, point: point.toArray() };
          });
        }
      });
      data = { points: best.filter((item) => item.point).map((item) => item.point) };
    }
    await api(`/api/assets/${state.asset.id}/collision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, data }),
    });
    state.asset = await json(`/api/assets/${state.asset.id}`);
    await refreshCollisionOverlay();
  } catch (error) {
    $("status").textContent = `error: ${error.message}`;
  }
});

$("note-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.asset) return;
  const text = $("note-text").value.trim();
  if (!text) return;
  try {
    await api("/api/notes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        asset_id: state.asset.id,
        category: $("note-category").value,
        note_text: text,
        author: $("note-author").value.trim() || "anonymous",
      }),
    });
    $("note-text").value = "";
    await refreshSelected();
  } catch (error) {
    $("status").textContent = `error: ${error.message}`;
  }
});

/* - live events */

function connectLive() {
  if (!window.EventSource) return;
  const query = state.token ? `?token=${encodeURIComponent(state.token)}` : "";
  const source = new EventSource(`/api/events${query}`);

  source.addEventListener("asset", () => {
    refreshAssets().catch(() => {});
  });
  source.addEventListener("asset-deleted", (event) => {
    const payload = JSON.parse(event.data);
    if (state.asset && state.asset.id === payload.asset_id) {
      clearSelection();
      $("status").textContent = "the asset you were viewing was deleted";
    }
    refreshAssets().catch(() => {});
  });
  for (const type of ["notes", "collision", "lod"]) {
    source.addEventListener(type, (event) => {
      const payload = JSON.parse(event.data || "{}");
      if (!state.asset || (payload.asset_id && payload.asset_id !== state.asset.id)) return;
      // Someone else changed what this tab is looking at: re-read, don't guess.
      const id = state.asset.id;
      json(`/api/assets/${id}`).then((asset) => {
        if (!state.asset || state.asset.id !== id) return;
        const keepLevel = state.level;
        state.asset = asset;
        renderNotes();
        showVerdict(asset);
        refreshCollisionOverlay().catch(() => {});
        if (type === "lod") {
          state.level = keepLevel;
          buildLodButtons();
        }
      }).catch(() => {});
    });
  }
  source.onerror = () => {
    $("status").textContent = "live: reconnecting";
  };
}

/* - rules inspector */

/* Read-only card: what the gate would enforce for this project. It renders the
 * server's /api/rules payload and nothing else - no editing lives here, because
 * a budget that can be changed from a review screen is a budget nobody owns. */

const rules = { loaded: false, loading: false };

function textureCell(edge, powerOfTwo) {
  if (edge === null || edge === undefined) {
    return '<span class="na" title="not enforced by the gate">-</span>';
  }
  const label = `${fmt(edge)} - ${fmt(edge)}`;
  if (!powerOfTwo) return escapeHtml(label);
  return `<span title="power-of-two required">${escapeHtml(label)}</span>`;
}

/* A number the gate does not enforce is not zero, it is unknown: showing "-"
 * with an explanation beats inventing a limit an artist would optimise for. */
function numberCell(value, options = {}) {
  const { looser = false, title = "" } = options;
  if (value === null || value === undefined || value === "") {
    return '<span class="na" title="not enforced by the gate">-</span>';
  }
  const className = looser ? ' class="loose"' : "";
  const hint = title ? ` title="${escapeHtml(title)}"` : "";
  return `<span${className}${hint}>${fmt(value)}</span>`;
}

function renderRulesRow(label, kind, budget, baseline, isDefault) {
  const looser = (value, base) => !isDefault && base !== null && base !== undefined
                                && value !== null && value !== undefined && value > base;
  const triangleIsLoose = looser(budget.max_triangles, baseline.max_triangles);
  const cells = [
    `<td class="cat">${escapeHtml(label)}${kind ? `<span class="kind">${escapeHtml(kind)}</span>` : ""}</td>`,
    `<td class="num">${numberCell(budget.max_triangles, {
      looser: triangleIsLoose,
      title: triangleIsLoose ? "triangle budget is looser than the project default" : "",
    })}</td>`,
    `<td class="num">${numberCell(budget.max_vertices)}</td>`,
    `<td class="num">${textureCell(budget.max_texture_edge, budget.power_of_two)}</td>`,
    `<td class="num">${numberCell(budget.max_material_slots)}</td>`,
  ];
  return `<tr class="${isDefault ? "is-default" : ""}">${cells.join("")}</tr>`;
}

function renderRules(data) {
  const meta = $("rules-meta");
  const host = $("rules-table");
  const budget = data.global_budget;

  if (!budget) {
    const message = data.error || "no budget available";
    meta.innerHTML = `<span class="badge bad">rules unavailable</span>`
      + `<span class="badge">${escapeHtml(data.source || "unknown source")}</span>`;
    host.innerHTML = `<div class="rules-note">${escapeHtml(message)}</div>`;
    return;
  }

  const tags = Array.isArray(data.tag_budgets) ? data.tag_budgets : [];
  const platform = data.target_platform && data.target_platform !== "default"
    ? `<span class="badge"><strong>platform</strong> ${escapeHtml(data.target_platform)}</span>`
    : `<span class="badge" title="no [platforms.&lt;name&gt;] profile applied">`
      + `<strong>platform</strong> default</span>`;
  const tagMode = `<span class="badge${data.tag_mode === "strict" ? " warn" : ""}"`
    + ` title="how a folder name is matched to a tag">`
    + `<strong>tags</strong> ${escapeHtml(data.tag_mode || "suggest")}</span>`;
  const pot = budget.power_of_two
    ? `<span class="badge"><strong>textures</strong> power-of-two</span>`
    : `<span class="badge warn"><strong>textures</strong> any size</span>`;
  const degenerate = budget.allow_degenerate
    ? `<span class="badge warn"><strong>degenerate</strong> allowed</span>`
    : `<span class="badge"><strong>degenerate</strong> blocked</span>`;

  meta.innerHTML = [platform, tagMode, pot, degenerate].join("");

  host.innerHTML = `
    <table class="rules-table">
      <thead>
        <tr>
          <th>Category / tag</th>
          <th class="num">Triangles</th>
          <th class="num">Vertices</th>
          <th class="num">Texture</th>
          <th class="num">Slots</th>
        </tr>
      </thead>
      <tbody>
        ${renderRulesRow("Project default", "global budget", budget, budget, true)}
        ${tags.map((tag) => renderRulesRow(tag.tag, "tag", tag, budget, false)).join("")}
      </tbody>
    </table>
    ${tags.length ? "" : `<div class="rules-note">No <code>[tags.*]</code> tables in this rules file -`
      + ` every asset is measured against the project default.</div>`}
    <div class="rules-note">
      Read-only. Change these in <code>rules.toml</code> (or the AssetHealth budget planner),
      then press -.
      ${budget.max_vertices === null || budget.max_vertices === undefined
        ? "Vertex caps are not enforced by the gate, so they show as -."
        : ""}
    </div>
    <div class="rules-source">${escapeHtml(data.source || "")}</div>`;

  // The status bar names the asset on screen; the rules never overwrite that.
  if (!state.asset) {
    $("status").textContent = tags.length
      ? `${tags.length} tag budget${tags.length === 1 ? "" : "s"} loaded`
      : "rules loaded";
  }
}

async function loadRules(force = false) {
  if (rules.loading || (rules.loaded && !force)) return;
  rules.loading = true;
  if (force || !rules.loaded) {
    $("rules-meta").innerHTML = '<div class="empty">Loading rules</div>';
  }
  try {
    renderRules(await json("/api/rules"));
    rules.loaded = true;
  } catch (error) {
    $("rules-meta").innerHTML =
      `<span class="badge bad">cannot load rules</span>`;
    $("rules-table").innerHTML =
      `<div class="rules-note">${escapeHtml(error.message)}</div>`;
  } finally {
    rules.loading = false;
  }
}

function setTab(name) {
  for (const tab of document.querySelectorAll(".tab")) {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  }
  $("pane-assets").classList.toggle("is-hidden", name !== "assets");
  $("pane-rules").classList.toggle("is-hidden", name !== "rules");
  document.body.classList.toggle("rules-open", name === "rules");
  if (window.location.hash !== `#${name}`) {
    window.history.replaceState(null, "", `#${name}`);
  }
  if (name === "rules") loadRules();
}

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => setTab(tab.dataset.tab));
}
$("rules-refresh").addEventListener("click", () => loadRules(true));

/* `#rules` opens the tab directly, so a lead can paste a link that lands on the
 * budgets instead of the asset list. `?tab=rules` does the same and survives the
 * tools that strip a fragment (headless capture, some chat clients). */
/* - gate verdicts, carried over from AssetHealth */
state.filter = state.filter || "all";

function applyVerdictFilter() {
  const wanted = state.filter;
  for (const row of $("asset-list").querySelectorAll(".asset")) {
    const verdict = row.dataset.verdict || "";
    row.hidden = !(wanted === "all"
      || (wanted === "attention" && verdict === "failed")
      || (wanted === "passed" && verdict === "passed"));
  }
}

for (const button of document.querySelectorAll("button.filter")) {
  button.addEventListener("click", () => {
    for (const other of document.querySelectorAll("button.filter")) {
      other.classList.toggle("is-active", other === button);
    }
    state.filter = button.dataset.filter || "all";
    applyVerdictFilter();
  });
}
// Rows are re-rendered after every upload, so re-apply rather than wiring each one.
new MutationObserver(applyVerdictFilter).observe($("asset-list"), { childList: true });

function showVerdict(assetOrId) {
  // Called with the selected asset by selectAsset - so uploads, live updates
  // and ?asset= links show it too, not only a click on a library card.
  const body = $("verdict-body");
  const asset = assetOrId && typeof assetOrId === "object" ? assetOrId
    : (state.assets || []).find((item) => String(item.id) === String(assetOrId));
  if (!asset) {
    body.innerHTML = '<div class="empty">No asset selected.</div>';
    return;
  }
  if (!verdictOf(asset)) {
    body.innerHTML = '<div class="empty">No gate verdict on this asset. '
      + 'Ones sent from the AssetHealth dashboard carry one.</div>';
    return;
  }
  const problems = String(asset.verdict_problems || "").split("\n").filter(Boolean);
  body.innerHTML =
    `<div class="verdict--line ${escapeHtml(verdictOf(asset))}">`
    + `${escapeHtml(verdictOf(asset).toUpperCase())}</div>`
    + (problems.length
      ? `<ul class="verdict-problems">${problems.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>`
      : '<div class="sub">No rule was broken.</div>')
    + (asset.verdict_tags ? `<div class="verdict-tags">Category: ${escapeHtml(asset.verdict_tags)}</div>` : "")
    + (asset.source ? `<div class="verdict-source">sent from ${escapeHtml(asset.source)}</div>` : "");
}


/* - boot */

resize();
animate();
(async () => {
  try {
    const health = await json("/api/health");
    $("status").textContent = health.auth ? "token mode" : "solo mode";
    await refreshAssets();

    // ?live=0 turns the stream off: headless screenshots and kiosk displays never
    // finish loading while an EventSource holds the connection open.
    const params = new URLSearchParams(location.search);
    if (params.get("live") !== "0") connectLive();

    // After the first API call, so a token prompt appears once, not per panel.
    const requestedTab = (params.get("tab") || window.location.hash || "")
      .replace(/^#/, "").toLowerCase();
    if (requestedTab === "rules" || requestedTab === "budgets") {
      setTab("rules");
    } else {
      loadRules();        // fetch on load, as specified: the panel is pre-populated
    }

    // ?asset=<id> is a shareable review link: "look at asset 12, LOD2".
    const wanted = Number(params.get("asset"));
    if (Number.isFinite(wanted) && wanted > 0) {
      await selectAsset(wanted);
      const level = params.get("lod");
      if (level) {
        const button = Array.from($("lod-buttons").children)
          .find((child) => child.dataset.level === level
                        || child.textContent.startsWith(level));
        if (button) button.click();
      }
    }
  } catch (error) {
    $("status").textContent = `cannot reach the server: ${error.message}`;
  }
})();
