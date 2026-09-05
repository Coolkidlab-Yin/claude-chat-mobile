/* claude-chat 前端：聊天室 = session */
"use strict";

const $ = (s) => document.querySelector(s);
const listEl = $("#room-list");
const msgsEl = $("#messages");
const inputEl = $("#input");
const chatScreen = $("#screen-chat");

let rooms = [];
let current = null;          // {slug, sid, title, project, project_name}
let activeRun = null;        // {id, es}
let stickBottom = true;
let toolChips = {};          // tool_use_id -> chip element
let typingEl = null;
let roomsTimer = null;
let serverInfo = {};         // /api/health 的結果（桌面 app 有沒有裝、同步方式）
api("/api/health").then((h) => { serverInfo = h; }).catch(() => {});

const mode = () => localStorage.getItem("cc-mode") || "auto";
const modelPick = () => localStorage.getItem("cc-model") || "default";
const effortPick = () => localStorage.getItem("cc-effort") || "default";
const showAll = () => localStorage.getItem("cc-show-all") === "1";
let projFilter = localStorage.getItem("cc-proj") || "全部";

/* ---------- 深淺色主題 ---------- */
const themePick = () => localStorage.getItem("cc-theme") || "auto";
const sysDark = window.matchMedia("(prefers-color-scheme: dark)");

function applyTheme() {
  const t = themePick();
  const dark = t === "dark" || (t === "auto" && sysDark.matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = dark ? "#0b0e17" : "#efebe2";
}
sysDark.addEventListener("change", () => { if (themePick() === "auto") applyTheme(); });
applyTheme();

/* 分段選鈕小工具 */
function bindSeg(id, getter, setter) {
  const box = $(id);
  const sync = () => {
    box.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.v === getter()));
  };
  box.querySelectorAll("button").forEach((b) => {
    b.onclick = () => { setter(b.dataset.v); sync(); };
  });
  sync();
  return sync;
}

/* ---------- 每個聊天室的模型/力度 ---------- */
const roomOv = (kind) => {
  if (!current) return null;
  if (current.sid) return localStorage.getItem("cc-room-" + kind + ":" + current.sid);
  return (current._ov || {})[kind] || null;
};
const setRoomOv = (kind, v) => {
  if (!current) return;
  if (current.sid) {
    const k = "cc-room-" + kind + ":" + current.sid;
    if (v === "default") localStorage.removeItem(k);
    else localStorage.setItem(k, v);
  } else {
    current._ov = current._ov || {};
    if (v === "default") delete current._ov[kind];
    else current._ov[kind] = v;
  }
};
const effModel = () => roomOv("model") || modelPick();
const effEffort = () => roomOv("effort") || effortPick();

const MODEL_SHORT = { default: "預設", fable: "Fable", opus: "Opus", sonnet: "Sonnet", haiku: "Haiku" };
const EFFORT_SHORT = { default: "", max: "最深", high: "多想", medium: "中", low: "快" };

function updateRoomBtn() {
  const m = effModel(), e = effEffort();
  let label = MODEL_SHORT[m] || m;
  if (e !== "default") label += "·" + (EFFORT_SHORT[e] || e);
  $("#btn-room").textContent = label;
}

/* ---------- 小工具 ---------- */
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d)) return "";
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const hm = pad(d.getHours()) + ":" + pad(d.getMinutes());
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return hm;
  const yd = new Date(now); yd.setDate(now.getDate() - 1);
  if (d.toDateString() === yd.toDateString()) return "昨天";
  if (d.getFullYear() === now.getFullYear()) return (d.getMonth() + 1) + "/" + d.getDate();
  return d.getFullYear() + "/" + (d.getMonth() + 1) + "/" + d.getDate();
}

function hueFor(name) {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}

/* 極簡 markdown（assistant 泡泡用） */
function md(src) {
  const lines = String(src).split("\n");
  let html = "", inCode = false, codeBuf = [], listType = null, para = [];

  const flushPara = () => {
    if (para.length) { html += "<p>" + para.join("<br>") + "</p>"; para = []; }
  };
  const flushList = () => { if (listType) { html += "</" + listType + ">"; listType = null; } };
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  for (const raw of lines) {
    if (raw.trimStart().startsWith("```")) {
      if (inCode) { html += "<pre><code>" + esc(codeBuf.join("\n")) + "</code></pre>"; codeBuf = []; }
      else { flushPara(); flushList(); }
      inCode = !inCode;
      continue;
    }
    if (inCode) { codeBuf.push(raw); continue; }
    const line = raw;
    const t = line.trim();
    if (!t) { flushPara(); flushList(); continue; }
    let m;
    if ((m = t.match(/^(#{1,4})\s+(.*)/))) {
      flushPara(); flushList();
      html += "<h3>" + inline(m[2]) + "</h3>";
    } else if ((m = t.match(/^[-*]\s+(.*)/))) {
      flushPara();
      if (listType !== "ul") { flushList(); html += "<ul>"; listType = "ul"; }
      html += "<li>" + inline(m[1]) + "</li>";
    } else if ((m = t.match(/^\d+[.)]\s+(.*)/))) {
      flushPara();
      if (listType !== "ol") { flushList(); html += "<ol>"; listType = "ol"; }
      html += "<li>" + inline(m[1]) + "</li>";
    } else if (t.startsWith(">")) {
      flushPara(); flushList();
      html += "<blockquote>" + inline(t.replace(/^>\s?/, "")) + "</blockquote>";
    } else if (t.startsWith("|") || t.startsWith("---")) {
      flushPara(); flushList();
      html += "<pre><code>" + esc(line) + "</code></pre>";
    } else {
      flushList(); para.push(inline(line));
    }
  }
  if (inCode && codeBuf.length) html += "<pre><code>" + esc(codeBuf.join("\n")) + "</code></pre>";
  flushPara(); flushList();
  // 合併相鄰的表格 pre
  return html.replace(/<\/code><\/pre><pre><code>/g, "\n");
}

/* 把回覆裡的 Windows 檔案路徑變成可預覽的影片/圖片/連結 */
const FILE_RE = /[A-Za-z]:(?:\\|\/)[^\s"'<>|?*`]+?\.(mp4|mov|webm|m4v|png|jpg|jpeg|gif|webp|mp3|wav|m4a|pdf|html|htm|md|txt|csv|json)\b/gi;

function fileEmbed(match, ext) {
  const raw = match.replace(/&amp;/g, "&");
  const url = "/api/file?path=" + encodeURIComponent(raw);
  const base = raw.split(/[\\/]/).pop();
  const e = ext.toLowerCase();
  if (["mp4", "mov", "webm", "m4v"].includes(e)) {
    return '<video controls playsinline preload="metadata" src="' + url + '"></video>' +
      '<a class="file-link" href="' + url + '" target="_blank">🎬 ' + esc(base) + "</a>";
  }
  if (["png", "jpg", "jpeg", "gif", "webp"].includes(e)) {
    return '<a href="' + url + '" target="_blank"><img src="' + url + '" loading="lazy"></a>';
  }
  if (["mp3", "wav", "m4a"].includes(e)) {
    return '<audio controls preload="none" src="' + url + '"></audio>' +
      '<a class="file-link" href="' + url + '" target="_blank">🎵 ' + esc(base) + "</a>";
  }
  return '<a class="file-link" href="' + url + '" target="_blank">📎 ' + esc(base) + "</a>";
}

function linkifyFiles(html) {
  // <pre> 區塊裡只給連結不嵌播放器，其他地方給完整預覽
  return html.split(/(<pre>[\s\S]*?<\/pre>)/).map((seg) => {
    if (seg.startsWith("<pre>")) {
      return seg.replace(FILE_RE, (m) => {
        const raw = m.replace(/&amp;/g, "&");
        return '<a href="/api/file?path=' + encodeURIComponent(raw) + '" target="_blank">' + m + "</a>";
      });
    }
    return seg.replace(FILE_RE, fileEmbed);
  }).join("");
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = "HTTP " + r.status;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  return r.json();
}

/* ---------- 聊天室清單 ---------- */
async function loadRooms(silent) {
  try {
    const data = await api("/api/rooms" + (showAll() ? "?all=1" : ""));
    rooms = data.rooms;
    renderChips();
    renderRooms();
  } catch (e) {
    if (!silent) listEl.innerHTML = '<div class="empty-hint">連不上伺服器：' + esc(e.message) + "</div>";
  }
}

function renderChips() {
  const names = [];
  for (const r of rooms) {
    if (r.project_name && !names.includes(r.project_name)) names.push(r.project_name);
  }
  if (projFilter !== "全部" && !names.includes(projFilter)) projFilter = "全部";
  const box = $("#proj-chips");
  box.innerHTML = "";
  for (const name of ["全部", ...names]) {
    const el = document.createElement("div");
    el.className = "chip" + (name === projFilter ? " on" : "");
    el.textContent = name;
    el.onclick = () => {
      projFilter = name;
      localStorage.setItem("cc-proj", name);
      renderChips();
      renderRooms();
    };
    box.appendChild(el);
  }
}

/* 全文搜尋（對話內容命中）：輸入 ≥2 字後 400ms 問一次伺服器 */
let searchHits = { q: "", map: {} };
let searchTimer = null;
function scheduleSearch(q) {
  clearTimeout(searchTimer);
  if (q.length < 2) { searchHits = { q: "", map: {} }; return; }
  searchTimer = setTimeout(async () => {
    try {
      const r = await api("/api/search?q=" + encodeURIComponent(q) + (showAll() ? "&all=1" : ""));
      const map = {};
      for (const room of r.rooms) map[room.sid] = room;
      searchHits = { q, map };
      if ($("#search").value.trim().toLowerCase() === q) renderRooms();
    } catch (e) { /* 搜不到就只留標題過濾 */ }
  }, 400);
}

function renderRooms() {
  const q = $("#search").value.trim().toLowerCase();
  let shown = projFilter === "全部" ? rooms : rooms.filter((r) => r.project_name === projFilter);
  if (q) {
    if (searchHits.q !== q) scheduleSearch(q);
    const hits = searchHits.q === q ? searchHits.map : {};
    shown = shown.filter((r) => hits[r.sid] || (r.title + r.preview + r.project_name).toLowerCase().includes(q));
    shown = shown.map((r) => {
      const h = hits[r.sid];
      if (!h || !h.hits.length) return r;
      const first = h.hits[0];
      return Object.assign({}, r, { preview: "🔎 " + (first.role === "user" ? "你：" : "") + first.snippet });
    });
  }
  if (!shown.length) {
    listEl.innerHTML = '<div class="empty-hint">' + (q ? (q.length >= 2 && searchHits.q !== q ? "搜尋對話內容中…" : "沒有符合的聊天室") : "這個專案還沒有聊天室") + "</div>";
    return;
  }
  listEl.innerHTML = "";
  for (const r of shown) {
    const el = document.createElement("div");
    el.className = "room";
    const h = hueFor(r.project_name);
    const initial = (r.project_name || "?").slice(0, 1).toUpperCase();
    const eng = r.engine || "claude";
    el.innerHTML =
      '<div class="avatar' + (eng !== "claude" ? " eng-" + eng : "") + '" style="--h:' + h + '">' +
        esc(eng === "claude" ? initial : (ENGINE_ICON[eng] || "?")) +
        (r.running ? '<span class="dot"></span>' : "") +
      "</div>" +
      '<div class="room-main">' +
        '<div class="room-top"><div class="room-title">' + esc(r.title) + '</div>' +
        '<div class="room-time">' + fmtTime(r.ts) + "</div></div>" +
        '<div class="room-bottom">' +
          '<div class="room-preview">' + esc(r.running ? "正在工作中…" : r.preview || "") + "</div>" +
          (eng !== "claude" ? '<span class="tag eng">' + esc(ENGINE_NAME[eng] || eng) + "</span>" : "") +
          '<span class="tag">' + esc(r.project_name) + "</span>" +
          (r.archived ? '<span class="tag arch">封存</span>' : "") +
          (r.live ? '<span class="tag live">桌機開著</span>' : "") +
          (eng === "claude" && r.app && !r.desktop ? '<span class="tag phone">只在手機</span>' : "") +
        "</div>" +
      "</div>";
    const wrap = document.createElement("div");
    wrap.className = "room-wrap";
    const acts = document.createElement("div");
    acts.className = "room-actions";
    acts.innerHTML =
      '<button class="ra-arch">' + (r.archived ? "解封" : "封存") + "</button>" +
      '<button class="ra-del">刪除</button>';
    acts.querySelector(".ra-arch").onclick = () => doArchive(r);
    acts.querySelector(".ra-del").onclick = () => doDelete(r);
    wrap.appendChild(acts);
    wrap.appendChild(el);
    attachRoomHandlers(el, r);
    listEl.appendChild(wrap);
  }
}

/* ---------- 左滑 / 長按聊天室 → 封存/刪除 ---------- */
let actionRoom = null;
let openRow = null;
const SWIPE_W = 136;

function closeOpenRow() {
  if (openRow && openRow._close) openRow._close();
  openRow = null;
}
listEl.addEventListener("scroll", closeOpenRow, { passive: true });

async function doArchive(r) {
  $("#sheet-actions").classList.add("hidden");
  closeOpenRow();
  try {
    await api("/api/room/" + r.slug + "/" + r.sid + "/archive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on: !r.archived }),
    });
    loadRooms(true);
  } catch (e) { alert("封存失敗：" + e.message); }
}

async function doDelete(r) {
  const ok = confirm("確定刪除「" + r.title + "」？\n\n對話紀錄會移到電腦上的垃圾桶資料夾（可救回），桌面 app 也會看不到這個對話。");
  $("#sheet-actions").classList.add("hidden");
  closeOpenRow();
  if (!ok) return;
  try {
    await api("/api/room/" + r.slug + "/" + r.sid + "/delete", { method: "POST" });
    loadRooms(true);
  } catch (e) { alert("刪除失敗：" + e.message); }
}

function attachRoomHandlers(el, r) {
  let sx = 0, sy = 0, dx = 0, base = 0;
  let dragging = false, fired = false, timer = null;
  const setX = (x, anim) => {
    el.style.transition = anim ? "" : "none";
    el.style.transform = "translateX(" + x + "px)";
  };
  const open = () => { closeOpenRow(); setX(-SWIPE_W, true); el.dataset.open = "1"; openRow = el; };
  const close = () => { setX(0, true); delete el.dataset.open; if (openRow === el) openRow = null; };
  el._close = close;

  el.addEventListener("pointerdown", (e) => {
    sx = e.clientX; sy = e.clientY; dx = 0;
    dragging = false; fired = false;
    base = el.dataset.open ? -SWIPE_W : 0;
    timer = setTimeout(() => { if (!dragging) { fired = true; openActions(r); } }, 550);
  });
  el.addEventListener("pointermove", (e) => {
    const mx = e.clientX - sx, my = e.clientY - sy;
    if (!dragging) {
      if (Math.abs(mx) > 10 && Math.abs(mx) > Math.abs(my)) {
        dragging = true;
        clearTimeout(timer);
        try { el.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      } else if (Math.abs(my) > 10) {
        clearTimeout(timer);
      }
    }
    if (dragging) {
      dx = mx;
      setX(Math.min(0, Math.max(-SWIPE_W - 20, base + mx)), false);
    }
  });
  const end = () => {
    clearTimeout(timer);
    if (dragging) {
      if (base + dx < -SWIPE_W / 2) open();
      else close();
    }
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
  el.addEventListener("contextmenu", (e) => { e.preventDefault(); openActions(r); });
  el.onclick = () => {
    if (fired || dragging) { fired = false; dragging = false; return; }
    if (openRow && openRow !== el) { closeOpenRow(); return; }
    if (el.dataset.open) { close(); return; }
    openRoom(r);
  };
}

function openActions(r) {
  actionRoom = r;
  $("#action-title").textContent = r.title;
  $("#btn-archive").textContent = r.archived ? "📤 解除封存" : "📥 封存（從清單收起來，紀錄還在）";
  const isClaude = (r.engine || "claude") === "claude";
  $("#btn-desktop").classList.toggle("hidden", !isClaude || !serverInfo.desktop_app || serverInfo.desktop_sync === "off");
  $("#btn-desktop").textContent = r.desktop ? "🖥 在桌面 App 打開" : "🖥 同步到桌面 App 並打開";
  $("#sheet-actions").classList.remove("hidden");
}

$("#btn-archive").onclick = () => { if (actionRoom) doArchive(actionRoom); };
$("#btn-delete").onclick = () => { if (actionRoom) doDelete(actionRoom); };
$("#btn-desktop").onclick = async () => {
  const r = actionRoom;
  if (!r) return;
  $("#sheet-actions").classList.add("hidden");
  try {
    const d = await api("/api/room/" + r.slug + "/" + r.sid + "/desktop", { method: "POST" });
    sysNote(d.result === "imported" ? "已登錄進桌面 App，電腦那邊已切到這個對話" : "桌面 App 已切到這個對話");
    setTimeout(() => loadRooms(true), 3000);
  } catch (e) {
    sysNote("同步失敗：" + e.message, true);
  }
};
$("#btn-rename").onclick = async () => {
  const r = actionRoom;
  if (!r) return;
  const t = prompt("聊天室名字（清空 = 還原成原本的）", r.title || "");
  if (t === null) return;
  $("#sheet-actions").classList.add("hidden");
  try {
    await api("/api/room/" + r.slug + "/" + r.sid + "/title", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: t.trim() }),
    });
    if (current && current.sid === r.sid) $("#chat-title").textContent = t.trim() || r.title;
    loadRooms(true);
  } catch (e) {
    sysNote("改名失敗：" + e.message, true);
  }
};

/* ---------- 進出聊天室 ---------- */
/* 上下文用量條 */
function setCtx(c) {
  const bar = $("#ctx-bar");
  if (!c || !c.tokens) { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  const fill = $("#ctx-fill");
  fill.style.width = Math.min(100, c.pct) + "%";
  fill.className = c.pct >= 85 ? "hot" : c.pct >= 70 ? "warn" : "";
  $("#ctx-label").textContent = "上下文 " + fmtTok(c.tokens) + " / " + fmtTok(c.window) + " · " + c.pct + "%";
}

function openRoom(room, fromPop) {
  current = Object.assign({}, room);
  toolChips = {};
  $("#chat-title").textContent = room.title || "新聊天室";
  setSub("");
  setCtx(null);
  updateRoomBtn();
  // 模型/力度按鈕只對 Claude Code 有意義
  $("#btn-room").classList.toggle("hidden", (room.engine || "claude") !== "claude");
  msgsEl.innerHTML = "";
  chatScreen.classList.remove("hidden-right");
  if (!fromPop) history.pushState({ chat: 1 }, "");
  stickBottom = true;
  if (room.sid) {
    loadHistory().then(() => {
      // 若這個房間有背景工作進行中 → 接上事件流
      api("/api/status").then((st) => {
        const info = st.running[current && current.sid];
        if (info) attachRun(info.run_id, info.n_events, true);
      }).catch(() => {});
    });
  } else {
    msgsEl.innerHTML = '<div class="sys-note">新聊天室（' + esc(room.project_name) + '）— 送出第一句就開始</div>';
  }
}

function closeRoom() {
  chatScreen.classList.add("hidden-right");
  detachRun(false);
  current = null;
  loadRooms(true);
}

window.addEventListener("popstate", () => { if (current) closeRoom(); });
$("#btn-back").onclick = () => {
  if (history.state && history.state.chat) history.back();
  else closeRoom();
};

const ENGINE_ICON = { claude: "✳", codex: "◆", grok: "𝕏", gemini: "✦", openai: "◎", deepseek: "◇", openrouter: "⇄" };
const ENGINE_NAME = { claude: "Claude Code", codex: "Codex", grok: "Grok", gemini: "Gemini", openai: "ChatGPT", deepseek: "DeepSeek", openrouter: "OpenRouter" };

function setSub(state) {
  const parts = [];
  if (current && current.engine && current.engine !== "claude") {
    parts.push(esc(ENGINE_NAME[current.engine] || current.engine));
  }
  if (current && current.project_name) parts.push(esc(current.project_name));
  const el = $("#chat-sub");
  el.innerHTML = parts.join(" · ") + (state ? ' · <span class="working">' + state + "</span>" : "");
}

/* ---------- 歷史訊息 ---------- */
async function loadHistory(before) {
  if (!current || !current.sid) return;
  const url = "/api/history/" + current.slug + "/" + current.sid +
    (before != null ? "?before=" + before : "");
  const data = await api(url);
  if (before == null && data.context) setCtx(data.context);
  const frag = document.createDocumentFragment();
  if (data.more) {
    const btn = document.createElement("div");
    btn.className = "load-more";
    btn.textContent = "載入更早的訊息";
    btn.onclick = () => { btn.remove(); loadHistory(data.oldest); };
    frag.appendChild(btn);
  }
  for (const it of data.items) frag.appendChild(renderItem(it));
  if (before != null) {
    const oldH = msgsEl.scrollHeight;
    msgsEl.prepend(frag);
    msgsEl.scrollTop += msgsEl.scrollHeight - oldH;
  } else {
    msgsEl.innerHTML = "";
    msgsEl.appendChild(frag);
    scrollBottom(true);
  }
}

function renderItem(it) {
  if (it.kind === "info") {
    const el = document.createElement("div");
    el.className = "info-chip";
    el.innerHTML = "📋 " + esc(it.label || "摘要") +
      '（點開看）<div class="full">' + esc(it.text) + "</div>";
    el.onclick = () => el.classList.toggle("expand");
    return el;
  }
  if (it.kind === "tool") {
    const el = document.createElement("div");
    el.className = "tool-chip";
    const state = it.ok === true ? '<span class="t-state ok">✓</span>'
      : it.ok === false ? '<span class="t-state bad">✕</span>'
      : '<span class="t-state"><span class="spin"></span></span>';
    el.innerHTML = '<span class="t-name">⚙ ' + esc(it.tool) + "</span>" +
      (it.detail ? '<span class="t-detail">' + esc(it.detail) + "</span>" : "") + state;
    el.onclick = () => el.classList.toggle("expand");
    if (it.tool_use_id) toolChips[it.tool_use_id] = el;
    return el;
  }
  const el = document.createElement("div");
  el.className = "msg " + (it.role === "user" ? "user" : "ai");
  const body = it.role === "user"
    ? '<div class="bubble">' + linkifyFiles(esc(it.text).replace(/\n/g, "<br>")) + "</div>"
    : '<div class="bubble">' + linkifyFiles(md(it.text)) + "</div>";
  el.innerHTML = body + (it.ts ? '<div class="msg-time">' + fmtTime(it.ts) + "</div>" : "");
  return el;
}

/* ---------- 捲動 ---------- */
function scrollBottom(force) {
  if (force || stickBottom) msgsEl.scrollTop = msgsEl.scrollHeight;
}
msgsEl.addEventListener("scroll", () => {
  stickBottom = msgsEl.scrollHeight - msgsEl.scrollTop - msgsEl.clientHeight < 80;
  $("#jump-bottom").classList.toggle("hidden", stickBottom);
});
$("#jump-bottom").onclick = () => { stickBottom = true; scrollBottom(true); $("#jump-bottom").classList.add("hidden"); };

/* ---------- 打字中指示 ---------- */
function showTyping() {
  if (typingEl) return;
  typingEl = document.createElement("div");
  typingEl.className = "msg ai";
  typingEl.innerHTML = '<div class="bubble typing"><i></i><i></i><i></i></div>';
  msgsEl.appendChild(typingEl);
  scrollBottom();
}
function hideTyping() { if (typingEl) { typingEl.remove(); typingEl = null; } }

/* ---------- 附件上傳 ---------- */
let attachments = [];   // {path, name, url, uploading}

const isImagePath = (p) => /\.(png|jpe?g|gif|webp)$/i.test(p || "");

function renderAttachStrip() {
  const strip = $("#attach-strip");
  if (!attachments.length) { strip.classList.add("hidden"); strip.innerHTML = ""; return; }
  strip.classList.remove("hidden");
  strip.innerHTML = "";
  attachments.forEach((a, i) => {
    const el = document.createElement("div");
    el.className = "attach-item" + (a.uploading ? " uploading" : "");
    el.innerHTML = (isImagePath(a.name) ? '<img src="' + a.url + '">'
      : '<div class="doc">📄<span>' + esc((a.name || "").split(".").pop().toUpperCase()) + "</span></div>") +
      '<div class="rm">✕</div>';
    el.querySelector(".rm").onclick = () => { attachments.splice(i, 1); renderAttachStrip(); };
    strip.appendChild(el);
  });
}

$("#btn-attach").onclick = () => $("#file-input").click();
$("#file-input").addEventListener("change", async (e) => {
  for (const f of e.target.files) {
    const item = { path: null, name: f.name, url: URL.createObjectURL(f), uploading: true };
    attachments.push(item);
    renderAttachStrip();
    try {
      const fd = new FormData();
      fd.append("file", f);
      const r = await fetch("/api/upload", { method: "POST", body: fd });
      if (!r.ok) throw new Error((await r.json()).detail || "HTTP " + r.status);
      const d = await r.json();
      item.path = d.path;
      item.uploading = false;
    } catch (err) {
      attachments = attachments.filter((x) => x !== item);
      sysNote("上傳失敗：" + err.message, true);
    }
    renderAttachStrip();
  }
  e.target.value = "";
});

/* ---------- 送訊息與事件流 ---------- */
async function sendMsg() {
  let text = inputEl.value.trim();
  if ((!text && !attachments.length) || !current || activeRun) return;
  if (attachments.some((a) => a.uploading)) { sysNote("圖片還在上傳，等一下再送"); return; }
  const paths = attachments.map((a) => a.path).filter(Boolean);
  if (paths.length) {
    const lines = paths.map((p) => (isImagePath(p) ? "[手機傳圖，請用 Read 工具查看: " : "[手機傳檔，請用 Read 工具讀取: ") + p + "]");
    text = (text ? text + "\n\n" : "") + lines.join("\n");
  }
  attachments = [];
  renderAttachStrip();
  inputEl.value = "";
  autoGrow();
  const el = document.createElement("div");
  el.className = "msg user";
  el.innerHTML = '<div class="bubble">' + linkifyFiles(esc(text).replace(/\n/g, "<br>")) + "</div>" +
    '<div class="msg-time">' + fmtTime(Date.now()) + "</div>";
  msgsEl.appendChild(el);
  stickBottom = true;
  scrollBottom(true);
  showTyping();
  try {
    const body = { text, mode: mode(), engine: current.engine || "claude" };
    if ((current.engine || "claude") === "claude") {
      if (effModel() !== "default") body.model = effModel();
      if (effEffort() !== "default") body.effort = effEffort();
    }
    if (current.sid) { body.sid = current.sid; body.slug = current.slug; }
    else body.project = current.project;
    const r = await api("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    attachRun(r.run_id, 0, false);
  } catch (e) {
    hideTyping();
    sysNote("送不出去：" + e.message, true);
  }
}

function attachRun(runId, from, isReattach) {
  detachRun(true);
  const es = new EventSource("/api/run/" + runId + "/events?start=" + (from || 0));
  activeRun = { id: runId, es };
  setSub("Claude 工作中…");
  $("#btn-send").classList.add("hidden");
  $("#btn-stop").classList.remove("hidden");
  if (isReattach) showTyping();

  es.onmessage = (ev) => {
    let it;
    try { it = JSON.parse(ev.data); } catch (e) { return; }
    if (it.kind === "init") {
      if (current && !current.sid && it.sid) current.sid = it.sid;
      return;
    }
    if (it.kind === "ctx") {
      setCtx(it);
      return;
    }
    if (it.kind === "note") {
      sysNote(it.text);
      return;
    }
    if (it.kind === "ask") {
      hideTyping();
      renderAskCard(it);
      showTyping();
      scrollBottom();
      return;
    }
    if (it.kind === "ask_done") {
      lockAskCard(it.ask_id, null);
      return;
    }
    if (it.kind === "perm") {
      hideTyping();
      renderPermCard(it);
      showTyping();
      scrollBottom();
      return;
    }
    if (it.kind === "perm_done") {
      lockPermCard(it.perm_id, it.decision === "allow" ? "已允許" : "已拒絕");
      return;
    }
    if (it.kind === "tool_ok") {
      const chip = toolChips[it.tool_use_id];
      if (chip) {
        const st = chip.querySelector(".t-state");
        if (st) { st.className = "t-state " + (it.ok ? "ok" : "bad"); st.textContent = it.ok ? "✓" : "✕"; }
      }
      return;
    }
    if (it.kind === "done") {
      finishRun(it);
      return;
    }
    hideTyping();
    it.ts = it.ts || Date.now();
    msgsEl.appendChild(renderItem(it));
    showTyping();
    scrollBottom();
  };
  es.onerror = () => {
    // 連線斷了：工作若還在跑，稍後重連；不在了就收尾
    if (!activeRun || activeRun.id !== runId) return;
    es.close();
    setTimeout(async () => {
      if (!activeRun || activeRun.id !== runId) return;
      try {
        const st = await api("/api/status");
        const still = current && current.sid && st.running[current.sid];
        if (still && still.run_id === runId) attachRun(runId, 0, true);
        else finishRun({ ok: true });
      } catch (e) { finishRun({ ok: false, error: "連線中斷" }); }
    }, 1500);
  };
}

function detachRun(keepUI) {
  if (activeRun && activeRun.es) activeRun.es.close();
  activeRun = null;
  if (!keepUI) {
    hideTyping();
    $("#btn-send").classList.remove("hidden");
    $("#btn-stop").classList.add("hidden");
  }
}

async function finishRun(doneEv) {
  const wasNew = current && !current.slug;
  detachRun(false);
  setSub("");
  if (doneEv.error) sysNote("出了點問題：" + doneEv.error, true);
  if (!current) return;
  if (doneEv.sid && !current.sid) current.sid = doneEv.sid;
  // 用檔案裡的正式紀錄取代串流畫面（順便補時間戳）
  if (wasNew || !current.slug) {
    await loadRooms(true);
    const found = rooms.find((r) => r.sid === current.sid);
    if (found) { current.slug = found.slug; $("#chat-title").textContent = found.title; }
  }
  if (current.slug && current.sid) {
    setTimeout(() => { if (current && !activeRun) loadHistory().catch(() => {}); }, 400);
  }
  loadRooms(true);
}

/* ---------- AskUserQuestion 選項卡 ---------- */
function renderAskCard(it) {
  const card = document.createElement("div");
  card.className = "msg ai ask-card";
  card.dataset.askId = it.ask_id;
  const picked = {};   // question -> Set(labels)

  let html = '<div class="ask-head">🙋 它想問你</div>';
  for (const q of it.questions || []) {
    const multi = !!q.multiSelect;
    html += '<div class="ask-q" data-q="' + esc(q.question) + '" data-multi="' + (multi ? 1 : 0) + '">';
    if (q.header) html += '<div class="ask-tag">' + esc(q.header) + "</div>";
    html += '<div class="ask-question">' + esc(q.question) + "</div>";
    for (const o of q.options || []) {
      html += '<button class="ask-opt" data-label="' + esc(o.label) + '">' +
        '<b>' + esc(o.label) + "</b>" +
        (o.description ? "<span>" + esc(o.description) + "</span>" : "") +
        "</button>";
    }
    if (multi) html += '<button class="ask-multi-ok">就選這些</button>';
    html += "</div>";
  }
  html += '<div class="ask-free"><input type="text" placeholder="或用打字回答…">' +
    '<button class="ask-send">送出</button></div>' +
    '<button class="ask-skip">跳過，讓它自己決定</button>';
  card.innerHTML = html;

  const submit = (answers, freeText, skipped) => {
    api("/api/ask/" + it.ask_id + "/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answers: answers || {}, free_text: freeText || "", skipped: !!skipped }),
    }).catch(() => {});
    lockAskCard(it.ask_id, skipped ? "（已跳過）" : null);
  };

  card.querySelectorAll(".ask-q").forEach((qEl) => {
    const qText = qEl.dataset.q;
    const multi = qEl.dataset.multi === "1";
    qEl.querySelectorAll(".ask-opt").forEach((btn) => {
      btn.onclick = () => {
        const label = btn.dataset.label;
        if (multi) {
          const set = picked[qText] = picked[qText] || new Set();
          if (set.has(label)) { set.delete(label); btn.classList.remove("on"); }
          else { set.add(label); btn.classList.add("on"); }
          return;
        }
        picked[qText] = new Set([label]);
        btn.classList.add("on");
        // 單選題全部答完才送出
        const allQ = [...card.querySelectorAll(".ask-q")];
        if (allQ.every((x) => picked[x.dataset.q] && picked[x.dataset.q].size)) {
          const answers = {};
          for (const [k, v] of Object.entries(picked)) answers[k] = [...v].join("、");
          submit(answers, "", false);
        }
      };
    });
  });
  card.querySelectorAll(".ask-multi-ok").forEach((btn) => {
    btn.onclick = () => {
      const answers = {};
      for (const [k, v] of Object.entries(picked)) answers[k] = [...v].join("、");
      submit(answers, "", false);
    };
  });
  card.querySelector(".ask-send").onclick = () => {
    const v = card.querySelector(".ask-free input").value.trim();
    if (!v) return;
    const answers = {};
    for (const [k, s] of Object.entries(picked)) answers[k] = [...s].join("、");
    submit(answers, v, false);
  };
  card.querySelector(".ask-skip").onclick = () => submit({}, "", true);

  msgsEl.appendChild(card);
}

function lockAskCard(askId, note) {
  const card = msgsEl.querySelector('.ask-card[data-ask-id="' + askId + '"]');
  if (!card || card.classList.contains("done")) return;
  card.classList.add("done");
  card.querySelectorAll("button, input").forEach((el) => { el.disabled = true; });
  if (note) {
    const n = document.createElement("div");
    n.className = "ask-note";
    n.textContent = note;
    card.appendChild(n);
  }
}

/* ---------- 逐項授權卡（先問我模式） ---------- */
function renderPermCard(it) {
  const card = document.createElement("div");
  card.className = "msg ai ask-card perm-card";
  card.dataset.permId = it.perm_id;
  card.innerHTML =
    '<div class="ask-head">' + (it.reason ? "⚠️ 危險指令，要放行嗎？" : "🔐 它想做這件事，可以嗎？") + "</div>" +
    (it.reason ? '<div class="perm-reason">' + esc(it.reason) + "</div>" : "") +
    '<div class="ask-tag">' + esc(it.tool || "工具") + "</div>" +
    (it.detail ? '<div class="ask-question">' + esc(it.detail) + "</div>" : "") +
    (it.preview && it.preview !== it.detail ? '<pre class="perm-preview">' + esc(it.preview) + "</pre>" : "") +
    '<div class="perm-btns">' +
      '<button class="ask-opt perm-allow"><b>允許</b></button>' +
      '<button class="ask-opt perm-deny"><b>拒絕</b></button>' +
    "</div>" +
    '<button class="ask-skip perm-all">這次工作剩下的全部允許（不再問）</button>';
  const submit = (decision) => {
    api("/api/perm/" + it.perm_id + "/answer", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
    }).catch(() => {});
    lockPermCard(it.perm_id, decision === "deny" ? "已拒絕" : decision === "allow_all" ? "已允許（之後不再問）" : "已允許");
  };
  card.querySelector(".perm-allow").onclick = () => submit("allow");
  card.querySelector(".perm-deny").onclick = () => submit("deny");
  card.querySelector(".perm-all").onclick = () => submit("allow_all");
  msgsEl.appendChild(card);
}

function lockPermCard(permId, note) {
  const card = msgsEl.querySelector('.perm-card[data-perm-id="' + permId + '"]');
  if (!card || card.classList.contains("done")) return;
  card.classList.add("done");
  card.querySelectorAll("button").forEach((el) => { el.disabled = true; });
  if (note) {
    const n = document.createElement("div");
    n.className = "ask-note";
    n.textContent = note;
    card.appendChild(n);
  }
}

function sysNote(text, isErr) {
  const el = document.createElement("div");
  el.className = "sys-note" + (isErr ? " err" : "");
  el.textContent = text;
  msgsEl.appendChild(el);
  scrollBottom();
}

$("#btn-send").onclick = sendMsg;
$("#btn-stop").onclick = async () => {
  if (!activeRun) return;
  try { await api("/api/run/" + activeRun.id + "/stop", { method: "POST" }); } catch (e) { /* ignore */ }
  sysNote("已請它停下");
};

/* ---------- 輸入框 ---------- */
function autoGrow() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 132) + "px";
}
inputEl.addEventListener("input", autoGrow);
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendMsg(); }
});

/* iOS 鍵盤把輸入列蓋住的修正 */
if (window.visualViewport) {
  const vv = window.visualViewport;
  const fix = () => {
    const gap = window.innerHeight - vv.height - vv.offsetTop;
    chatScreen.style.bottom = (gap > 0 ? gap : 0) + "px";
    scrollBottom();
  };
  vv.addEventListener("resize", fix);
  vv.addEventListener("scroll", fix);
}

/* ---------- 新聊天室：先選 AI，再選專案 ---------- */
let engines = [];

$("#btn-new").onclick = async () => {
  const sheet = $("#sheet-engine");
  sheet.classList.remove("hidden");
  const box = $("#engine-list");
  box.innerHTML = '<div class="empty-hint">載入中…</div>';
  try {
    const data = await api("/api/engines");
    engines = data.engines;
    box.innerHTML = "";
    for (const e of engines) {
      const usable = e.kind === "cli" ? e.ready : e.has_key;
      const el = document.createElement("div");
      el.className = "project-item" + (usable ? "" : " dim");
      el.innerHTML = '<div class="e-icon">' + esc(e.icon) + "</div>" +
        '<div><div class="p-name">' + esc(e.label) +
        (usable ? "" : '<span class="tag" style="margin-left:6px">未設定</span>') + "</div>" +
        '<div class="p-path">' + esc(e.note) + "</div></div>";
      el.onclick = () => {
        if (!usable) {
          sheet.classList.add("hidden");
          alert(e.kind === "cli" ? "這台電腦沒有安裝 " + e.label
            : "先到「設定 → 其他 AI」貼上 " + e.label + " 的 API key");
          return;
        }
        sheet.classList.add("hidden");
        if (e.kind === "api") {
          openRoom({ slug: null, sid: null, engine: e.id, title: "新聊天室",
                     project: "", project_name: e.label });
        } else {
          pickProject(e.id);
        }
      };
      box.appendChild(el);
    }
  } catch (e) {
    box.innerHTML = '<div class="empty-hint">' + esc(e.message) + "</div>";
  }
};

async function pickProject(engineId) {
  const sheet = $("#sheet-new");
  sheet.classList.remove("hidden");
  const box = $("#project-list");
  box.innerHTML = '<div class="empty-hint">載入中…</div>';
  try {
    const data = await api("/api/projects");
    box.innerHTML = "";
    for (const p of data.projects) {
      const el = document.createElement("div");
      el.className = "project-item";
      el.innerHTML = '<div><div class="p-name">' + esc(p.name) + '</div>' +
        '<div class="p-path">' + esc(p.path) + "</div></div>";
      el.onclick = () => {
        sheet.classList.add("hidden");
        openRoom({ slug: null, sid: null, engine: engineId, title: "新聊天室",
                   project: p.path, project_name: p.name });
      };
      box.appendChild(el);
    }
  } catch (e) {
    box.innerHTML = '<div class="empty-hint">' + esc(e.message) + "</div>";
  }
}

/* ---------- 其他 AI 的 API key ---------- */
let keysLoaded = false;

async function renderKeys() {
  const box = $("#keys-panel");
  box.innerHTML = '<div class="empty-hint">載入中…</div>';
  try {
    const data = await api("/api/engines");
    engines = data.engines;
    box.innerHTML = "";
    for (const e of engines.filter((x) => x.kind === "api")) {
      const row = document.createElement("div");
      row.className = "key-row";
      row.innerHTML =
        '<div class="key-head">' + esc(e.icon) + " " + esc(e.label) +
        (e.has_key ? '<span class="tag live">已設定</span>' : '<span class="tag">未設定</span>') + "</div>" +
        '<input class="key-in" type="password" placeholder="' +
        (e.has_key ? "已存好，要換再貼新的" : "貼上 API key") + '" autocomplete="off">' +
        '<input class="model-in" type="text" placeholder="型號" value="' + esc(e.model) + '">' +
        '<div class="key-btns"><button class="key-save">存起來</button>' +
        (e.has_key ? '<button class="key-clear">清除</button>' : "") + "</div>";
      const keyIn = row.querySelector(".key-in");
      const modelIn = row.querySelector(".model-in");
      row.querySelector(".key-save").onclick = async () => {
        try {
          await api("/api/engines/" + e.id + "/key", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key: keyIn.value || null, model: modelIn.value }),
          });
          keyIn.value = "";
          renderKeys();
        } catch (err) { alert("存不起來：" + err.message); }
      };
      const clr = row.querySelector(".key-clear");
      if (clr) clr.onclick = async () => {
        if (!confirm("清除 " + e.label + " 的 API key？")) return;
        await api("/api/engines/" + e.id + "/key", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key: "" }),
        });
        renderKeys();
      };
      box.appendChild(row);
    }
  } catch (e) {
    box.innerHTML = '<div class="empty-hint">' + esc(e.message) + "</div>";
  }
}

$("#keys-toggle").onclick = () => {
  const panel = $("#keys-panel");
  const closed = panel.classList.toggle("hidden");
  $("#keys-toggle").textContent = "Grok / Gemini / ChatGPT / DeepSeek… " + (closed ? "▾" : "▴");
  if (!closed && !keysLoaded) { keysLoaded = true; renderKeys(); }
};

/* ---------- 聊天室內調模型/力度 ---------- */
$("#btn-room").onclick = () => {
  if (!current) return;
  $("#sheet-room").classList.remove("hidden");
  bindSeg("#seg-room-model", () => roomOv("model") || "default",
    (v) => { setRoomOv("model", v); updateRoomBtn(); });
  bindSeg("#seg-room-effort", () => roomOv("effort") || "default",
    (v) => { setRoomOv("effort", v); updateRoomBtn(); });
};

/* ---------- 語音輸入 ---------- */
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null, recBase = "";

function stopRec() {
  if (rec) { try { rec.stop(); } catch (e) { /* ignore */ } rec = null; }
  $("#btn-mic").classList.remove("rec");
}

$("#btn-mic").onclick = () => {
  if (rec) { stopRec(); return; }
  if (!SR) {
    sysNote("這個瀏覽器不支援語音辨識——可以改用鍵盤上的 🎤 聽寫鍵");
    return;
  }
  rec = new SR();
  rec.lang = "zh-TW";
  rec.interimResults = true;
  rec.continuous = true;
  recBase = inputEl.value ? inputEl.value + " " : "";
  rec.onresult = (e) => {
    let done = "", interim = "";
    for (const r of e.results) {
      if (r.isFinal) done += r[0].transcript;
      else interim += r[0].transcript;
    }
    inputEl.value = recBase + done + interim;
    autoGrow();
  };
  rec.onend = () => { rec = null; $("#btn-mic").classList.remove("rec"); };
  rec.onerror = (e) => {
    if (e.error === "not-allowed" || e.error === "service-not-allowed") {
      sysNote("麥克風權限被擋了——也可以用鍵盤上的 🎤 聽寫鍵", true);
    }
    stopRec();
  };
  try {
    rec.start();
    $("#btn-mic").classList.add("rec");
  } catch (e) {
    stopRec();
  }
};

/* ---------- 用量 ---------- */
function fmtTok(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

function fmtReset(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const mins = Math.round((d - Date.now()) / 60000);
  if (mins <= 0) return "即將重置";
  if (mins < 60) return mins + " 分後重置";
  if (mins < 24 * 60) return Math.floor(mins / 60) + " 小時 " + (mins % 60) + " 分後重置";
  const wd = "日一二三四五六"[d.getDay()];
  const pad = (n) => String(n).padStart(2, "0");
  return "週" + wd + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + " 重置";
}

async function loadLimits() {
  const box = $("#limits-box");
  box.textContent = "查詢額度中…";
  try {
    const u = await api("/api/limits");
    if (!u.ok) { box.textContent = "額度查不到：" + u.error; return; }
    let html = "";
    for (const l of u.limits) {
      const cls = l.percent >= 80 ? "hot" : l.percent >= 50 ? "warn" : "";
      html += '<div class="lim-row"><div class="lim-top"><span class="l-name">' + esc(l.label) +
        '</span><span class="l-val">' + l.percent + "%" + (l.resets_at ? " · " + fmtReset(l.resets_at) : "") +
        '</span></div><div class="lim-track"><i class="' + cls + '" style="width:' +
        Math.min(100, l.percent) + '%"></i></div></div>';
    }
    if (u.credits) {
      html += '<div class="usage-row" style="margin-top:6px"><span class="u-k">加購額度</span><span class="u-v">$' +
        u.credits.used.toFixed(2) + " / $" + u.credits.limit.toFixed(2) + "</span></div>";
    }
    box.innerHTML = html || "沒有額度資料";
  } catch (e) {
    box.textContent = "額度查不到：" + e.message;
  }
}

async function loadUsage() {
  const box = $("#usage-box");
  box.textContent = "統計 token 中…（第一次會多花幾秒）";
  try {
    const u = await api("/api/usage");
    const row = (k, v) => '<div class="usage-row"><span class="u-k">' + esc(k) + '</span><span class="u-v">' + esc(v) + "</span></div>";
    const line = (d) => "回 " + d.msgs + " 次 · 輸入 " + fmtTok(d.in) + " · 輸出 " + fmtTok(d.out) + " · 快取 " + fmtTok(d.cache_read);
    let html = row("今天", line(u.today)) + row("近 7 天", line(u.window));
    const models = Object.entries(u.models).slice(0, 5);
    if (models.length) {
      html += '<div class="usage-sub">';
      for (const [name, d] of models) html += row(name, line(d));
      html += "</div>";
    }
    box.innerHTML = html;
  } catch (e) {
    box.textContent = "統計不出來：" + e.message;
  }
}

let usageLoaded = false;
$("#usage-toggle").onclick = () => {
  const panel = $("#usage-panel");
  const open = panel.classList.toggle("hidden");
  $("#usage-toggle").textContent = "額度百分比＋token 統計 " + (open ? "▾" : "▴");
  if (!open && !usageLoaded) {
    usageLoaded = true;
    loadLimits();
    loadUsage();
  } else if (!open) {
    loadLimits();
  }
};

/* ---------- 設定 ---------- */
$("#btn-settings").onclick = async () => {
  $("#sheet-settings").classList.remove("hidden");
  const bindRadios = (name, getter, key) => {
    document.querySelectorAll('input[name="' + name + '"]').forEach((r) => {
      r.checked = r.value === getter();
      r.onchange = () => localStorage.setItem(key, r.value);
    });
  };
  bindRadios("mode", mode, "cc-mode");
  bindRadios("model", modelPick, "cc-model");
  bindRadios("effort", effortPick, "cc-effort");
  bindSeg("#seg-theme", themePick, (v) => { localStorage.setItem("cc-theme", v); applyTheme(); });
  const chk = $("#chk-all");
  chk.checked = showAll();
  chk.onchange = () => {
    localStorage.setItem("cc-show-all", chk.checked ? "1" : "0");
    loadRooms();
  };
  try {
    const h = await api("/api/health");
    serverInfo = h;
    $("#health-line").textContent = "伺服器正常 · " + h.claude +
      (h.desktop_app ? " · 桌面 App：有" + (h.desktop_registry ? "（登錄檔已對上）" : "") : " · 桌面 App：沒裝");
    $("#desktop-sync-opts").classList.toggle("hidden", !h.desktop_app);
    document.querySelectorAll('input[name="dsync"]').forEach((r) => {
      r.checked = r.value === h.desktop_sync;
      r.onchange = async () => {
        try {
          const d = await api("/api/settings", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ desktop_sync: r.value }),
          });
          serverInfo.desktop_sync = d.desktop_sync;
        } catch (e) { sysNote("設定沒存成功：" + e.message, true); }
      };
    });
  } catch (e) {
    $("#health-line").textContent = "伺服器連不上：" + e.message;
  }
};

document.querySelectorAll("[data-close]").forEach((b) => {
  b.onclick = () => $("#" + b.dataset.close).classList.add("hidden");
});
document.querySelectorAll(".sheet-mask").forEach((m) => {
  m.addEventListener("click", (e) => { if (e.target === m) m.classList.add("hidden"); });
});

/* ---------- 啟動 ---------- */
$("#search").addEventListener("input", renderRooms);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    loadRooms(true);
    if (current && current.sid && !activeRun) {
      api("/api/status").then((st) => {
        const info = st.running[current.sid];
        if (info) attachRun(info.run_id, info.n_events, true);
        else loadHistory().catch(() => {});
      }).catch(() => {});
    }
  }
});
roomsTimer = setInterval(() => { if (!document.hidden && !current) loadRooms(true); }, 25000);
void roomsTimer;
loadRooms();
