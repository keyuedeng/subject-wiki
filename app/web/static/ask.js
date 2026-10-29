// Ask page: send questions and stream the answers in, the composer (Enter to send,
// Shift+Enter for a new line, mode switch, filter dropdowns), message actions, and the
// source preview panel that citation chips open.
//
// The server answers /ask/stream with newline-delimited JSON events: start, status, delta
// (the answer so far, as HTML), then done or error (the finished message's HTML).
(() => {
  const root = document.getElementById("ask");
  if (!root) return;
  let chatId = root.dataset.chatId || "";
  const thread = document.getElementById("thread");
  const form = document.getElementById("composer");
  const input = document.getElementById("ask-input");
  const sendBtn = form.querySelector(".send");
  const panel = document.getElementById("source-panel");
  const sidebar = document.getElementById("chat-sidebar");
  const pendingTpl = document.getElementById("pending-template");
  const placeholder = root.dataset.placeholder;
  let busy = false;

  // ---- Composer

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });
  input.addEventListener("input", autosize);
  form.addEventListener("submit", (e) => { e.preventDefault(); send(); });

  function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 240)}px`;
  }

  form.querySelectorAll("[data-mode-value]").forEach((b) =>
    b.addEventListener("click", () => { setMode(b.dataset.modeValue); input.focus(); }));

  function setMode(mode) {
    form.elements.mode.value = mode;
    form.querySelectorAll("[data-mode-value]").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.modeValue === mode)));
    input.placeholder = mode === "where" ? "Which topic? e.g. Information Expert" : placeholder;
  }

  // Filter dropdowns: the button shows what's picked; none picked means all.
  const dropdowns = [...form.querySelectorAll("details.dropdown")];
  dropdowns.forEach((d) => {
    d.addEventListener("change", () => label(d));
    d.querySelector("[data-clear]")?.addEventListener("click", () => {
      d.querySelectorAll("input:checked").forEach((c) => (c.checked = false));
      label(d);
      d.open = false;
    });
    d.addEventListener("toggle", () => {
      if (d.open) dropdowns.forEach((o) => o !== d && (o.open = false));
    });
    label(d);
  });

  function label(d) {
    const picked = [...d.querySelectorAll("input:checked")];
    d.querySelector("[data-label]").textContent =
      picked.length === 0 ? d.dataset.all
        : picked.length === 1 ? picked[0].dataset.short
        : `${picked.length} ${d.dataset.many}`;
    d.classList.toggle("active", picked.length > 0);
  }

  document.addEventListener("click", (e) => {
    dropdowns.forEach((d) => { if (d.open && !d.contains(e.target)) d.open = false; });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const open = dropdowns.find((d) => d.open);
    if (open) { open.open = false; open.querySelector("summary").focus(); return; }
    if (!panel.hidden) closePanel();
  });

  // ---- Actions anywhere on the page

  document.addEventListener("click", (e) => {
    const ask = e.target.closest("[data-ask]");
    if (ask) {
      input.value = ask.dataset.ask;
      setMode(ask.dataset.mode || "auto");
      send();
      return;
    }
    const copy = e.target.closest("[data-copy]");
    if (copy) {
      navigator.clipboard.writeText(copy.dataset.copy).then(
        () => flash(copy, "Copied"), () => window.showToast("Couldn't copy", true));
      return;
    }
    const toggle = e.target.closest("[data-toggle-sources]");
    if (toggle) {
      const list = toggle.closest(".msg").querySelector(".msg-sources");
      const open = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(open));
      list.hidden = !open;
      return;
    }
    const link = e.target.closest("a[data-preview]");
    if (link && e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) {
      e.preventDefault();
      openPanel(link.dataset.preview, link);
      return;
    }
    if (e.target.closest("[data-close-panel]")) { closePanel(); return; }
    const chats = e.target.closest(".chats-toggle");
    if (chats) {
      const open = chats.getAttribute("aria-expanded") !== "true";
      chats.setAttribute("aria-expanded", String(open));
      root.classList.toggle("sidebar-open", open);
    }
  });

  function flash(button, text) {
    const span = button.querySelector("span") || button;
    const before = span.textContent;
    span.textContent = text;
    setTimeout(() => (span.textContent = before), 1500);
  }

  // ---- Source preview panel

  async function openPanel(url, link) {
    document.querySelectorAll("[data-preview].active").forEach((a) => a.classList.remove("active"));
    link.classList.add("active");
    panel.hidden = false;
    root.classList.add("panel-open");
    panel.innerHTML = '<p class="panel-empty">Loading…</p>';
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      panel.innerHTML = await res.text();
      htmx.process(panel);
    } catch (err) {
      panel.innerHTML = `<div class="panel-head"><div class="panel-label">Source</div>
        <button type="button" class="panel-close" data-close-panel aria-label="Close source preview">✕</button></div>
        <p class="panel-empty">Couldn't load this source.</p>`;
    }
    panel.focus({ preventScroll: true });
  }

  function closePanel() {
    panel.hidden = true;
    root.classList.remove("panel-open");
    const active = document.querySelector("[data-preview].active");
    active?.classList.remove("active");
    active?.focus({ preventScroll: true });
  }

  // ---- Asking

  async function send() {
    const question = input.value.trim();
    if (!question || busy) return;
    setBusy(true);
    thread.querySelector(".thread-empty")?.remove();
    const data = new FormData(form);
    data.set("q", question);
    if (chatId) data.set("chat_id", chatId);
    thread.append(userBubble(question, data));
    const pending = pendingTpl.content.firstElementChild.cloneNode(true);
    thread.append(pending);
    input.value = "";
    autosize();
    scrollToEnd(true);
    try {
      const res = await fetch(form.action, { method: "POST", body: data });
      if (!res.ok) throw new Error(await errorText(res));
      await readEvents(res, (event) => handle(event, pending));
      if (pending.isConnected) throw new Error("The answer stopped before it finished");
    } catch (err) {
      if (pending.isConnected) pending.replaceWith(errorCard(err.message, question, data.get("mode")));
    } finally {
      setBusy(false);
      input.focus();
    }
  }

  async function readEvents(res, onEvent) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (line) onEvent(JSON.parse(line));
      }
    }
    if (buffer.trim()) onEvent(JSON.parse(buffer));
  }

  function handle(event, pending) {
    if (event.type === "start") {
      if (!chatId) {
        chatId = event.chat_id;
        root.dataset.chatId = chatId;
        history.replaceState(null, "", event.url);
      }
      if (event.sidebar) setSidebar(event.sidebar);
    } else if (event.type === "status") {
      pending.querySelector(".pending-text").textContent = event.text;
    } else if (event.type === "delta") {
      pending.classList.add("streaming");
      pending.querySelector(".pending-text").textContent = "Writing…";
      pending.querySelector(".answer-body").innerHTML = event.html;
      scrollToEnd();
    } else if (event.type === "done" || event.type === "error") {
      const tpl = document.createElement("template");
      tpl.innerHTML = event.html.trim();
      const nodes = [...tpl.content.childNodes];
      pending.replaceWith(...nodes);
      nodes.forEach((n) => n.nodeType === 1 && htmx.process(n));
      if (event.sidebar) setSidebar(event.sidebar);
      scrollToEnd();
    }
  }

  function setSidebar(html) {
    sidebar.innerHTML = html;
    htmx.process(sidebar);
  }

  function setBusy(on) {
    busy = on;
    sendBtn.disabled = on;
    form.classList.toggle("busy", on);
  }

  function userBubble(question, data) {
    const wrap = document.createElement("div");
    wrap.className = "msg msg-user";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = question;
    wrap.append(bubble);
    const parts = [];
    if (data.get("mode") === "where") parts.push("Where is it taught?");
    const weeks = data.getAll("week");
    if (weeks.length) parts.push(weeks.length === 1 ? `Week ${weeks[0]}` : `Weeks ${weeks.join(", ")}`);
    form.querySelectorAll('input[name="type"]:checked').forEach((c) => parts.push(c.dataset.short));
    if (parts.length) {
      const meta = document.createElement("div");
      meta.className = "bubble-meta";
      meta.textContent = parts.join(" · ");
      wrap.append(meta);
    }
    return wrap;
  }

  function errorCard(message, question, mode) {
    const card = document.createElement("article");
    card.className = "msg msg-error";
    card.setAttribute("role", "alert");
    card.innerHTML = `<div class="nc-body"><div class="nc-title">Couldn't answer this</div>
      <p></p><button type="button" class="btn-small">Try again</button></div>`;
    card.querySelector("p").textContent = message;
    const retry = card.querySelector("button");
    retry.dataset.ask = question;
    retry.dataset.mode = mode || "auto";
    return card;
  }

  async function errorText(res) {
    const text = await res.text();
    try { return JSON.parse(text).detail || text; } catch (_) { return text || `HTTP ${res.status}`; }
  }

  // Follow along while an answer streams in, unless the reader has scrolled up. The
  // conversation scrolls in its own column, or with the page on phones.
  const main = root.querySelector(".chat-main");
  function scrollToEnd(force) {
    const el = getComputedStyle(main).overflowY === "auto" ? main : document.scrollingElement;
    const nearBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 240;
    if (force || nearBottom) el.scrollTop = el.scrollHeight;
  }

  if (thread.querySelector(".msg")) scrollToEnd(true);
  input.focus();
})();
