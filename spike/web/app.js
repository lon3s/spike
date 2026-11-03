/* spike – Handy-Oberfläche. Spricht nur mit dem spike-Webserver auf dem PC (/api/…). */
"use strict";
const $ = s => document.querySelector(s);
const chatEl = $("#chat");
const S = {
  st: null, chats: [], archived: 0, showArchive: false, cur: null, title: "Neuer Chat",
  blocks: [], n: 0, live: {running: false}, runs: [], atts: [], since: 0, es: null, sending: false,
};
const LS = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} },
};

const ICON = {
  chev: '<svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="m9 6 6 6-6 6"/></svg>',
  check: '<svg class="ck" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="m5 12 5 5L20 7"/></svg>',
  copy: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  speak: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 5 6 9H3v6h3l5 4Z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/></svg>',
  warn: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg>',
  wrench: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4Z"/></svg>',
  term: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="m5 8 4 4-4 4M12 16h7"/></svg>',
  search: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  file: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z"/><path d="M14 3v6h6"/></svg>',
};
const TOOL_ICON = n => /shell|python/.test(n) ? ICON.term : /find|grep|search|web/.test(n) ? ICON.search
  : /file|read|write|edit/.test(n) ? ICON.file : ICON.wrench;

// ---------- Hilfen ----------
const esc = t => String(t ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
async function api(path, opts = {}) {
  const o = {credentials: "same-origin", ...opts};
  if (o.json !== undefined) { o.method = o.method || "POST"; o.body = JSON.stringify(o.json); o.headers = {"Content-Type": "application/json"}; }
  const r = await fetch(path, o);
  if (r.status === 401) { showLogin(); throw new Error("Nicht angemeldet"); }
  const ct = r.headers.get("Content-Type") || "";
  if (!r.ok) { let m = `Fehler ${r.status}`; try { m = (await r.json()).error || m; } catch {} throw new Error(m); }
  return ct.includes("json") ? r.json() : r.blob();
}
function toast(title, body, chat) {
  $("#pushT").textContent = title; $("#pushB").textContent = body || "";
  const p = $("#push"); p.dataset.chat = chat || ""; p.classList.add("show");
  clearTimeout(toast.t); toast.t = setTimeout(() => p.classList.remove("show"), 5000);
}
$("#push").onclick = () => { const c = $("#push").dataset.chat; $("#push").classList.remove("show"); if (c) openChat(c); };
function copyText(t) {
  const fallback = () => { const a = document.createElement("textarea"); a.value = t; document.body.appendChild(a); a.select(); try { document.execCommand("copy"); } catch {} a.remove(); };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(t).catch(fallback); else fallback();
  toast("Kopiert", "Text liegt in der Zwischenablage.");
}
const short = m => (m || "").split("/").pop().replace(/\.gguf$/i, "");
const nearBottom = () => chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 80;
const toBottom = () => { chatEl.scrollTop = chatEl.scrollHeight; };

// ---------- Markdown (klein, ohne Bibliothek) ----------
function inline(t) {
  const codes = [];
  t = esc(t).replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>')
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/__([^_]+)__/g, "<b>$1</b>")
    .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, "$1<i>$2</i>").replace(/(^|[^_\w])_([^_\s][^_]*?)_(?!\w)/g, "$1<i>$2</i>")
    .replace(/~~([^~]+)~~/g, "<s>$1</s>");
  return t.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[i]}</code>`);
}
function mdBlocks(text) {
  const lines = text.split("\n"), out = [];
  let i = 0;
  const isTableSep = l => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) { i++; continue; }
    let m;
    if ((m = l.match(/^(#{1,4})\s+(.*)/))) { out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); i++; continue; }
    if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(l)) { out.push("<hr>"); i++; continue; }
    if (/^\s*>/.test(l)) { const q = []; while (i < lines.length && /^\s*>/.test(lines[i])) q.push(lines[i++].replace(/^\s*>\s?/, "")); out.push(`<blockquote>${mdBlocks(q.join("\n"))}</blockquote>`); continue; }
    if (l.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => inline(c.trim()));
      let h = `<table><thead><tr>${cells(l).map(c => `<th>${c}</th>`).join("")}</tr></thead><tbody>`;
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) h += `<tr>${cells(lines[i++]).map(c => `<td>${c}</td>`).join("")}</tr>`;
      out.push(h + "</tbody></table>"); continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(l)) {
      const ordered = /^\s*\d/.test(l), items = [];
      while (i < lines.length && (/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
        if (/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) items.push(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ""));
        else items[items.length - 1] += " " + lines[i].trim();
        i++;
      }
      out.push(`<${ordered ? "ol" : "ul"}>${items.map(x => `<li>${inline(x.replace(/^\[ \]\s/, "☐ ").replace(/^\[x\]\s/i, "☑ "))}</li>`).join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }
    const p = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*>|\s*([-*+]|\d+[.)])\s+)/.test(lines[i])) p.push(lines[i++]);
    if (!p.length) p.push(lines[i++]);
    out.push(`<p>${p.map(inline).join("<br>")}</p>`);
  }
  return out.join("");
}
function md(text) {
  const parts = String(text || "").split(/^```/m);
  let html = "";
  parts.forEach((part, k) => {
    if (k % 2 === 0) { html += mdBlocks(part); return; }
    const nl = part.indexOf("\n");
    const lang = nl < 0 ? part.trim() : part.slice(0, nl).trim();
    const code = (nl < 0 ? "" : part.slice(nl + 1)).replace(/\n$/, "");
    html += `<div class="pre"><div class="lang"><span>${esc(lang || "code")}</span><button data-copy>Kopieren</button></div><pre>${esc(code)}</pre></div>`;
  });
  return html;
}
function diffHtml(d) {
  return d.split("\n").map(l => {
    const e = esc(l);
    if (l.startsWith("+++") || l.startsWith("---")) return `<span class="hunk">${e}</span>`;
    if (l.startsWith("@@")) return `<span class="hunk">${e}</span>`;
    if (l.startsWith("+")) return `<span class="add">${e}</span>`;
    if (l.startsWith("-")) return `<span class="del">${e}</span>`;
    return e + "\n";
  }).join("");
}

// ---------- Darstellung ----------
function attHtml(atts) {
  return (atts || []).map(a => a.url ? `<img src="${esc(a.url)}" alt="${esc(a.name)}" loading="lazy">`
    : `<div class="att">${ICON.file}<span>${esc(a.name)}</span></div>`).join("");
}
function toolHtml(b, liveOut) {
  const running = b.result == null;
  const st = running ? '<span class="spin"></span>' : b.denied ? '<span class="st no">abgelehnt</span>' : '<span class="st ok">✓</span>';
  let body = "";
  if (b.diff) body += `<pre class="term">${diffHtml(b.diff)}</pre>`;
  const out = running ? (liveOut || "") : (b.diff ? "" : b.result);
  if (out || running) body += `<pre class="term" data-out="${esc(b.id)}">${esc(out)}</pre>`;
  const open = b.diff || (running && liveOut) ? " open" : "";
  return `<details class="tool" data-tid="${esc(b.id)}"${open}><summary>${TOOL_ICON(b.name)}<code>${esc(b.name)} ${esc(b.args)}</code>${st}${ICON.chev}</summary>${body}</details>`;
}
function blockHtml(b, last) {
  switch (b.t) {
    case "user": return `<div class="u">${esc(b.text)}${attHtml(b.atts)}</div>`;
    case "ai": {
      let h = "";
      if (b.think) h += `<details class="think"><summary>💭 Gedanken</summary><div>${esc(b.think)}</div></details>`;
      if (b.text) h += `<div class="a">${md(b.text)}</div>`;
      if (b.text && last) h += `<div class="acts"><button data-act="copy" aria-label="Kopieren">${ICON.copy}</button><button data-act="speak" aria-label="Vorlesen">${ICON.speak}</button></div>`;
      return h;
    }
    case "tool": return toolHtml(b, S.live.tool && S.live.tool.id === b.id ? S.live.tool.out : "");
    case "shell": return `<div class="tool"><pre class="term">$ ${esc(b.cmd)}\n${esc(b.out)}${b.exit != null ? `\nexit ${b.exit}` : ""}</pre></div>`;
    case "ask": return `<div class="card askdone"><div class="h">❓ ${esc(b.q || "Rückfrage")}</div><div class="why">↳ ${esc(b.a)}</div></div>`;
    case "note": return `<div class="note">${attHtml(b.atts)}</div>`;
    case "sum": return `<div class="sum">📦 Älterer Verlauf ist zusammengefasst</div>`;
  }
  return "";
}
function render() {
  const stick = nearBottom();
  if (!S.cur && !S.blocks.length) {
    chatEl.innerHTML = `<div class="welcome"><b>Wie kann ich helfen?</b><span>Modell und Profil unten wechseln.<br>Mikro gedrückt halten zum Sprechen.</span></div><div id="livebox"></div>`;
  } else {
    const lastAi = S.live.running ? -1 : S.blocks.map(b => b.t).lastIndexOf("ai");
    chatEl.innerHTML = S.blocks.map((b, i) => blockHtml(b, i === lastAi)).join("") + '<div id="livebox"></div>';
  }
  renderLive();
  if (stick) toBottom();
}
function renderLive() {
  const box = $("#livebox"); if (!box) return;
  const L = S.live, stick = nearBottom();
  let h = "";
  if (L.running) {
    const lv = L.live || {};
    if (lv.think && !lv.content) h += `<details class="think"><summary><span class="spin"></span> denkt nach …</summary><div>${esc(lv.think)}</div></details>`;
    if (lv.content) h += `<div class="a cursor">${md(lv.content)}</div>`;
    if (L.pending && L.pending.kind === "ask") {
      const p = L.pending, o = p.options || [];
      const opts = p.multi
        ? o.map((x, i) => `<label class="askopt"><input type="checkbox" data-askcb="${i}"><span>${esc(x)}</span></label>`).join("")
          + `<div class="btns"><button class="yes" data-askok disabled>Übernehmen</button></div>`
        : o.map((x, i) => `<button class="askopt${i ? "" : " yes"}" data-ask="${i}">${esc(x)}</button>`).join("");
      h += `<div class="card ask"><div class="h">❓ ${esc(p.question || "Frage")}</div><div class="askopts">${opts}</div>
        <div class="hint">${p.multi ? "Ankreuzen und übernehmen" : "Antippen"} – oder unten eine eigene Antwort schreiben.</div></div>`;
    } else if (L.pending) {
      const p = L.pending;
      h += `<div class="card warn"><div class="h">${ICON.warn}Freigabe nötig</div>
        <pre class="term">${esc(p.text)}</pre>${p.diff ? `<pre class="term">${diffHtml(p.diff)}</pre>` : ""}
        <div class="btns"><button class="yes" data-ans="yes">Ja</button>${p.key ? `<button data-ans="always">Immer${p.short ? ": " + esc(p.short) : ""}</button>` : ""}<button class="no" data-ans="no">Nein</button></div>
        <div class="hint">Oder unten schreiben, was spike stattdessen tun soll.</div></div>`;
    }
    if (!lv.content && !lv.think && !L.pending && !L.tool) h += '<div class="typing" aria-label="spike schreibt"><i></i><i></i><i></i></div>';
    if (L.info && /^[⚠📦]/u.test(L.info)) h += `<div class="info">${esc(L.info)}</div>`;
  }
  if (L.error) h += `<div class="errbox">⚠ ${esc(L.error)}</div>`;
  const keep = [...box.querySelectorAll("[data-askcb]:checked")].map(cb => cb.dataset.askcb);   // Häkchen behalten
  box.innerHTML = h;
  keep.forEach(i => { const cb = box.querySelector(`[data-askcb="${i}"]`); if (cb) cb.checked = true; });
  const ok = box.querySelector("[data-askok]");
  if (ok) ok.disabled = !box.querySelector("[data-askcb]:checked");
  if (L.tool) {      // Live-Ausgabe eines laufenden Werkzeugs
    const pre = chatEl.querySelector(`[data-out="${CSS.escape(L.tool.id)}"]`);
    if (pre) { pre.textContent = L.tool.out; pre.scrollTop = pre.scrollHeight; if (L.tool.out) pre.closest("details").open = true; }
  }
  updateButtons();
  if (stick) toBottom();
}
function updateButtons() {
  const run = S.live.running, pending = run && S.live.pending;
  $("#send").hidden = run && !pending;
  $("#stop").hidden = !run || pending;
  $("#input").placeholder = pending ? (pending.kind === "ask" ? "Eigene Antwort …" : "Nein, stattdessen …") : run ? "spike antwortet …" : "Nachricht an spike …";
  const sp = $("#speed");
  if (run && S.live.speed > 0) sp.textContent = `${String(S.live.speed).replace(".", ",")} t/s`;
  else if (S.st) sp.textContent = S.st.error ? "⚠ Server" : `${Math.round(S.st.n_ctx / 1000)}k Kontext`;
  sp.title = S.st && S.st.error || "";
}
chatEl.addEventListener("change", e => {
  if (!e.target.matches("[data-askcb]")) return;
  const ok = chatEl.querySelector("[data-askok]");
  if (ok) ok.disabled = !chatEl.querySelector("[data-askcb]:checked");
});
chatEl.addEventListener("click", e => {
  const c = e.target.closest("[data-copy]");
  if (c) return copyText(c.closest(".pre").querySelector("pre").textContent);
  const ans = e.target.closest("[data-ans]");
  if (ans) return answer(ans.dataset.ans);
  const p = S.live.pending, opts = (p && p.options) || [];
  const ask = e.target.closest("[data-ask]");
  if (ask) return answerAsk([opts[+ask.dataset.ask]]);
  if (e.target.closest("[data-askok]"))
    return answerAsk([...chatEl.querySelectorAll("[data-askcb]:checked")].map(cb => opts[+cb.dataset.askcb]));
  const act = e.target.closest("[data-act]");
  if (act) {
    const b = S.blocks[S.blocks.map(x => x.t).lastIndexOf("ai")];
    if (!b) return;
    if (act.dataset.act === "copy") copyText(b.text); else speak(b.text, act);
  }
});

// ---------- Kopfzeile / Verbindung ----------
function setConn(ok, text) {
  $("#conn").classList.toggle("off", !ok);
  $("#connText").textContent = text;
  const st = S.st, ep = st && st.endpoints.find(e => e.name === st.sel.endpoint);
  $("#connBig").innerHTML = ok ? `Verbunden mit ${esc(st ? st.host : "PC")}<small>${esc(location.host)}${ep ? " · " + esc(ep.name) : ""}</small>`
    : `Keine Verbindung zum PC<small>${esc(location.host)}</small>`;
  document.querySelectorAll(".df .dot").forEach(d => d.classList.toggle("off", !ok));
}
function updateMeta() {
  const st = S.st; if (!st) return;
  $("#mLabel").textContent = short(st.sel.model) || "Modell";
  $("#pLabel").textContent = st.sel.profile + (st.sel.thinking === "an" ? " · 💭" : "");
  updateButtons();
}

// ---------- Live-Verbindung ----------
function connect() {
  if (S.es) S.es.close();
  const es = new EventSource(`/api/events?chat=${encodeURIComponent(S.cur || "")}&seit=${S.since}`);
  S.es = es;
  es.onopen = () => setConn(true, `PC verbunden · ${S.st ? S.st.sel.endpoint : ""}`);
  es.onerror = () => { setConn(false, "Keine Verbindung – versuche erneut …"); };
  es.addEventListener("live", ev => {
    const d = JSON.parse(ev.data), was = S.live.running;
    S.live = d;
    if ((d.running && d.n !== S.n) || (was && !d.running)) loadChat(true);
    else renderLive();
    if (was && !d.running) loadChats();
  });
  es.addEventListener("note", ev => {
    const n = JSON.parse(ev.data);
    S.since = Math.max(S.since, n.id);
    if (Date.now() / 1000 - n.time > 600) return;
    if (n.chat && n.chat === S.cur && document.visibilityState === "visible") return;   // steht schon im Chat
    toast(n.title, n.body, n.chat);
    try { navigator.vibrate && navigator.vibrate([60, 60, 60]); } catch {}
  });
  es.addEventListener("hello", ev => { S.since = Math.max(S.since, JSON.parse(ev.data).last); });
  es.addEventListener("runs", ev => { S.runs = JSON.parse(ev.data); renderList(); });
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || !S.st) return;
  if (!S.es || S.es.readyState === 2) connect();
  if (S.cur) loadChat(true);
  loadChats();
});

// ---------- Chats ----------
async function loadChats() {
  try {
    const d = await api(`/api/chats${S.showArchive ? "?archiv=1" : ""}`);
    S.chats = d.chats; S.archived = d.archived;
    renderList();
  } catch (e) { console.warn(e); }
}
function groupOf(mt) {
  const d = new Date(mt * 1000), now = new Date(), day = 864e5;
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (d >= start) return "Heute";
  if (d >= start - day) return "Gestern";
  if (d >= start - 6 * day) return "Letzte 7 Tage";
  if (d >= start - 30 * day) return "Letzte 30 Tage";
  return "Älter";
}
function renderList() {
  const q = $("#q").value.trim().toLowerCase();
  let html = "", g = "";
  S.chats.filter(c => !q || c.title.toLowerCase().includes(q)).forEach(c => {
    const grp = S.showArchive ? "Archiv" : groupOf(c.mtime);
    if (grp !== g) { g = grp; html += `<div class="grp">${g}</div>`; }
    const run = S.runs.includes(c.id) ? '<span class="run" title="antwortet"></span>' : "";
    html += `<div class="ci${c.id === S.cur ? " act" : ""}" data-id="${esc(c.id)}"><button class="open"><b>${esc(c.title)}</b>${run}</button><button class="more" aria-label="Mehr zu ${esc(c.title)}">⋯</button></div>`;
  });
  $("#list").innerHTML = html || `<p class="empty">${q ? "Kein Chat passt zu dieser Suche." : S.showArchive ? "Das Archiv ist leer." : "Noch keine Chats."}</p>`;
  $("#archText").textContent = S.showArchive ? "← Zurück zu den Chats" : `Archiv${S.archived ? " · " + S.archived + (S.archived === 1 ? " Chat" : " Chats") : ""}`;
  $("#archBtn").classList.toggle("on", S.showArchive);
}
$("#q").addEventListener("input", renderList);
$("#archBtn").onclick = () => { S.showArchive = !S.showArchive; loadChats(); };
$("#list").addEventListener("click", e => {
  const row = e.target.closest(".ci"); if (!row) return;
  const c = S.chats.find(x => x.id === row.dataset.id);
  if (e.target.closest(".more")) return chatMenu(c);
  openChat(c.id); drawer(false);
});
async function loadChat(keepScroll) {
  if (!S.cur) return;
  try {
    const d = await api(`/api/chat/${encodeURIComponent(S.cur)}`);
    if (d.id !== S.cur) return;
    S.blocks = d.blocks; S.n = d.n; S.live = d.live; S.title = d.title;
    $("#chatTitle").textContent = d.title;
    render();
    if (!keepScroll) toBottom();
  } catch (e) {
    if (/Nicht gefunden|FileNotFound|No such/.test(e.message)) { newChat(); return; }
    toast("Chat konnte nicht geladen werden", e.message);
  }
}
function openChat(id) {
  S.cur = id; LS.set("spike_chat", id);
  S.blocks = []; S.n = 0; S.live = {running: false};
  renderList(); connect(); loadChat();
}
function newChat() {
  S.cur = null; LS.set("spike_chat", null);
  S.blocks = []; S.n = 0; S.live = {running: false}; S.title = "Neuer Chat";
  $("#chatTitle").textContent = "Neuer Chat";
  render(); renderList(); connect(); drawer(false);
  $("#input").focus();
}
$("#newChat").onclick = newChat; $("#newTop").onclick = newChat;

// ---------- Senden ----------
async function send(textArg) {
  const inp = $("#input");
  const text = (textArg ?? inp.value).trim();
  if (S.live.running && S.live.pending) {           // Text während einer Freigabe = „Nein, stattdessen …“
    if (text) { inp.value = ""; grow(); S.live.pending.kind === "ask" ? answerAsk([], text) : answer("no", text); }
    return;
  }
  if ((!text && !S.atts.length) || S.live.running || S.sending) return;
  S.sending = true;
  const atts = S.atts.slice();
  S.blocks.push({t: "user", text, atts: atts.map(a => ({name: a.name, url: a.url}))});
  S.live = {running: true};
  inp.value = ""; grow(); S.atts = []; renderAtts(); render(); toBottom();
  try {
    const d = await api("/api/send", {json: {chat: S.cur, text, atts: atts.map(a => a.id)}});
    if (d.chat !== S.cur) { S.cur = d.chat; LS.set("spike_chat", d.chat); connect(); }
    loadChats();
  } catch (e) {
    S.live = {running: false, error: e.message}; S.blocks.pop(); render();
    inp.value = text; grow();
  } finally { S.sending = false; }
}
async function answerAsk(choices, text = "") {
  if (!S.cur) return;
  S.live.pending = null; renderLive();
  try { await api(`/api/chat/${encodeURIComponent(S.cur)}/answer`, {json: {choices, reason: text}}); }
  catch (e) { toast("Antwort fehlgeschlagen", e.message); }
}
async function answer(choice, reason = "") {
  if (!S.cur) return;
  S.live.pending = null; renderLive();
  try { await api(`/api/chat/${encodeURIComponent(S.cur)}/answer`, {json: {choice, reason}}); }
  catch (e) { toast("Freigabe fehlgeschlagen", e.message); }
}
$("#send").onclick = () => send();
$("#stop").onclick = () => S.cur && api(`/api/chat/${encodeURIComponent(S.cur)}/stop`, {json: {}}).catch(() => {});
const touch = matchMedia("(pointer: coarse)").matches;
$("#input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !touch) { e.preventDefault(); send(); } });
function grow() { const i = $("#input"); i.style.height = "auto"; i.style.height = Math.min(i.scrollHeight, 140) + "px"; }
$("#input").addEventListener("input", grow);

// ---------- Anhänge ----------
$("#attachBtn").onclick = () => $("#fileIn").click();
$("#fileIn").onchange = async e => {
  for (const f of [...e.target.files]) {
    if (S.atts.length >= 10) { toast("Zu viele Anhänge", "Höchstens 10 pro Nachricht."); break; }
    const tmp = {id: null, name: f.name, kind: "…", url: ""};
    S.atts.push(tmp); renderAtts();
    try {
      const d = await api("/api/upload", {method: "POST", body: f, headers: {"X-Name": encodeURIComponent(f.name)}});
      Object.assign(tmp, d);
      if (d.kind === "image" && S.st && !S.st.vision) toast("Hinweis", "Das aktuelle Modell kann keine Bilder sehen.");
    } catch (err) { S.atts.splice(S.atts.indexOf(tmp), 1); toast("Hochladen fehlgeschlagen", err.message); }
    renderAtts();
  }
  e.target.value = "";
};
function renderAtts() {
  $("#atts").innerHTML = S.atts.map((a, i) => `<div class="chip">${a.url ? `<img src="${esc(a.url)}" alt="">` : a.id ? ICON.file : '<span class="spin"></span>'}<span>${esc(a.name)}</span><button data-rm="${i}" aria-label="Entfernen">✕</button></div>`).join("");
  $("#send").disabled = S.atts.some(a => !a.id);
}
$("#atts").addEventListener("click", e => { const b = e.target.closest("[data-rm]"); if (b) { S.atts.splice(+b.dataset.rm, 1); renderAtts(); } });

// ---------- Sprechen (gedrückt halten) ----------
const mic = $("#mic"), rec = $("#rec"), cv = $("#wave");
const R = {media: null, stream: null, chunks: [], start: 0, timer: 0, raf: 0, analyser: null, ctx: null, held: false};
const canRecord = () => window.isSecureContext && navigator.mediaDevices && window.MediaRecorder;
function drawWave() {
  const w = cv.clientWidth, h = cv.clientHeight, dpr = devicePixelRatio || 1;
  if (cv.width !== w * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
  const g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  g.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
  const bars = Math.floor(w / 5), data = new Uint8Array(R.analyser ? R.analyser.frequencyBinCount : 0);
  if (R.analyser) R.analyser.getByteFrequencyData(data);
  for (let i = 0; i < bars; i++) {
    const v = data.length ? data[Math.floor(i / bars * data.length * .6)] / 255 : .2;
    const bh = Math.max(3, v * h);
    g.fillRect(i * 5, (h - bh) / 2, 3, bh);
  }
  R.raf = requestAnimationFrame(drawWave);
}
async function recStart(e) {
  e.preventDefault();
  if (S.live.running && !S.live.pending) return;
  if (!canRecord()) return;             // ohne HTTPS: Tippen öffnet die Rekorder-App (siehe click)
  R.held = true;
  try {
    R.stream = R.stream && R.stream.active ? R.stream : await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true}});
  } catch (err) { R.held = false; toast("Kein Mikrofon", "Bitte dem Browser den Zugriff aufs Mikrofon erlauben."); return; }
  if (!R.held) { toast("Mikrofon bereit", "Jetzt gedrückt halten und sprechen."); return; }
  const type = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"].find(t => MediaRecorder.isTypeSupported(t)) || "";
  R.media = new MediaRecorder(R.stream, type ? {mimeType: type} : {});
  R.chunks = [];
  R.media.ondataavailable = ev => ev.data.size && R.chunks.push(ev.data);
  R.media.start(250);
  try {
    R.ctx = R.ctx || new AudioContext();
    const src = R.ctx.createMediaStreamSource(R.stream);
    R.analyser = R.ctx.createAnalyser(); R.analyser.fftSize = 128; src.connect(R.analyser);
  } catch { R.analyser = null; }
  R.start = Date.now(); mic.classList.add("on"); rec.hidden = false;
  R.timer = setInterval(() => { const s = Math.floor((Date.now() - R.start) / 1000); $("#recT").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }, 250);
  try { navigator.vibrate && navigator.vibrate(30); } catch {}
  drawWave();
}
function recStop() {
  R.held = false;
  if (!R.media || R.media.state === "inactive") return;
  const dur = Date.now() - R.start, media = R.media;
  clearInterval(R.timer); cancelAnimationFrame(R.raf); mic.classList.remove("on"); rec.hidden = true; $("#recT").textContent = "0:00";
  media.onstop = () => {
    R.stream.getTracks().forEach(t => t.stop()); R.stream = null;
    if (dur < 600) { toast("Zu kurz", "Mikro gedrückt halten, sprechen, dann loslassen."); return; }
    transcribe(new Blob(R.chunks, {type: media.mimeType || "audio/webm"}));
  };
  media.stop();
}
async function transcribe(blob) {
  mic.classList.add("busy"); const ph = $("#input").placeholder; $("#input").placeholder = "Erkenne Sprache …";
  try {
    const d = await api("/api/stt", {method: "POST", body: blob, headers: {"Content-Type": blob.type || "audio/webm"}});
    if (d.text) send(d.text); else toast("Nichts verstanden", "Bitte noch einmal versuchen.");
  } catch (e) { toast("Spracherkennung fehlgeschlagen", e.message); }
  finally { mic.classList.remove("busy"); $("#input").placeholder = ph; }
}
mic.addEventListener("pointerdown", recStart);
["pointerup", "pointercancel"].forEach(ev => mic.addEventListener(ev, recStop));
mic.addEventListener("contextmenu", e => e.preventDefault());
mic.addEventListener("click", () => { if (!canRecord()) $("#audioIn").click(); });
$("#audioIn").onchange = e => { const f = e.target.files[0]; if (f) transcribe(f); e.target.value = ""; };

// ---------- Vorlesen ----------
let audio = null;
async function speak(text, btn) {
  if (audio) { audio.pause(); audio = null; btn.classList.remove("on"); return; }
  if (speechSynthesis && speechSynthesis.speaking) { speechSynthesis.cancel(); btn.classList.remove("on"); return; }
  btn.classList.add("on");
  try {
    if (!S.st || !S.st.tts) throw new Error("kein Piper");
    const blob = await api("/api/tts", {json: {text}});
    audio = new Audio(URL.createObjectURL(blob));
    audio.onended = () => { audio = null; btn.classList.remove("on"); };
    await audio.play();
  } catch {
    audio = null;
    if (!window.speechSynthesis) { btn.classList.remove("on"); return toast("Vorlesen geht nicht", "Weder Piper am PC noch Sprachausgabe im Browser."); }
    const u = new SpeechSynthesisUtterance(text.replace(/```[\s\S]*?```/g, "").replace(/[*_`#>|]/g, ""));
    u.lang = "de-DE"; u.onend = () => btn.classList.remove("on");
    speechSynthesis.speak(u);
  }
}

// ---------- Seitenleiste und Auswahl-Blätter ----------
function drawer(o) { $(".drawer").classList.toggle("show", o); $("#scrim").classList.toggle("show", o); if (o) loadChats(); }
$("#menuBtn").onclick = () => drawer(true);
$("#scrim").onclick = () => { drawer(false); sheet(null); };
let touchX = null;
document.addEventListener("touchstart", e => { const x = e.touches[0].clientX; touchX = x < 24 || $(".drawer").classList.contains("show") ? x : null; }, {passive: true});
document.addEventListener("touchend", e => {
  if (touchX == null) return;
  const dx = e.changedTouches[0].clientX - touchX; touchX = null;
  if (dx > 60) drawer(true); else if (dx < -60) drawer(false);
}, {passive: true});

function sheet(html) {
  const el = $("#sheet");
  if (!html) { el.classList.remove("show"); if (!$(".drawer").classList.contains("show")) $("#scrim").classList.remove("show"); return; }
  $("#sheetBody").innerHTML = html;
  el.classList.add("show"); $("#scrim").classList.add("show");
}
function modelSheet() {
  const st = S.st; if (!st) return;
  let h = `<h3>Server</h3>` + st.endpoints.map(e => `<button class="opt${e.name === st.sel.endpoint ? " on" : ""}" data-ep="${esc(e.name)}"><div><b>${esc(e.name)}</b><small>${esc(e.url)}</small></div>${ICON.check}</button>`).join("");
  h += `<h3>Modell <button data-reload>neu laden</button></h3>`;
  if (st.error) h += `<p>⚠ ${esc(st.error)}</p>`;
  h += st.models.map(m => `<button class="opt${m === st.sel.model ? " on" : ""}" data-model="${esc(m)}"><div><b>${esc(short(m))}</b>${m.includes("/") ? `<small>${esc(m)}</small>` : ""}</div>${ICON.check}</button>`).join("");
  h += `<p>Kontext ${Math.round(st.n_ctx / 1000)}k${st.vision ? " · sieht Bilder" : ""}</p>`;
  sheet(h);
}
function profileSheet() {
  const st = S.st; if (!st) return;
  const tools = p => p.tools.includes("*") ? "alle Werkzeuge" : p.tools.length ? `${p.tools.length} Werkzeuge` : "keine Werkzeuge";
  let h = `<h3>Profil</h3>` + st.profiles.map(p => `<button class="opt${p.name === st.sel.profile ? " on" : ""}" data-prof="${esc(p.name)}"><div><b>${esc(p.name)}</b><small>${esc(tools(p))}</small></div>${ICON.check}</button>`).join("");
  h += `<h3>Denken</h3><div class="seg">${[["standard", "Standard"], ["an", "An"], ["aus", "Aus"]].map(([k, l]) => `<button data-think="${k}" class="${k === st.sel.thinking ? "on" : ""}">${l}</button>`).join("")}</div>`;
  sheet(h);
}
async function select(data, close = true) {
  try { S.st = await api("/api/select", {json: data}); updateMeta(); } catch (e) { toast("Auswahl fehlgeschlagen", e.message); }
  if (close) sheet(null);
}
$("#modelBtn").onclick = modelSheet;
$("#profileBtn").onclick = profileSheet;
$("#sheetBody").addEventListener("click", async e => {
  const b = e.target.closest("button"); if (!b) return;
  const d = b.dataset;
  if (d.ep) { await select({endpoint: d.ep}, false); modelSheet(); }
  else if (d.reload !== undefined) { b.textContent = "lädt …"; try { S.st = await api("/api/state?neu=1"); updateMeta(); } catch {} modelSheet(); }
  else if (d.model) select({model: d.model});
  else if (d.prof) select({profile: d.prof});
  else if (d.think) { b.parentNode.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); select({thinking: d.think}, false); }
  else if (d.cm) chatAction(d.cm, d.id);
  else if (d.close !== undefined) sheet(null);
});
function chatMenu(c) {
  sheet(`<h3>${esc(c.title)}</h3>
    <button class="opt" data-cm="rename" data-id="${esc(c.id)}"><div><b>Umbenennen</b></div></button>
    <button class="opt" data-cm="archive" data-id="${esc(c.id)}"><div><b>${S.showArchive ? "Aus dem Archiv holen" : "Archivieren"}</b></div></button>
    <button class="opt danger" data-cm="delete" data-id="${esc(c.id)}"><div><b>Löschen</b></div></button>`);
}
async function chatAction(action, id) {
  const c = S.chats.find(x => x.id === id) || {title: S.title, id};
  if (action === "rename") {
    sheet(`<h3>Umbenennen</h3><form id="renForm"><input class="field" id="renIn" value="${esc(c.title)}" maxlength="80">
      <div class="row"><button type="button" data-close>Abbrechen</button><button class="primary" type="submit">Speichern</button></div></form>`);
    const inp = $("#renIn"); inp.focus(); inp.select();
    $("#renForm").onsubmit = async ev => {
      ev.preventDefault();
      await meta(id, {title: inp.value});
      if (id === S.cur) $("#chatTitle").textContent = inp.value.trim() || c.title;
      sheet(null);
    };
  } else if (action === "archive") {
    await meta(id, {archived: !S.showArchive}); sheet(null);
    if (id === S.cur && !S.showArchive) newChat();
  } else if (action === "delete") {
    sheet(`<h3>Chat löschen?</h3><p>„${esc(c.title)}“ wird endgültig gelöscht, auch am PC.</p>
      <div class="row"><button data-close>Abbrechen</button><button class="dangerb" data-cm="delete2" data-id="${esc(id)}">Löschen</button></div>`);
  } else if (action === "delete2") {
    try { await api(`/api/chat/${encodeURIComponent(id)}/delete`, {json: {}}); } catch (e) { toast("Löschen fehlgeschlagen", e.message); }
    sheet(null); if (id === S.cur) newChat(); loadChats();
  }
}
async function meta(id, fields) {
  try { await api(`/api/chat/${encodeURIComponent(id)}/meta`, {json: fields}); } catch (e) { toast("Ändern fehlgeschlagen", e.message); }
  loadChats();
}

// ---------- Anmeldung und Start ----------
function showLogin(err) {
  if (S.es) S.es.close();
  $("#main").hidden = true; $("#login").hidden = false;
  $("#loginErr").textContent = err || (location.search.includes("falsch") ? "Der Schlüssel im Link stimmt nicht (mehr)." : "");
}
$("#loginForm").onsubmit = async e => {
  e.preventDefault();
  try {
    const r = await fetch("/api/login", {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json"}, body: JSON.stringify({token: $("#loginToken").value.trim()})});
    if (!r.ok) throw new Error((await r.json()).error);
    history.replaceState(null, "", "/");
    boot();
  } catch (err) { $("#loginErr").textContent = err.message || "Anmeldung fehlgeschlagen"; }
};
async function boot() {
  try { S.st = await api("/api/state"); } catch (e) { if (e.message !== "Nicht angemeldet") showLogin(e.message); return; }
  $("#login").hidden = true; $("#main").hidden = false;
  if (location.search) history.replaceState(null, "", "/");
  updateMeta(); setConn(true, `PC verbunden · ${S.st.sel.endpoint}`);
  await loadChats();
  const last = LS.get("spike_chat");
  if (last && S.chats.some(c => c.id === last)) openChat(last); else newChat();
}
boot();
