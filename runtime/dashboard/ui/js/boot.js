'use strict';
/* The search bar, every click and key, the one-second ticker, and start-up. */

/* ---------- the search bar: the server finds and ranks; the browser marks the query and moves the selection ---------- */
let gsTimer = null;
function gsInput(q) {
  UI.gs = {q, at: 0, list: [], data: null, error: null};
  clearTimeout(gsTimer);
  if (!q.trim()) { drawGs(); return; }
  gsTimer = setTimeout(() => gsFetch(q), 120);
}
async function gsFetch(q) {
  let env;
  try { env = await fetchEnv('search', '/api/search?q=' + encodeURIComponent(q.trim())); }
  catch (e) {
    if (UI.gs.q !== q) return;
    UI.gs.error = e instanceof ApiDown ? "Can't reach the warden, so search waits until it answers." : e instanceof ApiFailed ? `Search failed: ${e.message}` : 'Search failed.';
    if (e instanceof ApiDown) wardenDown(e.since, e.note); else if (e instanceof NeedsDevice) askDevice();
    drawGs(); return;
  }
  if (!env || UI.gs.q !== q) return;
  const p = env.parts.search;
  UI.gs.data = p.ok ? p.data : null; UI.gs.error = p.ok ? null : `Search failed: ${p.error}`; drawGs();
}
const gsMark = (s, q) => { const e = esc(s), n = q.trim(); if (!n) return e; return e.replace(new RegExp('(' + esc(n).replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'ig'), '<mark>$1</mark>'); };
const gsIcon = i => i.kind === 'ktile' ? ktile(i.key, i.tone) : i.kind === 'ava' ? `<span class="ava ${esc(i.key)}" style="width:30px;height:30px;font-size:10px">${esc(i.text || '')}</span>` : `<span class="ico">${icon(i.key, 'sm')}</span>`;
function drawGs() {
  const pop = $('#gs-pop'), q = UI.gs.q;
  if (!q.trim()) { pop.hidden = true; $('#gq').setAttribute('aria-expanded', 'false'); return; }
  if (!UI.gs.data && !UI.gs.error) return;
  if (UI.gs.error) { pop.innerHTML = `<div class="gs-empty">${esc(UI.gs.error)}</div>`; pop.hidden = false; return; }
  const {groups, more} = UI.gs.data;
  UI.gs.list = groups.flatMap(g => g.items);
  if (more) UI.gs.list.push({kind: 'all', title: more, go: true});
  UI.gs.at = Math.min(UI.gs.at, Math.max(0, UI.gs.list.length - 1));
  let i = 0;
  const row = x => { const k = i++; return `<button class="gs-item ${x.kind === 'all' ? 'gs-all' : ''}" role="option" id="gs-${k}" data-gs="${k}" aria-selected="${k === UI.gs.at}">${x.kind === 'all' ? `<span class="ico">${icon('book-open', 'sm')}</span>` : gsIcon(x.icon)}<span style="min-width:0"><div class="t">${x.kind === 'all' ? esc(x.title) : gsMark(x.title, q)}</div>${x.sub ? `<div class="s">${gsMark(x.sub, q)}</div>` : ''}</span><span class="faint">${x.go ? icon('chevron-right', 'sm') : ''}</span></button>`; };
  pop.innerHTML = groups.length ? groups.map(g => `<div class="gs-group">${esc(g.label)}</div>${g.items.map(row).join('')}`).join('') + (more ? row(UI.gs.list.at(-1)) : '') : `<div class="gs-empty">Nothing matches “${esc(q.trim())}”.</div>`;
  pop.hidden = false; $('#gq').setAttribute('aria-expanded', 'true'); $('#gq').setAttribute('aria-activedescendant', 'gs-' + UI.gs.at);
}
function gsKey(e) {
  const L = UI.gs.list || [];
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); if (!L.length) return true; UI.gs.at = (UI.gs.at + (e.key === 'ArrowDown' ? 1 : -1) + L.length) % L.length; drawGs(); const el = $('#gs-' + UI.gs.at); if (el) el.scrollIntoView({block: 'nearest'}); return true; }
  if (e.key === 'Enter') { e.preventDefault(); if (L.length) pickGs(UI.gs.at); return true; }
  if (e.key === 'Escape') { e.preventDefault(); closeGs(); return true; }
  return false;
}
function pickGs(i) {
  const x = (UI.gs.list || [])[i]; if (!x) return;
  const q = UI.gs.q; closeGs();
  if (x.kind === 'doc') { if (x.icon && x.icon.key === 'task') openTask(x.id); else openDoc(x.id); }
  else if (x.kind === 'agent') openAgent(x.id);
  else if (x.kind === 'hist') openHist(x.id);
  else if (x.kind === 'page') go(x.id);
  else if (x.kind === 'setting') { UI.flashRow = x.id; go('settings', {back: true}); }
  else if (x.kind === 'all') { UI.lib = libFresh(q.trim()); go('library', {back: true}); }
}
function openGs() { $('.top').classList.add('searching'); const q = $('#gq'); q.focus(); q.select(); if (q.value.trim()) drawGs(); }
function closeGs() { const q = $('#gq'); q.value = ''; UI.gs = {q: '', at: 0}; clearTimeout(gsTimer); $('#gs-pop').hidden = true; q.setAttribute('aria-expanded', 'false'); q.blur(); $('.top').classList.remove('searching'); }
document.addEventListener('focusin', e => { if (e.target.id === 'gq' && e.target.value.trim()) drawGs(); });

/* A changed filter draws from what is already known, if anything, then asks the server. */
function viewChanged() { if (UI.data[PAGE[UI.page].url()]) render({keep: true}); loadPage(); }
const MORE = {requests: 'reqPages', done: 'donePages', slice: 'slicePages', reflect: 'reflectPages'};
function moreRows(what, btn) { btn.disabled = true; if (what === 'library') UI.lib.pages++; else UI[MORE[what]]++; loadPage(); }
const agentsTree = () => { const env = UI.data[PAGE.agents.url()]; return env ? partData(env, 'tree') : null; };

/* ---------- events ---------- */
document.addEventListener('click', e => {
  if (!e.target.closest('.dd, .hint, #prefs')) closePops(!!e.target.closest('#gsearch, #gs-open'));
  const tb = e.target.closest('.toast [data-t]'); if (tb) { toastAction(tb); return; }
  const ghost = e.target.closest('.sheet-ghost'); if (ghost) { popTo(+ghost.dataset.level); return; }
  const t = e.target.closest('button,[data-go],[data-task],[data-agent],[data-hist],[data-open-doc],[data-libsel],a');
  if (!t) { flushDirty(); return; }
  const d = t.dataset, close = !!d.close;
  if (t.tagName === 'A' && t.href) return;
  if (d.write !== undefined && UI.down) { e.stopPropagation(); writesOff(); return; }
  if (d.hint !== undefined) { const pop = t.nextElementSibling, open = pop.hidden; closePops(); if (open) { pop.hidden = false; t.setAttribute('aria-expanded', 'true'); placePop(pop); } return; }
  if (d.ddbtn !== undefined) { const menu = t.nextElementSibling, open = menu.hidden; closePops(); if (open) { menu.hidden = false; t.setAttribute('aria-expanded', 'true'); placePop(menu); (menu.querySelector('[aria-selected="true"]') || menu.querySelector('button')).focus(); } return; }
  if (d.ddopt !== undefined) { closePops(); DDPICK[d.ddkind](d.ddkey, d.v); return; }
  if (d.action === 'prefs') { const m = $('#prefs-menu'), open = m.hidden; closePops(); m.hidden = !open; t.setAttribute('aria-expanded', String(open)); return; }
  if (d.themeSet) { if (close) closeSheet(); setTheme(d.themeSet); if (sheetOpen() && $('#sheet-title').textContent === 'More') openMore(); return; }
  if (d.origin) { const box = document.getElementById('origin-' + d.origin), open = box.hidden; box.hidden = !open; t.setAttribute('aria-expanded', String(open)); if (open && !box.innerHTML) box.innerHTML = builtFromHTML(UI.origins[d.origin], d.origin); return; }
  if (d.rp !== undefined) { replayClick(t); return; }
  if (d.replay) { openReplay(d.replay); return; }
  if (d.brief) { openBrief(d.brief); return; }
  if (d.histPage) { UI.agentsUrl = d.histPage; showKnown(); return; }
  if (d.go) { const back = !!t.closest('#page, #sheet') && !t.closest('.more-grid'); if (d.tab) { UI.workTab = d.tab; UI.reqPages = UI.donePages = 1; } closeSheet(); go(d.go, {back}); return; }
  if (d.libOpen) { openInLibrary(d.libOpen); return; }
  if (d.gs !== undefined) { pickGs(+d.gs); return; }
  if (d.more) { moreRows(d.more, t); return; }
  if (d.needAct) { needAct(d.id, d.needAct, t, close); return; }
  if (d.sendMsg) { e.stopPropagation(); sendMessage(d.sendMsg, t); return; }
  if (d.propAct) { e.stopPropagation(); propAct(d.id, d.propAct, t, close); return; }
  if (d.propMove) { e.stopPropagation(); propMove(d.id, +d.propMove, t); return; }
  if (d.apprMove) { e.stopPropagation(); apprMove(d.id, +d.apprMove, t); return; }
  if (d.drop) { e.stopPropagation(); propAct(d.drop, 'drop', t, close); return; }
  if (d.pic) { openPic(d.pic); return; }
  if (d.boost) { e.stopPropagation(); boostTask(d.boost, true, t, close); return; }
  if (d.unboost) { e.stopPropagation(); boostTask(d.unboost, false, t, close); return; }
  if (d.boostreq) { e.stopPropagation(); act(`/api/requests/${encodeURIComponent(d.boostreq)}/boost`, {}, {btn: t, icon: 'arrow-up-circle'}); return; }
  if (d.unpark) { e.stopPropagation(); act(`/api/parked/${encodeURIComponent(d.unpark)}/unpark`, {}, {btn: t}); return; }
  if (d.toggle) { e.stopPropagation(); UI.agentsClosed.has(d.toggle) ? UI.agentsClosed.delete(d.toggle) : UI.agentsClosed.add(d.toggle); syncTree(); return; }
  if (d.tree) { UI.agentsClosed = d.tree === 'close' ? new Set(treeIds(agentsTree())) : new Set(); syncTree(); return; }
  if (d.stop) { e.stopPropagation(); openStop(d.stop); return; }
  if (d.stopnow) { stopNow(d.stopnow, t); return; }
  if (d.task) { if (t.matches('.prop') && e.target.closest('.dd, .prop-a')) return; openTask(d.task); return; }
  if (d.agent) { openAgent(d.agent); return; }
  if (d.hist) { openHist(d.hist); return; }
  if (d.openDoc) { e.stopPropagation(); openDoc(d.openDoc); return; }
  if (d.libsel) { if (isPhone()) { UI.lib.sel = d.libsel; openDoc(d.libsel); return; } selectDoc(d.libsel); return; }
  if (d.worktab) { UI.workTab = d.worktab; UI.reqPages = UI.donePages = 1; viewChanged(); return; }
  if (d.doneView) { UI.doneView = d.doneView; UI.donePages = 1; viewChanged(); return; }
  if (d.slotOpen) { openSlot(t); return; }
  if (d.inbox) { UI.reqInbox = d.inbox; UI.reqPages = 1; viewChanged(); return; }
  if (d.out) { UI.reqOut = d.out; UI.reqPages = 1; viewChanged(); return; }
  if (d.range) { UI.spendRange = +d.range; UI.slicePages = 1; viewChanged(); return; }
  if (d.slice) { UI.sliceKey = d.slice; UI.slicePages = 1; viewChanged(); return; }
  if (d.librange) { libFilter({range: d.librange}); return; }
  if (d.rkind) { UI.reflectKind = UI.reflectKind === d.rkind ? 'all' : d.rkind; UI.reflectPages = 1; viewChanged(); return; }
  if (d.step) { stepSetting(d.key, +d.step); return; }
  if (d.resetkey) { resetSetting(d.resetkey); return; }
  if (d.default) { defaultSetting(d.default); return; }
  if (d.listdel) { listDel(d.listdel, +d.i); return; }
  if (d.rule) { ruleToggle(d.rule, d.on === 'true'); return; }
  if (d.acctUse) { useAccount(d.acctUse, t); return; }
  if (d.acctRemove) { removeAccount(d.acctRemove); return; }
  if (d.acctRemoveNow) { act(d.acctRemoveNow, {}, {btn: t, icon: 'x-mark', onOk: closeSheet}); return; }
  if (d.acctAdd !== undefined) { addAccount(t); return; }
  const a = d.action;
  if (a === 'close-sheet') closeSheet();
  else if (a === 'sheet-back') sheetBack();
  else if (a === 'gs-open') openGs();
  else if (a === 'gs-close') closeGs();
  else if (a === 'back') { if (UI.hist) history.back(); else restoreView(UI.back); }
  else if (a === 'promote') openPromote();
  else if (a === 'yolo-on') yoloOn();
  else if (a === 'yolo-off') yoloOff(t);
  else if (a === 'promote-back') openPushBack(d.sha);
  else if (a === 'outage-stop') act('/api/outage/stop', {}, {btn: t, icon: 'stop-circle'});
  else if (a === 'start-orch') act('/api/orchestrator/start', {}, {btn: t});
  else if (a === 'pause') pauseFactory();
  else if (a === 'resume') resumeFactory();
  else if (a === 'review') reviewSettings();
  else if (a === 'discard') { UI.edits.clear(); render({keep: true}); toast('Changes discarded', 'x-mark'); }
  else if (a === 'change-repo') changeRepo();
  else if (a === 'lib-clear-q') libFilter({q: ''});
  else if (a === 'lib-reset') { const sel = UI.lib.sel; UI.lib = {...libFresh(), sel}; viewChanged(); }
  else if (a === 'help') { closePops(); openHelp(); }
  else flushDirty();
});
document.addEventListener('input', e => {
  const t = e.target;
  if (t.id === 'gq') gsInput(t.value);
  else if (t.dataset.rpSeek !== undefined) replaySeek(t.value);
  else if (t.dataset.answer) UI.answers[t.dataset.answer] = t.value;
  else if (t.dataset.msg) UI.drafts[t.dataset.msg] = t.value;
  else if (t.dataset.acct) UI.acct[t.dataset.acct] = t.value;
});
document.addEventListener('change', e => {
  const t = e.target, d = t.dataset;
  if (d.kind) libKind(d.kind, t.checked);
  else if (t.id === 'lib-from') libFilter({from: t.value});
  else if (t.id === 'lib-to') libFilter({to: t.value});
  else if (d.setn) setVal(d.setn, t.value);
  else if (d.sett) setVal(d.sett, t.value);
});
document.addEventListener('focusout', () => setTimeout(flushDirty, 0));
document.addEventListener('keydown', e => {
  const ae = document.activeElement;
  if (ae.id === 'gq') { if (gsKey(e)) return; }
  if (e.key === 'Escape') { if (UI.device || replayEscape()) return; if (anyPop()) closePops(); else if (sheetStack.length) sheetBack(); else closeSheet(); return; }
  if (ae.dataset.split && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) { e.preventDefault(); resizePane(ae, (e.key === 'ArrowLeft' ? -1 : 1) * (e.shiftKey ? 64 : 16)); return; }
  const menu = ae && ae.closest && ae.closest('.dd-menu');
  if (menu && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) { e.preventDefault(); const opts = $$('button', menu), i = opts.indexOf(ae); opts[(i + (e.key === 'ArrowDown' ? 1 : -1) + opts.length) % opts.length].focus(); return; }
  if (e.key === 'Enter' && ae.dataset.listadd) { e.preventDefault(); listAdd(ae.dataset.listadd, ae); return; }
  if (e.key === 'Enter' && ae.dataset.answer) { e.preventDefault(); needAct(ae.dataset.answer, 'answer', null, false); return; }
  if (e.key === 'Enter' && ae.dataset.acct) { e.preventDefault(); addAccount($('[data-acct-add]')); return; }
  if (/INPUT|SELECT|TEXTAREA/.test(ae.tagName)) return;
  if (e.key === 'Enter' && ae.dataset.libsel) { ae.click(); return; }
  if (UI.device) return;
  if (e.key === '/') { e.preventDefault(); openGs(); return; }
  if (UI.gPending) { UI.gPending = false; const p = UI.pages.find(x => x[3] === e.key.toLowerCase()); if (p) go(p[0]); return; }
  if (e.key === 'g') { UI.gPending = true; setTimeout(() => UI.gPending = false, 1200); }
});
window.addEventListener('resize', () => closePops());
$('#scrim').addEventListener('click', closeSheet);

/* ---------- the one-second ticker: the live indicator and the triage countdown ---------- */
setInterval(() => {
  drawLive();
  const c = $('#triage-cd'); if (c && c.dataset.at) c.textContent = countdown(c.dataset.at);
}, 1000);

/* ---------- start ---------- */
async function start() {
  UI.device = false;
  renderShell();
  await loadShell();
  if (UI.device || UI.halted) return;
  const want = (location.hash || '').slice(1);
  UI.page = PAGE[want] && (!UI.pages.length || UI.pages.some(p => p[0] === want)) ? want : (UI.pages[0] ? UI.pages[0][0] : 'home');
  // telemetry: expected — the history API is optional: without it the page works, only its back steps go
  try { history.replaceState({page: UI.page, back: null}, '', '#' + UI.page); } catch (e) { }
  renderShell(); render();
  await loadPage();
  const ops = openOps();
  ops.forEach((o, i) => runOp(o.id, i < ops.length - 1));
  clearTimeout(UI.pollT);
  if (!UI.device && !UI.halted) UI.pollT = setTimeout(poll, (UI.shell ? UI.shell.live.refresh_seconds : TIMEOUT_MS / 1000) * 1000);
}
start();
