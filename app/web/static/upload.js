// Subject page: drop files anywhere to upload (or onto a board section to file them there),
// per-file progress, and dragging chips between sections.
// Uploads go one request per file, 3 at a time, so each file gets its own progress and result.
(() => {
  const main = document.querySelector("main[data-subject-id]");
  if (!main) return;
  const subjectId = main.dataset.subjectId;
  const view = main.dataset.view;
  const rules = JSON.parse(document.getElementById("upload-rules").textContent);
  const CONCURRENCY = 3;
  const CHIP_MIME = "application/x-subject-wiki-file";
  const ICONS = { stored: "✓", duplicate: "↺", rejected: "✕", error: "✕" };

  const dropzone = document.getElementById("dropzone");
  const input = document.getElementById("file-input");
  const uploads = document.getElementById("uploads");
  const list = document.getElementById("upload-list");
  const title = document.getElementById("uploads-title");
  const clearBtn = document.getElementById("uploads-clear");
  const viewEl = document.getElementById("view");

  const queue = [];
  const ghosts = new Set(); // items dropped onto a section, shown there while uploading
  let active = 0;
  let changed = false;
  let refreshTimer = null;
  let dragTimer = null;
  let moveSeq = 0;
  let counts = { stored: 0, duplicate: 0, rejected: 0, error: 0 };

  const types = (e) => [...(e.dataTransfer?.types || [])];
  const hasFiles = (e) => types(e).includes("Files");
  const isChip = (e) => types(e).includes(CHIP_MIME);

  // ---- Drag state: a page-wide overlay for files, hidden "Other" zones revealed for chips.
  // dragleave is unreliable across child elements, so the state clears shortly after the
  // last dragover instead.
  window.addEventListener("dragover", (e) => {
    e.preventDefault();
    if (hasFiles(e)) document.body.classList.add("dragging-files");
    if (isChip(e)) document.body.classList.add("dragging-chip");
    clearTimeout(dragTimer);
    dragTimer = setTimeout(clearDragState, 150);
  });
  function clearDragState() {
    document.body.classList.remove("dragging-files", "dragging-chip");
    viewEl.querySelectorAll(".slot.over").forEach((s) => s.classList.remove("over"));
  }

  // Files dropped anywhere outside a section: the classifier decides where they go.
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    clearDragState();
    if (hasFiles(e)) enqueue(e.dataTransfer.files);
  });

  dropzone.addEventListener("click", () => input.click());
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });
  input.addEventListener("change", () => { enqueue(input.files); input.value = ""; });

  // ---- Board sections accept new files (filed there directly) and existing chips (moved).
  // Delegated on #view because the board is re-rendered after changes.
  viewEl.addEventListener("dragstart", (e) => {
    const chip = e.target.closest?.(".chip[data-file-id]");
    if (!chip) return;
    e.dataTransfer.setData(CHIP_MIME, chip.dataset.fileId);
    e.dataTransfer.effectAllowed = "move";
    chip.classList.add("dragging");
  });
  viewEl.addEventListener("dragend", (e) => e.target.closest?.(".chip")?.classList.remove("dragging"));
  viewEl.addEventListener("dragover", (e) => {
    const slot = e.target.closest(".slot");
    viewEl.querySelectorAll(".slot.over").forEach((s) => s !== slot && s.classList.remove("over"));
    if (slot && (hasFiles(e) || isChip(e))) slot.classList.add("over");
  });
  viewEl.addEventListener("drop", (e) => {
    const slot = e.target.closest(".slot");
    if (!slot) return; // falls through to the page-wide handler
    e.preventDefault();
    e.stopPropagation();
    clearDragState();
    const target = { slot: slot.dataset.slot, week: slot.dataset.week ?? "" };
    if (isChip(e)) moveChip(e.dataTransfer.getData(CHIP_MIME), slot, target);
    else if (hasFiles(e)) enqueue(e.dataTransfer.files, target);
  });

  // Lecture popovers: close when clicking elsewhere or pressing Escape.
  document.addEventListener("click", (e) => {
    document.querySelectorAll("details.lecture[open]").forEach((d) => {
      if (!d.contains(e.target)) d.open = false;
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll("details.lecture[open]").forEach((d) => (d.open = false));
  });

  // Move the chip on screen immediately, then save. The server answers with the
  // re-rendered board (lectures regroup, completeness updates); on failure, undo.
  async function moveChip(fileId, slot, target) {
    const chip = viewEl.querySelector(`.chip[data-file-id="${fileId}"]`);
    const chips = slot.querySelector(".chips");
    if (!chip || !chips || chip.parentElement === chips) return;
    const origin = { parent: chip.parentElement, next: chip.nextSibling };
    chip.classList.remove("dragging");
    chip.classList.add("saving");
    chip.closest("details.lecture")?.removeAttribute("open");
    chips.querySelector(".none")?.remove();
    chips.appendChild(chip);
    updateUnsorted();

    const seq = ++moveSeq;
    const form = new FormData();
    form.append("slot", target.slot);
    if (target.week) form.append("week", target.week);
    try {
      const res = await fetch(`/files/${fileId}/move`, { method: "POST", body: form });
      if (!res.ok) throw new Error(await errorText(res));
      const html = await res.text();
      if (seq === moveSeq) swapView(html); // a later move's response will cover this one
    } catch (err) {
      chip.classList.remove("saving");
      if (origin.parent.isConnected) {
        origin.parent.insertBefore(chip, origin.next?.parentNode === origin.parent ? origin.next : null);
        updateUnsorted();
      }
      window.showToast(`Couldn't move file: ${err.message}`, true);
    }
  }

  function swapView(html) {
    viewEl.innerHTML = html;
    htmx.process(viewEl);
    renderGhosts();
  }

  function updateUnsorted() {
    const section = viewEl.querySelector(".board-section.unsorted");
    if (!section) return;
    const n = section.querySelectorAll(".chip[data-file-id]").length;
    const count = section.querySelector(".unsorted-count");
    if (count) count.textContent = n;
    section.hidden = n === 0;
  }

  async function errorText(res) {
    const text = await res.text();
    try { return JSON.parse(text).detail || text; } catch (_) { return text || `HTTP ${res.status}`; }
  }

  // ---- Upload queue

  clearBtn.addEventListener("click", () => {
    list.innerHTML = "";
    uploads.hidden = true;
    counts = { stored: 0, duplicate: 0, rejected: 0, error: 0 };
  });

  function enqueue(files, target) {
    if (!files || !files.length) return;
    uploads.hidden = false;
    clearBtn.hidden = true;
    for (const file of files) {
      const row = addRow(file, target);
      const problem = precheck(file);
      if (problem) {
        finish(row, "rejected", problem);
      } else {
        const item = { file, target, row };
        queue.push(item);
        if (target) ghosts.add(item);
      }
    }
    renderGhosts();
    pump();
    maybeDone(); // in case every file was rejected up front
  }

  function precheck(file) {
    const ext = file.name.includes(".") ? file.name.split(".").pop().toLowerCase() : "";
    const mime = (file.type || "").toLowerCase();
    if (rules.audioVideo.includes(ext) || mime.startsWith("audio/") || mime.startsWith("video/")) {
      return "Audio and video aren't supported yet. Upload the lecture's caption file (.vtt) or transcript (.txt) instead.";
    }
    if (!rules.accepted.includes(ext)) {
      return `Unsupported file type (${ext ? "." + ext : "no extension"}). Accepted: PDF, PPTX, DOCX, VTT, SRT, TXT.`;
    }
    if (file.size === 0) return "File is empty.";
    if (file.size > rules.maxBytes) return `File is ${mb(file.size)} MB; the limit is ${mb(rules.maxBytes)} MB.`;
    return null;
  }

  function pump() {
    while (active < CONCURRENCY && queue.length) {
      active++;
      const item = queue.shift();
      send(item).finally(() => {
        ghosts.delete(item);
        // A stored file's placeholder stays until the refreshed board replaces it.
        if (item.outcome !== "stored") item.ghostEl?.remove();
        active--;
        pump();
        maybeDone();
      });
    }
    updateTitle();
  }

  function send(item) {
    const { file, target, row } = item;
    return new Promise((resolve) => {
      const form = new FormData();
      form.append("file", file);
      if (target) {
        form.append("slot", target.slot);
        if (target.week) form.append("week", target.week);
      }
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/subjects/${subjectId}/files`);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) setProgress(row, e.loaded / e.total); };
      xhr.onload = () => {
        let body = {};
        try { body = JSON.parse(xhr.responseText); } catch (_) {}
        if (xhr.status >= 200 && xhr.status < 300) {
          item.outcome = body.outcome;
          finish(row, body.outcome, body.message);
          if (body.outcome === "stored") {
            changed = true;
            dropzone.classList.add("slim");
            if (view === "board") refreshSoon();
          }
        } else {
          finish(row, "error", body.detail ? String(body.detail) : `Upload failed (HTTP ${xhr.status})`);
        }
        resolve();
      };
      xhr.onerror = () => { finish(row, "error", "Network error; is the server running?"); resolve(); };
      setProgress(row, 0);
      row.querySelector(".u-status").textContent = "Uploading…";
      xhr.send(form);
    });
  }

  function addRow(file, target) {
    const li = document.createElement("li");
    li.className = "u-row pending";
    li.innerHTML = `
      <span class="u-icon" aria-hidden="true">•</span>
      <span class="u-name"></span>
      <span class="u-size muted"></span>
      <span class="u-bar"><span></span></span>
      <span class="u-status muted">Waiting…</span>`;
    li.querySelector(".u-name").textContent = file.name;
    li.querySelector(".u-size").textContent = `${mb(file.size, 1)} MB` + (target ? ` → ${slotLabel(target)}` : "");
    list.prepend(li);
    return li;
  }

  function setProgress(row, fraction) {
    row.querySelector(".u-bar > span").style.width = `${Math.round(fraction * 100)}%`;
    if (fraction >= 1) row.querySelector(".u-status").textContent = "Classifying…";
  }

  function finish(row, outcome, message) {
    outcome = ICONS[outcome] ? outcome : "error";
    counts[outcome]++;
    row.className = `u-row ${outcome}`;
    row.querySelector(".u-icon").textContent = ICONS[outcome];
    row.querySelector(".u-status").textContent = message || outcome;
    row.querySelector(".u-bar > span").style.width = "100%";
    updateTitle();
  }

  function updateTitle() {
    const waiting = queue.length + active;
    const parts = [];
    if (counts.stored) parts.push(`${counts.stored} stored`);
    if (counts.duplicate) parts.push(`${counts.duplicate} already uploaded`);
    if (counts.rejected) parts.push(`${counts.rejected} rejected`);
    if (counts.error) parts.push(`${counts.error} failed`);
    title.textContent = waiting
      ? `Uploading ${waiting} file${waiting === 1 ? "" : "s"}…` + (parts.length ? ` (${parts.join(", ")})` : "")
      : parts.join(" · ") || "Done";
  }

  // The board refreshes as each file lands; the table waits for the whole batch so it
  // doesn't re-render under an inline edit.
  function maybeDone() {
    if (queue.length || active) return;
    clearBtn.hidden = false;
    if (changed) {
      changed = false;
      if (view === "table") refreshView();
    }
  }

  function refreshSoon() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refreshView, 250);
  }

  function refreshView() {
    htmx.ajax("GET", `/subjects/${subjectId}/view/${view}`, { target: "#view", swap: "innerHTML" });
  }

  // Placeholder chips in the sections files were dropped on, re-added after each refresh.
  viewEl.addEventListener("htmx:afterSwap", renderGhosts);

  function renderGhosts() {
    for (const item of ghosts) {
      if (item.ghostEl?.isConnected) continue;
      const chips = viewEl.querySelector(
        `.slot[data-slot="${item.target.slot}"]` +
        (item.target.week ? `[data-week="${item.target.week}"]` : ":not([data-week])") + " .chips");
      if (!chips) continue;
      chips.querySelector(".none")?.remove();
      const ghost = document.createElement("div");
      ghost.className = "chip ghost";
      ghost.innerHTML = '<div class="chip-main"><span class="chip-title"></span></div><div class="chip-file">Uploading…</div>';
      ghost.querySelector(".chip-title").textContent = item.file.name;
      chips.appendChild(ghost);
      item.ghostEl = ghost;
    }
  }

  function slotLabel(target) {
    const el = viewEl.querySelector(`.slot[data-slot="${target.slot}"] .slot-label`);
    const label = el ? el.textContent : target.slot;
    return target.week ? `Week ${target.week} ${label}` : label;
  }

  function mb(bytes, digits = 0) {
    return (bytes / 1048576).toFixed(digits);
  }
})();
