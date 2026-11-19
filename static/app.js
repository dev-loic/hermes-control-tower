// Hermes Control Tower — front
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const md = (s) => DOMPurify.sanitize(marked.parse(s || "", { breaks: true }));
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
};

const LOCALE = "en-GB";
const OWNER = { user: "Your move", hermes: "Hermes' move", waiting: "Waiting", none: "Nothing to do" };
const STATUS = { open: "Open", snoozed: "Snoozed", done: "Done", dropped: "Dropped" };
const isRoutine = (t) => !!t.cron_job_id;
const FILTERS = [
  { id: "user", label: "Your move", icon: "🫵", test: (t) => !isRoutine(t) && t.status === "open" && t.owner === "user" },
  { id: "hermes", label: "Hermes' move", icon: "🤖", test: (t) => !isRoutine(t) && t.status === "open" && t.owner === "hermes" },
  { id: "waiting", label: "Waiting", icon: "⏳", test: (t) => !isRoutine(t) && t.status === "open" && (t.owner === "waiting" || t.owner === "none") },
  { id: "open", label: "All open", icon: "📂", test: (t) => !isRoutine(t) && t.status === "open" },
  { sep: true },
  { id: "routines", label: "Routines", icon: "🔁", test: (t) => isRoutine(t), badge: (ts) => ts.filter((t) => isRoutine(t) && t.cron && t.cron.last_status && t.cron.last_status !== "ok").length },
  { sep: true },
  { id: "snoozed", label: "Snoozed", icon: "⏸️", test: (t) => !isRoutine(t) && t.status === "snoozed" },
  { id: "done", label: "Done", icon: "✅", test: (t) => !isRoutine(t) && t.status === "done" },
  { id: "dropped", label: "Dropped", icon: "🗑️", test: (t) => !isRoutine(t) && t.status === "dropped" },
];

const state = { topics: [], filter: localStorage.getItem("filter") || "open", sel: localStorage.getItem("sel"), q: "", detail: null, es: null, live: null };
if (!FILTERS.some((f) => f.id === state.filter)) state.filter = "open";

function rel(ts) {
  if (!ts) return "";
  const d = (Date.now() / 1000 - ts) / 86400;
  if (d < 1 / 24) return "just now";
  if (d < 1) return `${Math.round(d * 24)}h ago`;
  if (d < 2) return "yesterday";
  if (d < 30) return `${Math.round(d)}d ago`;
  return new Date(ts * 1000).toLocaleDateString(LOCALE, { day: "numeric", month: "short" });
}
const fmtDate = (ts) => new Date(ts * 1000).toLocaleDateString(LOCALE, { day: "numeric", month: "short", year: "numeric" });
function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso), now = new Date();
  const day = d.toDateString() === now.toDateString() ? "today" :
    d.toDateString() === new Date(now.getTime() + 86400000).toDateString() ? "tomorrow" :
    d.toDateString() === new Date(now.getTime() - 86400000).toDateString() ? "yesterday" :
    d.toLocaleDateString(LOCALE, { weekday: "short", day: "numeric", month: "short" });
  return `${day}, ${d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" })}`;
}
function cronPill(c) {
  if (!c) return `<span class="pill none">Unknown job</span>`;
  if (!c.enabled) return `<span class="pill snoozed">Paused</span>`;
  if (c.last_status && c.last_status !== "ok") return `<span class="pill dropped">Last run failed</span>`;
  return `<span class="pill done">Active</span>`;
}

// ------------------------------------------------------------------ data
async function loadTopics() {
  const data = await api("/api/topics");
  state.topics = data.topics;
  renderFilters();
  renderList();
  renderRefresh(data.refresh, data.last_refresh);
  if (data.refresh.running) pollRefresh();
}

function renderRefresh(r, last) {
  const btn = $("#refreshBtn");
  btn.disabled = !!r.running;
  btn.textContent = r.running ? "⏳ Analysing…" : "↻ Refresh";
  $("#refreshInfo").textContent = r.running ? r.message : r.error ? "⚠️ " + r.error.slice(0, 80) : last ? "Updated " + rel(last) : "";
}

let refreshTimer = null;
function pollRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    const r = await api("/api/refresh");
    renderRefresh(r);
    if (r.running) pollRefresh();
    else { await loadTopics(); if (state.sel) openTopic(state.sel, true); toast(r.error ? "Analysis failed" : "Topics updated"); }
  }, 2500);
}

// ------------------------------------------------------------------ sidebar & list
function renderFilters() {
  $("#filters").innerHTML = FILTERS.map((f) => {
    if (f.sep) return '<div class="sep"></div>';
    const n = state.topics.filter(f.test).length;
    const bad = f.badge ? f.badge(state.topics) : 0;
    return `<button data-f="${f.id}" class="${state.filter === f.id ? "active" : ""}"><span>${f.icon}</span>${f.label}${bad ? `<span class="alert-dot" title="${bad} failing">!</span>` : ""}<span class="count">${n || ""}</span></button>`;
  }).join("");
  document.querySelectorAll("#filters button").forEach((b) => (b.onclick = () => { state.filter = b.dataset.f; localStorage.setItem("filter", state.filter); renderFilters(); renderList(); }));
}

function renderList() {
  const f = FILTERS.find((x) => x.id === state.filter) || FILTERS[3];
  const q = state.q.toLowerCase();
  const items = q ? state.topics.filter((t) => (t.title + " " + t.summary + " " + t.next_step).toLowerCase().includes(q)) : state.topics.filter(f.test);
  const card = (t) => {
    const r = isRoutine(t);
    const sub = r ? (t.cron ? `${esc(t.cron.schedule)} · next ${esc(fmtWhen(t.cron.next_run_at))}` : "") : esc(t.next_step);
    const pill = r ? cronPill(t.cron) :
      t.status === "open" ? `<span class="pill ${t.owner}">${OWNER[t.owner]}</span>` :
      `<span class="pill ${t.status}">${STATUS[t.status]}${t.status === "snoozed" && t.snooze_until ? " → " + new Date(t.snooze_until * 1000).toLocaleDateString(LOCALE, { day: "numeric", month: "short" }) : ""}</span>`;
    return `
    <div class="card ${t.id === state.sel ? "sel" : ""} ${["done", "dropped"].includes(t.status) || (r && t.cron && !t.cron.enabled) ? "dim" : ""}" data-id="${t.id}">
      ${t.pinned ? '<span class="pin">📌</span>' : ""}
      <div class="emo">${esc(t.emoji)}</div>
      <div class="body">
        <div class="title">${esc(t.title)}</div>
        ${sub ? `<div class="next">${sub}</div>` : ""}
        <div class="meta">${t.running ? '<span class="dot-run"></span>' : ""}${pill}<span>${r ? (t.cron?.last_run_at ? "ran " + rel(Date.parse(t.cron.last_run_at) / 1000) : "") : rel(t.last_activity)}</span></div>
      </div>
    </div>`;
  };
  let html = "";
  if (!items.length) html = `<div class="muted" style="padding:30px 10px;text-align:center">${q ? "No results" : "Nothing here 🎉"}</div>`;
  else if (state.filter === "open" && !q) {
    for (const o of ["user", "hermes", "waiting", "none"]) {
      const g = items.filter((t) => t.owner === o);
      if (g.length) html += `<div class="group-title">${OWNER[o]} · ${g.length}</div>` + g.map(card).join("");
    }
  } else html = items.map(card).join("");
  $("#list").innerHTML = html;
  document.querySelectorAll(".card").forEach((c) => (c.onclick = () => openTopic(c.dataset.id)));
}

$("#search").oninput = (e) => { state.q = e.target.value; renderList(); };

// ------------------------------------------------------------------ detail
async function openTopic(id, keepScroll) {
  state.sel = id;
  localStorage.setItem("sel", id);
  renderList();
  let t;
  try { t = await api("/api/topics/" + id); } catch { $("#detail").innerHTML = '<div class="empty-state"><div class="big">🗼</div><p>Topic not found</p></div>'; return; }
  state.detail = t;
  renderDetail(keepScroll);
  if (t.running) attachStream(id);
}

function routineHead(t) {
  const c = t.cron;
  if (!c) return "";
  return `<div class="routine-info">
      <div><span class="k">Schedule</span>${esc(c.schedule)}</div>
      <div><span class="k">Next run</span>${c.enabled ? esc(fmtWhen(c.next_run_at)) : "paused"}</div>
      <div><span class="k">Last run</span>${esc(fmtWhen(c.last_run_at))} ${c.last_status ? `<span class="pill ${c.last_status === "ok" ? "done" : "dropped"}">${esc(c.last_status)}</span>` : ""}</div>
      ${c.deliver ? `<div><span class="k">Delivered to</span>${esc(String(c.deliver).split(":")[0])}</div>` : ""}
    </div>${c.last_error && c.last_status !== "ok" ? `<div class="box err"><h4>Last error</h4><p>${esc(String(c.last_error).slice(0, 400))}</p></div>` : ""}`;
}

function renderDetail(keepScroll) {
  const t = state.detail;
  const r = isRoutine(t);
  const prevScroll = keepScroll ? $(".d-scroll")?.scrollTop : null;
  const sessions = (t.sessions || []).map((s) => `<div class="sess" data-sid="${esc(s.id)}"><span>${esc(s.title || "(untitled)")}</span><span class="when">${fmtDate(s.started_at)}</span></div>`).join("");
  const isClosed = ["done", "dropped", "snoozed"].includes(t.status);
  const actions = r ? `
        ${t.cron?.enabled ? `<button class="act" data-a="cron:pause">⏸️ Pause</button>` : `<button class="act primary" data-a="cron:resume">▶️ Resume</button>`}
        <button class="act" data-a="cron:run">⚡ Run now</button>
        <button class="act drop" data-a="cron:remove">🗑️ Delete routine</button>` : `
        ${isClosed ? `<button class="act primary" data-a="reopen">↩︎ Reopen</button>` : ""}
        ${t.status !== "done" ? `<button class="act done" data-a="done">✅ Done</button>` : ""}
        ${t.status !== "snoozed" ? `<div class="menu-wrap"><button class="act" data-a="snooze">⏸️ Snooze ▾</button></div>` : ""}
        ${t.status !== "dropped" ? `<button class="act drop" data-a="drop">🗑️ Drop</button>` : ""}
        <button class="act" data-a="pin">${t.pinned ? "📌 Unpin" : "📌 Pin"}</button>`;
  const quick = r ? [
    ["How did the last runs go?", "How did the last runs of this routine go? Any failures or anything to improve?"],
    ["Change it", "I'd like to change this routine. Show me what it does today (schedule, prompt, delivery) so we can adjust it."],
    ["Is it still useful?", "Is this routine still useful to me? Give me your honest take and suggest keeping, changing or stopping it."],
  ] : [
    ["Where are we?", "Where are we on this topic? Check whether anything new happened (emails, tasks…)."],
    ["Next step?", "What is the concrete next step, and can you take care of it?"],
    ["Go ahead", "Go ahead and take care of the next step."],
    ["Remind me", "Add a Todoist reminder about this topic for next week."],
  ];
  $("#detail").innerHTML = `
    <div class="d-head">
      <div class="d-title"><span class="emo">${esc(t.emoji)}</span><h1 contenteditable="true" spellcheck="false" id="tTitle">${esc(t.title)}</h1></div>
      <div class="d-pills">
        ${r ? `<span class="pill waiting">🔁 Routine</span>${cronPill(t.cron)}` : `<span class="pill ${t.status === "open" ? "none" : t.status}">${STATUS[t.status]}</span>
        <select id="ownerSel" title="Whose move">${Object.entries(OWNER).map(([k, v]) => `<option value="${k}" ${k === t.owner ? "selected" : ""}>${v}</option>`).join("")}</select>`}
        <span class="muted small">· last activity ${rel(t.last_activity)}</span>
      </div>
      <div class="actions">${actions}</div>
    </div>
    <div class="d-scroll">
      <div class="d-info">
        ${r ? routineHead(t) : ""}
        ${t.summary ? `<div class="box"><h4>${r ? "What it does" : "Where we are"}</h4><p>${esc(t.summary)}</p></div>` : ""}
        ${t.next_step ? `<div class="box next"><h4>${r ? "Pending tweak" : "Next step"}</h4><p>${esc(t.next_step)}</p></div>` : ""}
        ${sessions ? `<details class="sessions"><summary>${t.sessions.length} linked conversation${t.sessions.length > 1 ? "s" : ""}</summary>${sessions}</details>` : ""}
      </div>
      <div class="chat" id="chat"></div>
    </div>
    <div class="composer">
      <div class="quick">${quick.map(([l, q]) => `<button data-q="${esc(q)}">${esc(l)}</button>`).join("")}</div>
      <div class="input-row">
        <textarea id="input" rows="1" placeholder="${r ? "Ask about or adjust this routine…" : "Talk to Hermes about this topic…"}"></textarea>
        <button class="send" id="sendBtn">Send</button>
      </div>
    </div>`;
  renderChat();
  if (prevScroll != null) $(".d-scroll").scrollTop = prevScroll;
  else scrollChat();

  const title = $("#tTitle");
  title.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); title.blur(); } };
  title.onblur = async () => { const v = title.textContent.trim(); if (v && v !== t.title) { await api("/api/topics/" + t.id, { method: "PATCH", body: { title: v } }); t.title = v; loadTopics(); } };
  const os = $("#ownerSel");
  if (os) os.onchange = async (e) => { await api("/api/topics/" + t.id, { method: "PATCH", body: { owner: e.target.value } }); t.owner = e.target.value; loadTopics(); };
  document.querySelectorAll(".actions .act").forEach((b) => (b.onclick = () => action(b.dataset.a, b)));
  document.querySelectorAll(".sess").forEach((s) => (s.onclick = () => showSession(s.dataset.sid)));
  document.querySelectorAll(".quick button").forEach((b) => (b.onclick = () => send(b.dataset.q)));
  const input = $("#input");
  input.oninput = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px"; };
  input.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(input.value); } };
  $("#sendBtn").onclick = () => (state.live ? stop() : send(input.value));
  updateSendBtn();
}

function renderChat() {
  const t = state.detail;
  const msgs = t.messages || [];
  let html = msgs.length ? "" : `<div class="hint">Ask a question or give an instruction: Hermes gets the full context of this ${isRoutine(t) ? "routine" : "topic"}.</div>`;
  for (const m of msgs) {
    if (m.role === "user") html += `<div class="msg user">${esc(m.content)}</div>`;
    else {
      const tools = JSON.parse(m.tools || "[]");
      const uniq = [...new Set(tools)];
      html += `<div class="msg assistant">${uniq.length ? `<div class="tools">${uniq.map((n) => { const k = tools.filter((x) => x === n).length; return `<span class="tool-chip">⚙︎ ${esc(n)}${k > 1 ? " ×" + k : ""}</span>`; }).join("")}</div>` : ""}<div class="md">${md(m.content)}</div></div>`;
    }
  }
  if (state.live && state.live.topic === t.id) {
    const l = state.live;
    html += `<div class="msg assistant">${l.tools.length ? `<div class="tools">${l.tools.map((x) => `<span class="tool-chip ${x.error ? "err" : ""}">${x.done ? "✓" : "…"} ${esc(x.name)}${x.preview ? " · " + esc(x.preview) : ""}</span>`).join("")}</div>` : ""}${l.text ? `<div class="md">${md(l.text)}</div>` : `<div class="typing"><span class="dot-run"></span>${esc(l.status || "Hermes is thinking…")}</div>`}</div>`;
  }
  $("#chat").innerHTML = html;
}

function scrollChat() { const s = $(".d-scroll"); if (s) s.scrollTop = s.scrollHeight; }
function updateSendBtn() { const b = $("#sendBtn"); if (!b) return; const live = state.live && state.live.topic === state.detail?.id; b.textContent = live ? "■ Stop" : "Send"; b.classList.toggle("stop", !!live); }

async function send(text, display) {
  text = (text || "").trim();
  const t = state.detail;
  if (!text || !t || state.live) return;
  try { await api(`/api/topics/${t.id}/chat`, { method: "POST", body: { message: text, display } }); }
  catch (e) { toast(e.message); return; }
  t.messages = [...(t.messages || []), { role: "user", content: display || text }];
  $("#input").value = "";
  attachStream(t.id);
}

function attachStream(id) {
  if (state.es) state.es.close();
  state.live = { topic: id, text: "", tools: [], status: "" };
  renderChat(); scrollChat(); updateSendBtn();
  const es = new EventSource(`/api/topics/${id}/stream`);
  state.es = es;
  const nearBottom = () => { const s = $(".d-scroll"); return !s || s.scrollHeight - s.scrollTop - s.clientHeight < 140; };
  es.onmessage = async (e) => {
    const ev = JSON.parse(e.data);
    const l = state.live;
    if (!l) return;
    const stick = nearBottom();
    if (ev.type === "delta") l.text += ev.text;
    else if (ev.type === "status") l.status = ev.text;
    else if (ev.type === "tool") { l.tools.push({ name: ev.name, preview: ev.preview }); if (l.text && !l.text.endsWith("\n\n")) l.text += "\n\n"; }
    else if (ev.type === "tool_done") { const x = [...l.tools].reverse().find((x) => x.name === ev.name && !x.done); if (x) { x.done = true; x.error = ev.error; } }
    else if (ev.type === "end") {
      es.close(); state.es = null; state.live = null;
      if (state.sel === id) await openTopic(id, true);
      scrollChat();
      loadTopics();
      return;
    }
    if (state.sel === id && state.detail?.id === id) { renderChat(); if (stick) scrollChat(); }
  };
  es.onerror = () => { es.close(); state.es = null; state.live = null; updateSendBtn(); setTimeout(() => state.sel === id && openTopic(id, true), 1500); };
}

async function stop() { if (state.detail) await api(`/api/topics/${state.detail.id}/stop`, { method: "POST" }); }

// ------------------------------------------------------------------ actions
async function setStatus(status, extra = {}) {
  const t = state.detail;
  const prev = { status: t.status, snooze_until: t.snooze_until };
  await api("/api/topics/" + t.id, { method: "PATCH", body: { status, ...extra } });
  await loadTopics();
  await openTopic(t.id, true);
  return prev;
}

async function cron(actionName) {
  const t = state.detail;
  try { await api(`/api/topics/${t.id}/cron`, { method: "POST", body: { action: actionName } }); }
  catch (e) { toast("⚠️ " + e.message); return; }
  await loadTopics();
  await openTopic(t.id, true);
  toast({ pause: "Routine paused", resume: "Routine resumed", run: "Routine will run on the next scheduler tick", remove: "Routine deleted" }[actionName]);
}

async function action(a, btn) {
  const t = state.detail;
  if (a.startsWith("cron:")) {
    const what = a.slice(5);
    if (what === "remove") {
      modal(`<header>🗑️ Delete the routine “${esc(t.title)}”?</header>
        <div class="content"><p>The Hermes cron job will be removed and will stop running. This cannot be undone (you can ask Hermes to recreate it later).</p></div>
        <footer><button class="act" data-m="cancel">Cancel</button><button class="act primary" data-m="ok">Delete</button></footer>`,
        async (c) => { if (c === "ok") await cron("remove"); });
    } else await cron(what);
    return;
  }
  if (a === "reopen") { await setStatus("open"); toast("Topic reopened"); }
  if (a === "done") { const prev = await setStatus("done"); toast("Marked as done", () => setStatus(prev.status)); }
  if (a === "pin") { await api("/api/topics/" + t.id, { method: "PATCH", body: { pinned: !t.pinned } }); await loadTopics(); openTopic(t.id, true); }
  if (a === "snooze") {
    const wrap = btn.parentElement;
    if ($(".menu", wrap)) { $(".menu", wrap).remove(); return; }
    const opts = [["Tomorrow", 1], ["In 3 days", 3], ["In a week", 7], ["In a month", 30]];
    const m = document.createElement("div");
    m.className = "menu";
    m.innerHTML = opts.map(([l, d]) => `<button data-d="${d}">${l}</button>`).join("");
    wrap.appendChild(m);
    m.querySelectorAll("button").forEach((b) => (b.onclick = async () => {
      const until = new Date(); until.setDate(until.getDate() + +b.dataset.d); until.setHours(8, 0, 0, 0);
      await setStatus("snoozed", { snooze_until: until.getTime() / 1000 });
      toast(`Snoozed until ${until.toLocaleDateString(LOCALE, { day: "numeric", month: "long" })}`);
    }));
    setTimeout(() => document.addEventListener("click", function h(e) { if (!wrap.contains(e.target)) { m.remove(); document.removeEventListener("click", h); } }), 0);
  }
  if (a === "drop") {
    modal(`
      <header>🗑️ Drop “${esc(t.title)}”?</header>
      <div class="content"><p>The topic leaves your list (you'll still find it under “Dropped”).</p>
      <p>Hermes can also look at what is still attached to it on its side (Todoist tasks, cron jobs, drafts, reminders) and suggest a cleanup. Nothing is deleted without your confirmation.</p></div>
      <footer><button class="act" data-m="cancel">Cancel</button><button class="act" data-m="hide">Just hide it</button><button class="act primary" data-m="clean">Hide and suggest a cleanup</button></footer>`,
      async (choice) => {
        if (choice === "cancel") return;
        const prev = await setStatus("dropped");
        if (choice === "clean") {
          send("I'm dropping this topic, I'm no longer interested. List what is still attached to it on your side (Todoist tasks, Hermes cron jobs, Gmail drafts, reminders, temp files) and suggest a cleanup. Don't delete anything without my explicit confirmation.", "🗑️ I'm dropping this topic: check what's still attached to it and suggest a cleanup.");
        } else toast("Topic dropped", () => setStatus(prev.status));
      });
  }
}

$("#newTopic").onclick = () => {
  modal(`
    <header>✨ New topic</header>
    <div class="content"><input type="text" id="newTitle" placeholder="e.g. Find a school for September"><p class="muted small">You can then talk about it with Hermes right away.</p></div>
    <footer><button class="act" data-m="cancel">Cancel</button><button class="act primary" data-m="ok">Create</button></footer>`,
    async (c) => {
      if (c !== "ok") return;
      const title = $("#newTitle").value.trim();
      if (!title) return;
      const t = await api("/api/topics", { method: "POST", body: { title } });
      state.filter = "open"; await loadTopics(); openTopic(t.id);
      setTimeout(() => $("#input")?.focus(), 200);
    });
  setTimeout(() => { const i = $("#newTitle"); i.focus(); i.onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); $('[data-m="ok"]').click(); } }; }, 30);
};

$("#refreshBtn").onclick = async () => { const r = await api("/api/refresh", { method: "POST" }); renderRefresh(r.refresh); pollRefresh(); };

async function showSession(sid) {
  const data = await api("/api/sessions/" + encodeURIComponent(sid));
  const msgs = data.messages.map((m) => `<div class="tr-msg"><div class="who">${m.role === "user" ? "You" : "Hermes"} · ${new Date(m.timestamp * 1000).toLocaleString(LOCALE, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</div><div class="md">${m.role === "user" ? esc(m.content).replace(/\n/g, "<br>") : md(m.content)}</div></div>`).join("");
  modal(`<header><span>${esc(data.meta?.title || "Conversation")}</span><button class="x" data-m="cancel">✕</button></header><div class="content">${msgs || '<p class="muted">Empty</p>'}</div>`, null, true);
}

// ------------------------------------------------------------------ modal / toast
function modal(html, cb, wide) {
  const bg = $("#modalBg"), m = $("#modal");
  m.className = "modal" + (wide ? " wide" : "");
  m.innerHTML = html;
  bg.classList.remove("hidden");
  const close = () => bg.classList.add("hidden");
  bg.onclick = (e) => { if (e.target === bg) close(); };
  m.querySelectorAll("[data-m]").forEach((b) => (b.onclick = async () => { const c = b.dataset.m; if (cb) await cb(c); close(); }));
  document.onkeydown = (e) => { if (e.key === "Escape") close(); };
}

let toastTimer;
function toast(text, undo) {
  const el = $("#toast");
  el.innerHTML = `<span>${esc(text)}</span>${undo ? '<button id="undoBtn">Undo</button>' : ""}`;
  el.classList.remove("hidden");
  if (undo) $("#undoBtn").onclick = async () => { el.classList.add("hidden"); await undo(); };
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 6000);
}

// ------------------------------------------------------------------ boot
loadTopics().then(() => { if (state.sel && state.topics.some((t) => t.id === state.sel)) openTopic(state.sel); });
setInterval(() => { if (!state.live) loadTopics(); }, 60000);
