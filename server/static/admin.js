/* AssetGatekeeper admin: rules, presets and the rescan stream.
 *
 * Rules of engagement, in the same spirit as the gate: nothing is sent to the
 * server until a button is pressed, every failure states what to fix, and the
 * page never pretends a save happened (it only clears the dirty flag when the
 * server says 200).
 */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  csrf: "",
  user: null,
  presets: [],
  presetName: "",      // which formula the numbers came from (recorded in the audit)
  document: null,      // the rules document as the server has it
  original: "",        // serialised snapshot, for the dirty flag
  scan: null,
  stream: null,
};

/* - project */

function setProjectStatus(info) {
  const parts = [];
  parts.push(`rules.toml: ${info.rules_path}`);
  parts.push(info.rules_exists ? "found" : "will be created on save");
  $("project-status").textContent = parts.join(" - ");
  $("project-status").style.color = info.rules_error ? "var(--bad)" : "var(--muted)";
}

function renderRecent(rows) {
  const host = $("recent");
  host.innerHTML = "";
  for (const row of rows || []) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "small ghost";
    chip.textContent = row.label || row.path;
    chip.title = row.path;
    chip.addEventListener("click", () => {
      $("project-path").value = row.path;
      useProject();
    });
    host.appendChild(chip);
  }
  if (!(rows || []).length) host.innerHTML = `<span class="help">none yet</span>`;
}

async function useProject() {
  const path = $("project-path").value.trim();
  if (!path) { notice("Type or pick a folder first.", "warn"); return; }
  $("project-use").disabled = true;
  try {
    const info = await api("/admin/api/project", { method: "POST", json: { path } });
    $("where").textContent = info.rules_path;
    setProjectStatus(info);
    state.document = info.document || {};
    fillForm(state.document);
    renderRecent(info.recent);
    // The form now describes *that* file, not a preset. Leaving the dropdown on
    // "Nintendo 64" while the numbers show 50,000 is how a director saves a
    // budget they did not intend.
    $("preset").value = "";
    state.presetName = "";
    $("target").value = info.rules_exists ? "from rules.toml" : "- no rules.toml yet -";
    $("preset-notes").textContent = info.rules_exists
      ? "Numbers loaded from the project's rules.toml."
      : "No rules.toml here yet - pick a preset or type the numbers, then Save.";
    $("scan-root").textContent = info.project;
    notice(info.rules_exists
      ? `Project set: ${info.project} - existing rules.toml loaded.`
      : `Project set: ${info.project} - no rules.toml yet; Save will create one.`,
      "ok");
  } catch (error) {
    notice(`That folder cannot be used: ${error.message}`, "err");
  } finally {
    $("project-use").disabled = false;
  }
}

/* - first-run wizard (10 Nov 2026) */
// A first start asked for nothing and offered a page of budget fields; a new
// user pressed Scan with no project picked and got the tool's own test files.
// The wizard asks three things - folder, engine, target - then saves and scans.

const ENGINE_HELP = {
  unreal: "Names are checked the Unreal way: <code>SM_</code>/<code>SK_</code> meshes, "
    + "<code>T_</code> textures with a map suffix (<code>_D</code>, <code>_N</code>, <code>_BaseColor</code>...).",
  unity: "Texture names need a Unity map suffix (<code>_Albedo</code>, <code>_Normal</code>, "
    + "<code>_MaskMap</code>...); meshes are not name-checked.",
  godot: "Texture names need a Godot map suffix (<code>_albedo</code>, <code>_normal</code>, "
    + "<code>_orm</code>...); meshes are not name-checked.",
  "": "Texture names need a common map suffix (<code>_BaseColor</code>, <code>_Normal</code>, "
    + "<code>_Albedo</code>...).",
};

function wizardEngine() {
  const picked = document.querySelector('input[name="wiz-engine"]:checked');
  return picked ? picked.value : "";
}

function setupWizard(data) {
  const params = new URLSearchParams(window.location.search);
  const first = !data.project && !(data.recent_projects || []).length;
  if (!(first || params.get("setup") === "1")) return;
  $("wizard").hidden = false;
  const allowed = $("wiz-allowed");
  const seen = new Set();
  for (const root of data.allowed_roots || []) {
    const key = String(root).toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    const option = document.createElement("option");
    option.value = root;
    option.textContent = root;
    allowed.appendChild(option);
  }
  allowed.addEventListener("change", (event) => {
    if (event.target.value) $("wiz-path").value = event.target.value;
    $("wiz-keep-row").hidden = true;
  });
  $("wiz-path").addEventListener("input", () => { $("wiz-keep-row").hidden = true; });
  const target = $("wiz-target");
  for (const preset of state.presets) {
    const option = document.createElement("option");
    option.value = preset.name;
    option.textContent = `${preset.label} - ${preset.character_triangles.toLocaleString()} `
      + `triangles, ${preset.max_texture_edge} px`;
    target.appendChild(option);
  }
  target.value = state.presets.some((row) => row.name === "pc") ? "pc" : (state.presets[0] || {}).name;
  document.querySelectorAll('input[name="wiz-engine"]').forEach((input) =>
    input.addEventListener("change", () => { $("wiz-engine-help").innerHTML = ENGINE_HELP[wizardEngine()]; }));
  $("wiz-go").addEventListener("click", runWizard);
  $("wiz-skip").addEventListener("click", () => { $("wizard").hidden = true; });
}

async function runWizard() {
  const path = $("wiz-path").value.trim();
  if (!path) { notice("Type your project folder, or pick an allowed one.", "warn"); return; }
  $("wiz-go").disabled = true;
  try {
    const info = await api("/admin/api/project", { method: "POST", json: { path } });
    // A project that already has rules: ask once whether to keep its numbers.
    if (info.rules_exists && $("wiz-keep-row").hidden) {
      $("wiz-keep-row").hidden = false;
      notice(`${info.project} already has a rules.toml. Keep its numbers, or untick the box `
        + "to use the budget you picked; then press the button again.", "warn");
      return;
    }
    $("project-path").value = info.project;
    $("where").textContent = info.rules_path;
    setProjectStatus(info);
    renderRecent(info.recent);
    state.document = info.document || {};
    fillForm(state.document);
    const keep = info.rules_exists && $("wiz-keep").checked;
    if (!keep) applyPreset($("wiz-target").value);
    const engine = wizardEngine();
    const document = budgetDocument();
    if (engine) document.naming = { preset: [engine] };
    const result = await api("/admin/api/apply", {
      method: "POST",
      json: { document, action: "rescan",
              summary: `first-run setup: ${engine || "other engine"}, `
                + (keep ? "kept the project's rules" : `preset ${$("wiz-target").value}`) },
    });
    $("wizard").hidden = true;
    markClean();
    notice(`Set up ${info.project}: ${engine ? engine[0].toUpperCase() + engine.slice(1) : "common"} `
      + `naming, ${keep ? "its own rules.toml" : $("wiz-target").selectedOptions[0].textContent}. `
      + "The first check is running below.", "ok");
    await loadState(false);
    if (result.scan && result.scan.started) watchScan();
    else if (result.scan && result.scan.blocked) notice(result.scan.blocked, "warn");
    $("scan-log").scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (error) {
    notice(`That did not work: ${error.message}`, "err");
  } finally {
    $("wiz-go").disabled = false;
  }
}

/* - helpers */

function notice(message, kind = "ok") {
  const banner = $("banner");
  if (!message) { banner.hidden = true; return; }
  banner.textContent = message;
  banner.className = `notice ${kind}`;
  banner.hidden = false;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

async function api(path, options = {}) {
  const headers = Object.assign({ "Accept": "application/json" }, options.headers || {});
  if (state.csrf && options.method && options.method !== "GET") {
    headers["X-CSRF-Token"] = state.csrf;
  }
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
    delete options.json;
  }
  const response = await fetch(path, Object.assign({}, options, { headers }));
  if (response.status === 401) {
    window.location.href = "/admin/login";
    throw new Error("signed out");
  }
  let body = null;
  try { body = await response.json(); } catch (_) { /* not JSON */ }
  if (!response.ok) {
    const detail = (body && (body.error || body.detail)) || response.statusText;
    throw new Error(detail);
  }
  return body;
}

/* - the form */

function budgetDocument() {
  // No `platform` key on purpose: the preset is a formula that fills these
  // numbers, not an overlay pointing at a [platforms.*] table. Writing a name
  // the gate cannot resolve would make the whole file refuse to load.
  return {
    mesh: {
      max_triangles: Number($("char-tris").value || 1),
      max_material_slots: Number($("slots").value || 1),
      max_uv_channels: Number($("uvs").value || 1),
      max_bone_influences: Number($("influences").value || 1),
      check_references: $("refs").checked,
      allow_degenerate: $("degenerate").checked,
    },
    texture: {
      max_edge: Number($("tex-edge").value || 1),
      power_of_two: $("pot").checked,
      pbr: $("pbr").checked,
      naming: $("naming").checked,
    },
    tagging: { mode: $("tag-mode").value },
    tags: collectTags(),
  };
}

function collectTags() {
  const tags = {};
  for (const row of $("tags").querySelectorAll("tr")) {
    // The "no categories yet" row is a <tr> too, and it has no inputs. Reading
    // .value from the null it returns is what broke Save on a project without
    // any [tags.*] entries, so skip any row that is not an editable one.
    const nameInput = row.querySelector("input[data-kind=name]");
    const trisInput = row.querySelector("input[data-kind=tris]");
    if (!nameInput || !trisInput) continue;
    const name = nameInput.value.trim();
    const tris = Number(trisInput.value || 0);
    if (!name || !tris) continue;
    tags[name] = { max_triangles: tris };
  }
  return tags;
}

function renderTags(tags) {
  const body = $("tags");
  body.innerHTML = "";
  const entries = Object.entries(tags || {});
  if (!entries.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="3" class="sub">`
      + `No categories - every asset uses the project default. Add one below `
      + `(the folder or filename prefix, e.g. <code>actors</code>).</td></tr>`;
    return;
  }
  for (const [name, budget] of entries) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><input data-kind="name" value="${escapeHtml(name)}"></td>
      <td class="num"><input data-kind="tris" type="number" min="1" step="1"
           value="${Number(budget.max_triangles || 1)}"></td>
      <td class="num"><button type="button" class="small ghost" data-remove>remove</button></td>`;
    row.querySelector("[data-remove]").addEventListener("click", () => {
      row.remove();
      markDirty();
    });
    row.querySelectorAll("input").forEach((input) => input.addEventListener("input", markDirty));
    body.appendChild(row);
  }
}

function fillForm(document) {
  const mesh = document.mesh || {};
  const texture = document.texture || {};
  const tags = document.tags || {};
  $("tag-mode").value = (document.tagging && document.tagging.mode) || "suggest";
  $("slots").value = mesh.max_material_slots || 3;
  $("uvs").value = mesh.max_uv_channels || 4;
  $("influences").value = mesh.max_bone_influences || 4;
  $("tex-edge").value = texture.max_edge || 2048;
  $("pot").checked = texture.power_of_two !== false;
  $("pbr").checked = texture.pbr !== false;
  $("naming").checked = texture.naming !== false;
  $("refs").checked = mesh.check_references !== false;
  $("degenerate").checked = !!mesh.allow_degenerate;

  // The two headline numbers are the character/prop categories; the base budget
  // is the character figure, so the form cannot disagree with itself.
  const character = tags.character ? tags.character.max_triangles : (mesh.max_triangles || 50000);
  const prop = tags.prop ? tags.prop.max_triangles : Math.round((mesh.max_triangles || 50000) / 4);
  $("char-tris").value = character;
  $("prop-tris").value = prop;

  const rest = Object.assign({}, tags);
  delete rest.character;
  delete rest.prop;
  renderTags(rest);
  markClean();
}

function syncCharacterBudget() {
  // The base budget follows the character cap; props are the exception, not the rule.
  const character = Number($("char-tris").value || 0);
  if (character > 0) state.character = character;
}

function markDirty() {
  state.character = state.character || 0;
  syncCharacterBudget();
  $("dirty").textContent = "unsaved changes";
  $("dirty").style.color = "var(--warn)";
}

function markClean() {
  $("dirty").textContent = "all changes saved";
  $("dirty").style.color = "var(--muted)";
}

function applyPreset(name) {
  const preset = state.presets.find((row) => row.name === name);
  if (!preset) { $("preset-notes").textContent = ""; return; }
  $("char-tris").value = preset.character_triangles;
  $("prop-tris").value = preset.prop_triangles;
  $("tex-edge").value = preset.max_texture_edge;
  $("slots").value = preset.max_material_slots;
  $("pot").checked = !!preset.power_of_two;
  $("pbr").checked = !!preset.pbr;
  state.presetName = preset.name;
  $("target").value = `${preset.label} (${preset.name})`;
  $("preset-notes").textContent =
    `${preset.label}: ${preset.notes || ""} - these numbers are written directly; no platform overlay is set.`;
  markDirty();
}

/* - actions */

async function saveWithRescan(rescan) {
  const buttons = [$("save"), $("apply-scan")];
  buttons.forEach((button) => { button.disabled = true; });
  notice(rescan ? "Applying and starting a rescan" : "Applying", "warn");
  try {
    const result = await api("/admin/api/apply", {
      method: "POST",
      json: { document: budgetDocument(),
              action: rescan ? "rescan" : "save",
              summary: state.presetName ? `preset ${state.presetName}` : "manual edit" },
    });
    markClean();
    const changed = (result.changed || []).length;
    const notes = (result.notes || []).join(" ");
    notice((changed
      ? `Applied ${changed} change${changed === 1 ? "" : "s"} to ${result.path}.`
      : `No changes - ${result.path} already matches.`) + (notes ? ` ${notes}` : ""),
      notes ? "warn" : "ok");
    await loadState(false);
    if (result.scan && result.scan.started) watchScan();
    else if (rescan && result.scan && result.scan.blocked) notice(result.scan.blocked, "warn");
    else if (rescan) notice("A scan was already running; it was left alone.", "warn");
  } catch (error) {
    notice(`Could not apply: ${error.message}`, "err");
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

/* - scan stream */

function scanLine(text, kind = "muted") {
  const line = document.createElement("div");
  line.className = kind;
  line.textContent = text;
  const log = $("scan-log");
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function setCounts(totals) {
  $("c-total").textContent = totals.total || 0;
  $("c-passed").textContent = totals.passed || 0;
  $("c-failed").textContent = totals.failed || 0;
}

function watchScan() {
  if (state.stream) return;
  const stream = new EventSource("/admin/api/scan/events");
  state.stream = stream;
  $("scan-status").textContent = "running";

  stream.onmessage = (message) => {
    let event;
    try { event = JSON.parse(message.data); } catch (_) { return; }
    if (event.type === "scan_start") {
      $("scan-log").innerHTML = "";
      setCounts({ total: 0, passed: 0, failed: 0 });
      $("scan-status").textContent = "running";
      scanLine(`scan started: ${event.root}`, "muted");
    } else if (event.type === "asset") {
      const label = `${event.passed ? "PASS" : "FAIL"}  ${event.name}  ${event.metric || ""}`;
      scanLine(event.passed ? label : `${label}  ${(event.problems || []).join("; ")}`,
               event.passed ? "passed" : "failed");
      setCounts(event.totals || {});
    } else if (event.type === "log") {
      scanLine(event.text, "muted");
    } else if (event.type === "skipped") {
      scanLine(event.text, "muted");
    } else if (event.type === "scan_end") {
      setCounts(event.totals || {});
      $("scan-status").textContent = event.status === "done" ? "complete" : "failed";
      const skipped = (event.skipped && event.skipped.total)
        ? `, ${event.skipped.total} skipped (not a supported type)` : "";
      scanLine(event.status === "done"
        ? `done: ${event.totals.passed} passed, ${event.totals.failed} failed${skipped}`
        : `scan failed: ${event.error || "see the server log"}`, "muted");
      stream.close();
      state.stream = null;
    }
  };
  stream.onerror = () => {
    // EventSource retries by itself; a clean end closes above.
    $("scan-status").textContent = "reconnecting";
  };
}

/* - boot */

async function loadState(firstTime = true) {
  const data = await api("/admin/api/state");
  state.csrf = data.csrf;
  state.user = data.user;
  state.presets = data.presets || [];
  state.document = data.document || {};
  $("who").textContent = data.solo ? "Solo mode: just you on this PC"
    : `${data.user.username}  ${data.user.role}`;
  $("accounts-on").hidden = !data.solo;
  $("logout").hidden = !!data.solo;     // nothing to sign out of
  $("where").textContent = data.rules_path;
  $("scan-root").textContent = data.scan_root || "your project folder (pick it above)";
  $("project-path").value = data.project || data.scan_root || "";
  setProjectStatus({ rules_path: data.rules_path, rules_exists: data.rules_exists,
                     rules_error: data.rules_error });
  renderRecent(data.recent_projects);

  if (firstTime) {
    const select = $("preset");
    for (const preset of state.presets) {
      const option = document.createElement("option");
      option.value = preset.name;
      option.textContent = `${preset.label} - ${preset.character_triangles.toLocaleString()} char`;
      select.appendChild(option);
    }
    setupWizard(data);
    $("project-use").addEventListener("click", useProject);
    $("project-path").addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); useProject(); }
    });
    const allowed = $("allowed-roots");
    const seenRoots = new Set();
    for (const root of data.allowed_roots || []) {
      const key = String(root).toLowerCase();
      if (seenRoots.has(key)) continue;      // one entry per folder, always
      seenRoots.add(key);
      const option = document.createElement("option");
      option.value = root;
      option.textContent = root;
      allowed.appendChild(option);
    }
    allowed.addEventListener("change", (event) => {
      if (event.target.value) $("project-path").value = event.target.value;
    });
    $("preset").addEventListener("change", (event) => applyPreset(event.target.value));
    $("save").addEventListener("click", () => saveWithRescan(false));
    $("apply-scan").addEventListener("click", () => saveWithRescan(true));
    $("add-tag").addEventListener("click", () => {
      const name = $("new-tag").value.trim();
      const tris = Number($("new-tag-tris").value || 0);
      if (!name || !tris) { notice("A category needs a name and a polygon budget.", "warn"); return; }
      const tags = collectTags();
      tags[name] = { max_triangles: tris };
      renderTags(tags);
      $("new-tag").value = "";
      $("new-tag-tris").value = "";
      markDirty();
    });
    $("logout").addEventListener("click", async () => {
      await fetch("/admin/api/logout", { method: "POST", headers: { "X-CSRF-Token": state.csrf } });
      window.location.href = "/admin/login";
    });
    // No "platform" here: that input is a read-only target display now.
    for (const id of ["char-tris", "prop-tris", "tex-edge", "slots", "uvs",
                      "influences", "tag-mode"]) {
      $(id).addEventListener("input", markDirty);
    }
    for (const id of ["pot", "pbr", "naming", "refs", "degenerate"]) {
      $(id).addEventListener("change", markDirty);
    }
  }

  fillForm(state.document);
  renderAudit(data.audit || []);
  if (data.scan) {
    state.scan = data.scan;
    setCounts(data.scan.totals || {});
    $("scan-status").textContent = data.scan.status || "idle";
    if (data.scan.status === "running") watchScan();
  }
}

function renderAudit(rows) {
  const body = $("audit");
  body.innerHTML = "";
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="5" class="sub">No changes recorded yet.</td></tr>`;
    return;
  }
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml((row.changed_at || "").replace("T", " "))}</td>
      <td class="who-col">${escapeHtml(row.username)} <span class="sub">(${escapeHtml(row.role)})</span></td>
      <td><code>${escapeHtml(row.target)}</code></td>
      <td class="old">${escapeHtml(row.old_value)}</td>
      <td class="new">${escapeHtml(row.new_value)}</td>`;
    body.appendChild(tr);
  }
}

window.addEventListener("beforeunload", (event) => {
  if ($("dirty").textContent === "unsaved changes") {
    event.preventDefault();
    event.returnValue = "";
  }
});

loadState().catch((error) => notice(`Cannot load the rules: ${error.message}`, "err"));
