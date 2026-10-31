/* Art Workstation viewer.
 *
 * Three.js comes from a CDN (the only external dependency in the whole system);
 * everything else — routing, auth, notes, collision — is the stdlib server.
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
  originals: new Map(), // material -> {color, map, ...} for the textured restore
};

/* ------------------------------------------------------------------ auth */

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

/* --------------------------------------------------------------- three.js */

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

/* ---------------------------------------------------------------- loading */

const gltfLoader = new GLTFLoader();
const objLoader = new OBJLoader();

function disposeObject(object) {
  if (!object) return;
  object.traverse((node) => {
    if (node.geometry) node.geometry.dispose();
    if (node.material) {
      const materials = Array.isArray(node.material) ? node.material : [node.material];
      materials.forEach((material) => {
        for (const value of Object.values(material)) {
          if (value && value.isTexture) value.dispose();
        }
        material.dispose();
      });
    }
  });
  scene.remove(object);
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
  const path = level === "source"
    ? `/api/assets/${assetId}/file`
    : `/api/assets/${assetId}/lods/${level}/file`;
  const response = await api(path);
  const buffer = await response.arrayBuffer();
  const name = level === "source"
    ? (state.asset ? state.asset.filename : "") : `${level}.gltf`;
  const lower = name.toLowerCase();

  $("viewport-hint").style.display = "none";
  disposeObject(state.sceneObjects.model);

  let object;
  if (lower.endsWith(".obj")) {
    object = objLoader.parse(new TextDecoder().decode(buffer));
  } else {
    const gltf = await new Promise((resolve, reject) =>
      gltfLoader.parse(buffer, "", resolve, reject));
    object = gltf.scene;
  }

  state.originals.clear();
  object.traverse((node) => {
    if (!node.isMesh) return;
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
  applyWireframe();
  buildMaterialControls();
  refreshCollisionExport();
  updateReadout();
  await refreshCollisionOverlay();
}

/* -------------------------------------------------------------- materials */

function applyMaterialMode() {
  const object = state.sceneObjects.model;
  if (!object) return;
  object.traverse((node) => {
    if (!node.isMesh) return;
    if (state.geometryMode) {
      node.material = new THREE.MeshNormalMaterial({ flatShading: false });
    } else {
      const materials = Array.isArray(node.material) ? node.material : [node.material];
      const restore = (material) => {
        const original = state.originals.get(material);
        if (!original) {
          // LODs are written without materials; give them something readable.
          material.color.set(0.62, 0.64, 0.68);
          material.metalness = 0.05;
          material.roughness = 0.7;
          return;
        }
        if (original.color) material.color.copy(original.color);
        if (original.map) { material.map = original.map; material.needsUpdate = true; }
        if (original.metalness !== undefined) material.metalness = original.metalness;
        if (original.roughness !== undefined) material.roughness = original.roughness;
      };
      materials.forEach(restore);
    }
  });
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
}

/* --------------------------------------------------------------- collision */

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
  if (!state.asset) return;
  $("collision-info").innerHTML = "";
  const collision = state.asset.collision;
  if (!collision) {
    $("collision-info").innerHTML =
      '<div class="empty">No proxy yet. Pick a type and rebuild.</div>';
    return;
  }
  renderCollisionInfo(collision);
  if (!state.collision) return;
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
    push("size", size.join(" × "));
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

/* ------------------------------------------------------------------ assets */

function fmt(n) { return Number(n || 0).toLocaleString(); }

async function refreshAssets() {
  const data = await json("/api/assets");
  state.assets = data.assets || [];
  const list = $("asset-list");
  if (!state.assets.length) {
    list.innerHTML = '<div class="empty">Nothing uploaded yet.<br>'
      + "Drop a .obj / .gltf / .glb anywhere.</div>";
    return;
  }
  list.innerHTML = "";
  state.assets.forEach((asset) => {
    const element = document.createElement("div");
    element.className = "asset" + (state.asset && state.asset.id === asset.id ? " active" : "");
    element.dataset.id = asset.id;
    const heavy = asset.triangle_count > 50000 ? ' <span class="bad">!</span>' : "";
    element.innerHTML =
      `<div class="name">${escapeHtml(asset.filename)}</div>`
      + `<div class="meta">${fmt(asset.triangle_count)} tris · `
      + `${fmt(asset.vertex_count)} verts · ${asset.material_slots} mat${heavy}</div>`
      + `<div class="meta">${asset.lod_count} LOD · ${asset.note_count} note`
      + `${asset.note_count === 1 ? "" : "s"}</div>`;
    element.addEventListener("click", () => selectAsset(asset.id));
    list.appendChild(element);
  });
  const stats = await json("/api/stats");
  $("stats").textContent =
    `${stats.assets} assets · ${fmt(stats.triangles)} tris · ${stats.lods} LODs · `
    + `${stats.notes} notes`;
}

async function selectAsset(id) {
  try {
    state.asset = await json(`/api/assets/${id}`);
    state.level = "source";
    buildLodButtons();
    renderNotes();
    await refreshAssets();
    await loadModel(id, "source");
    $("status").textContent = state.asset.filename;
    $("note-submit").disabled = false;
    $("note-text").disabled = false;
    $("collision-derive").disabled = false;
  } catch (error) {
    $("status").textContent = `error: ${error.message}`;
  }
}

function buildLodButtons() {
  const container = $("lod-buttons");
  container.innerHTML = "";
  const levels = [{ level: "source", label: "Source" }].concat(
    (state.asset.lods || []).map((lod) => ({
      level: lod.level,
      label: `${lod.level} · ${fmt(lod.triangle_count)}`,
    })));
  levels.forEach((item) => {
    const button = document.createElement("button");
    button.className = "chip" + (item.level === state.level ? " active" : "");
    button.textContent = item.label;
    button.addEventListener("click", async () => {
      state.level = item.level;
      Array.from(container.children).forEach((child) =>
        child.classList.toggle("active", child === button));
      try {
        await loadModel(state.asset.id, item.level);
      } catch (error) {
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
  $("readout").innerHTML =
    `<span>${state.level === "source" ? "source" : state.level}</span>`
    + `<span>${fmt(tris)} tris</span><span>${fmt(verts)} verts</span>`
    + `<span>${share}</span><span>uploaded ${asset.uploaded_at.replace("T", " ")}</span>`
    + `<span>by ${escapeHtml(asset.uploader || "unknown")}</span>`;
}

/* ------------------------------------------------------------------- notes */

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

/* ------------------------------------------------ per-material instruments */

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
        await selectAsset(state.asset.id);
      } catch (error) { $("status").textContent = `error: ${error.message}`; }
    });
    container.appendChild(element);
  });
}

/* ------------------------------------------------------------------ upload */

async function uploadFile(file) {
  const query = new URLSearchParams({
    filename: file.name,
    uploader: $("note-author").value.trim() || "anonymous",
  });
  $("status").textContent = `uploading ${file.name}…`;
  const response = await api(`/api/assets?${query}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: file,
  });
  const asset = await response.json();
  await refreshAssets();
  await selectAsset(asset.id);
  $("status").textContent = `${asset.filename} · ${fmt(asset.triangle_count)} tris`;
}

/* ------------------------------------------------------------------ wiring */

$("upload").addEventListener("change", (event) => {
  if (event.target.files[0]) uploadFile(event.target.files[0]);
  event.target.value = "";
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
  const file = event.dataTransfer.files[0];
  if (file) uploadFile(file).catch((error) => {
    $("status").textContent = `error: ${error.message}`;
  });
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
    if (kind === "box") {
      const box = new THREE.Box3().setFromObject(state.sceneObjects.model);
      data = { min: box.min.toArray(), max: box.max.toArray() };
    } else if (kind === "sphere") {
      const box = new THREE.Box3().setFromObject(state.sceneObjects.model);
      const center = box.getCenter(new THREE.Vector3());
      const size = box.getSize(new THREE.Vector3());
      data = { center: center.toArray(), radius: Math.max(size.x, size.y, size.z) / 2 };
    } else {
      const points = [];
      state.sceneObjects.model.traverse((node) => {
        if (!node.isMesh) return;
        const position = node.geometry.attributes.position;
        const step = Math.max(1, Math.floor(position.count / 400));
        for (let i = 0; i < position.count; i += step) {
          points.push([position.getX(i), position.getY(i), position.getZ(i)]);
        }
      });
      data = { points: points.slice(0, 4000) };
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
    await selectAsset(state.asset.id);
  } catch (error) {
    $("status").textContent = `error: ${error.message}`;
  }
});

/* -------------------------------------------------------------- live events */

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
      state.asset = null;
      disposeObject(state.sceneObjects.model);
      state.sceneObjects.model = null;
      $("viewport-hint").style.display = "";
      $("note-submit").disabled = true;
      $("readout").textContent = "";
    }
    refreshAssets().catch(() => {});
  });
  for (const type of ["notes", "collision", "lod"]) {
    source.addEventListener(type, (event) => {
      const payload = JSON.parse(event.data || "{}");
      if (!state.asset || (payload.asset_id && payload.asset_id !== state.asset.id)) return;
      // Someone else changed what this tab is looking at: re-read, don't guess.
      json(`/api/assets/${state.asset.id}`).then((asset) => {
        const keepLevel = state.level;
        state.asset = asset;
        renderNotes();
        refreshCollisionOverlay().catch(() => {});
        if (type === "lod") {
          state.level = keepLevel;
          buildLodButtons();
        }
      }).catch(() => {});
    });
  }
  source.onerror = () => {
    $("status").textContent = "live: reconnecting…";
  };
}

/* -------------------------------------------------------------------- boot */

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
/* ------------------------------------------------------- rules inspector */

/* Read-only card: what the gate would enforce for this project. It renders the
 * server's /api/rules payload and nothing else - no editing lives here, because
 * a budget that can be changed from a review screen is a budget nobody owns. */

const rules = { loaded: false, loading: false };

function textureCell(edge, powerOfTwo) {
  if (edge === null || edge === undefined) {
    return '<span class="na" title="not enforced by the gate">—</span>';
  }
  const label = `${fmt(edge)} × ${fmt(edge)}`;
  if (!powerOfTwo) return escapeHtml(label);
  return `<span title="power-of-two required">${escapeHtml(label)}</span>`;
}

/* A number the gate does not enforce is not zero, it is unknown: showing "—"
 * with an explanation beats inventing a limit an artist would optimise for. */
function numberCell(value, options = {}) {
  const { looser = false, title = "" } = options;
  if (value === null || value === undefined || value === "") {
    return '<span class="na" title="not enforced by the gate">—</span>';
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
      then press ↻.
      ${budget.max_vertices === null || budget.max_vertices === undefined
        ? "Vertex caps are not enforced by the gate, so they show as —."
        : ""}
    </div>
    <div class="rules-source">${escapeHtml(data.source || "")}</div>`;

  $("status").textContent = tags.length
    ? `${tags.length} tag budget${tags.length === 1 ? "" : "s"} loaded`
    : "rules loaded";
}

async function loadRules(force = false) {
  if (rules.loading || (rules.loaded && !force)) return;
  rules.loading = true;
  if (force || !rules.loaded) {
    $("rules-meta").innerHTML = '<div class="empty">Loading rules…</div>';
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
const requestedTab = (new URLSearchParams(window.location.search).get("tab")
  || window.location.hash || "").replace(/^#/, "").toLowerCase();
if (requestedTab === "rules" || requestedTab === "budgets") {
  setTab("rules");
} else {
  loadRules();          // fetch on load, as specified: the panel is pre-populated
}

if (params.get("live") !== "0") connectLive();

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
