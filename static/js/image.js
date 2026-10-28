"use strict";
(() => {
  const {CLASSES, state, setDownload, formatSize, chip} = RD;
  const $ = (id) => document.getElementById(id);
  const MAX_FILES = 20;
  const MAX_BYTES = 16 * 1024 * 1024;
  let items = [];
  let nextId = 1;
  let running = false;
  let stopping = false;
  let runTotal = 0;
  let runDone = 0;
  let current = null;
  let view = "result";

  function showError(message) {
    $("error").textContent = message;
    $("error").hidden = !message;
  }

  function position(label) {
    const [vertical, horizontal] = label;
    if (vertical === "中" && horizontal === "中") return "中央";
    return [vertical !== "中" ? `${vertical}方` : "", horizontal !== "中" ? `${horizontal}側` : ""].filter(Boolean).join("・");
  }

  function addFiles(files) {
    if (running) return;
    const problems = [];
    const known = new Set(items.map((item) => item.key));
    for (const file of files) {
      const key = `${file.name}|${file.size}|${file.lastModified}`;
      if (known.has(key)) continue;
      if (!/\.(jpe?g|png|webp)$/i.test(file.name)) problems.push(`「${file.name}」格式不支援（僅限 JPG、PNG、WEBP）`);
      else if (file.size === 0 || file.size > MAX_BYTES) problems.push(`「${file.name}」超過 16 MB 或是空檔案`);
      else if (items.length >= MAX_FILES) {
        problems.push(`一次最多 ${MAX_FILES} 張，其餘圖片未加入`);
        break;
      } else {
        known.add(key);
        items.push({id: nextId++, key, file, url: URL.createObjectURL(file), state: "queued", result: null, error: ""});
      }
    }
    showError(problems.join("\n"));
    render();
  }

  function removeItem(id) {
    const item = items.find((entry) => entry.id === id);
    if (!item || running) return;
    URL.revokeObjectURL(item.url);
    items = items.filter((entry) => entry.id !== id);
    render();
  }

  function clearAll() {
    if (running) return;
    for (const item of items) URL.revokeObjectURL(item.url);
    items = [];
    $("fileInput").value = "";
    showError("");
    render();
  }

  function tile(item) {
    const li = document.createElement("li");
    li.className = "tile";
    li.dataset.state = item.state;
    const open = document.createElement("button");
    open.type = "button";
    open.className = "open";
    const image = document.createElement("img");
    image.alt = item.file.name;
    image.loading = "lazy";
    image.src = item.state === "done" ? item.result.image : item.url;
    open.append(image);
    const body = document.createElement("div");
    body.className = "tile-body";
    const badge = document.createElement("span");
    badge.className = "badge";
    if (item.state === "done") {
      const total = item.result.summary.total;
      badge.classList.add(total ? "found" : "clean");
      badge.textContent = total ? `發現 ${total} 處` : "未發現損壞";
      open.setAttribute("aria-label", `查看 ${item.file.name} 的辨識結果`);
      open.addEventListener("click", () => openViewer(item));
    } else if (item.state === "running") {
      badge.classList.add("running");
      badge.textContent = "辨識中…";
      open.disabled = true;
    } else if (item.state === "error") {
      badge.classList.add("error");
      badge.textContent = "失敗";
      open.disabled = true;
    } else {
      badge.textContent = "等待辨識";
      open.disabled = true;
    }
    const name = document.createElement("span");
    name.className = "tile-name";
    name.title = item.file.name;
    name.textContent = item.file.name;
    body.append(badge, name);
    if (item.state === "error") {
      const message = document.createElement("span");
      message.className = "tile-error";
      message.textContent = item.error;
      body.append(message);
    }
    li.append(open, body);
    if (!running) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "remove";
      remove.textContent = "✕";
      remove.setAttribute("aria-label", `移除 ${item.file.name}`);
      remove.addEventListener("click", () => removeItem(item.id));
      li.append(remove);
    }
    return li;
  }

  function renderSummary() {
    const done = items.filter((item) => item.state === "done");
    $("summary").hidden = done.length === 0;
    if (!done.length) return;
    const totals = Object.fromEntries(Object.keys(CLASSES).map((code) => [code, 0]));
    let found = 0;
    let all = 0;
    for (const item of done) {
      const summary = item.result.summary;
      if (summary.total) found += 1;
      all += summary.total;
      for (const code of Object.keys(CLASSES)) totals[code] += summary[code];
    }
    const stats = [["已辨識圖片", `${done.length} 張`], ["發現損壞的圖片", `${found} 張`], ["損壞總數", `${all} 處`]];
    $("summary").replaceChildren(...stats.map(([label, value]) => {
      const box = document.createElement("div");
      box.className = "stat";
      const caption = document.createElement("span");
      const number = document.createElement("strong");
      caption.textContent = label;
      number.textContent = value;
      box.append(caption, number);
      return box;
    }));
    if (all) {
      const chips = document.createElement("div");
      chips.className = "chips";
      for (const code of Object.keys(CLASSES)) if (totals[code]) chips.append(chip(code, totals[code]));
      $("summary").append(chips);
    }
  }

  function render() {
    const hasItems = items.length > 0;
    $("queue").hidden = !hasItems;
    $("dropzone").classList.toggle("compact", hasItems);
    $("fileInput").disabled = running;
    if (!hasItems) return;
    const pending = items.filter((item) => item.state === "queued" || item.state === "error").length;
    const done = items.filter((item) => item.state === "done").length;
    $("queueTitle").textContent = `${items.length} 張圖片`;
    if (running) $("queueSub").textContent = `　正在辨識 ${Math.min(runDone + 1, runTotal)} / ${runTotal}`;
    else if (done && !pending) $("queueSub").textContent = "　全部完成，點選圖片查看詳細結果";
    else if (done) $("queueSub").textContent = `　已完成 ${done} 張，還有 ${pending} 張待辨識`;
    else $("queueSub").textContent = "　準備就緒";
    $("detect").textContent = pending > 1 ? `開始辨識（${pending} 張）` : "開始辨識";
    $("detect").disabled = running || pending === 0 || !state.ready;
    $("detect").hidden = running;
    $("stop").hidden = !running;
    $("stop").disabled = stopping;
    $("stop").textContent = stopping ? "正在停止…" : "停止";
    $("clearAll").disabled = running;
    $("progress").hidden = !running;
    $("progress").value = runTotal ? runDone / runTotal * 100 : 0;
    $("grid").replaceChildren(...items.map(tile));
    renderSummary();
  }

  async function detectOne(item) {
    const form = new FormData();
    form.append("image", item.file);
    const response = await fetch("/api/detect/image", {method: "POST", body: form});
    let body = null;
    try {
      body = await response.json();
    } catch (reason) {
      throw new Error("伺服器回應無法解析");
    }
    if (!response.ok || !body.success) throw new Error(body.error?.message || "辨識失敗");
    return body;
  }

  async function run() {
    if (running) return;
    const queue = items.filter((item) => item.state === "queued" || item.state === "error");
    if (!queue.length) return;
    running = true;
    stopping = false;
    runTotal = queue.length;
    runDone = 0;
    showError("");
    for (const item of queue) {
      if (stopping) break;
      item.state = "running";
      render();
      try {
        item.result = await detectOne(item);
        item.state = "done";
      } catch (reason) {
        item.state = "error";
        item.error = reason instanceof TypeError ? "無法連線到伺服器" : reason.message;
      }
      runDone += 1;
    }
    running = false;
    stopping = false;
    runTotal = 0;
    render();
    const failed = items.filter((item) => item.state === "error").length;
    if (failed) showError(`${failed} 張圖片辨識失敗，可再按「開始辨識」重試。`);
  }

  /* Viewer */
  function doneItems() {
    return items.filter((item) => item.state === "done");
  }

  function select(id, fromBox = false) {
    for (const row of $("vRows").querySelectorAll("tr[data-id]")) row.classList.toggle("selected", Number(row.dataset.id) === id);
    for (const rect of $("vOverlay").querySelectorAll("rect")) rect.classList.toggle("active", Number(rect.dataset.id) === id);
    if (fromBox) {
      $("vDetails").open = true;
      $("vRows").querySelector(`tr[data-id="${id}"]`)?.scrollIntoView({block: "nearest"});
    }
  }

  function showView() {
    const result = current.result;
    for (const button of document.querySelectorAll(".seg [data-view]")) button.setAttribute("aria-pressed", String(button.dataset.view === view));
    $("vImage").src = view === "result" ? result.image : result.original_image;
    $("vImage").alt = view === "result" ? "標示出損壞位置的圖片" : "原始圖片";
    $("vOverlay").style.display = view === "result" ? "" : "none";
  }

  function fillViewer() {
    const result = current.result;
    const total = result.summary.total;
    $("vName").textContent = current.file.name;
    $("vMeta").textContent = `${result.image_width} × ${result.image_height} px · ${formatSize(result.input_info.size_bytes)} · ${result.processing_time.toFixed(2)} 秒`;
    $("vMessage").textContent = total ? `發現 ${total} 處可能的損壞` : "沒有偵測到損壞";
    $("vCounts").replaceChildren(...Object.keys(CLASSES).filter((code) => result.summary[code]).map((code) => chip(code, result.summary[code])));
    setDownload($("vDownload"), result.download_url);
    setDownload($("vJson"), result.json_url);
    setDownload($("vCsv"), result.csv_url);
    $("vOverlay").setAttribute("viewBox", `0 0 ${result.image_width} ${result.image_height}`);
    $("vOverlay").replaceChildren();
    $("vRows").replaceChildren();
    $("vDetails").open = false;
    $("vDetails").hidden = total === 0;
    for (const detection of result.detections) {
      const box = detection.bbox;
      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      for (const [key, value] of Object.entries({x: box.x1, y: box.y1, width: box.width, height: box.height})) rect.setAttribute(key, value);
      rect.dataset.id = detection.id;
      rect.addEventListener("click", () => select(detection.id, true));
      $("vOverlay").append(rect);
      const row = $("vRows").insertRow();
      row.dataset.id = detection.id;
      row.insertCell().textContent = detection.id;
      const type = row.insertCell();
      const dot = document.createElement("i");
      dot.className = "dot";
      dot.style.background = detection.color;
      type.append(dot, ` ${RD.displayName(detection.class_code)}`);
      row.insertCell().textContent = `${(detection.confidence * 100).toFixed(0)}%`;
      row.insertCell().textContent = position(detection.position.label);
      row.insertCell().textContent = `(${Math.round(box.x1)}, ${Math.round(box.y1)}) – (${Math.round(box.x2)}, ${Math.round(box.y2)})`;
      row.addEventListener("click", () => select(detection.id));
    }
    const list = doneItems();
    const index = list.indexOf(current);
    $("vPrev").disabled = index <= 0;
    $("vNext").disabled = index >= list.length - 1;
    showView();
  }

  function openViewer(item) {
    current = item;
    view = "result";
    fillViewer();
    if (!$("viewer").open) $("viewer").showModal();
  }

  function step(direction) {
    const list = doneItems();
    const target = list[list.indexOf(current) + direction];
    if (target) {
      current = target;
      view = "result";
      fillViewer();
    }
  }

  $("fileInput").addEventListener("change", (event) => {
    addFiles([...event.target.files]);
    event.target.value = "";
  });
  const zone = $("dropzone");
  for (const name of ["dragenter", "dragover"]) zone.addEventListener(name, (event) => {
    event.preventDefault();
    if (!running) zone.classList.add("dragging");
  });
  for (const name of ["dragleave", "drop"]) zone.addEventListener(name, (event) => {
    event.preventDefault();
    zone.classList.remove("dragging");
  });
  zone.addEventListener("drop", (event) => addFiles([...event.dataTransfer.files]));
  $("detect").addEventListener("click", run);
  $("stop").addEventListener("click", () => {
    stopping = true;
    render();
  });
  $("clearAll").addEventListener("click", clearAll);
  $("vClose").addEventListener("click", () => $("viewer").close());
  $("vPrev").addEventListener("click", () => step(-1));
  $("vNext").addEventListener("click", () => step(1));
  $("viewer").addEventListener("click", (event) => {
    if (event.target === $("viewer")) $("viewer").close();
  });
  $("viewer").addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft") step(-1);
    if (event.key === "ArrowRight") step(1);
  });
  for (const button of document.querySelectorAll(".seg [data-view]")) button.addEventListener("click", () => {
    view = button.dataset.view;
    showView();
  });
  $("vImage").addEventListener("error", () => showError("圖片無法顯示，結果可能已過期（保留 24 小時），請重新辨識。"));
  document.addEventListener("rd:health", render);
  render();
})();
