'use strict';
/* The shell every page shares: the API client, toasts, the live indicator, the router and small drawing helpers.
   The browser draws what the server sends. It formats numbers, lays things out and animates; it computes no state. */

const UI_API = 1;
/* How long a request waits: what the server last said (its own wait for the warden, and a margin). The first page
   load waits this long before the server has said anything. */
let TIMEOUT_MS = 60000;
class TimedOut extends Error { constructor() { super('No answer came in time.'); } }
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const icon = (n, cls = '') => `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true">${(ICONS[n] || []).map(d => `<path stroke-linecap="round" stroke-linejoin="round" d="${d}"/>`).join('')}</svg>`;
const sicon = n => `<svg viewBox="0 0 24 24" aria-hidden="true">${(SOLID[n] || []).map(([d, eo]) => `<path ${eo ? 'fill-rule="evenodd" clip-rule="evenodd" ' : ''}d="${d}"/>`).join('')}</svg>`;
const money = v => v == null ? 'unknown' : '$' + v.toFixed(2);
const money0 = v => v == null ? 'unknown' : '$' + Math.round(v).toLocaleString('en-US');
const hhmm = d => d.toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'});
const sleep = ms => new Promise(r => setTimeout(r, ms));
const isPhone = () => window.matchMedia('(max-width:760px)').matches;

/* Tones are the server's words for colour; these are only how each one is painted. */
const TONE_VAR = {good: '--good', warn: '--warn', bad: '--bad', accent: '--accent', info: '--f-impl', violet: '--f-design', pink: '--f-qa', amber: '--f-intake', muted: '--faint', ring: '--idle', '': '--idle'};
const toneColor = t => `var(${TONE_VAR[t || ''] || '--idle'})`;
const cv = c => c.startsWith('--') ? `var(${c})` : c;
const pill = (text, tone = '', extra = '') => `<span class="chip ${tone} ${extra}">${esc(text)}</span>`;
const chip = c => pill(c.text, c.tone || '', c.mono ? 'mono' : '');
const ava = (family, initials, size) => `<span class="ava ${family}"${size ? ` style="width:${size}px;height:${size}px;font-size:10px"` : ''}>${esc(initials || '')}</span>`;
const ktile = (ic, tone) => { const c = toneColor(tone); return `<span class="ktile" style="color:${c};background:color-mix(in srgb,${c} 15%,transparent)">${sicon(ic)}</span>`; };
/* A picture is an image the server serves under its own strict policy; a picture that fails to load says so in place. */
const picture = (url, alt) => url ? `<img src="${esc(url)}" alt="${esc(alt || '')}" loading="lazy">` : `<span class="pic-gone">No picture</span>`;
document.addEventListener('error', e => { const t = e.target; if (t.tagName === 'IMG' && !t.dataset.gone) { t.dataset.gone = '1'; t.outerHTML = '<span class="pic-gone">The picture could not be shown.</span>'; } }, true);

/* Server HTML (documents, the outage line, search marks) is narrowed to the tags a reader needs. */
const SAFE = {B: [], I: [], EM: [], STRONG: [], MARK: [], CODE: [], PRE: [], P: [], BR: [], H1: [], H2: [], H3: [], H4: [], UL: [], OL: [], LI: ['class'], BLOCKQUOTE: [], HR: [], TABLE: [], THEAD: [], TBODY: [], TR: [], TH: ['style'], TD: ['style'], DIV: ['class'], SPAN: ['class', 'data-open-doc', 'tabindex'], A: ['href'], INPUT: ['type', 'checked', 'disabled']};
const DROP = new Set(['SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'EMBED', 'TEMPLATE', 'SVG', 'MATH', 'IMG', 'LINK', 'META', 'FORM']);
function safeHtml(html, allow = SAFE) {
  const t = document.createElement('template'); t.innerHTML = html;
  const walk = node => [...node.childNodes].forEach(n => {
    if (n.nodeType === 3) return;
    if (n.nodeType !== 1 || DROP.has(n.tagName.toUpperCase())) { n.remove(); return; }
    walk(n);
    const keep = allow[n.tagName];
    if (!keep) { n.replaceWith(...n.childNodes); return; }
    [...n.attributes].forEach(a => { if (!keep.includes(a.name)) n.removeAttribute(a.name); });
    if (n.hasAttribute('style') && !/^\s*text-align:\s*(left|right|center);?\s*$/.test(n.getAttribute('style'))) n.removeAttribute('style');
    if (n.tagName === 'A') { if (!/^https:\/\//.test(n.getAttribute('href') || '')) n.removeAttribute('href'); n.setAttribute('target', '_blank'); n.setAttribute('rel', 'noopener'); }
    if (n.tagName === 'INPUT') { if (n.getAttribute('type') !== 'checkbox') n.remove(); else n.setAttribute('disabled', ''); }
  });
  walk(t.content);
  const d = document.createElement('div'); d.append(t.content); return d.innerHTML;
}
const boldOnly = html => safeHtml(html, {B: []});

/* ---------- the API client ---------- */
class ApiDown extends Error { constructor(since) { super('The warden did not answer.'); this.since = since || null; } }
class NeedsDevice extends Error {}
class Halted extends Error {}
class ApiFailed extends Error {}

async function request(method, url, body) {
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  const headers = {Accept: 'application/json'};
  if (method === 'POST') Object.assign(headers, {'Content-Type': 'application/json', 'X-Software': '1'});
  let res;
  try { res = await fetch(url, {method, headers, credentials: 'same-origin', cache: 'no-store', signal: ctl.signal, body: body ? JSON.stringify(body) : undefined}); }
  catch (e) { if (ctl.signal.aborted) throw new TimedOut(); throw new ApiDown(null); }
  finally { clearTimeout(timer); }
  if (res.status === 401) throw new NeedsDevice('This device is not signed in.');
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if ([502, 503, 504].includes(res.status)) throw new ApiDown(data && data.warden ? data.warden.since : null);
  if (data && data.ok === false && (data.refused || data.reason)) return data;
  if (!res.ok) throw new ApiFailed((data && (data.reason || data.error)) || `The server answered ${res.status}.`);
  if (data == null) throw new ApiFailed('The answer could not be read.');
  return data;
}
function envOk(env) {
  if (!env || typeof env !== 'object' || !env.parts) throw new ApiFailed('The answer could not be read.');
  if (env.api !== UI_API) { factoryUpdated(); throw new Halted(); }
  if (typeof env.timeout_ms === 'number') TIMEOUT_MS = env.timeout_ms;
  if (env.warden && env.warden.ok === false) wardenDown(env.warden.since);
  else { wardenUp(); UI.lastOk = Date.now(); }
  return env;
}
/* Each answer is numbered per key, so an older answer never overwrites a newer one. */
let SEQ = 0;
const LATEST = {};
async function fetchEnv(key, url) {
  const n = ++SEQ; LATEST[key] = n;
  const env = envOk(await request('GET', url));
  return LATEST[key] === n ? env : null;
}
function newKey() { const a = new Uint8Array(16); crypto.getRandomValues(a); return [...a].map(b => b.toString(16).padStart(2, '0')).join(''); }
const post = (url, body, key) => request('POST', url, {...body, idempotency_key: key || newKey()});

function loadFailed(e, what, retry) {
  if (e instanceof Halted) return;
  if (e instanceof NeedsDevice) { askDevice(); return; }
  if (e instanceof ApiDown) { wardenDown(e.since); return; }
  if (e instanceof ApiFailed) { alertToast('load:' + what, `${what} could not be loaded: ${e.message}`, {retry}); return; }
  alertToast('bug:' + what, `${what} could not be drawn.`, {retry});
  throw e;
}

/* Every action: nothing in the browser changes until the server answers, then the named parts reload. */
async function act(url, body = {}, opts = {}) {
  if (UI.down) { writesOff(); return null; }
  const key = opts.key || newKey(), btn = opts.btn, was = btn ? btn.disabled : false;
  if (btn) { btn.disabled = true; btn.setAttribute('aria-busy', 'true'); }
  let ans;
  try { ans = await post(url, body, key); }
  catch (e) {
    if (e instanceof NeedsDevice) askDevice();
    else if (e instanceof TimedOut) alertToast('write:' + url, 'No answer came in time, so it may have gone through. Retrying is safe: it is never done twice.', {retry: () => act(url, body, {...opts, key})});
    else if (e instanceof ApiDown) { wardenDown(e.since); alertToast('write:' + url, 'That did not go through: the warden did not answer. Nothing changed.', {retry: () => act(url, body, {...opts, key})}); }
    else alertToast('write:' + url, `That did not go through: ${e.message}`, {retry: () => act(url, body, {...opts, key})});
    return null;
  } finally { if (btn && btn.isConnected) { btn.disabled = was; btn.removeAttribute('aria-busy'); } }
  if (ans.ok) {
    if (ans.message) toast(ans.message, opts.icon);
    if (opts.onOk) opts.onOk(ans);
    if (ans.op) runOp(ans.op, opts.opTitle || 'Working');
    refreshParts(ans.refresh || []);
  } else refusedToast(ans);
  return ans;
}
function refusedToast(ans) {
  alertToast('refused:' + (ans.refused || ans.reason), ans.reason || 'The factory refused that.');
  refreshParts(['all']);
}
function refreshParts(names) {
  if (names.includes('shell') || names.includes('all')) loadShell();
  const P = PAGE[UI.page], env = P && UI.data[P.url()];
  if (names.includes('all') || names.includes(UI.page) || (env && names.some(n => n in env.parts))) loadPage({flip: true});
  refreshSheet(names);
}

/* ---------- state ---------- */
const UI = {page: null, shell: null, pages: [], data: {}, back: null, hist: false, lastOk: 0, down: null, fails: 0, halted: false, device: false,
  workTab: 'tasks', reqInbox: 'all', reqOut: 'all', reqPages: 1, histPages: 1, sliceKey: 'family', spendRange: 14, slicePages: 1, reflectKind: 'all', reflectPages: 1,
  agentsClosed: new Set(), gPending: false, gs: {q: '', at: 0}, edits: new Map(), failed: new Set(), dirty: false, pendingScroll: null, sigs: {}};
const PAGE = {};

/* ---------- toasts ---------- */
function toast(msg, ic = 'check') {
  const el = document.createElement('div'); el.className = 'toast'; el.innerHTML = icon(ic, 'sm') + esc(msg); $('#toasts').append(el);
  setTimeout(() => { el.style.transition = 'opacity .3s'; el.style.opacity = '0'; setTimeout(() => el.remove(), 320); }, 2600);
}
/* An error toast stays until closed or timed out; the same failure again counts up instead of stacking. */
function alertToast(key, msg, opts = {}) {
  let el = $$('#toasts .toast.alert').find(t => t.dataset.key === key);
  if (el) {
    const n = +el.dataset.n + 1; el.dataset.n = n;
    el.querySelector('.t-msg').innerHTML = esc(msg) + `<span class="t-n">×${n}</span>`;
    el.retry = opts.retry || el.retry;
  } else {
    el = document.createElement('div'); el.className = 'toast alert'; el.setAttribute('role', 'alert'); el.dataset.key = key; el.dataset.n = 1;
    el.innerHTML = `${icon('exclamation-triangle', 'sm')}<span class="t-msg">${esc(msg)}</span><span class="t-acts">${opts.retry ? '<button class="t-btn" data-t="retry">Retry</button>' : ''}${opts.reload ? '<button class="t-btn" data-t="reload">Reload</button>' : ''}<button class="t-btn x" data-t="close" aria-label="Close">${icon('x-mark', 'sm')}</button></span>`;
    el.retry = opts.retry; $('#toasts').append(el);
  }
  clearTimeout(el.timer);
  if (!opts.persistent) el.timer = setTimeout(() => dismissToast(key), 12000);
  return el;
}
function dismissToast(key) { $$('#toasts .toast.alert').filter(t => t.dataset.key === key).forEach(t => { t.removeAttribute('data-key'); clearTimeout(t.timer); t.style.transition = 'opacity .3s'; t.style.opacity = '0'; setTimeout(() => t.remove(), 320); }); }
function toastAction(btn) {
  const el = btn.closest('.toast'), what = btn.dataset.t;
  if (what === 'reload') { location.reload(); return; }
  dismissToast(el.dataset.key);
  if (what === 'retry' && el.retry) el.retry();
}

/* ---------- the warden's reachability, the live indicator, old data ---------- */
function wardenDown(since) {
  if (!UI.down) {
    UI.down = since || hhmm(new Date()); UI.fails = 0;
    alertToast('warden', `Can't reach the warden since ${UI.down}. The page shows what it last knew; actions wait until it answers.`, {persistent: true});
  }
  UI.fails++; drawLive(); markWrites();
}
function wardenUp() {
  if (!UI.down) return;
  UI.down = null; UI.fails = 0; dismissToast('warden'); dismissToast('writes-off'); toast('The warden answers again.'); drawLive(); markWrites();
}
function factoryUpdated() {
  if (UI.halted) return;
  UI.halted = true; clearTimeout(UI.pollT);
  alertToast('updated', 'The factory was updated. Reload to see it.', {persistent: true, reload: true});
}
const staleMs = () => UI.shell ? UI.shell.live.stale_seconds * 1000 : Infinity;
const isOld = () => !!UI.down || (UI.lastOk > 0 && Date.now() - UI.lastOk > staleMs());
function drawLive() {
  const live = $('#live'), txt = $('#live-text');
  if (UI.down) { live.className = 'live down'; txt.textContent = `Can't reach the warden since ${UI.down}`; }
  else if (!UI.lastOk) { live.className = 'live stale'; txt.textContent = 'Connecting'; }
  else { const s = Math.round((Date.now() - UI.lastOk) / 1000); txt.textContent = s < 3 ? 'Live' : 'Updated ' + s + ' s ago'; live.className = 'live' + (Date.now() - UI.lastOk > staleMs() ? ' stale' : ''); }
  $('#page').classList.toggle('old', isOld());
}
/* While the warden is unreachable every write is off, with the reason on hand. */
function markWrites() {
  $$('[data-write]').forEach(b => {
    b.classList.toggle('writes-off', !!UI.down);
    if (UI.down) { if (!b.hasAttribute('data-title0')) b.setAttribute('data-title0', b.title); b.title = `Can't reach the warden since ${UI.down}`; }
    else if (b.hasAttribute('data-title0')) { b.title = b.getAttribute('data-title0'); b.removeAttribute('data-title0'); }
  });
}
function writesOff() { alertToast('writes-off', `Can't reach the warden since ${UI.down}, so nothing can change now. Try again when it answers.`); }

/* ---------- small drawing helpers ---------- */
function hint(text, right) { return `<span class="hint"><button class="hint-btn" data-hint aria-expanded="false" aria-label="What does this mean?"><span>?</span></button><span class="hint-pop${right ? ' right' : ''}" role="tooltip" hidden>${esc(text)}</span></span>`; }
function dd(kind, key, value, opts, label, width, write) {
  const o = opts.map(x => Array.isArray(x) ? x : [x, x]), cur = (o.find(x => x[0] === value) || [value, value])[1];
  return `<div class="dd" ${width ? `style="width:${width}px"` : 'style="width:100%"'}><button class="dd-btn" data-ddbtn ${write ? 'data-write' : ''} aria-haspopup="listbox" aria-expanded="false" aria-label="${esc(label)}"><span>${esc(cur)}</span>${icon('chevron-down', 'sm')}</button><div class="dd-menu" role="listbox" hidden>${o.map(([v, l]) => `<button role="option" data-ddopt data-ddkind="${kind}" data-ddkey="${esc(key)}" data-v="${esc(v)}" aria-selected="${v === value}">${icon('check', 'sm')}<span>${esc(l)}</span></button>`).join('')}</div></div>`;
}
const DDPICK = {};
function closePops(keepSearch) {
  $$('.dd-menu:not([hidden])').forEach(m => { m.hidden = true; m.previousElementSibling.setAttribute('aria-expanded', 'false'); });
  $$('.hint-pop:not([hidden])').forEach(pp => { pp.hidden = true; pp.previousElementSibling.setAttribute('aria-expanded', 'false'); });
  const pm = $('#prefs-menu'); if (pm && !pm.hidden) { pm.hidden = true; $('#prefs-btn').setAttribute('aria-expanded', 'false'); }
  const gp = $('#gs-pop'); if (gp && !gp.hidden && !keepSearch) { gp.hidden = true; $('#gq').setAttribute('aria-expanded', 'false'); }
}
const anyPop = () => !!$('.dd-menu:not([hidden]), .hint-pop:not([hidden]), #prefs-menu:not([hidden]), #gs-pop:not([hidden])');
function placePop(pop) {
  pop.style.left = ''; pop.style.transform = ''; pop.classList.remove('up');
  if (!pop.classList.contains('hint-pop') || !pop.dataset.fixedRight) pop.classList.remove('right');
  const clip = pop.closest('#main, #sheet-body'), box = clip ? clip.getBoundingClientRect() : {left: 0, right: window.innerWidth};
  const lo = Math.max(box.left, 0) + 8, hi = Math.min(box.right, window.innerWidth) - 8;
  let r = pop.getBoundingClientRect();
  if (r.right > hi) { pop.classList.add('right'); r = pop.getBoundingClientRect(); }
  if (r.left < lo) { pop.classList.remove('right'); pop.style.left = '0'; pop.style.transform = 'none'; r = pop.getBoundingClientRect(); if (r.left < lo) pop.style.left = (lo - r.left) + 'px'; }
  if (pop.classList.contains('dd-menu') && r.bottom > window.innerHeight - 8 && r.top > r.height + 80) pop.classList.add('up');
}
function ph(title, help, sub) { return `<div><h1>${title}${help ? hint(help) : ''}</h1>${sub ? `<div class="sub">${esc(sub)}</div>` : ''}</div>`; }
function ring(p, size = 54, label = '') { const r = 22, c = 2 * Math.PI * r; return `<svg class="ring" viewBox="0 0 54 54" style="width:${size}px;height:${size}px" role="img" aria-label="${label}"><circle class="bg" cx="27" cy="27" r="${r}"/><circle class="fg" cx="27" cy="27" r="${r}" stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - Math.min(1, p))}" transform="rotate(-90 27 27)"/></svg>`; }
function spark(vals, color = 'var(--accent)') { if (vals.length < 2) return ''; const w = 220, h = 40, mx = Math.max(...vals) || 1, pts = vals.map((v, i) => [i / (vals.length - 1) * w, h - 4 - v / mx * (h - 10)]); const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' '); const last = pts[pts.length - 1]; return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><path d="${line} L${w} ${h} L0 ${h} Z" fill="${color}" fill-opacity=".12"/><path d="${line}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"/><circle cx="${last[0]}" cy="${last[1]}" r="3" fill="${color}"/></svg>`; }
const countdown = iso => { const t = Math.max(0, Math.round((new Date(iso) - Date.now()) / 1000)); return `${Math.floor(t / 3600)}:${String(Math.floor(t % 3600 / 60)).padStart(2, '0')}:${String(t % 60).padStart(2, '0')}`; };
/* A part the server could not build says so in its own place; the rest of the page still draws. */
const failedPart = (err, cls = 'card') => `<div class="${cls} part-failed" role="status">${icon('exclamation-triangle', 'sm')}<span><b>This part could not be drawn.</b> ${esc(err)}</span></div>`;
function part(env, name, draw, cls) {
  const p = env.parts[name];
  if (!p) return failedPart(`The answer had no ${name}.`, cls);
  return p.ok ? draw(p.data) : failedPart(p.error, cls);
}
const brokenRow = (r, extra = '') => `<div class="row broken ${extra}" role="status"><span class="rank">${icon('exclamation-triangle', 'sm')}</span><div style="min-width:0"><div class="tt">This row could not be read.</div><div class="ts"><span class="mono">${esc(r.id)}</span>${esc(r.error)}</div></div></div>`;
const showMore = (what, more) => more ? `<div class="showmore"><button class="btn sm quiet" data-more="${what}">Show more</button></div>` : '';
function notePartFailures(env) {
  Object.entries(env.parts).forEach(([name, p]) => {
    if (p.ok) { UI.failed.delete(name); return; }
    if (!UI.failed.has(name)) { UI.failed.add(name); alertToast('part:' + name, p.error); }
  });
}
function flipFirst() { const f = {}; $$('#page [data-flip]').forEach(el => f[el.dataset.flip] = el.getBoundingClientRect().top); return f; }
function flipPlay(first) { $$('#page [data-flip]').forEach(el => { const f = first[el.dataset.flip]; if (f == null) return; const d = f - el.getBoundingClientRect().top; if (!d) return; el.animate([{transform: `translateY(${d}px)`}, {transform: 'none'}], {duration: 380, easing: 'cubic-bezier(.2,.8,.2,1)'}); }); }

/* ---------- theme ---------- */
function theme() { try { return localStorage.getItem('ef-theme') || 'system'; } catch (e) { return 'system'; } }
function setTheme(t) { try { localStorage.setItem('ef-theme', t); } catch (e) { /* a private window keeps the theme for this page only */ } applyTheme(); renderShell(); }
function applyTheme() { const t = theme(); if (t === 'system') document.documentElement.removeAttribute('data-theme'); else document.documentElement.setAttribute('data-theme', t); }
applyTheme();

/* ---------- shell ---------- */
async function loadShell() {
  try {
    const env = await fetchEnv('shell', '/api/shell'); if (!env) return;
    const p = env.parts.shell;
    if (!p.ok) { alertToast('part:shell', p.error); return; }
    UI.shell = p.data; UI.pages = p.data.pages; renderShell();
  } catch (e) { loadFailed(e, 'The top bar', loadShell); }
}
const pillHTML = (name, p) => `<i class="dot ${p.state === 'healthy' ? 'good' : p.state === 'down' ? 'bad' : ''}"></i><b>${name}</b><span class="sha">${esc(p.sha || 'unknown')}</span>`;
function renderShell() {
  $('#brand-mark').innerHTML = icon('bolt', 'sm');
  $('#gs-open').innerHTML = icon('magnifying-glass'); $('.gs-ico').innerHTML = icon('magnifying-glass', 'sm'); $('.gs-close').innerHTML = icon('x-mark', 'sm');
  $('#top-promote').innerHTML = `${icon('rocket-launch', 'sm')}<span class="hide-sm">Push to prod</span>`;
  $('#sheet-x').innerHTML = icon('x-mark'); $('#sheet-back').innerHTML = icon('chevron-left');
  $('#prefs-btn').innerHTML = `${icon('cog-6-tooth')}Preferences`;
  $('#prefs-menu').innerHTML = `<div class="label">Theme</div><div class="seg" role="group" aria-label="Theme">${['system', 'light', 'dark'].map(t => `<button data-theme-set="${t}" aria-pressed="${theme() === t}">${t[0].toUpperCase() + t.slice(1)}</button>`).join('')}</div><button class="nav" data-action="help">${icon('bolt')}Keyboard shortcuts</button>`;
  const S = UI.shell; if (!S) return;
  const counts = S.nav || {};
  $('#sidenav').innerHTML = UI.pages.map(([id, name, ic]) => `<button class="nav" data-go="${id}" ${UI.page === id ? 'aria-current="page"' : ''}>${icon(ic)}${esc(name)}${counts[id] ? `<span class="count">${counts[id]}</span>` : ''}</button>`).join('');
  const more = UI.pages.slice(4).map(p => p[0]);
  $('#bottomnav').innerHTML = UI.pages.slice(0, 4).map(([id, name, ic]) => `<button data-go="${id}" ${UI.page === id ? 'aria-current="page"' : ''}>${icon(ic)}${esc(name)}</button>`).join('') + `<button data-go="more" ${more.includes(UI.page) ? 'aria-current="page"' : ''}>${icon('adjustments-horizontal')}More</button>`;
  $('#repo').textContent = S.repo;
  $('#pill-staging').innerHTML = pillHTML('Staging', S.pills.staging);
  $('#pill-prod').innerHTML = pillHTML('Prod', S.pills.prod);
  markWrites();
}

/* ---------- router: pages, history, the back link ---------- */
const pageName = id => (UI.pages.find(p => p[0] === id) || [id, id])[1];
const viewLabel = () => UI.page === 'work' && PAGE.work.tabLabel() ? `${pageName('work')} › ${PAGE.work.tabLabel()}` : pageName(UI.page);
const viewOf = () => ({page: UI.page, workTab: UI.workTab, reqInbox: UI.reqInbox, reqOut: UI.reqOut, scroll: $('#main').scrollTop, back: UI.back || null, label: viewLabel()});
function go(page, opts = {}) {
  if (page === 'more') { openMore(); return; }
  if (!PAGE[page]) return;
  closePops();
  const from = viewOf();
  try { history.replaceState(from, '', '#' + UI.page); history.pushState({page, back: opts.back ? from : null}, '', '#' + page); UI.hist = true; } catch (e) { UI.hist = false; }
  UI.back = opts.back ? from : null;
  UI.page = page; renderShell(); render(); $('#main').scrollTop = 0; $('#page').focus({preventScroll: true});
  loadPage();
}
function restoreView(v) {
  closeSheet(); closePops();
  UI.page = v.page; if (v.workTab) Object.assign(UI, {workTab: v.workTab, reqInbox: v.reqInbox, reqOut: v.reqOut});
  UI.back = v.back || null;
  renderShell(); render(); $('#main').scrollTop = v.scroll || 0;
  if (!UI.data[PAGE[UI.page].url()]) UI.pendingScroll = v.scroll || 0;
  loadPage();
}
window.addEventListener('popstate', e => { if (e.state && e.state.page && PAGE[e.state.page]) restoreView(e.state); });

/* A page re-draws only when nothing on it is being used: an open menu or a focused field waits for the next chance. */
const typing = el => el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || (el.tagName === 'INPUT' && !['checkbox', 'radio', 'button'].includes(el.type));
const busyPage = () => anyPop() || ($('#page').contains(document.activeElement) && typing(document.activeElement)) || !!$('.split.dragging');
function render(opts = {}) {
  const P = PAGE[UI.page]; if (!P) return;
  const env = UI.data[P.url()], main = $('#main'), top = main.scrollTop, first = opts.flip ? flipFirst() : null;
  const inner = {}; $$('#page [data-keep-scroll]').forEach(el => inner[el.dataset.keepScroll] = el.scrollTop);
  const was = opts.flip ? Object.fromEntries($$('#page .row[data-flip]').map(el => [el.dataset.flip, el.innerHTML])) : null;
  const back = UI.back ? `<button class="backlink" data-action="back">${icon('chevron-left', 'sm')}Back to ${esc(UI.back.label)}</button>` : '';
  $('#page').innerHTML = back + (env ? P.render(env) : `<div class="empty loading">${UI.down ? "Can't reach the warden. This page shows once it answers." : 'Loading…'}</div>`);
  if (opts.keep) main.scrollTop = top;
  $$('#page [data-keep-scroll]').forEach(el => { if (inner[el.dataset.keepScroll] != null) el.scrollTop = inner[el.dataset.keepScroll]; });
  if (P.after) P.after(env);
  if (first) flipPlay(first);
  /* A row the server changed is washed with the accent for a moment; a new or unchanged row is not. */
  if (was) $$('#page .row[data-flip]').forEach(el => { const w = was[el.dataset.flip]; if (w != null && w !== el.innerHTML) el.classList.add('wash'); });
  if (UI.pendingScroll != null && env) { main.scrollTop = UI.pendingScroll; UI.pendingScroll = null; }
  UI.dirty = false; markWrites(); drawLive();
}
/* An answer that says the same as the last one draws nothing, so a poll never disturbs a page that has not changed. */
const signature = env => JSON.stringify(env.parts, (k, v) => k === 'as_of' ? undefined : v);
function flushDirty() { if (UI.dirty && !busyPage()) render({keep: true}); }
async function loadPage(opts = {}) {
  const id = UI.page, P = PAGE[id]; if (!P) return;
  const url = P.url();
  try {
    const env = P.load ? await P.load(url) : await fetchEnv('page', url);
    if (!env || UI.page !== id || P.url() !== url) return;
    const sig = signature(env), same = UI.data[url] && UI.sigs[url] === sig;
    UI.data[url] = env; UI.sigs[url] = sig; notePartFailures(env);
    if (same && !UI.dirty) { drawLive(); markWrites(); }
    else if (busyPage()) UI.dirty = true;
    else render({keep: true, flip: opts.flip});
    clearTimeout(UI.soonT);
    const soon = P.soon && P.soon(env);
    if (soon) UI.soonT = setTimeout(() => loadPage({flip: true}), soon);
  } catch (e) { loadFailed(e, 'This page', () => loadPage()); }
}
/* Paged lists load every page shown so far, so a refresh never drops rows the owner asked for. */
async function loadPaged(url, pages, partName, rowsKey = 'rows', param = 'page') {
  const sep = url.includes('?') ? '&' : '?';
  const envs = [];
  for (let n = 1; n <= pages; n++) { const env = await fetchEnv('page', `${url}${sep}${param}=${n}`); if (!env) return null; envs.push(env); }
  const base = envs[0], p = base.parts[partName];
  if (!p || !p.ok) return base;
  for (const env of envs.slice(1)) {
    const q = env.parts[partName]; if (!q.ok) { base.parts[partName] = q; return base; }
    p.data[rowsKey] = p.data[rowsKey].concat(q.data[rowsKey]); p.data.more = q.data.more;
  }
  return base;
}

/* ---------- polling: every refresh_seconds while the tab shows, backing off while the warden is away ---------- */
async function poll() {
  clearTimeout(UI.pollT);
  if (document.hidden || UI.halted || UI.device) return;
  await Promise.all([loadShell(), loadPage({flip: true}), refreshSheet()]);
  if (UI.halted || UI.device) return;
  const every = (UI.shell ? UI.shell.live.refresh_seconds : TIMEOUT_MS / 1000) * 1000;
  UI.pollT = setTimeout(poll, UI.down ? every * Math.min(2 ** UI.fails, 8) : every);
}
document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(UI.pollT); else poll(); });
