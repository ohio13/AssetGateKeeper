/* AssetHealth -> Art Workstation bridge: one row, one click, one asset.
 *
 * Deliberately not a multi-select toolbar: the point of the button is to look at
 * the thing you just saw fail. The server enforces a hard batch ceiling of 10 so
 * a stuck click cannot queue the workstation; this file caps in-flight requests
 * at the same number to keep the browser honest about it too.
 */
"use strict";

(() => {
  const LIMIT = 10;
  let inFlight = 0;
  let timer = 0;

  const toast = (() => {
    let element = document.getElementById("send-toast");
    if (!element) {
      element = document.createElement("div");
      element.id = "send-toast";
      Object.assign(element.style, {
        position: "fixed", right: "22px", bottom: "22px", maxWidth: "440px",
        padding: "11px 15px", borderRadius: "10px", font: "13px/1.45 ui-sans-serif, sans-serif",
        background: "#161b24", color: "#e6e9ef", border: "1px solid #262f3d",
        boxShadow: "0 10px 30px rgba(0,0,0,.45)", opacity: "0", transition: "opacity .18s",
        pointerEvents: "none", zIndex: "20",
      });
      document.body.appendChild(element);
    }
    return element;
  })();

  function say(text, kind) {
    toast.textContent = text;
    toast.style.borderColor = kind === "err" ? "#ff6b6b"
      : kind === "warn" ? "#e0af68" : "#3ecf8e";
    toast.style.opacity = "1";
    window.clearTimeout(timer);
    timer = window.setTimeout(() => { toast.style.opacity = "0"; }, 6500);
  }

  async function send(button) {
    const verdictId = Number(button.dataset.verdict);
    if (!verdictId) return;
    if (inFlight >= LIMIT) {
      say(`At most ${LIMIT} assets at a time - wait for this batch to land.`, "warn");
      return;
    }
    const original = button.textContent;
    inFlight += 1;
    button.disabled = true;
    button.textContent = "Sending…";
    try {
      const response = await fetch("/api/send-to-workstation", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ verdicts: [verdictId] }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      const result = (body.results || [])[0] || {};
      if (!result.ok) throw new Error(result.error || "the workstation refused it");
      button.textContent = "Sent ✓";
      button.classList.add("done");
      say(`Sent ${result.name || "the asset"} to the Art Workstation (asset #${result.asset_id}).`);
    } catch (error) {
      button.textContent = original;
      button.disabled = false;
      say(`Could not send: ${error.message}`, "err");
    } finally {
      inFlight -= 1;
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest && event.target.closest("button.send");
    if (!button) return;
    event.preventDefault();
    send(button);
  });
})();
