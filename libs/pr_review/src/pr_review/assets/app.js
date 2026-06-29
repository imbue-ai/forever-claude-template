"use strict";

// All fetches are RELATIVE (no leading slash) so they resolve under the
// /service/pr-review/ proxy prefix.
async function api(path) {
  const res = await fetch(path);
  const data = await res.json().catch(() => ({ error: "bad response" }));
  if (!res.ok || data.error) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

// POST helper for write actions (JSON body).
async function api2(path, payload) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({ error: "bad response" }));
  if (!res.ok || data.error) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

// ---------- small helpers ----------
const COLOR = {
  passing: "ok", pending: "warn", failing: "danger", none: "idle",
  approved: "ok", commented: "warn", "changes requested": "danger",
};
const REVLABEL = { none: "no reviews", commented: "commented", approved: "approved", "changes requested": "changes req" };

function esc(s) { return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function attr(s) { return esc(s).replace(/"/g, "&quot;"); }

function relTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const secs = Math.max(1, Math.round((Date.now() - then) / 1000));
  const mins = Math.round(secs / 60), hrs = Math.round(mins / 60), days = Math.round(hrs / 24);
  if (secs < 90) return "just now";
  if (mins < 90) return mins + "m";
  if (hrs < 36) return hrs + "h";
  if (days < 30) return days + "d";
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function spineColor(s) {
  if (!s) return "gray";
  if (s.has_conflicts || s.ci?.verdict === "failing" || s.review_decision === "changes requested") return "red";
  if (s.ci?.verdict === "pending") return "amber";
  if (s.state === "draft") return "gray";
  return "green";
}

const EXT_LANG = {
  py: "python", js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
  json: "json", md: "markdown", html: "html", css: "css", scss: "scss", sh: "shell",
  bash: "shell", yml: "yaml", yaml: "yaml", toml: "ini", ini: "ini", cfg: "ini",
  c: "c", h: "cpp", cc: "cpp", cpp: "cpp", hpp: "cpp", cxx: "cpp", rs: "rust",
  go: "go", java: "java", rb: "ruby", php: "php", sql: "sql", xml: "xml", txt: "plaintext",
};
function langFor(path) {
  const ext = (path.split(".").pop() || "").toLowerCase();
  return EXT_LANG[ext] || "plaintext";
}

// ---------- concurrency pool ----------
async function pool(items, limit, worker) {
  const queue = items.slice();
  const runners = Array.from({ length: Math.min(limit, queue.length) }, async () => {
    while (queue.length) {
      const item = queue.shift();
      try { await worker(item); } catch (_e) { /* leave row un-enriched */ }
    }
  });
  await Promise.all(runners);
}

// ===================================================================
// LIST VIEW
// ===================================================================
let CURRENT_FILTER = "all";
let LIST_DATA = null;
let STATUS_CACHE = {};   // "repo#num" -> status object

function keyFor(repo, num) { return repo + "#" + num; }

function rowHTML(pr) {
  const k = keyFor(pr.repo, pr.number);
  const st = STATUS_CACHE[k];
  const sp = spineColor(st);
  const draft = (st ? st.state : pr.state) === "draft";
  let ciDot = '<span class="dot idle"></span>', ciVal = "&middot;&middot;&middot;", ciLoad = "loading";
  let revDot = '<span class="dot idle"></span>', revVal = "&middot;&middot;&middot;", revLoad = "loading";
  let metaExtra = "";
  if (st) {
    ciLoad = ""; revLoad = "";
    ciDot = `<span class="dot ${COLOR[st.ci.verdict]} ${st.ci.verdict === "pending" ? "pulse" : ""}"></span>`;
    ciVal = st.ci.verdict;
    revDot = `<span class="dot ${COLOR[st.review_decision]}"></span>`;
    revVal = REVLABEL[st.review_decision];
    const bits = [
      `<span class="branch">${esc(st.head)}<span class="arrow">&rarr;</span>${esc(st.base)}</span>`,
      `<span class="diffstat"><span class="add">+${st.diffstat.additions}</span> <span class="del">&minus;${st.diffstat.deletions}</span></span>`,
    ];
    if (st.has_conflicts) bits.push('<span style="color:var(--danger)">&#9888; conflicts</span>');
    metaExtra = bits.join(" ");
  }
  return `
  <div class="row" data-key="${attr(k)}" data-repo="${attr(pr.repo)}" data-num="${pr.number}">
    <div class="spine ${sp}"></div>
    <div class="row-main">
      <div class="row-title">
        <span class="t">${esc(pr.title)}</span>
        ${draft ? '<span class="tag draft">draft</span>' : ""}
      </div>
      <div class="row-meta">
        <span class="repo">${esc(pr.repo)}</span><span>#${pr.number}</span>
        ${metaExtra}
        ${pr.comments ? `<span class="comment-count">&#128172; ${pr.comments}</span>` : ""}
        <span>&middot; ${relTime(pr.updated_at)}</span>
      </div>
    </div>
    <div class="row-signals">
      <div class="sig ${ciLoad}"><span>${ciDot}</span><span class="val">${ciVal}</span><span class="lbl">ci</span></div>
      <div class="sig ${revLoad}"><span>${revDot}</span><span class="val">${revVal}</span><span class="lbl">review</span></div>
    </div>
  </div>`;
}

function passesFilter(pr) {
  const st = STATUS_CACHE[keyFor(pr.repo, pr.number)];
  const state = st ? st.state : pr.state;
  if (CURRENT_FILTER === "all") return true;
  if (CURRENT_FILTER === "draft") return state === "draft";
  if (CURRENT_FILTER === "ready") return state === "ready";
  if (CURRENT_FILTER === "attention") return st && (st.has_conflicts || st.ci?.verdict === "failing" || st.review_decision === "changes requested");
  return true;
}

function renderList() {
  if (!LIST_DATA) return;
  const mine = LIST_DATA.authored.filter(passesFilter);
  const reqs = LIST_DATA.review_requested.filter(passesFilter);
  const el = document.getElementById("list");
  el.innerHTML = `
    <div class="controls">
      <div class="chips">
        ${["all", "attention", "ready", "draft"].map(f =>
          `<button class="chip ${f === CURRENT_FILTER ? "on" : ""}" data-filter="${f}">${
            { all: "All", attention: "Needs attention", ready: "Ready", draft: "Drafts" }[f]}</button>`).join("")}
      </div>
    </div>
    <section class="section">
      <div class="section-head"><h2>Created by you</h2><span class="n">${LIST_DATA.authored.length}</span><div class="line"></div></div>
      <div id="mine-rows">${mine.map(rowHTML).join("") || '<div class="empty"><div class="small">Nothing matches this filter.</div></div>'}</div>
    </section>
    <section class="section">
      <div class="section-head"><h2>Awaiting your review</h2><span class="n">${LIST_DATA.review_requested.length}</span><div class="line"></div></div>
      ${reqs.length ? `<div id="req-rows">${reqs.map(rowHTML).join("")}</div>`
        : '<div class="empty"><div class="big">Nothing waiting on you</div><div class="small">PRs where your review is requested will appear here.</div></div>'}
    </section>`;
  el.querySelectorAll(".chip").forEach(c => c.addEventListener("click", () => { CURRENT_FILTER = c.dataset.filter; renderList(); }));
  el.querySelectorAll(".row").forEach(r => r.addEventListener("click", () => openDetail(r.dataset.repo, +r.dataset.num)));
}

function cssEsc(s) { return s.replace(/[#.:/]/g, "\\$&"); }
function rebindRow(pr) {
  const rowEl = document.querySelector(`.row[data-key="${cssEsc(keyFor(pr.repo, pr.number))}"]`);
  if (rowEl) rowEl.addEventListener("click", () => openDetail(pr.repo, pr.number));
}

// ===================================================================
// DETAIL VIEW (Monaco)
// ===================================================================
let monacoReady = null;
function loadMonaco() {
  if (!monacoReady) {
    monacoReady = new Promise((resolve) => {
      require(["vs/editor/editor.main"], () => {
        monaco.editor.defineTheme("cockpit", {
          base: "vs-dark", inherit: true, rules: [],
          colors: { "editor.background": "#0E1116", "editorGutter.background": "#0E1116", "diffEditor.insertedTextBackground": "#1b332688", "diffEditor.removedTextBackground": "#3a1d1f88" },
        });
        // Type-aware hover for Python (Jedi), on head-content models only.
        monaco.languages.registerHoverProvider("python", {
          provideHover: async (model, pos) => {
            const meta = MODEL_META.get(model.uri.toString());
            if (!meta || meta.side !== "head" || !DETAIL) return null;
            const pr = DETAIL.pr;
            const [owner, name] = pr.head_repo.split("/");
            try {
              const d = await api(`api/repo/${owner}/${name}/${pr.head_sha}/pyhover?path=${encodeURIComponent(meta.path)}&line=${pos.lineNumber}&col=${pos.column}`);
              if (!d || !d.contents) return null;
              return { contents: [{ value: d.contents }] };
            } catch (e) { return null; }
          },
        });
        resolve(monaco);
      });
    });
  }
  return monacoReady;
}

let DETAIL = null;        // { pr, files }
let diffEditor = null, plainEditor = null;
let activeFile = null;    // changed-file path currently shown

// Track Monaco models so the Python hover/definition providers can map a model
// back to its repo-relative path + side ("head" content is intelligence-eligible).
let modelSeq = 0;
const MODEL_META = new Map();   // model.uri string -> { path, side }
let CREATED_MODELS = [];

// Pending review comments (PR-scoped, span multiple files) + view-zone bookkeeping.
let pendingComments = [];        // { path, line, side, body }
let CZ_MOD = [];                 // thread view-zone ids on the modified editor
let CZ_ORIG = [];                // thread view-zone ids on the original editor
let VIEWER = "";
function makeModel(content, path, side) {
  const uri = monaco.Uri.parse("pr://" + (modelSeq++) + "/" + encodeURI(path));
  const model = monaco.editor.createModel(content, langFor(path), uri);
  MODEL_META.set(model.uri.toString(), { path, side });
  CREATED_MODELS.push(model);
  return model;
}

async function openDetail(repo, number) {
  const [owner, name] = repo.split("/");
  document.getElementById("list").classList.add("hidden");
  const d = document.getElementById("detail");
  d.classList.remove("hidden");
  d.innerHTML = '<div class="loading-note">Fetching the code for this pull request…</div>';
  document.getElementById("brandctx").textContent = "// " + repo + " #" + number;
  let data;
  try {
    [data] = await Promise.all([api(`api/pr/${owner}/${name}/${number}`), loadMonaco()]);
  } catch (e) {
    d.innerHTML = `<div class="err-note" style="margin:22px">Couldn't open this PR: ${esc(e.message)}</div>
      <div style="margin:0 22px"><button class="btn" id="backErr">&larr; Back</button></div>`;
    document.getElementById("backErr").addEventListener("click", closeDetail);
    return;
  }
  DETAIL = data;
  pendingComments = [];
  try {
    DETAIL.conversation = await api(`api/pr/${owner}/${name}/${number}/conversation`);
  } catch (e) {
    DETAIL.conversation = { comments: [], reviews: [], review_comments: [] };
  }
  renderDetailShell();
  if (DETAIL.files.length) selectChangedFile(DETAIL.files[0].path);
}

function statusBadges(pr) {
  const ci = `<span class="statusbadge"><span class="dot ${COLOR[pr.ci.verdict]}"></span>CI ${pr.ci.verdict}</span>`;
  const rev = `<span class="statusbadge"><span class="dot ${COLOR[pr.review_decision]}"></span>${REVLABEL[pr.review_decision]}</span>`;
  const state = pr.state === "draft"
    ? '<span class="statusbadge"><span class="dot idle"></span>Draft</span>'
    : '<span class="statusbadge"><span class="dot ok"></span>Open</span>';
  const conflict = pr.has_conflicts ? '<span class="statusbadge" style="color:var(--danger)"><span class="dot danger"></span>Conflicts</span>' : "";
  return state + ci + rev + conflict;
}

function renderDetailShell() {
  const pr = DETAIL.pr;
  const conv = DETAIL.conversation || { comments: [], reviews: [], review_comments: [] };
  const convCount = conv.comments.length + conv.reviews.filter(r => r.body || r.state !== "COMMENTED").length;
  const d = document.getElementById("detail");
  d.innerHTML = `
    <div class="detail-head">
      <div class="crumb"><a id="backBtn">&larr; All pull requests</a><span class="sep">/</span><span>${esc(pr.repo)}</span><span class="sep">#${pr.number}</span>
        <span class="spacer"></span><a href="${attr(pr.url)}" target="_blank" rel="noopener">Open on GitHub &#8599;</a></div>
      <div class="detail-title-row"><h2 class="detail-title" id="dtitle">${esc(pr.title)}</h2>
        <button class="btn ghost sm" id="editTitleBtn">Edit title</button></div>
      <div class="detail-sub">${statusBadges(pr)}
        <span>${esc(pr.head)} &rarr; ${esc(pr.base)}</span>
        <span class="diffstat"><span class="add">+${pr.diffstat.additions}</span> <span class="del">&minus;${pr.diffstat.deletions}</span> &middot; ${pr.diffstat.changed_files} files</span>
      </div>
      <div class="detail-tabs">
        <button class="dtab on" data-dtab="files">Files changed <span class="tcount">${DETAIL.files.length}</span></button>
        <button class="dtab" data-dtab="conversation">Conversation <span class="tcount">${convCount}</span></button>
      </div>
    </div>
    <div class="detail-body" id="pane-files">
      <div class="sidebar">
        <div class="sidebar-tabs">
          <button class="sidebar-tab on" data-stab="changed">Changed (${DETAIL.files.length})</button>
          <button class="sidebar-tab" data-stab="browse">All files</button>
        </div>
        <div class="sidebar-scroll" id="sb-changed">${changedFilesHTML()}</div>
        <div class="sidebar-scroll hidden" id="sb-browse">
          <input class="file-filter" id="fileFilter" placeholder="Filter files in this repo…" />
          <div id="browse-list" class="loading-note" style="padding:14px">Loading file tree…</div>
        </div>
      </div>
      <div class="editor-wrap">
        <div class="editor-bar" id="editorBar"><span class="epath">Select a file</span></div>
        <div id="editor"></div>
        <div class="refs-panel hidden" id="refsPanel"></div>
      </div>
    </div>
    <div class="conv-pane hidden" id="pane-conversation"></div>`;
  d.querySelector("#backBtn").addEventListener("click", closeDetail);
  d.querySelectorAll(".sidebar-tab").forEach(t => t.addEventListener("click", () => switchSidebar(t.dataset.stab)));
  d.querySelectorAll("#sb-changed .fitem").forEach(it => it.addEventListener("click", () => selectChangedFile(it.dataset.path)));
  d.querySelectorAll(".dtab").forEach(t => t.addEventListener("click", () => switchDetailTab(t.dataset.dtab)));
  d.querySelector("#editTitleBtn").addEventListener("click", editTitle);
  renderConversation();
}

function switchDetailTab(which) {
  document.querySelectorAll(".dtab").forEach(t => t.classList.toggle("on", t.dataset.dtab === which));
  document.getElementById("pane-files").classList.toggle("hidden", which !== "files");
  document.getElementById("pane-conversation").classList.toggle("hidden", which !== "conversation");
}

// ---- conversation: comments, reviews, title/description editing ----
function mdLite(s) {
  let h = esc(s);
  h = h.replace(/!\[[^\]]*\]\([^)]*\)/g, "");                       // drop images (badge icons etc.)
  h = h.replace(/```([\s\S]*?)```/g, (m, c) => "<pre class=\"md-pre\">" + c.replace(/^\n/, "") + "</pre>");
  h = h.replace(/`([^`]+)`/g, "<code>$1</code>");
  h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  h = h.replace(/^\s{0,3}#{1,6}\s*(.+)$/gm, "<strong>$1</strong>");
  h = h.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, "<a href=\"$2\" target=\"_blank\" rel=\"noopener\">$1</a>");
  h = h.replace(/\n/g, "<br>");
  return h;
}
function avatarFor(user) {
  const bot = /\[bot\]$/.test(user);
  return `<span class="avatar-sm ${bot ? "bot" : ""}">${esc(user.replace(/\[bot\]$/, "").slice(0, 2).toUpperCase())}</span>`;
}
function reviewStateLabel(s) {
  return { APPROVED: "approved", CHANGES_REQUESTED: "changes requested", COMMENTED: "reviewed", DISMISSED: "dismissed" }[s] || (s || "").toLowerCase();
}

function renderConversation() {
  const pr = DETAIL.pr;
  const conv = DETAIL.conversation || { comments: [], reviews: [], review_comments: [] };
  const items = [];
  for (const c of conv.comments) items.push({ t: c.created_at, html: commentCard(c) });
  for (const r of conv.reviews) {
    if (!r.body && r.state === "COMMENTED") continue; // a bare "commented" review with no body is noise
    items.push({ t: r.submitted_at, html: reviewCard(r) });
  }
  items.sort((a, b) => new Date(a.t || 0) - new Date(b.t || 0));
  const pane = document.getElementById("pane-conversation");
  pane.innerHTML = `<div class="conv-col">
    ${descCard(pr)}
    ${items.map(i => i.html).join("")}
    <div class="composer-card">
      <div class="h">Add a comment</div>
      <textarea class="composer" id="generalComposer" placeholder="Leave a comment on this pull request…"></textarea>
      <div class="composer-actions"><span class="status" id="generalStatus"></span>
        <button class="btn primary" id="generalSubmit">Comment</button></div>
    </div>
  </div>`;
  pane.querySelector("#editDescBtn").addEventListener("click", editDesc);
  pane.querySelector("#generalSubmit").addEventListener("click", submitGeneralComment);
}

function descCard(pr) {
  return `<div class="cmt-card desc">
    <div class="chead">${avatarFor(pr.author)}<span class="who">${esc(pr.author)}</span><span class="when">opened this PR</span>
      <span class="spacer" style="flex:1"></span><button class="btn ghost sm" id="editDescBtn">Edit description</button></div>
    <div class="cbody" id="descBody">${pr.body ? mdLite(pr.body) : '<span style="color:var(--faint)">No description.</span>'}</div>
  </div>`;
}
function commentCard(c) {
  return `<div class="cmt-card">
    <div class="chead">${avatarFor(c.user)}<span class="who">${esc(c.user)}</span><span class="when">${relTime(c.created_at)}</span></div>
    <div class="cbody">${mdLite(c.body)}</div></div>`;
}
function reviewCard(r) {
  const st = (r.state || "").toLowerCase();
  return `<div class="cmt-card review">
    <div class="chead">${avatarFor(r.user)}<span class="who">${esc(r.user)}</span>
      <span class="pill ${st}">${reviewStateLabel(r.state)}</span><span class="when">${relTime(r.submitted_at)}</span></div>
    ${r.body ? `<div class="cbody">${mdLite(r.body)}</div>` : ""}</div>`;
}

function editTitle() {
  const h = document.getElementById("dtitle");
  const btn = document.getElementById("editTitleBtn");
  const input = document.createElement("input");
  input.className = "detail-title-edit";
  input.id = "dtitle";
  input.value = DETAIL.pr.title;
  h.replaceWith(input);
  input.focus();
  btn.textContent = "Save title";
  btn.onclick = async () => {
    const newTitle = input.value.trim();
    if (!newTitle || newTitle === DETAIL.pr.title) { cancelTitleEdit(input); return; }
    btn.disabled = true; btn.textContent = "Saving…";
    try {
      await api2(`api/pr/${repoPath()}/${DETAIL.pr.number}/edit`, { title: newTitle });
      DETAIL.pr.title = newTitle;
      flashNote("Title updated on GitHub");
    } catch (e) { flashNote("Couldn't update title: " + e.message); }
    btn.disabled = false;
    cancelTitleEdit(input);
  };
}
function cancelTitleEdit(input) {
  const h = document.createElement("h2");
  h.className = "detail-title"; h.id = "dtitle"; h.textContent = DETAIL.pr.title;
  input.replaceWith(h);
  const btn = document.getElementById("editTitleBtn");
  btn.textContent = "Edit title"; btn.onclick = editTitle;
}

function editDesc() {
  const body = document.getElementById("descBody");
  const btn = document.getElementById("editDescBtn");
  const ta = document.createElement("textarea");
  ta.className = "desc-edit composer"; ta.id = "descBody"; ta.style.minHeight = "200px";
  ta.value = DETAIL.pr.body || "";
  body.replaceWith(ta);
  ta.focus();
  btn.textContent = "Save description";
  btn.onclick = async () => {
    btn.disabled = true; btn.textContent = "Saving…";
    try {
      await api2(`api/pr/${repoPath()}/${DETAIL.pr.number}/edit`, { body: ta.value });
      DETAIL.pr.body = ta.value;
      flashNote("Description updated on GitHub");
    } catch (e) { flashNote("Couldn't update description: " + e.message); }
    btn.disabled = false;
    renderConversation();
  };
}

async function submitGeneralComment() {
  const ta = document.getElementById("generalComposer");
  const status = document.getElementById("generalStatus");
  const btn = document.getElementById("generalSubmit");
  const body = ta.value.trim();
  if (!body) return;
  btn.disabled = true; status.textContent = "Posting…";
  try {
    await api2(`api/pr/${repoPath()}/${DETAIL.pr.number}/comment`, { body });
    await reloadConversation();
    flashNote("Comment posted to GitHub");
  } catch (e) { status.textContent = "Failed: " + e.message; btn.disabled = false; }
}

async function reloadConversation() {
  try {
    DETAIL.conversation = await api(`api/pr/${repoPath()}/${DETAIL.pr.number}/conversation`);
  } catch (e) { /* keep old */ }
  // refresh the conversation tab count + pane
  const conv = DETAIL.conversation;
  const convCount = conv.comments.length + conv.reviews.filter(r => r.body || r.state !== "COMMENTED").length;
  const tab = document.querySelector('.dtab[data-dtab="conversation"] .tcount');
  if (tab) tab.textContent = convCount;
  renderConversation();
}

function repoPath() { return DETAIL.pr.repo; }

function changedFilesHTML() {
  return DETAIL.files.map(f => {
    const stBadge = ["added", "removed", "renamed"].includes(f.status)
      ? `<span class="badge-st ${f.status}">${f.status[0]}</span>` : "";
    return `<div class="fitem" data-path="${attr(f.path)}">
      ${stBadge}
      <span class="fpath" title="${attr(f.path)}">${esc(f.path)}</span>
      <span class="fstat"><span class="add">+${f.additions}</span> <span class="del">&minus;${f.deletions}</span></span>
    </div>`;
  }).join("");
}

function switchSidebar(which) {
  document.querySelectorAll(".sidebar-tab").forEach(t => t.classList.toggle("on", t.dataset.stab === which));
  document.getElementById("sb-changed").classList.toggle("hidden", which !== "changed");
  document.getElementById("sb-browse").classList.toggle("hidden", which !== "browse");
  if (which === "browse") loadBrowseTree();
}

function setActiveFitem(container, path) {
  document.querySelectorAll(`#${container} .fitem`).forEach(it => it.classList.toggle("on", it.dataset.path === path));
}

function disposeEditors() {
  if (diffEditor) { diffEditor.dispose(); diffEditor = null; }
  if (plainEditor) { plainEditor.dispose(); plainEditor = null; }
  for (const m of CREATED_MODELS) { MODEL_META.delete(m.uri.toString()); m.dispose(); }
  CREATED_MODELS = [];
  CZ_MOD = []; CZ_ORIG = [];   // zones die with their editor
  document.getElementById("editor").innerHTML = "";
}

// ---- inline review line comments (view zones on the diff) ----
function measureZoneHeight(node) {
  const width = (document.getElementById("editor").clientWidth || 800) - 70;
  const probe = document.createElement("div");
  probe.style.cssText = "position:absolute;visibility:hidden;left:-9999px;top:0;width:" + width + "px";
  document.body.appendChild(probe);
  probe.appendChild(node);
  const h = node.offsetHeight;
  document.body.removeChild(probe);
  return h;
}

function cmtNode(user, when, bodyHtml, pending) {
  const el = document.createElement("div");
  el.className = "cz-cmt";
  el.innerHTML = `<div class="h"><span class="who">${esc(user)}</span>` +
    (pending ? '<span class="pending">pending</span>' : `<span class="when">${esc(when)}</span>`) +
    `</div><div class="b">${bodyHtml}</div>`;
  return el;
}

function threadNode(existing, pending) {
  const wrap = document.createElement("div");
  wrap.className = "cz";
  const t = document.createElement("div");
  t.className = "cz-thread";
  wrap.appendChild(t);
  for (const c of existing) t.appendChild(cmtNode(c.user, relTime(c.created_at), mdLite(c.body), false));
  for (const c of pending) t.appendChild(cmtNode(VIEWER || "you", "", esc(c.body), true));
  return wrap;
}

function clearZones(editor, ids) {
  editor.changeViewZones((acc) => { for (const id of ids) acc.removeZone(id); });
  ids.length = 0;
}

function addZone(editor, afterLine, node, ids) {
  const h = measureZoneHeight(node);
  editor.changeViewZones((acc) => { ids.push(acc.addZone({ afterLineNumber: afterLine, heightInPx: h + 2, domNode: node })); });
}

function renderFileComments(path) {
  if (!diffEditor) return;
  const me = diffEditor.getModifiedEditor();
  const orig = diffEditor.getOriginalEditor();
  clearZones(me, CZ_MOD);
  clearZones(orig, CZ_ORIG);
  const conv = DETAIL.conversation || { review_comments: [] };
  const groups = {};
  for (const c of conv.review_comments.filter(c => c.path === path && c.line)) {
    const k = (c.side || "RIGHT") + ":" + c.line;
    (groups[k] = groups[k] || { existing: [], pending: [] }).existing.push(c);
  }
  for (const c of pendingComments.filter(c => c.path === path)) {
    const k = c.side + ":" + c.line;
    (groups[k] = groups[k] || { existing: [], pending: [] }).pending.push(c);
  }
  for (const k of Object.keys(groups)) {
    const side = k.slice(0, k.indexOf(":"));
    const line = +k.slice(k.indexOf(":") + 1);
    const ed = side === "LEFT" ? orig : me;
    const ids = side === "LEFT" ? CZ_ORIG : CZ_MOD;
    addZone(ed, line, threadNode(groups[k].existing, groups[k].pending), ids);
  }
}

function attachCommentAction(editor, side) {
  editor.addAction({
    id: "pr-add-comment-" + side,
    label: "Add review comment on this line",
    contextMenuGroupId: "1_modification",
    contextMenuOrder: 0,
    run: (ed) => openCommentComposer(ed, ed.getPosition().lineNumber, side),
  });
}

function openCommentComposer(editor, line, side) {
  const node = document.createElement("div");
  node.className = "cz cz-composer";
  node.innerHTML = `<div class="cz-anchor">${esc(activeFile)}:${line} · ${side === "LEFT" ? "old" : "new"} side</div>
    <textarea placeholder="Comment on this line…"></textarea>
    <div class="row"><button class="btn ghost sm cz-cancel">Cancel</button><button class="btn primary sm cz-add">Add to review</button></div>`;
  let zid;
  const h = measureZoneHeight(node);
  editor.changeViewZones((acc) => { zid = acc.addZone({ afterLineNumber: line, heightInPx: h + 2, domNode: node }); });
  const ta = node.querySelector("textarea");
  ta.focus();
  const remove = () => editor.changeViewZones((acc) => acc.removeZone(zid));
  node.querySelector(".cz-cancel").onclick = remove;
  node.querySelector(".cz-add").onclick = () => {
    const body = ta.value.trim();
    if (!body) { remove(); return; }
    pendingComments.push({ path: activeFile, line, side, body });
    remove();
    renderFileComments(activeFile);
    updateReviewBar();
  };
}

function updateReviewBar() {
  let bar = document.getElementById("reviewBar");
  if (!pendingComments.length) { if (bar) bar.remove(); return; }
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "reviewBar";
    bar.className = "review-bar";
    document.getElementById("detail").appendChild(bar);
  }
  const n = pendingComments.length;
  bar.innerHTML = `<span class="n">${n} pending comment${n > 1 ? "s" : ""}</span>
    <input class="summary" id="reviewSummary" placeholder="Overall review summary (optional)…">
    <select id="reviewEvent"><option value="COMMENT">Comment</option><option value="APPROVE">Approve</option><option value="REQUEST_CHANGES">Request changes</option></select>
    <button class="btn ghost sm" id="reviewDiscard">Discard</button>
    <button class="btn primary sm" id="reviewSubmit">Submit review</button>`;
  bar.querySelector("#reviewDiscard").onclick = () => { pendingComments = []; updateReviewBar(); if (activeFile) renderFileComments(activeFile); };
  bar.querySelector("#reviewSubmit").onclick = submitReview;
}

async function submitReview() {
  const summary = (document.getElementById("reviewSummary") || {}).value || "";
  const event = (document.getElementById("reviewEvent") || {}).value || "COMMENT";
  const btn = document.getElementById("reviewSubmit");
  btn.disabled = true; btn.textContent = "Submitting…";
  try {
    await api2(`api/pr/${repoPath()}/${DETAIL.pr.number}/review`, {
      commit_id: DETAIL.pr.head_sha, body: summary, event,
      comments: pendingComments.map(c => ({ path: c.path, line: c.line, side: c.side, body: c.body })),
    });
    pendingComments = [];
    flashNote("Review submitted to GitHub");
    await reloadConversation();
    updateReviewBar();
    if (activeFile) renderFileComments(activeFile);
  } catch (e) {
    flashNote("Couldn't submit review: " + e.message);
    btn.disabled = false; btn.textContent = "Submit review";
  }
}

async function selectChangedFile(path) {
  activeFile = path;
  setActiveFitem("sb-changed", path);
  const file = DETAIL.files.find(f => f.path === path);
  const pr = DETAIL.pr;
  const bar = document.getElementById("editorBar");
  bar.innerHTML = `<span class="epath">${esc(path)}</span><span class="etag">${file.status}</span>
    <span class="spacer"></span><span class="mono" style="color:var(--faint)">diff vs ${esc(pr.base)}</span>`;
  disposeEditors();
  document.getElementById("editor").innerHTML = '<div class="editor-empty"><div class="inner">Loading diff…</div></div>';
  const [owner, name] = pr.repo.split("/");
  const qs = new URLSearchParams({
    path, head_repo: pr.head_repo, head_sha: pr.head_sha, base_sha: pr.base_sha,
    status: file.status, previous_path: file.previous_path || "",
  });
  let content;
  try {
    content = await api(`api/pr/${owner}/${name}/${pr.number}/file?${qs.toString()}`);
  } catch (e) {
    document.getElementById("editor").innerHTML = `<div class="editor-empty"><div class="inner err-note">${esc(e.message)}</div></div>`;
    return;
  }
  if (file.is_binary || content.binary) {
    document.getElementById("editor").innerHTML = '<div class="editor-empty"><div class="inner"><div class="big">Binary file</div>Not shown as text.</div></div>';
    return;
  }
  document.getElementById("editor").innerHTML = "";
  const original = makeModel(content.base, path, "base");
  const modified = makeModel(content.head, path, "head");
  diffEditor = monaco.editor.createDiffEditor(document.getElementById("editor"), {
    theme: "cockpit", readOnly: true, automaticLayout: true,
    renderSideBySide: true, fontFamily: '"IBM Plex Mono", monospace', fontSize: 12.5,
    hideUnchangedRegions: { enabled: true, contextLineCount: 3, minimumLineCount: 4 },
    scrollBeyondLastLine: false, renderOverviewRuler: true,
  });
  diffEditor.setModel({ original, modified });
  attachUsageAction(diffEditor.getModifiedEditor());
  attachUsageAction(diffEditor.getOriginalEditor());
  attachPyDefAction(diffEditor.getModifiedEditor());
  attachCommentAction(diffEditor.getModifiedEditor(), "RIGHT");
  attachCommentAction(diffEditor.getOriginalEditor(), "LEFT");
  renderFileComments(path);
}

// ---- Python go-to-definition (Jedi) ----
function attachPyDefAction(editor) {
  editor.addAction({
    id: "pr-review-pydef",
    label: "Go to definition (Python)",
    contextMenuGroupId: "navigation",
    contextMenuOrder: 0.5,
    keybindings: [monaco.KeyCode.F12],
    run: (ed) => runPyDef(ed, ed.getPosition()),
  });
  // Cmd/Ctrl-click is the natural gesture for go-to-definition.
  editor.onMouseDown((e) => {
    if (!(e.event.ctrlKey || e.event.metaKey)) return;
    if (e.target.type !== monaco.editor.MouseTargetType.CONTENT_TEXT || !e.target.position) return;
    runPyDef(editor, e.target.position);
  });
}

async function runPyDef(ed, pos) {
  const model = ed.getModel();
  const meta = MODEL_META.get(model.uri.toString());
  if (!meta || meta.side !== "head" || !DETAIL || langFor(meta.path) !== "python") return;
  const pr = DETAIL.pr;
  const [owner, name] = pr.head_repo.split("/");
  let d;
  try {
    d = await api(`api/repo/${owner}/${name}/${pr.head_sha}/pydef?path=${encodeURIComponent(meta.path)}&line=${pos.lineNumber}&col=${pos.column}`);
  } catch (e) { flashNote("Definition lookup failed: " + e.message); return; }
  if (!d || d.found === false) { flashNote("No definition found"); return; }
  if (d.in_repo) {
    flashNote(`${d.name} — ${d.type} · ${d.path}:${d.line}`);
    openRepoFile(d.path, d.line);
  } else {
    flashNote(`${d.name} — ${d.type} in ${d.path.split("/").pop()}:${d.line} (outside this repo)`);
  }
}

function flashNote(msg) {
  let t = document.getElementById("defToast");
  if (!t) { t = document.createElement("div"); t.id = "defToast"; t.className = "def-toast"; document.body.appendChild(t); }
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._hide);
  t._hide = setTimeout(() => t.classList.remove("show"), 3800);
}

// ---- code navigation: find usages / go to definition ----
function attachUsageAction(editor) {
  editor.addAction({
    id: "pr-review-find-usages",
    label: "Find usages / definition in repo",
    contextMenuGroupId: "navigation",
    contextMenuOrder: 0,
    keybindings: [monaco.KeyMod.Shift | monaco.KeyCode.F12],
    run: (ed) => {
      const pos = ed.getPosition();
      const word = ed.getModel().getWordAtPosition(pos);
      if (word && word.word) findUsages(word.word);
    },
  });
}

async function findUsages(symbol) {
  const pr = DETAIL.pr;
  const [owner, name] = pr.head_repo.split("/");
  const panel = document.getElementById("refsPanel");
  panel.classList.remove("hidden");
  panel.innerHTML = `<div class="refs-head"><span class="rtitle">Usages of <code>${esc(symbol)}</code></span>
    <span class="rmeta">searching…</span><span class="spacer" style="flex:1"></span>
    <button class="btn ghost sm" id="refsClose">Close</button></div>`;
  panel.querySelector("#refsClose").addEventListener("click", hideRefs);
  let data;
  try {
    data = await api(`api/repo/${owner}/${name}/${pr.head_sha}/usages?name=${encodeURIComponent(symbol)}`);
  } catch (e) {
    panel.querySelector(".rmeta").textContent = "error: " + e.message;
    return;
  }
  renderRefs(symbol, data);
}

function hideRefs() {
  const panel = document.getElementById("refsPanel");
  panel.classList.add("hidden");
  panel.innerHTML = "";
}

function refLineHTML(r) {
  return `<div class="ref-line ${r.is_def ? "isdef" : ""}" data-path="${attr(r.path.replace(/^\.\//, ""))}" data-line="${r.line}">
    <span class="lno">${r.line}</span><span class="src">${esc(r.text.replace(/\t/g, "  "))}</span>
    ${r.is_def ? '<span class="defbadge">def</span>' : ""}</div>`;
}

function renderRefs(symbol, data) {
  const panel = document.getElementById("refsPanel");
  const defs = data.results.filter(r => r.is_def);
  const refs = data.results;
  // group all references by file (preserving line order)
  const byFile = {};
  for (const r of refs) (byFile[r.path] = byFile[r.path] || []).push(r);
  let html = `<div class="refs-head"><span class="rtitle">Usages of <code>${esc(symbol)}</code></span>
    <span class="rmeta">${data.total}${data.truncated ? "+" : ""} in ${Object.keys(byFile).length} files · ${data.definitions} likely def${data.definitions === 1 ? "" : "s"}</span>
    <span class="spacer" style="flex:1"></span><button class="btn ghost sm" id="refsClose">Close</button></div>
    <div class="refs-scroll">`;
  if (defs.length) {
    html += `<div class="refs-section">Likely definition${defs.length === 1 ? "" : "s"}</div>`;
    html += defs.map(refLineHTML).join("");
  }
  html += `<div class="refs-section">All references</div>`;
  for (const path of Object.keys(byFile).sort()) {
    html += `<div class="ref-file">${esc(path.replace(/^\.\//, ""))}</div>`;
    html += byFile[path].sort((a, b) => a.line - b.line).map(refLineHTML).join("");
  }
  html += "</div>";
  panel.innerHTML = html;
  panel.querySelector("#refsClose").addEventListener("click", hideRefs);
  panel.querySelectorAll(".ref-line").forEach(el =>
    el.addEventListener("click", () => openRepoFile(el.dataset.path, +el.dataset.line)));
}

// ---- open-any-file ----
let BROWSE_FILES = null;
async function loadBrowseTree() {
  if (BROWSE_FILES) return;
  const pr = DETAIL.pr;
  const [owner, name] = pr.head_repo.split("/");
  try {
    const data = await api(`api/repo/${owner}/${name}/${pr.head_sha}/tree`);
    BROWSE_FILES = data.files;
  } catch (e) {
    document.getElementById("browse-list").innerHTML = `<div class="err-note">${esc(e.message)}</div>`;
    return;
  }
  renderBrowse("");
  document.getElementById("fileFilter").addEventListener("input", (e) => renderBrowse(e.target.value));
}

function renderBrowse(filter) {
  const f = filter.toLowerCase();
  const matched = BROWSE_FILES.filter(p => p.toLowerCase().includes(f)).slice(0, 400);
  const list = document.getElementById("browse-list");
  list.className = "";
  list.innerHTML = matched.map(p =>
    `<div class="fitem" data-path="${attr(p)}"><span class="fpath" title="${attr(p)}">${esc(p)}</span></div>`).join("")
    + (BROWSE_FILES.filter(p => p.toLowerCase().includes(f)).length > 400 ? '<div class="loading-note" style="padding:12px">Refine filter to see more…</div>' : "");
  list.querySelectorAll(".fitem").forEach(it => it.addEventListener("click", () => openRepoFile(it.dataset.path)));
}

async function openRepoFile(path, line) {
  activeFile = null;
  setActiveFitem("browse-list", path);
  const pr = DETAIL.pr;
  const [owner, name] = pr.head_repo.split("/");
  const bar = document.getElementById("editorBar");
  bar.innerHTML = `<span class="epath">${esc(path)}</span><span class="etag">context</span>
    <span class="spacer"></span><span class="mono" style="color:var(--faint)">read-only @ ${esc(pr.head.slice(0, 24))}</span>`;
  disposeEditors();
  document.getElementById("editor").innerHTML = '<div class="editor-empty"><div class="inner">Loading file…</div></div>';
  let content;
  try {
    content = await api(`api/repo/${owner}/${name}/${pr.head_sha}/file?path=${encodeURIComponent(path)}`);
  } catch (e) {
    document.getElementById("editor").innerHTML = `<div class="editor-empty"><div class="inner err-note">${esc(e.message)}</div></div>`;
    return;
  }
  document.getElementById("editor").innerHTML = "";
  const model = makeModel(content.content, path, "head");
  plainEditor = monaco.editor.create(document.getElementById("editor"), {
    model, theme: "cockpit", readOnly: true, automaticLayout: true,
    fontFamily: '"IBM Plex Mono", monospace', fontSize: 12.5, scrollBeyondLastLine: false, minimap: { enabled: true },
  });
  attachUsageAction(plainEditor);
  attachPyDefAction(plainEditor);
  if (line) {
    plainEditor.revealLineInCenter(line);
    plainEditor.setPosition({ lineNumber: line, column: 1 });
    plainEditor.createDecorationsCollection([{
      range: new monaco.Range(line, 1, line, 1),
      options: { isWholeLine: true, className: "ref-hit-line", linesDecorationsClassName: "ref-hit-gutter" },
    }]);
  }
}

function closeDetail() {
  disposeEditors();
  const bar = document.getElementById("reviewBar");
  if (bar) bar.remove();
  pendingComments = [];
  DETAIL = null; BROWSE_FILES = null; activeFile = null;
  document.getElementById("detail").classList.add("hidden");
  document.getElementById("list").classList.remove("hidden");
  document.getElementById("brandctx").textContent = "// review cockpit";
}

// ===================================================================
// boot
// ===================================================================
async function boot() {
  try {
    const me = await api("api/prs");
    LIST_DATA = me;
    VIEWER = me.viewer || "";
    document.getElementById("viewer").innerHTML =
      `<span class="avatar">${esc((me.viewer || "?").slice(0, 2).toUpperCase())}</span><span>${esc(me.viewer)}</span>`;
    renderList();
    const all = [...me.authored, ...me.review_requested];
    pool(all, 6, async (pr) => {
      const [owner, name] = pr.repo.split("/");
      const st = await api(`api/pr/${owner}/${name}/${pr.number}/status`);
      STATUS_CACHE[keyFor(pr.repo, pr.number)] = st;
      const rowEl = document.querySelector(`.row[data-key="${cssEsc(keyFor(pr.repo, pr.number))}"]`);
      if (rowEl) { rowEl.outerHTML = rowHTML(pr); rebindRow(pr); }
    });
  } catch (e) {
    document.getElementById("list").innerHTML = `<div class="err-note">Couldn't load your pull requests: ${esc(e.message)}</div>`;
  }
}

document.getElementById("homeLink").addEventListener("click", () => { if (DETAIL) closeDetail(); });
document.getElementById("refreshBtn").addEventListener("click", () => { STATUS_CACHE = {}; boot(); });
boot();
