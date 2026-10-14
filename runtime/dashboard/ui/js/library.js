'use strict';
/* Library: every file the factory writes, filtered and searched by the server, with a reader beside the list. */

const LIB_HINT = 'Every file the factory writes, newest first. The search bar at the top finds any of them by title, id or text.';
const libFresh = (q = '') => ({kinds: null, range: 'all', from: '', to: '', status: 'all', inbox: 'all', q, sel: null, pages: 1});
UI.lib = libFresh();
UI.docs = {};
PAGE.library = {
  url: () => {
    const L = UI.lib, q = new URLSearchParams();
    if (L.kinds) q.set('kinds', [...L.kinds].join(','));
    q.set('range', L.range);
    if (L.range === 'custom') { q.set('from', L.from); q.set('to', L.to); }
    q.set('status', L.status); q.set('inbox', L.inbox);
    if (L.q.trim()) q.set('q', L.q.trim());
    return '/api/library?' + q;
  },
  load: url => UI.lib.pages > 1 ? loadPaged(url, UI.lib.pages, 'library', 'results') : fetchEnv('page', url),
  render: pLibrary,
  after: env => { setLibCols($('.lib'), libCols()); if (env && UI.lib.sel && !UI.docs[UI.lib.sel]) loadDoc(UI.lib.sel); }
};
const libData = () => { const env = UI.data[PAGE.library.url()]; return env ? partData(env, 'library') : null; };
function pLibrary(env) {
  const d = partData(env, 'library'), head = `<div class="page-h">${ph('Library', LIB_HINT)}\n</div>`;
  if (!d) return head + part(env, 'library', () => '');
  const L = UI.lib, res = d.results.filter(r => !r.broken);
  if (!res.find(r => r.id === L.sel) && !isPhone()) L.sel = res[0] ? res[0].id : null;
  const look = k => { const x = d.kinds.find(y => y.key === k); return ktile(x.icon, x.tone); };
  return `${head}
    <div class="lib">
      <div class="card filters">
        <div class="fgroup"><div class="label">Kind</div>${d.kinds.map(k => `<label class="check"><input type="checkbox" id="kind-${esc(k.key)}" data-kind="${esc(k.key)}" ${k.checked ? 'checked' : ''}>${esc(k.label)}<span class="c">${k.count}</span></label>`).join('')}</div>
        <div class="fgroup"><div class="label">When</div><div class="pillset">${d.ranges.map(([k, n]) => `<button class="pill" data-librange="${esc(k)}" aria-pressed="${d.range === k}">${esc(n)}</button>`).join('')}</div>
          ${d.range === 'custom' ? `<div class="dates"><input type="date" id="lib-from" value="${esc(L.from)}" aria-label="From"><input type="date" id="lib-to" value="${esc(L.to)}" aria-label="To"></div>` : ''}</div>
        <div class="fgroup"><div class="label">Status</div>${dd('lib-status', '', d.status, d.statuses, 'Status')}</div>
        <div class="fgroup"><div class="label">Inbox</div>${dd('lib-inbox', '', d.inbox, d.inboxes, 'Inbox')}</div>
        <button class="btn sm quiet" data-action="lib-reset">${icon('arrow-path', 'sm')}Clear filters</button>
      </div>
      <div class="split" data-split="f" role="separator" aria-orientation="vertical" aria-label="Resize the filters. Double-click to reset." tabindex="0"></div>
      <div class="card results"><div class="group-h"><span class="label">${esc(d.total)}</span></div>${d.query ? `<div class="qchip">${icon('magnifying-glass', 'sm')}<span style="flex:1;min-width:0">Matching “<b style="color:var(--fg)">${esc(d.query)}</b>”</span><button class="btn sm quiet" data-action="lib-clear-q">${icon('x-mark', 'sm')}Clear</button></div>` : ''}<div class="rows" data-keep-scroll="lib-list">${d.results.length ? d.results.map(r => r.broken ? brokenRow(r) : `<div class="res" data-libsel="${esc(r.id)}" aria-current="${r.id === L.sel}" tabindex="0">${look(r.kind)}<div style="min-width:0"><div class="rt"><span>${safeHtml(r.title_html, {MARK: []})}</span></div><div class="ts">${r.chips.map(chip).join('')}</div><div class="snip">${safeHtml(r.snippet_html, {MARK: []})}</div></div><div class="when">${esc(r.date)}<small>${esc(r.time)}</small></div></div>`).join('') + showMore('library', d.more) : '<div class="empty">Nothing matches. Clear a filter or the search.</div>'}</div></div>
      <div class="split" data-split="r" role="separator" aria-orientation="vertical" aria-label="Resize the file list. Double-click to reset." tabindex="0"></div>
      <article class="card reader" id="reader" data-keep-scroll="lib-reader">${readerHTML(L.sel)}</article>
    </div>`;
}
function readerHTML(id) {
  if (!id) return '<div class="empty">Pick a file.</div>';
  const env = UI.docs[id];
  if (!env) return '<div class="empty">Loading…</div>';
  const p = env.parts.doc || Object.values(env.parts)[0];
  return p.ok ? docHTML(p.data) : failedPart(p.error, '');
}
function docHTML(d) {
  const fmVal = v => { const t = v.doc ? `<span class="doclink" data-open-doc="${esc(v.doc)}" tabindex="0">${esc(v.text)}</span>` : esc(v.text); return v.tone == null ? t : `<span class="chip ${esc(v.tone)}">${t}</span>`; };
  return `<div class="doc-head">${ktile(d.kind_icon, d.kind_tone)}<div><div class="dh-kind">${esc(d.kind_label)} <span class="faint mono" style="font-weight:500">${esc(d.id)}</span></div><div class="dh-when">${esc(d.when)}</div></div></div>${d.fm.length ? `<dl class="fm-grid">${d.fm.map(f => `<div><dt>${esc(f.label)}</dt><dd>${f.values.map(fmVal).join(' ')}</dd></div>`).join('')}</dl>` : ''}${safeHtml(d.html)}${d.brief_url ? `<div class="spec-full"><span class="ico">${icon('document-text')}</span><div style="flex:1;min-width:0"><b>The brief</b><div class="muted" style="font-size:12px">Design, diagram, mock-ups and your comments, in its brief</div></div><button class="btn sm primary" data-brief="${esc(d.brief_url)}">${icon('document-text', 'sm')}Open brief</button></div>` : ''}`;
}
async function loadDoc(id) {
  let env;
  try { env = await fetchEnv('doc', '/api/doc/' + encodeURIComponent(id)); } catch (e) { loadFailed(e, 'This file', () => loadDoc(id)); return; }
  if (!env) return;
  UI.docs[id] = env;
  const reader = $('#reader');
  if (reader && UI.lib.sel === id) { reader.innerHTML = readerHTML(id); reader.scrollTop = 0; }
}
/* Selecting a file updates only the reader and the highlighted row, so nothing else on the page moves. */
function selectDoc(id) {
  UI.lib.sel = id;
  $$('[data-libsel]').forEach(el => el.setAttribute('aria-current', String(el.dataset.libsel === id)));
  const reader = $('#reader');
  if (reader) { reader.innerHTML = readerHTML(id); reader.scrollTop = 0; }
  loadDoc(id); keepInList(id);
}
function keepInList(id) {
  const it = $(`[data-libsel="${CSS.escape(id)}"]`); if (!it) return;
  const list = it.parentElement, r = it.getBoundingClientRect(), lr = list.getBoundingClientRect();
  if (r.top < lr.top) list.scrollTop -= lr.top - r.top + 8;
  else if (r.bottom > lr.bottom) list.scrollTop += r.bottom - lr.bottom + 8;
}
SHEETS.doc = {what: 'This file', url: id => '/api/doc/' + encodeURIComponent(id), draw: (env, id) => { const p = env.parts.doc || Object.values(env.parts)[0]; return p && p.ok ? [p.data.title, `<div class="reader" style="display:block;padding:0;max-height:none">${docHTML(p.data)}</div>`, UI.page === 'library' ? '' : `<button class="btn" data-lib-open="${esc(id)}">${icon('book-open', 'sm')}Open in Library</button>`] : ['File ' + id, failedPart(p ? p.error : 'The answer was empty.'), '']; }};
function openDoc(id) {
  if (UI.page === 'library' && !sheetOpen() && !isPhone()) { selectDoc(id); return; }
  openKeyed('doc', id);
}
function openInLibrary(id) {
  closeSheet();
  UI.lib = {...libFresh(), sel: id};
  go('library', {back: true});
}
function libFilter(change) { Object.assign(UI.lib, change, {pages: 1}); const top = $('#main').scrollTop; if (UI.data[PAGE.library.url()]) render({keep: true}); $('#main').scrollTop = top; loadPage(); }
DDPICK['lib-status'] = (_, v) => libFilter({status: v});
DDPICK['lib-inbox'] = (_, v) => libFilter({inbox: v});
function libKind(key, on) { const d = libData(); const set = new Set(d ? d.kinds.filter(k => k.checked).map(k => k.key) : []); if (on) set.add(key); else set.delete(key); libFilter({kinds: set}); }

/* ---------- pane sizes: a per-browser convenience, so they live in localStorage ---------- */
const LIBCOLS = 'ef-lib-cols';
// telemetry: expected — storage a private window refuses leaves the panes at their own sizes
function libCols() { try { return JSON.parse(localStorage.getItem(LIBCOLS) || 'null'); } catch (e) { return null; } }
// telemetry: expected — without storage the sizes last until the page reloads
function saveLibCols(c) { try { c ? localStorage.setItem(LIBCOLS, JSON.stringify(c)) : localStorage.removeItem(LIBCOLS); } catch (e) { } }
function setLibCols(lib, c) {
  if (!lib) return;
  if (c && c.f) lib.style.setProperty('--lib-f', c.f + 'px'); else lib.style.removeProperty('--lib-f');
  if (c && c.r) lib.style.setProperty('--lib-r', `minmax(0, ${c.r}px)`); else lib.style.removeProperty('--lib-r');
}
function paneWidths(lib) {
  const f = lib.querySelector('[data-split="f"]').offsetParent ? lib.querySelector('.filters').getBoundingClientRect().width : (libCols() || {}).f || 220;
  return {f: Math.round(f), r: Math.round(lib.querySelector('.results').getBoundingClientRect().width)};
}
function clampPane(which, w, lib, c) {
  if (which === 'f') return Math.max(160, Math.min(360, w));
  const filters = lib.querySelector('[data-split="f"]').offsetParent ? c.f + 16 : 0;
  return Math.max(260, Math.min(lib.getBoundingClientRect().width - filters - 16 - 320, w));
}
function resizePane(sp, dx) {
  const lib = sp.closest('.lib'), which = sp.dataset.split, c = {...(libCols() || {}), ...paneWidths(lib)};
  c[which] = Math.round(clampPane(which, c[which] + dx, lib, c)); setLibCols(lib, c); saveLibCols(c);
}
document.addEventListener('pointerdown', e => {
  const sp = e.target.closest('[data-split]'); if (!sp || e.button !== 0) return;
  e.preventDefault();
  const lib = sp.closest('.lib'), which = sp.dataset.split, x0 = e.clientX, c = {...(libCols() || {}), ...paneWidths(lib)}, w0 = c[which];
  sp.setPointerCapture(e.pointerId); sp.classList.add('dragging'); document.body.classList.add('resizing');
  const move = ev => { c[which] = Math.round(clampPane(which, w0 + ev.clientX - x0, lib, c)); setLibCols(lib, c); };
  const up = () => { sp.removeEventListener('pointermove', move); sp.classList.remove('dragging'); document.body.classList.remove('resizing'); if (c[which] !== w0) saveLibCols(c); };
  sp.addEventListener('pointermove', move); sp.addEventListener('pointerup', up, {once: true}); sp.addEventListener('pointercancel', up, {once: true});
});
document.addEventListener('dblclick', e => { const sp = e.target.closest('[data-split]'); if (!sp) return; saveLibCols(null); setLibCols(sp.closest('.lib'), null); toast('Library panes back to their first sizes', 'arrow-path'); });
