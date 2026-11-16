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

const OWNER = { user: "À toi", hermes: "À moi", waiting: "En attente", none: "Rien à faire" };
const STATUS = { open: "En cours", snoozed: "En pause", done: "Fait", dropped: "Droppé" };
const FILTERS = [
  { id: "user", label: "À toi", icon: "🫵", test: (t) => t.status === "open" && t.owner === "user" },
  { id: "hermes", label: "À moi", icon: "🤖", test: (t) => t.status === "open" && t.owner === "hermes" },
  { id: "waiting", label: "En attente", icon: "⏳", test: (t) => t.status === "open" && (t.owner === "waiting" || t.owner === "none") },
  { id: "open", label: "En cours", icon: "📂", test: (t) => t.status === "open" },
  { sep: true },
  { id: "snoozed", label: "En pause", icon: "⏸️", test: (t) => t.status === "snoozed" },
  { id: "done", label: "Faits", icon: "✅", test: (t) => t.status === "done" },
  { id: "dropped", label: "Droppés", icon: "🗑️", test: (t) => t.status === "dropped" },
];

const state = { topics: [], filter: localStorage.getItem("filter") || "open", sel: localStorage.getItem("sel"), q: "", detail: null, es: null, live: null };

function rel(ts) {
  if (!ts) return "";
  const d = (Date.now() / 1000 - ts) / 86400;
  if (d < 1 / 24) return "à l'instant";
  if (d < 1) return `il y a ${Math.round(d * 24)} h`;
  if (d < 2) return "hier";
  if (d < 30) return `il y a ${Math.round(d)} j`;
  return new Date(ts * 1000).toLocaleDateString("fr-FR", { day: "numeric", month: "short" });
}
const fmtDate = (ts) => new Date(ts * 1000).toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });

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
  btn.textContent = r.running ? "⏳ Analyse en cours…" : "↻ Mettre à jour";
  $("#refreshInfo").textContent = r.running ? r.message : r.error ? "⚠️ " + r.error.slice(0, 80) : last ? "Mis à jour " + rel(last) : "";
}

let refreshTimer = null;
function pollRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    const r = await api("/api/refresh");
    renderRefresh(r);
    if (r.running) pollRefresh();
    else { await loadTopics(); if (state.sel) openTopic(state.sel, true); toast(r.error ? "L'analyse a échoué" : "Sujets mis à jour"); }
  }, 2500);
}

// ------------------------------------------------------------------ sidebar & list
function renderFilters() {
  $("#filters").innerHTML = FILTERS.map((f) =>
    f.sep ? '<div class="sep"></div>' :
    `<button data-f="${f.id}" class="${state.filter === f.id ? "active" : ""}"><span>${f.icon}</span>${f.label}<span class="count">${state.topics.filter(f.test).length || ""}</span></button>`
  ).join("");
  document.querySelectorAll("#filters button").forEach((b) => (b.onclick = () => { state.filter = b.dataset.f; localStorage.setItem("filter", state.filter); renderFilters(); renderList(); }));
}

function renderList() {
  const f = FILTERS.find((x) => x.id === state.filter) || FILTERS[3];
  const q = state.q.toLowerCase();
  let items = q ? state.topics.filter((t) => (t.title + " " + t.summary + " " + t.next_step).toLowerCase().includes(q)) : state.topics.filter(f.test);
  const card = (t) => `
    <div class="card ${t.id === state.sel ? "sel" : ""} ${["done", "dropped"].includes(t.status) ? "dim" : ""}" data-id="${t.id}">
      ${t.pinned ? '<span class="pin">📌</span>' : ""}
      <div class="emo">${esc(t.emoji)}</div>
      <div class="body">
        <div class="title">${esc(t.title)}</div>
        ${t.next_step ? `<div class="next">${esc(t.next_step)}</div>` : ""}
        <div class="meta">
          ${t.running ? '<span class="dot-run"></span>' : ""}
          ${t.status === "open" ? `<span class="pill ${t.owner}">${OWNER[t.owner]}</span>` : `<span class="pill ${t.status}">${STATUS[t.status]}${t.status === "snoozed" && t.snooze_until ? " → " + new Date(t.snooze_until * 1000).toLocaleDateString("fr-FR", { day: "numeric", month: "short" }) : ""}</span>`}
          <span>${rel(t.last_activity)}</span>
        </div>
      </div>
    </div>`;
  let html = "";
  if (!items.length) html = `<div class="muted" style="padding:30px 10px;text-align:center">${q ? "Aucun résultat" : "Rien ici 🎉"}</div>`;
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
  try { t = await api("/api/topics/" + id); } catch { $("#detail").innerHTML = '<div class="empty-state"><div class="big">🧭</div><p>Sujet introuvable</p></div>'; return; }
  state.detail = t;
  renderDetail(keepScroll);
  if (t.running) attachStream(id);
}

function renderDetail(keepScroll) {
  const t = state.detail;
  const prevScroll = keepScroll ? $(".d-scroll")?.scrollTop : null;
  const sessions = (t.sessions || []).map((s) => `<div class="sess" data-sid="${esc(s.id)}"><span>${esc(s.title || "(sans titre)")}</span><span class="when">${fmtDate(s.started_at)}</span></div>`).join("");
  const isClosed = ["done", "dropped", "snoozed"].includes(t.status);
  $("#detail").innerHTML = `
    <div class="d-head">
      <div class="d-title"><span class="emo">${esc(t.emoji)}</span><h1 contenteditable="true" spellcheck="false" id="tTitle">${esc(t.title)}</h1></div>
      <div class="d-pills">
        <span class="pill ${t.status === "open" ? "none" : t.status}">${STATUS[t.status]}</span>
        <select id="ownerSel" title="Qui a la main">${Object.entries(OWNER).map(([k, v]) => `<option value="${k}" ${k === t.owner ? "selected" : ""}>${v}</option>`).join("")}</select>
        <span class="muted small">· dernière activité ${rel(t.last_activity)}</span>
      </div>
      <div class="actions">
        ${isClosed ? `<button class="act primary" data-a="reopen">↩︎ Rouvrir</button>` : ""}
        ${t.status !== "done" ? `<button class="act done" data-a="done">✅ Fait</button>` : ""}
        ${t.status !== "snoozed" ? `<div class="menu-wrap"><button class="act" data-a="snooze">⏸️ Pause ▾</button></div>` : ""}
        ${t.status !== "dropped" ? `<button class="act drop" data-a="drop">🗑️ Drop</button>` : ""}
        <button class="act" data-a="pin">${t.pinned ? "📌 Désépingler" : "📌 Épingler"}</button>
      </div>
    </div>
    <div class="d-scroll">
      <div class="d-info">
        ${t.summary ? `<div class="box"><h4>Où on en est</h4><p>${esc(t.summary)}</p></div>` : ""}
        ${t.next_step ? `<div class="box next"><h4>Prochaine étape</h4><p>${esc(t.next_step)}</p></div>` : ""}
        ${sessions ? `<details class="sessions"><summary>${t.sessions.length} conversation${t.sessions.length > 1 ? "s" : ""} liée${t.sessions.length > 1 ? "s" : ""}</summary>${sessions}</details>` : ""}
      </div>
      <div class="chat" id="chat"></div>
    </div>
    <div class="composer">
      <div class="quick">
        <button data-q="Où on en est sur ce sujet ? Vérifie s'il y a du nouveau (mails, tâches…).">Où on en est ?</button>
        <button data-q="Quelle est la prochaine étape concrète, et est-ce que tu peux t'en charger ?">Prochaine étape ?</button>
        <button data-q="Vas-y, occupe-toi de la prochaine étape.">Vas-y, fais-le</button>
        <button data-q="Rappelle-moi ce sujet dans Todoist pour la semaine prochaine.">Rappel Todoist</button>
      </div>
      <div class="input-row">
        <textarea id="input" rows="1" placeholder="Parle-moi de ce sujet…"></textarea>
        <button class="send" id="sendBtn">Envoyer</button>
      </div>
    </div>`;
  renderChat();
  if (prevScroll != null) $(".d-scroll").scrollTop = prevScroll;
  else scrollChat();

  const title = $("#tTitle");
  title.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); title.blur(); } };
  title.onblur = async () => { const v = title.textContent.trim(); if (v && v !== t.title) { await api("/api/topics/" + t.id, { method: "PATCH", body: { title: v } }); t.title = v; loadTopics(); } };
  $("#ownerSel").onchange = async (e) => { await api("/api/topics/" + t.id, { method: "PATCH", body: { owner: e.target.value } }); t.owner = e.target.value; loadTopics(); };
  document.querySelectorAll(".actions .act").forEach((b) => (b.onclick = (e) => action(b.dataset.a, b, e)));
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
  let html = msgs.length ? "" : `<div class="hint">Pose une question ou donne une consigne : Hermes reprend tout le contexte du sujet.</div>`;
  for (const m of msgs) {
    if (m.role === "user") html += `<div class="msg user">${esc(m.content)}</div>`;
    else {
      const tools = JSON.parse(m.tools || "[]");
      const uniq = [...new Set(tools)];
      html += `<div class="msg assistant">${uniq.length ? `<div class="tools">${uniq.map((n) => `<span class="tool-chip">⚙︎ ${esc(n)}${tools.filter((x) => x === n).length > 1 ? " ×" + tools.filter((x) => x === n).length : ""}</span>`).join("")}</div>` : ""}<div class="md">${md(m.content)}</div></div>`;
    }
  }
  if (state.live && state.live.topic === t.id) {
    const l = state.live;
    html += `<div class="msg assistant" id="liveMsg">${l.tools.length ? `<div class="tools">${l.tools.map((x) => `<span class="tool-chip ${x.error ? "err" : ""}">${x.done ? "✓" : "…"} ${esc(x.name)}${x.preview ? " · " + esc(x.preview) : ""}</span>`).join("")}</div>` : ""}${l.text ? `<div class="md">${md(l.text)}</div>` : `<div class="typing"><span class="dot-run"></span>${esc(l.status || "Hermes réfléchit…")}</div>`}</div>`;
  }
  $("#chat").innerHTML = html;
}

function scrollChat() { const s = $(".d-scroll"); if (s) s.scrollTop = s.scrollHeight; }
function updateSendBtn() { const b = $("#sendBtn"); if (!b) return; const live = state.live && state.live.topic === state.detail?.id; b.textContent = live ? "■ Stop" : "Envoyer"; b.classList.toggle("stop", !!live); }

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

async function action(a, btn) {
  const t = state.detail;
  if (a === "reopen") { await setStatus("open"); toast("Sujet rouvert"); }
  if (a === "done") { const prev = await setStatus("done"); toast("Marqué comme fait", () => setStatus(prev.status)); }
  if (a === "pin") { await api("/api/topics/" + t.id, { method: "PATCH", body: { pinned: !t.pinned } }); await loadTopics(); openTopic(t.id, true); }
  if (a === "snooze") {
    const wrap = btn.parentElement;
    if ($(".menu", wrap)) { $(".menu", wrap).remove(); return; }
    const opts = [["Demain", 1], ["Dans 3 jours", 3], ["Dans une semaine", 7], ["Dans un mois", 30]];
    const m = document.createElement("div");
    m.className = "menu";
    m.innerHTML = opts.map(([l, d]) => `<button data-d="${d}">${l}</button>`).join("");
    wrap.appendChild(m);
    m.querySelectorAll("button").forEach((b) => (b.onclick = async () => {
      const until = new Date(); until.setDate(until.getDate() + +b.dataset.d); until.setHours(8, 0, 0, 0);
      await setStatus("snoozed", { snooze_until: until.getTime() / 1000 });
      toast(`En pause jusqu'au ${until.toLocaleDateString("fr-FR", { day: "numeric", month: "long" })}`);
    }));
    setTimeout(() => document.addEventListener("click", function h(e) { if (!wrap.contains(e.target)) { m.remove(); document.removeEventListener("click", h); } }), 0);
  }
  if (a === "drop") {
    modal(`
      <header>🗑️ Dropper « ${esc(t.title)} » ?</header>
      <div class="content"><p>Le sujet disparaît de ta liste (tu le retrouves dans « Droppés »).</p>
      <p>Je peux aussi regarder ce qui y est encore lié de mon côté (tâches Todoist, crons, brouillons, rappels) et te proposer de le nettoyer. Je ne supprime rien sans ta confirmation.</p></div>
      <footer><button class="act" data-m="cancel">Annuler</button><button class="act" data-m="hide">Juste masquer</button><button class="act primary" data-m="clean">Masquer et proposer le nettoyage</button></footer>`,
      async (choice) => {
        if (choice === "cancel") return;
        const prev = await setStatus("dropped");
        if (choice === "clean") {
          send("Je droppe ce sujet, il ne m'intéresse plus. Liste ce qui y est encore lié de ton côté (tâches Todoist, crons Hermes, brouillons Gmail, rappels, fichiers temporaires) et propose-moi le nettoyage. Ne supprime rien sans ma confirmation explicite.", "🗑️ Je droppe ce sujet : regarde ce qui y est encore lié et propose-moi le nettoyage.");
        } else toast("Sujet droppé", () => setStatus(prev.status));
      });
  }
}

$("#newTopic").onclick = () => {
  modal(`
    <header>✨ Nouveau sujet</header>
    <div class="content"><input type="text" id="newTitle" placeholder="Ex : Trouver une école pour la rentrée"><p class="muted small">Tu pourras ensuite en parler directement avec moi.</p></div>
    <footer><button class="act" data-m="cancel">Annuler</button><button class="act primary" data-m="ok">Créer</button></footer>`,
    async (c) => {
      if (c !== "ok") return;
      const title = $("#newTitle").value.trim();
      if (!title) return;
      const t = await api("/api/topics", { method: "POST", body: { title } });
      state.filter = "open"; await loadTopics(); openTopic(t.id);
      setTimeout(() => $("#input")?.focus(), 200);
    });
  setTimeout(() => { const i = $("#newTitle"); i.focus(); i.onkeydown = (e) => e.key === "Enter" && $('[data-m="ok"]').click(); }, 30);
};

$("#refreshBtn").onclick = async () => { const r = await api("/api/refresh", { method: "POST" }); renderRefresh(r.refresh); pollRefresh(); };

async function showSession(sid) {
  const data = await api("/api/sessions/" + encodeURIComponent(sid));
  const msgs = data.messages.map((m) => `<div class="tr-msg"><div class="who">${m.role === "user" ? "Toi" : "Hermes"} · ${new Date(m.timestamp * 1000).toLocaleString("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</div><div class="md">${m.role === "user" ? esc(m.content).replace(/\n/g, "<br>") : md(m.content)}</div></div>`).join("");
  modal(`<header><span>${esc(data.meta?.title || "Conversation")}</span><button class="x" data-m="cancel">✕</button></header><div class="content">${msgs || '<p class="muted">Vide</p>'}</div>`, null, true);
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
  el.innerHTML = `<span>${esc(text)}</span>${undo ? '<button id="undoBtn">Annuler</button>' : ""}`;
  el.classList.remove("hidden");
  if (undo) $("#undoBtn").onclick = async () => { el.classList.add("hidden"); await undo(); };
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 6000);
}

// ------------------------------------------------------------------ boot
loadTopics().then(() => { if (state.sel && state.topics.some((t) => t.id === state.sel)) openTopic(state.sel); });
setInterval(() => { if (!state.live) loadTopics(); }, 60000);
