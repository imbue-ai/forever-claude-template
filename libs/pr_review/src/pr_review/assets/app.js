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
let LIST_DATA = null;
let STATUS_CACHE = {};   // "repo#num" -> status object

// ---- view state ----
// filter/group/sort persist across reloads; search resets each load.
const PREFS_KEY = "prr.view";
function loadPrefs() {
  const defaults = { filter: "all", group: "repo", sort: "updated" };
  try { return { ...defaults, ...JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") }; }
  catch (_e) { return defaults; }
}
const _prefs = loadPrefs();
let CURRENT_FILTER = _prefs.filter;   // all | attention | ready | draft
let CURRENT_GROUP = _prefs.group;     // repo | none
let CURRENT_SORT = _prefs.sort;       // updated | newest | oldest | active | attention
let SEARCH = "";
const COLLAPSED = new Set();          // collapsed repo groups (in-memory)

function savePrefs() {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify({ filter: CURRENT_FILTER, group: CURRENT_GROUP, sort: CURRENT_SORT })); }
  catch (_e) { /* storage unavailable -- choices just won't persist */ }
}

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
    <div class="spine ${sp}" title="${attr(spineReason(pr))}"></div>
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

function needsAttention(st) {
  return !!(st && (st.has_conflicts || st.ci?.verdict === "failing" || st.review_decision === "changes requested"));
}

// Plain-English meaning of a row's status spine -- used as the hover tooltip so
// the color is never a mystery.
function spineReason(pr) {
  const st = STATUS_CACHE[keyFor(pr.repo, pr.number)];
  if (!st) return "Checking status…";
  const why = [];
  if (st.has_conflicts) why.push("merge conflicts");
  if (st.ci?.verdict === "failing") why.push("CI failing");
  if (st.review_decision === "changes requested") why.push("changes requested");
  if (why.length) return "Needs attention: " + why.join(", ");
  if (st.ci?.verdict === "pending") return "CI running";
  if (st.state === "draft") return "Draft";
  return "Ready";
}

function passesFilter(pr) {
  const st = STATUS_CACHE[keyFor(pr.repo, pr.number)];
  const state = st ? st.state : pr.state;
  if (CURRENT_FILTER === "all") return true;
  if (CURRENT_FILTER === "draft") return state === "draft";
  if (CURRENT_FILTER === "ready") return state === "ready";
  if (CURRENT_FILTER === "attention") return needsAttention(st);
  return true;
}

// Live tallies for the triage filter strip. Status loads in lazily, so these
// are recomputed as each PR's status arrives.
function countByFilter() {
  const all = [...LIST_DATA.authored, ...LIST_DATA.review_requested];
  const c = { all: all.length, attention: 0, ready: 0, draft: 0 };
  for (const pr of all) {
    const st = STATUS_CACHE[keyFor(pr.repo, pr.number)];
    const state = st ? st.state : pr.state;
    if (state === "draft") c.draft++;
    if (state === "ready") c.ready++;
    if (needsAttention(st)) c.attention++;
  }
  return c;
}

function updateChipCounts() {
  if (!LIST_DATA) return;
  const c = countByFilter();
  document.querySelectorAll(".chip").forEach((ch) => {
    const span = ch.querySelector(".cnt");
    if (span) span.textContent = c[ch.dataset.filter];
    if (ch.dataset.filter === "attention") ch.classList.toggle("has-items", c.attention > 0);
  });
}

// ---- view transforms: search -> filter -> sort -> group ----
function matchesSearch(pr) {
  if (!SEARCH) return true;
  const q = SEARCH.toLowerCase();
  return pr.title.toLowerCase().includes(q) || pr.repo.toLowerCase().includes(q);
}
function prTime(pr, field) { return new Date(pr[field] || 0).getTime(); }
function sortPRs(list) {
  const arr = list.slice();
  if (CURRENT_SORT === "newest") arr.sort((a, b) => prTime(b, "created_at") - prTime(a, "created_at"));
  else if (CURRENT_SORT === "oldest") arr.sort((a, b) => prTime(a, "created_at") - prTime(b, "created_at"));
  else if (CURRENT_SORT === "active") arr.sort((a, b) => (b.comments || 0) - (a.comments || 0) || prTime(b, "updated_at") - prTime(a, "updated_at"));
  else if (CURRENT_SORT === "attention") {
    const rank = pr => (needsAttention(STATUS_CACHE[keyFor(pr.repo, pr.number)]) ? 0 : 1);
    arr.sort((a, b) => rank(a) - rank(b) || prTime(b, "updated_at") - prTime(a, "updated_at"));
  } else arr.sort((a, b) => prTime(b, "updated_at") - prTime(a, "updated_at"));
  return arr;
}
function groupByRepo(prs) {
  const m = new Map();
  for (const pr of prs) { if (!m.has(pr.repo)) m.set(pr.repo, []); m.get(pr.repo).push(pr); }
  return [...m.entries()].map(([repo, list]) => ({ repo, prs: list }));
}
function applyView(list) {
  return sortPRs(list.filter(pr => passesFilter(pr) && matchesSearch(pr)));
}
function bucketHTML(prs) {
  if (CURRENT_GROUP !== "repo") return prs.map(rowHTML).join("");
  return groupByRepo(prs).map(g => `
    <div class="repo-group ${COLLAPSED.has(g.repo) ? "collapsed" : ""}">
      <div class="repo-head" data-repo-toggle="${attr(g.repo)}">
        <span class="caret" aria-hidden="true">&#9656;</span>
        <span class="repo-name">${esc(g.repo)}</span>
        <span class="repo-n">${g.prs.length}</span>
      </div>
      <div class="repo-rows">${g.prs.map(rowHTML).join("")}</div>
    </div>`).join("");
}

// The toolbar is rendered once and left in place; only #results is rebuilt as
// the view changes, so the search box keeps focus while you type.
function renderList() {
  if (!LIST_DATA) return;
  const el = document.getElementById("list");
  const counts = countByFilter();
  const chipLabel = { all: "All", attention: "Needs attention", ready: "Ready", draft: "Drafts" };
  const sortLabel = { updated: "Recently updated", newest: "Newest", oldest: "Oldest", active: "Most active", attention: "Needs attention first" };
  el.innerHTML = `
    <div class="controls">
      <div class="search">
        <span class="si" aria-hidden="true">&#9906;</span>
        <input id="prSearch" type="search" placeholder="Search title or repo…" value="${attr(SEARCH)}" autocomplete="off" />
      </div>
      <div class="chips">
        ${["all", "attention", "ready", "draft"].map(f => {
          const extra = f === "attention" && counts.attention > 0 ? " has-items" : "";
          return `<button class="chip ${f} ${f === CURRENT_FILTER ? "on" : ""}${extra}" data-filter="${f}">${chipLabel[f]}<span class="cnt">${counts[f]}</span></button>`;
        }).join("")}
      </div>
      <div class="seg" id="groupSeg">
        ${["repo", "none"].map(g => `<button class="segbtn ${g === CURRENT_GROUP ? "on" : ""}" data-group="${g}">${g === "repo" ? "By repo" : "Flat"}</button>`).join("")}
      </div>
      <label class="sortwrap">Sort
        <select id="sortSel">${Object.keys(sortLabel).map(s => `<option value="${s}" ${s === CURRENT_SORT ? "selected" : ""}>${sortLabel[s]}</option>`).join("")}</select>
      </label>
    </div>
    <div class="legend" title="The colored strip on the left of each pull request shows its status at a glance.">
      <span class="lkey">Status</span>
      <span class="litem"><i class="sw red"></i>Needs attention</span>
      <span class="litem"><i class="sw amber"></i>CI running</span>
      <span class="litem"><i class="sw green"></i>Ready</span>
      <span class="litem"><i class="sw gray"></i>Draft</span>
    </div>
    <div id="results"></div>`;
  const search = el.querySelector("#prSearch");
  search.addEventListener("input", () => { SEARCH = search.value.trim(); renderResults(); });
  el.querySelectorAll(".chip").forEach(c => c.addEventListener("click", () => {
    CURRENT_FILTER = c.dataset.filter; savePrefs();
    el.querySelectorAll(".chip").forEach(x => x.classList.toggle("on", x === c));
    renderResults();
  }));
  el.querySelectorAll(".segbtn").forEach(s => s.addEventListener("click", () => {
    CURRENT_GROUP = s.dataset.group; savePrefs();
    el.querySelectorAll(".segbtn").forEach(x => x.classList.toggle("on", x === s));
    renderResults();
  }));
  el.querySelector("#sortSel").addEventListener("change", (e) => { CURRENT_SORT = e.target.value; savePrefs(); renderResults(); });
  renderResults();
}

function renderResults() {
  const results = document.getElementById("results");
  if (!results) return;
  const mine = applyView(LIST_DATA.authored);
  const reqs = applyView(LIST_DATA.review_requested);
  const mineEmpty = SEARCH ? "No pull requests match your search." : "Nothing matches this filter.";
  results.innerHTML = `
    <section class="section">
      <div class="section-head"><h2>Created by you</h2><span class="n">${mine.length}/${LIST_DATA.authored.length}</span><div class="line"></div></div>
      <div id="mine-rows">${mine.length ? bucketHTML(mine) : `<div class="empty"><div class="small">${mineEmpty}</div></div>`}</div>
    </section>
    <section class="section">
      <div class="section-head"><h2>Awaiting your review</h2><span class="n">${reqs.length}/${LIST_DATA.review_requested.length}</span><div class="line"></div></div>
      ${reqs.length ? `<div id="req-rows">${bucketHTML(reqs)}</div>`
        : `<div class="empty"><div class="big">Nothing waiting on you</div><div class="small">${SEARCH ? "No matches in PRs awaiting your review." : "PRs where your review is requested will appear here."}</div></div>`}
    </section>`;
  results.querySelectorAll(".repo-head").forEach(h => h.addEventListener("click", () => {
    const repo = h.dataset.repoToggle;
    if (COLLAPSED.has(repo)) COLLAPSED.delete(repo); else COLLAPSED.add(repo);
    h.closest(".repo-group").classList.toggle("collapsed");
  }));
  results.querySelectorAll(".row").forEach(bindRow);
}

// When the active view depends on lazily-loaded status (attention filter/sort),
// settle the ordering once status arrives -- coalesced so it runs at most a few times.
let _reflowPending = false;
function scheduleStatusReflow() {
  if (CURRENT_FILTER !== "attention" && CURRENT_SORT !== "attention") return;
  if (_reflowPending) return;
  _reflowPending = true;
  setTimeout(() => { _reflowPending = false; renderResults(); }, 300);
}

function cssEsc(s) { return s.replace(/[#.:/]/g, "\\$&"); }
function bindRow(r) {
  r.addEventListener("click", () => openDetail(r.dataset.repo, +r.dataset.num));
  r.addEventListener("contextmenu", (e) => { e.preventDefault(); openRowMenu(e, r.dataset.repo, +r.dataset.num); });
}
function rebindRow(pr) {
  const rowEl = document.querySelector(`.row[data-key="${cssEsc(keyFor(pr.repo, pr.number))}"]`);
  if (rowEl) bindRow(rowEl);
}

// ===================================================================
// PR ACTIONS -- close / reopen / merge, shared by the detail page and the
// home-page right-click menu. Drafts can't be flipped to "ready for review"
// here: GitHub exposes that only via its GraphQL API, which this tool's
// credentialed access (REST + git) does not include.
// ===================================================================
function actionStatus(repo, number) { return STATUS_CACHE[keyFor(repo, number)] || null; }
function findPR(repo, number) {
  if (!LIST_DATA) return null;
  return [...LIST_DATA.authored, ...LIST_DATA.review_requested].find(p => p.repo === repo && p.number === number) || null;
}
function mergeBlockedReason(s) {
  if (!s) return null;
  if (s.state === "draft") return "This PR is a draft.";
  if (s.has_conflicts || s.mergeable_state === "dirty") return "This PR has merge conflicts.";
  return null;
}

// Drop a PR from the in-memory list after it's closed/merged, returning enough
// to restore it (used by the close "Undo").
function removeFromList(repo, number) {
  if (!LIST_DATA) return null;
  for (const bucket of ["authored", "review_requested"]) {
    const i = LIST_DATA[bucket].findIndex(p => p.repo === repo && p.number === number);
    if (i >= 0) { const [pr] = LIST_DATA[bucket].splice(i, 1); renderResults(); updateChipCounts(); return { pr, bucket }; }
  }
  return null;
}
function addToList(pr, bucket) {
  if (!LIST_DATA || !pr) return;
  LIST_DATA[bucket].push(pr);
  renderResults();
  updateChipCounts();
}

// A small modal returning a Promise: resolves to the chosen value (or `true`),
// or `null` if cancelled. ``extraHTML`` may contain one [data-modal-input].
function confirmDialog({ title, message, confirmLabel, danger, extraHTML }) {
  return new Promise((resolve) => {
    const ov = document.createElement("div");
    ov.className = "modal-overlay";
    ov.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true" aria-label="${attr(title)}">
        <div class="modal-title">${esc(title)}</div>
        <div class="modal-body">${message}</div>
        ${extraHTML || ""}
        <div class="modal-actions">
          <button class="btn ghost" data-act="cancel">Cancel</button>
          <button class="btn ${danger ? "danger" : "primary"}" data-act="ok">${esc(confirmLabel)}</button>
        </div>
      </div>`;
    document.body.appendChild(ov);
    const input = () => { const el = ov.querySelector("[data-modal-input]"); return el ? el.value : true; };
    const done = (v) => { ov.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
    const onKey = (e) => { if (e.key === "Escape") done(null); else if (e.key === "Enter") done(input()); };
    ov.addEventListener("mousedown", (e) => { if (e.target === ov) done(null); });
    ov.querySelector('[data-act="cancel"]').onclick = () => done(null);
    ov.querySelector('[data-act="ok"]').onclick = () => done(input());
    document.addEventListener("keydown", onKey);
    ov.querySelector('[data-act="ok"]').focus();
  });
}

async function actionMerge(repo, number, ctx) {
  const s = ctx || actionStatus(repo, number);
  const blocked = mergeBlockedReason(s);
  if (blocked) { flashNote(blocked + " It can't be merged here."); return false; }
  const head = (s && s.head) || "this branch", base = (s && s.base) || "the base branch";
  const method = await confirmDialog({
    title: `Merge #${number}?`,
    message: `Merge <code>${esc(head)}</code> into <code>${esc(base)}</code> on GitHub. This can't be undone.`,
    confirmLabel: "Merge",
    extraHTML: `<label class="modal-field">Method
      <select data-modal-input>
        <option value="merge">Create a merge commit</option>
        <option value="squash">Squash and merge</option>
        <option value="rebase">Rebase and merge</option>
      </select></label>`,
  });
  if (!method) return false;
  try {
    await api2(`api/pr/${repo}/${number}/merge`, { method });
    removeFromList(repo, number);
    flashNote(`Merged #${number}`);
    return true;
  } catch (e) { flashNote("Couldn't merge: " + e.message); return false; }
}

async function actionClose(repo, number) {
  const ok = await confirmDialog({
    title: `Close #${number}?`,
    message: "This closes the pull request on GitHub without merging. You can reopen it afterward.",
    confirmLabel: "Close pull request",
    danger: true,
  });
  if (!ok) return false;
  try {
    await api2(`api/pr/${repo}/${number}/state`, { state: "closed" });
    const removed = removeFromList(repo, number);
    flashAction(`Closed #${number}`, "Undo", async () => {
      try {
        await api2(`api/pr/${repo}/${number}/state`, { state: "open" });
        if (removed) addToList(removed.pr, removed.bucket);
        flashNote(`Reopened #${number}`);
      } catch (e) { flashNote("Couldn't reopen: " + e.message); }
    });
    return true;
  } catch (e) { flashNote("Couldn't close: " + e.message); return false; }
}

function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => flashNote("Link copied"), () => flashNote("Couldn't copy link"));
    return;
  }
  const ta = document.createElement("textarea");
  ta.value = text; document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); flashNote("Link copied"); } catch (_e) { flashNote("Couldn't copy link"); }
  ta.remove();
}

// ---- home-page right-click menu ----
function closeRowMenu() { const m = document.getElementById("ctxMenu"); if (m) m.remove(); }
function openRowMenu(e, repo, number) {
  closeRowMenu();
  const st = actionStatus(repo, number);
  const pr = findPR(repo, number);
  const url = (pr && pr.url) || `https://github.com/${repo}/pull/${number}`;
  const isDraft = (st ? st.state : (pr && pr.state)) === "draft";
  const items = [{ label: "Open", fn: () => openDetail(repo, number) }];
  if (!isDraft) {
    const blocked = mergeBlockedReason(st);
    items.push({ label: "Merge…", disabled: !!blocked, title: blocked || "", fn: () => actionMerge(repo, number, st) });
  }
  items.push({ label: "Close…", danger: true, fn: () => actionClose(repo, number) });
  items.push({ sep: true });
  items.push({ label: "Copy link", fn: () => copyText(url) });
  items.push({ label: "Open on GitHub", fn: () => window.open(url, "_blank", "noopener") });

  const menu = document.createElement("div");
  menu.className = "ctx-menu";
  menu.id = "ctxMenu";
  menu.innerHTML = items.map((it, i) => it.sep
    ? '<div class="ctx-sep"></div>'
    : `<button class="ctx-item ${it.danger ? "danger" : ""}" data-i="${i}"${it.disabled ? ` disabled title="${attr(it.title)}"` : ""}>${esc(it.label)}</button>`).join("");
  document.body.appendChild(menu);
  const rect = menu.getBoundingClientRect();
  let x = e.clientX, y = e.clientY;
  if (x + rect.width > window.innerWidth) x = window.innerWidth - rect.width - 8;
  if (y + rect.height > window.innerHeight) y = window.innerHeight - rect.height - 8;
  menu.style.left = Math.max(8, x) + "px";
  menu.style.top = Math.max(8, y) + "px";
  menu.querySelectorAll(".ctx-item").forEach(b => b.addEventListener("click", () => {
    const it = items[+b.dataset.i];
    closeRowMenu();
    if (!it.disabled) it.fn();
  }));
}
document.addEventListener("click", closeRowMenu);
document.addEventListener("scroll", closeRowMenu, true);
window.addEventListener("blur", closeRowMenu);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeRowMenu(); });

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
          colors: { "editor.background": "#0A0D13", "editorGutter.background": "#0A0D13", "diffEditor.insertedTextBackground": "#15331f88", "diffEditor.removedTextBackground": "#3a1d1f88" },
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

// Line wrapping in the code view -- off by default (diffs read better unwrapped),
// toggled from the editor bar and remembered across sessions.
let WORD_WRAP = (() => { try { return localStorage.getItem("prr.wrap") === "1"; } catch (_e) { return false; } })();
function wrapValue() { return WORD_WRAP ? "on" : "off"; }
function wrapBtnHTML() {
  return `<button class="btn ghost sm wrap-btn ${WORD_WRAP ? "on" : ""}" id="wrapBtn" title="Toggle line wrapping">Wrap</button>`;
}
function applyWrap() {
  const opts = { wordWrap: wrapValue() };
  if (diffEditor) { diffEditor.getModifiedEditor().updateOptions(opts); diffEditor.getOriginalEditor().updateOptions(opts); }
  if (plainEditor) plainEditor.updateOptions(opts);
  const btn = document.getElementById("wrapBtn");
  if (btn) btn.classList.toggle("on", WORD_WRAP);
}
function toggleWrap() {
  WORD_WRAP = !WORD_WRAP;
  try { localStorage.setItem("prr.wrap", WORD_WRAP ? "1" : "0"); } catch (_e) { /* not persisted */ }
  applyWrap();
}
function bindWrapBtn() {
  const btn = document.getElementById("wrapBtn");
  if (btn) btn.addEventListener("click", toggleWrap);
}

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
  document.getElementById("brandctx").textContent = repo + " #" + number;
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

function detailActionsHTML(pr) {
  const parts = [];
  if (pr.state === "draft") {
    parts.push('<span class="draft-note" title="GitHub exposes draft -> ready for review only through its GraphQL API, which this tool\'s access does not include.">Mark ready isn\'t available here</span>');
  } else {
    const blocked = mergeBlockedReason(pr);
    parts.push(`<button class="btn primary sm" id="actMerge"${blocked ? ` disabled title="${attr(blocked)}"` : ""}>Merge</button>`);
  }
  parts.push('<button class="btn ghost sm" id="actCopyLink">Copy link</button>');
  parts.push('<button class="btn danger sm" id="actClose">Close</button>');
  return `<span class="detail-actions">${parts.join("")}</span>`;
}

function bindDetailActions(pr) {
  const merge = document.getElementById("actMerge");
  if (merge) merge.addEventListener("click", async () => { if (await actionMerge(pr.repo, pr.number, pr)) closeDetail(); });
  const copy = document.getElementById("actCopyLink");
  if (copy) copy.addEventListener("click", () => copyText(pr.url));
  const close = document.getElementById("actClose");
  if (close) close.addEventListener("click", async () => { if (await actionClose(pr.repo, pr.number)) closeDetail(); });
}

function renderDetailShell() {
  const pr = DETAIL.pr;
  const conv = DETAIL.conversation || { comments: [], reviews: [], review_comments: [] };
  const convCount = conv.comments.length + conv.reviews.filter(r => r.body || r.state !== "COMMENTED").length;
  const d = document.getElementById("detail");
  d.innerHTML = `
    <div class="detail-head">
      <div class="crumb"><a id="backBtn">&larr; All pull requests</a><span class="sep">/</span><span>${esc(pr.repo)}</span><span class="sep">#${pr.number}</span>
        <span class="spacer"></span>${detailActionsHTML(pr)}<a href="${attr(pr.url)}" target="_blank" rel="noopener">Open on GitHub &#8599;</a></div>
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
  bindDetailActions(pr);
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
  closeCommentDock();
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

// The composer is a normal DOM panel docked below the editor -- a textarea
// inside a Monaco view zone can't receive keyboard focus (Monaco intercepts it).
function openCommentComposer(editor, line, side) {
  closeCommentDock();
  const dock = document.createElement("div");
  dock.id = "commentDock";
  dock.className = "comment-dock";
  dock.innerHTML = `<div class="cd-head">Comment on <code>${esc(activeFile)}:${line}</code> · ${side === "LEFT" ? "old" : "new"} side
      <span class="spacer" style="flex:1"></span><button class="btn ghost sm" id="cdCancel">Cancel</button></div>
    <textarea class="composer" id="cdText" placeholder="Comment on this line…"></textarea>
    <div class="composer-actions"><button class="btn primary sm" id="cdAdd">Add to review</button></div>`;
  document.querySelector(".editor-wrap").appendChild(dock);
  // The pending-review bar is fixed to the bottom of the editor area; lift the
  // dock above it so its "Add to review" button isn't hidden behind the bar.
  const reviewBar = document.getElementById("reviewBar");
  if (reviewBar) dock.style.marginBottom = reviewBar.offsetHeight + 10 + "px";
  const ta = dock.querySelector("#cdText");
  ta.focus();
  dock.querySelector("#cdCancel").onclick = closeCommentDock;
  dock.querySelector("#cdAdd").onclick = () => {
    const body = ta.value.trim();
    if (!body) { closeCommentDock(); return; }
    pendingComments.push({ path: activeFile, line, side, body });
    closeCommentDock();
    renderFileComments(activeFile);
    updateReviewBar();
  };
}

function closeCommentDock() {
  const dock = document.getElementById("commentDock");
  if (dock) dock.remove();
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
    <span class="spacer"></span><span class="mono" style="color:var(--faint)">diff vs ${esc(pr.base)}</span>${wrapBtnHTML()}`;
  bindWrapBtn();
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
    scrollBeyondLastLine: false, renderOverviewRuler: true, wordWrap: wrapValue(),
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

function ensureToast() {
  let t = document.getElementById("defToast");
  if (!t) { t = document.createElement("div"); t.id = "defToast"; t.className = "def-toast"; document.body.appendChild(t); }
  return t;
}
function flashNote(msg) {
  const t = ensureToast();
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._hide);
  t._hide = setTimeout(() => t.classList.remove("show"), 3800);
}
// A toast with a single inline action (e.g. "Undo"). The action stays available
// a little longer than a plain note.
function flashAction(msg, actionLabel, onAction) {
  const t = ensureToast();
  t.textContent = "";
  const span = document.createElement("span");
  span.textContent = msg;
  const btn = document.createElement("button");
  btn.className = "toast-act";
  btn.textContent = actionLabel;
  btn.onclick = () => { t.classList.remove("show"); onAction(); };
  t.append(span, btn);
  t.classList.add("show");
  clearTimeout(t._hide);
  t._hide = setTimeout(() => t.classList.remove("show"), 7000);
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
    <span class="spacer"></span><span class="mono" style="color:var(--faint)">read-only @ ${esc(pr.head.slice(0, 24))}</span>${wrapBtnHTML()}`;
  bindWrapBtn();
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
    wordWrap: wrapValue(),
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
  document.getElementById("brandctx").textContent = "all open work";
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
      updateChipCounts();
      scheduleStatusReflow();
    });
  } catch (e) {
    document.getElementById("list").innerHTML = `<div class="err-note">Couldn't load your pull requests: ${esc(e.message)}</div>`;
  }
}

document.getElementById("homeLink").addEventListener("click", () => { if (DETAIL) closeDetail(); });
document.getElementById("refreshBtn").addEventListener("click", () => { STATUS_CACHE = {}; boot(); });
boot();
