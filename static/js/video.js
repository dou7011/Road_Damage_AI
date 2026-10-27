"use strict";
(() => {
  const {CLASSES, setDownload} = RD;
  const el = (id) => document.getElementById(id);
  const storageKey = "road-damage-video-job";
  const GET_PHASE = new Set(["uploading", "fetching_info", "downloading", "validating"]);
  const phases = {
    uploading: "正在上傳影片…", validating: "正在檢查影片…",
    fetching_info: "正在取得 YouTube 影片資訊…", downloading: "正在下載影片…",
    processing: "AI 正在逐幀辨識…", encoding: "正在輸出辨識影片…",
    checking_output: "正在檢查輸出檔案…", completed: "辨識完成", failed: "處理失敗"
  };
  let file = null;
  let busy = false;
  let jobId = null;
  let timer = null;
  const ROW_HEIGHT = 52;
  const TICKS = 300;
  let rowsRequest = null;
  let rows = [];
  let fps = 30;
  let duration = 0;
  let lastFrame = -1;
  let follow = true;
  let userScrollUntil = 0;
  let rafId = null;
  let renderQueued = false;
  const nodes = new Map();

  function error(message) {
    el("videoError").textContent = message;
    el("videoError").hidden = !message;
  }

  function remember(id) {
    try {
      if (id) localStorage.setItem(storageKey, id);
      else localStorage.removeItem(storageKey);
    } catch (reason) {
      console.warn("無法保存影片工作 ID；重新整理後將無法接續進度。", reason);
    }
  }

  function seconds(value) {
    if (value < 60) return `${value.toFixed(1)} 秒`;
    return `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`;
  }

  function layout() {
    el("inputCard").hidden = busy || !el("result").hidden;
    el("videoDetect").disabled = busy || !file;
    el("videoReset").hidden = !file;
    el("videoInput").disabled = busy;
    for (const id of ["youtubeAnalyze", "youtubeUrl", "youtubeAuthorized"]) el(id).disabled = busy;
  }

  function clearResults() {
    if (rowsRequest) rowsRequest.abort();
    rowsRequest = null;
    rows = [];
    lastFrame = -1;
    stopTracking();
    el("result").hidden = true;
    for (const node of nodes.values()) node.remove();
    nodes.clear();
    el("detSpacer").style.height = "0";
    el("playhead").hidden = true;
    el("timeline").replaceChildren();
    el("eventCount").textContent = "";
    el("nowInfo").textContent = "—";
    el("eventEmpty").hidden = true;
    el("videoResult").pause();
    el("videoResult").removeAttribute("src");
    el("videoResult").load();
    for (const id of ["videoDownload", "videoJson", "videoCsv"]) setDownload(el(id), null);
    for (const id of ["videoDuration", "videoResolution", "videoFps", "videoFrames", "videoTime",
      "videoAverageTime", "videoDamagedFrames", "videoTotal", "videoOverallConfidence", "videoInference",
      "videoInputDetails"]) el(id).textContent = "—";
    el("videoStats").replaceChildren();
    el("youtubeInfo").hidden = true;
    el("youtubeInfo").textContent = "";
  }

  function choose(candidate) {
    if (busy || !candidate) return;
    if (!/\.(mp4|avi|mov|mkv)$/i.test(candidate.name)) return error("只支援 MP4、AVI、MOV、MKV 影片。");
    if (candidate.size === 0 || candidate.size > 256 * 1024 * 1024) return error("影片必須大於 0 且不超過 256 MB。");
    file = candidate;
    jobId = null;
    remember(null);
    clearResults();
    error("");
    el("videoProgressArea").hidden = true;
    el("videoDropzone").classList.add("has-file");
    el("videoFileLabel").textContent = file.name;
    el("videoFileInfo").textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB · 已就緒`;
    layout();
  }

  function resetInput() {
    file = null;
    el("videoInput").value = "";
    el("videoDropzone").classList.remove("has-file");
    el("videoFileLabel").textContent = "拖曳影片到這裡";
    el("videoFileInfo").innerHTML = "或 <u>選擇檔案</u>";
  }

  function stepState(status) {
    const order = ["get", "detect", "encode"];
    const active = GET_PHASE.has(status) ? 0 : status === "processing" ? 1 : 2;
    for (const item of el("steps").children) {
      const index = order.indexOf(item.dataset.step);
      item.className = status === "completed" || index < active ? "done" : index === active ? "active" : "";
    }
  }

  function progress(job) {
    el("videoProgressArea").hidden = false;
    el("videoPhase").textContent = phases[job.status] || job.status;
    const waiting = GET_PHASE.has(job.status) && !job.progress;
    if (waiting) el("videoProgress").removeAttribute("value");
    else el("videoProgress").value = job.progress;
    el("videoPercent").textContent = waiting ? "" : `${Math.round(job.progress)}%`;
    stepState(job.status);
    let info = "";
    if (job.status === "processing") info = `已處理 ${job.processed_frames} / ${job.total_frames ?? "—"} 個畫面 · 已耗時 ${seconds(job.elapsed_seconds)}`;
    else if (job.status === "downloading") info = `已下載 ${((job.downloaded_bytes || 0) / 1024 / 1024).toFixed(1)} MB`;
    else if (job.elapsed_seconds) info = `已耗時 ${seconds(job.elapsed_seconds)}`;
    el("videoProgressInfo").textContent = info;
    if (job.source_type === "youtube") selectTab("youtubeTab");
    if (job.source_info) {
      const source = job.source_info;
      el("youtubeInfo").hidden = false;
      el("youtubeInfo").textContent = `${source.title}\n${source.channel} · ${seconds(source.duration)} · ${source.width} × ${source.height}`;
    }
  }

  function render(result) {
    el("result").hidden = false;
    el("videoResult").src = result.video;
    setDownload(el("videoDownload"), result.download_url);
    setDownload(el("videoJson"), result.json_url);
    setDownload(el("videoCsv"), result.csv_url);
    el("videoDuration").textContent = seconds(result.duration);
    el("videoResolution").textContent = `${result.width} × ${result.height}`;
    el("videoFps").textContent = result.fps.toFixed(2);
    el("videoFrames").textContent = result.processed_frames;
    el("videoTime").textContent = seconds(result.processing_time);
    el("videoAverageTime").textContent = `${(result.average_frame_time * 1000).toFixed(1)} ms`;
    el("videoDamagedFrames").textContent = result.frames_with_detections;
    el("videoTotal").textContent = result.summary.total;
    el("videoOverallConfidence").textContent = result.overall_average_confidence === null ? "—（無偵測）"
      : `${(result.overall_average_confidence * 100).toFixed(0)}%`;
    el("videoInference").textContent = seconds(result.inference_time);
    el("videoInputDetails").textContent = result.input_info
      ? `${result.input_info.name} · ${(result.input_info.size_bytes / 1024 / 1024).toFixed(1)} MB`
      : result.source === "youtube" ? "YouTube" : "上傳影片";
    el("videoMessage").textContent = result.summary.total
      ? `在 ${result.frames_with_detections} 個畫面中偵測到損壞。`
      : "沒有偵測到損壞，但這不代表道路沒有問題。";
    el("videoStats").replaceChildren();
    for (const [code, item] of Object.entries(CLASSES)) {
      const row = el("videoStats").insertRow();
      const type = row.insertCell();
      const dot = document.createElement("i");
      dot.className = "dot";
      dot.style.background = item.color;
      type.append(dot, ` ${item.label}`);
      row.insertCell().textContent = result.summary[code];
      row.insertCell().textContent = result.average_confidence[code] === null
        ? "—" : `${(result.average_confidence[code] * 100).toFixed(0)}%`;
    }
    loadRows(result);
    layout();
    el("result").scrollIntoView({behavior: "smooth", block: "start"});
  }

  function clock(value, decimals = 0) {
    const minutes = Math.floor(value / 60);
    const rest = value - minutes * 60;
    return `${minutes}:${rest.toFixed(decimals).padStart(decimals ? decimals + 3 : 2, "0")}`;
  }

  function currentFrame() {
    return Math.max(0, Math.floor(el("videoResult").currentTime * fps + 1e-6));
  }

  // First row whose frame_index is >= frame.
  function lowerBound(frame) {
    let low = 0;
    let high = rows.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (rows[middle][0] < frame) low = middle + 1;
      else high = middle;
    }
    return low;
  }

  function jumpToFrame(frame) {
    const video = el("videoResult");
    video.pause();
    // Aim at the middle of the frame so rounding never lands on a neighbour.
    video.currentTime = Math.min((frame + 0.5) / fps, Math.max(0, (video.duration || duration) - 0.001));
    userScrollUntil = 0;
    lastFrame = -1;
    sync();
  }

  function makeRow(index) {
    const [frameIndex, timestamp, code, confidence, id] = rows[index];
    const info = CLASSES[code];
    const row = document.createElement("button");
    row.type = "button";
    row.className = "det-row";
    row.style.top = `${index * ROW_HEIGHT}px`;
    const when = document.createElement("span");
    when.className = "when";
    const time = document.createElement("b");
    time.textContent = clock(timestamp, 2);
    const frameLabel = document.createElement("small");
    frameLabel.textContent = `第 ${frameIndex} 幀`;
    when.append(time, frameLabel);
    const what = document.createElement("span");
    what.className = "what";
    const dot = document.createElement("i");
    dot.className = "dot";
    dot.style.background = info?.color ?? "#999";
    const name = document.createElement("span");
    name.textContent = info?.label ?? code;
    const tag = document.createElement("small");
    tag.textContent = `#${id}`;
    what.append(dot, name, tag);
    const score = document.createElement("span");
    score.className = "score";
    score.textContent = `${(confidence * 100).toFixed(0)}%`;
    row.append(when, what, score);
    row.setAttribute("aria-label", `跳到第 ${frameIndex} 幀，${info?.label ?? code}，信心 ${(confidence * 100).toFixed(0)}%`);
    row.addEventListener("click", () => jumpToFrame(frameIndex));
    return row;
  }

  // Only rows near the viewport exist; existing nodes are updated in place so clicks are never lost.
  function drawRows() {
    renderQueued = false;
    const list = el("eventList");
    const first = Math.max(0, Math.floor(list.scrollTop / ROW_HEIGHT) - 4);
    const last = Math.min(rows.length, Math.ceil((list.scrollTop + list.clientHeight) / ROW_HEIGHT) + 4);
    for (const [index, node] of nodes) {
      if (index < first || index >= last) {
        node.remove();
        nodes.delete(index);
      }
    }
    for (let index = first; index < last; index += 1) {
      if (!nodes.has(index)) {
        const node = makeRow(index);
        nodes.set(index, node);
        list.append(node);
      }
    }
    for (const [index, node] of nodes) node.classList.toggle("active", rows[index][0] === lastFrame);
  }

  function queueDraw() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(drawRows);
  }

  // Keeps the list, playhead and timeline aligned with the frame currently on screen.
  function sync() {
    if (!rows.length && !duration) return;
    const frame = currentFrame();
    if (frame === lastFrame) return;
    lastFrame = frame;
    const video = el("videoResult");
    const start = lowerBound(frame);
    let end = start;
    while (end < rows.length && rows[end][0] === frame) end += 1;
    const count = end - start;
    el("nowInfo").textContent = `第 ${frame} 幀 · ${clock(frame / fps, 2)} · ${count ? `${count} 個標點` : "沒有標點"}`;
    const head = el("timeline").querySelector(".head");
    if (head && duration) head.style.left = `${Math.min(100, video.currentTime / duration * 100)}%`;
    const playhead = el("playhead");
    playhead.hidden = count > 0 || !rows.length;
    playhead.style.top = `${start * ROW_HEIGHT}px`;
    if (follow && rows.length && Date.now() >= userScrollUntil) {
      const list = el("eventList");
      const target = start * ROW_HEIGHT + (count ? count * ROW_HEIGHT / 2 : 0) - list.clientHeight / 2;
      list.scrollTop = Math.max(0, target);
    }
    drawRows();
  }

  function track() {
    sync();
    rafId = el("videoResult").paused || el("videoResult").ended ? null : requestAnimationFrame(track);
  }

  function startTracking() {
    if (rafId === null) rafId = requestAnimationFrame(track);
  }

  function stopTracking() {
    if (rafId !== null) cancelAnimationFrame(rafId);
    rafId = null;
  }

  function renderRows(result) {
    el("eventCount").textContent = rows.length ? `共 ${rows.length} 個標點` : "";
    el("eventEmpty").hidden = rows.length > 0;
    el("detSpacer").style.height = `${rows.length * ROW_HEIGHT}px`;
    const bucketColor = new Map();
    for (const [, timestamp, code] of rows) {
      const bucket = Math.min(TICKS - 1, Math.floor(timestamp / duration * TICKS));
      if (!bucketColor.has(bucket)) bucketColor.set(bucket, CLASSES[code]?.color ?? "#fb7185");
    }
    const ticks = [];
    for (const [bucket, color] of bucketColor) {
      const tick = document.createElement("i");
      tick.className = "tick";
      tick.style.left = `${bucket / TICKS * 100}%`;
      tick.style.width = `${100 / TICKS}%`;
      tick.style.background = color;
      ticks.push(tick);
    }
    const head = document.createElement("i");
    head.className = "head";
    el("timeline").replaceChildren(...ticks, head);
    el("eventList").scrollTop = 0;
    lastFrame = -1;
    sync();
  }

  async function loadRows(result) {
    if (rowsRequest) rowsRequest.abort();
    const controller = new AbortController();
    rowsRequest = controller;
    fps = result.fps;
    duration = result.duration;
    try {
      const response = await fetch(result.detections_url, {
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30000)])
      });
      const body = await response.json();
      if (!response.ok || !body.success) throw new Error(body.error?.message || "無法取得標點。");
      if (rowsRequest !== controller) return;
      rows = body.rows;
      renderRows(result);
    } catch (reason) {
      if (controller.signal.aborted) return;
      el("eventCount").textContent = "";
      el("eventEmpty").hidden = false;
      el("eventEmpty").textContent = `無法載入標點：${reason.message}`;
    }
  }

  async function poll() {
    el("videoProgressArea").hidden = false;
    el("videoResume").hidden = true;
    try {
      const response = await fetch(`/api/detect/video/jobs/${jobId}`, {signal: AbortSignal.timeout(15000)});
      const body = await response.json();
      if (!response.ok || !body.success) {
        if (response.status === 404) {
          jobId = null;
          remember(null);
          busy = false;
          el("videoProgressArea").hidden = true;
          layout();
          return error("先前的工作已過期或伺服器已重啟，請重新開始。");
        }
        throw new Error(body.error?.message || "無法查詢進度。");
      }
      error("");
      const job = body.job;
      progress(job);
      if (job.status === "completed" || job.status === "failed") {
        busy = false;
        el("videoProgressArea").hidden = true;
        if (job.status === "completed") render(job.result);
        else {
          remember(null);
          layout();
          error(job.error.message);
        }
      } else {
        timer = setTimeout(poll, 1000);
      }
    } catch (reason) {
      error(`進度查詢中斷：${reason.message}。背景工作可能仍在進行，請按「重新查詢進度」，不要重複提交。`);
      el("videoResume").hidden = false;
    }
  }

  async function submit(endpoint, options, status) {
    if (busy) return;
    clearTimeout(timer);
    jobId = null;
    remember(null);
    clearResults();
    error("");
    el("videoResume").hidden = true;
    busy = true;
    layout();
    progress({status, progress: 0, processed_frames: 0, total_frames: null, elapsed_seconds: 0});
    try {
      const response = await fetch(endpoint, {method: "POST", ...options});
      const body = await response.json();
      if (!response.ok || !body.success) {
        busy = false;
        el("videoProgressArea").hidden = true;
        layout();
        return error(body.error?.message || "提交失敗，請稍後再試。");
      }
      jobId = body.job_id;
      remember(jobId);
      await poll();
    } catch (reason) {
      busy = false;
      el("videoProgressArea").hidden = true;
      layout();
      error(`無法連線到伺服器（${reason.message}）。若網路曾中斷，工作可能已建立，請稍候再試，避免重複提交。`);
    }
  }

  function selectTab(id) {
    for (const [tab, panel] of [["uploadVideoTab", "uploadVideoPanel"], ["youtubeTab", "youtubePanel"]]) {
      const active = tab === id;
      el(tab).setAttribute("aria-selected", String(active));
      el(tab).tabIndex = active ? 0 : -1;
      el(panel).hidden = !active;
    }
  }

  el("videoInput").addEventListener("change", (event) => choose(event.target.files[0]));
  for (const name of ["dragenter", "dragover"]) el("videoDropzone").addEventListener(name, (event) => {
    event.preventDefault();
    if (!busy) el("videoDropzone").classList.add("dragging");
  });
  for (const name of ["dragleave", "drop"]) el("videoDropzone").addEventListener(name, (event) => {
    event.preventDefault();
    el("videoDropzone").classList.remove("dragging");
  });
  el("videoDropzone").addEventListener("drop", (event) => {
    if (busy) return;
    if (event.dataTransfer.files.length !== 1) return error("請一次選擇一部影片。");
    choose(event.dataTransfer.files[0]);
  });
  el("videoReset").addEventListener("click", () => {
    resetInput();
    error("");
    layout();
  });
  el("videoDetect").addEventListener("click", () => {
    if (!file || busy) return;
    const form = new FormData();
    form.append("video", file);
    submit("/api/detect/video/jobs", {body: form}, "uploading");
  });
  el("youtubeForm").addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return;
    if (!el("youtubeUrl").value.trim()) return error("請貼上 YouTube 影片連結。");
    if (!el("youtubeAuthorized").checked) return error("請先勾選使用權限確認。");
    submit("/api/detect/youtube", {
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({url: el("youtubeUrl").value.trim(), authorized: true})
    }, "fetching_info");
  });
  for (const id of ["uploadVideoTab", "youtubeTab"]) {
    el(id).addEventListener("click", () => selectTab(id));
    el(id).addEventListener("keydown", (event) => {
      if (["ArrowLeft", "ArrowRight"].includes(event.key)) {
        event.preventDefault();
        const next = id === "youtubeTab" ? "uploadVideoTab" : "youtubeTab";
        selectTab(next);
        el(next).focus();
      }
    });
  }
  el("videoAgain").addEventListener("click", () => {
    clearTimeout(timer);
    jobId = null;
    remember(null);
    clearResults();
    resetInput();
    error("");
    layout();
    window.scrollTo({top: 0, behavior: "smooth"});
  });
  el("videoResume").addEventListener("click", poll);
  el("videoResult").addEventListener("error", () => {
    if (el("videoResult").getAttribute("src")) error("影片無法播放，結果可能已過期。請改用支援 H.264 的瀏覽器，或下載影片後播放。");
  });
  const video = el("videoResult");
  video.addEventListener("play", startTracking);
  for (const name of ["pause", "ended"]) video.addEventListener(name, () => {
    stopTracking();
    sync();
  });
  for (const name of ["seeked", "timeupdate", "loadedmetadata"]) video.addEventListener(name, sync);
  el("eventList").addEventListener("scroll", queueDraw);
  // Manual scrolling pauses auto-follow briefly so the list does not fight the user.
  for (const name of ["wheel", "touchmove", "pointerdown"]) {
    el("eventList").addEventListener(name, (event) => {
      userScrollUntil = Date.now() + 3000;
    }, {passive: true});
  }
  el("followToggle").addEventListener("click", () => {
    follow = !follow;
    el("followToggle").setAttribute("aria-pressed", String(follow));
    userScrollUntil = 0;
    lastFrame = -1;
    sync();
  });
  el("timeline").addEventListener("click", (event) => {
    if (!duration) return;
    const box = el("timeline").getBoundingClientRect();
    const frame = Math.round(Math.min(1, Math.max(0, (event.clientX - box.left) / box.width)) * duration * fps);
    if (!rows.length) return jumpToFrame(frame);
    const next = lowerBound(frame);
    const candidates = [rows[next - 1], rows[next]].filter(Boolean);
    const nearest = candidates.reduce((best, row) => Math.abs(row[0] - frame) < Math.abs(best[0] - frame) ? row : best);
    jumpToFrame(nearest[0]);
  });

  try {
    jobId = localStorage.getItem(storageKey);
  } catch (reason) {
    console.warn("無法讀取上次的影片工作 ID。", reason);
  }
  clearResults();
  if (jobId && /^[a-f0-9]{32}$/.test(jobId)) {
    busy = true;
    layout();
    poll();
  } else layout();
})();
