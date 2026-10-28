"use strict";
(() => {
  // Class names/colors come from the server (road_damage/classes.py); keys are raw model class codes.
  const CLASSES = JSON.parse(document.getElementById("class-catalog").textContent);
  const displayName = (code) => CLASSES[code]?.display_name ?? code;
  const state = {ready: false};

  function setDownload(element, url) {
    if (url) element.href = url;
    else element.removeAttribute("href");
    element.classList.toggle("disabled", !url);
    element.setAttribute("aria-disabled", String(!url));
  }

  function formatSize(bytes) {
    return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
  }

  function chip(code, count) {
    const item = CLASSES[code];
    const span = document.createElement("span");
    span.className = "chip";
    const dot = document.createElement("i");
    dot.className = "dot";
    dot.style.background = item.color;
    const text = document.createElement("span");
    text.textContent = item.display_name;
    const number = document.createElement("b");
    number.textContent = count;
    span.append(dot, text, number);
    return span;
  }

  async function checkHealth() {
    const box = document.getElementById("health");
    try {
      const response = await fetch("/api/health", {signal: AbortSignal.timeout(8000)});
      const body = await response.json();
      if (!response.ok || !body.success || !body.model.loaded) throw new Error("not ready");
      state.ready = true;
      box.dataset.state = "ok";
      box.lastElementChild.textContent = "模型就緒";
    } catch (reason) {
      state.ready = false;
      box.dataset.state = "offline";
      box.lastElementChild.textContent = "服務未連線";
    }
    document.dispatchEvent(new CustomEvent("rd:health"));
  }

  window.RD = {CLASSES, displayName, state, setDownload, formatSize, chip};
  checkHealth();
  setInterval(checkHealth, 30000);
})();
