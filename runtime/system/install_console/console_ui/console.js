'use strict';
/* The install console's page. It draws what the console sends and sends back what the owner does: every sentence comes
   from the console. A run is its phases, a progress bar and the running phase's one line, never a command or its
   output; it is redrawn in place as it moves, so a half-typed answer is never lost. */

const POLL_MS = 800, IDLE_MS = 15000, TIMEOUT_MS = 10000;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const icon = (n, cls = '') => `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true">${(ICONS[n] || []).map(d => `<path stroke-linecap="round" stroke-linejoin="round" d="${d}"/>`).join('')}</svg>`;
const safeUrl = u => /^(https:\/\/|otpauth:\/\/)/.test(u) ? u : '';
const S = {page: null, timer: null, watching: false, run: null, qkey: null, answered: null, ending: null};

/* ---------- the console's answers ---------- */
async function api(method, url, body) {
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, {method, credentials: 'same-origin', cache: 'no-store', signal: ctl.signal,
      headers: method === 'POST' ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
    const data = await res.json().catch(() => null);
    if (!data) throw new Error(`The console answered ${res.status}.`);
    return data;
  } catch (e) {
    throw e.name === 'AbortError' || e instanceof TypeError ? new Error('The console does not answer. Is it still running?') : e;
  } finally { clearTimeout(timer); }
}
function toast(msg, bad = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (bad ? ' alert' : '');
  el.innerHTML = icon(bad ? 'exclamation-triangle' : 'check', 'sm') + `<span class="t-msg">${esc(msg)}</span>`;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), bad ? 6000 : 2600);
}
async function send(url, body) {
  try { const r = await api('POST', url, body); if (!r.ok) toast(r.reason, true); return r; }
  catch (e) { toast(e.message, true); return {ok: false}; }
}

/* ---------- the page ---------- */
function skeleton() {
  $('#brand-mark').innerHTML = icon('bolt', 'sm');
  $('#page').innerHTML = `<div class="page-h"><div><h1 id="title"></h1><div class="sub" id="sub"></div></div></div>
    <section class="section"><div class="sec-h"><div class="label" id="status-label"></div></div><div class="card cstatus" id="status"></div></section>
    <section class="section" id="run-sec" hidden><div class="card crun" id="run"></div></section>
    <section class="section" id="install"></section>
    <section class="section" id="uninstall"></section>`;
}
function renderPage(p) {
  if (!$('#title')) skeleton();
  S.page = p;
  $('#title').textContent = p.title; $('#sub').textContent = p.sub; $('#status-label').textContent = p.status_label;
  const close = $('#close'); close.textContent = p.close; close.hidden = false;
  renderStatus(p.status); renderForm(p.install); renderUninstall(p.uninstall); renderRun(p.run);
  busy(p.busy);
}
function busy(on) { $$('[data-action="install"],[data-action="look"]').forEach(b => { b.disabled = on; }); }

function renderStatus(s) {
  const link = s.address && safeUrl(s.address.url) ? `<a class="btn sm" href="${esc(s.address.url)}" target="_blank" rel="noopener">${icon('arrow-top-right-on-square', 'sm')}${esc(s.address.label)}</a>` : '';
  const facts = s.facts.map(f => `<div><div class="l">${esc(f.label)}</div><div class="v mono">${esc(f.value)}</div>${f.sub ? `<div class="s">${esc(f.sub)}</div>` : ''}</div>`).join('');
  const services = s.services.map(x => `<li><i class="dot ${x.ok ? 'good' : 'bad'}"></i><span>${esc(x.name)}</span><span class="muted">${esc(x.text)}</span></li>`).join('');
  const drills = s.drills ? `<div class="cdrills"><span class="chip ${s.drills.ok ? 'good' : 'bad'}">${esc(s.drills.summary)}</span>${s.drills.failed.map(d => `<div class="cnote"><b>${esc(d.name)}</b>: ${esc(d.evidence)}</div>`).join('')}</div>` : '';
  $('#status').innerHTML = `<div class="cs-top"><i class="dot ${s.tone}"></i><b>${esc(s.headline)}</b>${link}</div>`
    + (s.problem ? `<div class="cnote bad">${esc(s.problem)}</div>` : '')
    + (facts ? `<div class="kv cfacts">${facts}</div>` : '') + (services ? `<ul class="csvc">${services}</ul>` : '') + drills;
}

/* The form is drawn once; later answers change only its words, never what the owner typed or chose. */
function renderForm(f) {
  const el = $('#install');
  if (!el.dataset.drawn) {
    el.dataset.drawn = '1';
    const c = f.commit;
    el.innerHTML = `<div class="sec-h"><div class="label" id="f-heading"></div></div>
      <div class="card cform">
        ${f.installed ? `<div class="cf-row"><div class="cf-l">${esc(f.installed.label)}</div><div class="cf-commit"><b class="mono">${esc(f.installed.short)}</b> ${esc(f.installed.subject)}</div></div>` : ''}
        <div class="cf-row"><div class="cf-l">${esc(c.label)}</div>
          ${c.value ? `<div class="cf-commit"><b class="mono">${esc(c.short)}</b> ${esc(c.subject)}<span class="help">${esc(c.note)}</span></div>` : ''}
          ${c.problem ? `<div class="cnote bad">${esc(c.problem)}</div>` : ''}<div class="cf-ci" id="f-ci" hidden></div></div>
        <div class="cf-row"><label class="cf-l" for="f-repo">${esc(f.repo.label)}</label><input class="cf-in mono" id="f-repo" autocomplete="off" spellcheck="false" value="${esc(f.repo.value)}"><span class="help">${esc(f.repo.hint)}</span></div>
        ${f.kept ? `<div class="cf-row"><div class="cf-l">${esc(f.kept.label)}</div><ul class="cf-kept">${f.kept.items.map(i => `<li>${esc(i)}</li>`).join('')}</ul></div>` : ''}
        <div class="cf-checks">${f.choices.map(k => `<label class="cf-check"><input type="checkbox" id="f-${esc(k.id)}"${k.checked ? ' checked' : ''}><span><b>${esc(k.label)}</b><span class="help">${esc(k.hint)}</span></span></label>`).join('')}</div>
        <div class="cf-foot"><label class="cf-switch"><input type="checkbox" role="switch" id="f-dry"${f.dry.checked ? ' checked' : ''}><span class="sw"></span><span><b>${esc(f.dry.label)}</b><span class="help">${esc(f.dry.hint)}</span></span></label>
          <button class="btn primary" data-action="install" id="f-go"></button></div>
      </div>`;
    $('#f-dry').addEventListener('change', () => labelGo());
  }
  $('#f-heading').textContent = f.heading;
  renderCi(f.commit.ci);
  labelGo();
}
/* The commit's CI changes while the console runs, so it is drawn again with every answer. */
function renderCi(ci) {
  const el = $('#f-ci');
  el.hidden = !ci;
  if (!ci) { el.innerHTML = ''; return; }
  const links = ci.links.filter(l => safeUrl(l.url)).map(l => `<a class="btn sm quiet" href="${esc(l.url)}" target="_blank" rel="noopener">${icon('arrow-top-right-on-square', 'sm')}${esc(l.label)}</a>`).join('');
  const html = `<div class="cf-ci-h"><span class="chip ${esc(ci.tone)}">${esc(ci.label)}</span><span>${esc(ci.text)}</span></div>${links ? `<div class="cq-links">${links}</div>` : ''}`;
  if (el.innerHTML !== html) el.innerHTML = html;
}
function labelGo() { const f = S.page.install; $('#f-go').textContent = $('#f-dry').checked ? f.dry_button : f.button; }

function renderUninstall(u) {
  const el = $('#uninstall');
  if (!el.dataset.drawn) {
    el.dataset.drawn = '1';
    el.innerHTML = `<div class="sec-h"><div class="label">${esc(u.heading)}</div></div><div class="card cun"><p>${esc(u.text)}</p><button class="btn danger" data-action="look">${esc(u.button)}</button></div>`;
  }
}

/* ---------- a run: its phases, the question it waits on, and how it ended ---------- */
const mark = m => m === 'spin' ? '<span class="spin"></span>' : m === 'ring' ? '<span class="dot ring"></span>' : icon(m, 'sm');
function renderRun(run) {
  const sec = $('#run-sec');
  S.run = run;
  if (!run) { sec.hidden = true; $('#run').innerHTML = ''; S.runId = null; return; }
  sec.hidden = false;
  if (S.runId !== run.id) {
    S.runId = run.id; S.qkey = null; S.ending = null;
    $('#run').innerHTML = `<div class="cr-h"><b id="run-title"></b><span class="chip" id="run-chip"></span><span class="chip warn" id="run-badge"></span><button class="btn sm quiet" data-end="log" id="run-log" hidden></button></div>
      <div class="cprog"><div class="cbar" role="progressbar" aria-labelledby="run-title" aria-valuemin="0" aria-valuemax="100"><i id="run-bar"></i></div><div class="cdoing" id="run-doing" aria-live="polite"></div></div>
      <div id="q-top"></div><ol class="cphases" id="phases"></ol><div class="cnotes" id="notes"></div><div id="ending"></div>`;
  }
  $('#run-title').textContent = run.title;
  const chip = $('#run-chip'); chip.className = `chip ${run.chip.tone}`; chip.textContent = run.chip.text;
  const badge = $('#run-badge'); badge.textContent = run.badge; badge.hidden = !run.badge;
  const logBtn = $('#run-log'); logBtn.hidden = !run.log; logBtn.innerHTML = run.log ? icon('document-text', 'sm') + esc(run.log) : '';
  $('#run-bar').style.width = `${run.progress}%`; $('.cbar').setAttribute('aria-valuenow', run.progress);
  if ($('#run-doing').textContent !== run.activity) $('#run-doing').textContent = run.activity;
  const ol = $('#phases');
  run.phases.forEach((ph, i) => {
    let li = ol.querySelector(`[data-i="${i}"]`);
    if (!li) {
      ol.insertAdjacentHTML('beforeend', `<li class="cphase" data-i="${i}"><span class="cmark"></span><span class="ct"></span><span class="cs"></span></li>`);
      li = ol.lastElementChild; $('.ct', li).textContent = ph.title;
    }
    if (li.dataset.state !== ph.state) { li.dataset.state = ph.state; $('.cmark', li).innerHTML = mark(ph.mark); $('.cs', li).textContent = ph.state_text; }
  });
  const notes = run.notes.join('\n');
  if ($('#notes').textContent !== notes) $('#notes').textContent = notes;
  renderQuestion(run.question);
  renderEnding(run.ending);
}

/* A QR code the console drew as one path, on a light ground whatever the theme, so any phone scans it. */
const qrCode = c => `<svg class="cqr" viewBox="0 0 ${c.size} ${c.size}" role="img" aria-label="${esc(c.label)}"><rect width="${c.size}" height="${c.size}" fill="#fff"/><path fill="#000" d="${esc(c.path)}"/></svg>`;

function renderQuestion(q) {
  const key = q && q.id !== S.answered ? q.id + '|' + q.note : null;
  if (key === S.qkey) return;
  S.qkey = key;
  $$('.cq').forEach(e => e.remove());
  if (!key) return;
  const field = q.field ? `<input id="q-in" class="cf-in" type="${q.field.type === 'password' ? 'password' : 'text'}" aria-labelledby="q-title" autocomplete="off" spellcheck="false">` : '';
  const links = q.links.filter(l => safeUrl(l.url)).map(l => `<a class="btn sm quiet" href="${esc(l.url)}" target="_blank" rel="noopener">${icon('arrow-top-right-on-square', 'sm')}${esc(l.label)}</a>`).join('');
  $('#q-top').innerHTML = `<div class="cq card" data-q="${esc(q.id)}" role="group" aria-labelledby="q-title">
    <div class="cq-t">${icon('lock-closed', 'sm')}<b id="q-title">${esc(q.title)}</b></div>
    ${q.text ? `<p class="cq-x">${esc(q.text)}</p>` : ''}${q.qr ? qrCode(q.qr) : ''}
    ${q.boxes.map((b, j) => `<div class="cbox"><div class="cbox-l">${esc(b.label)}</div><div class="cbox-v"><code class="mono">${esc(b.value)}</code><button class="btn sm" data-copy="${j}">${esc(b.copy)}</button></div></div>`).join('')}
    ${field}${links ? `<div class="cq-links">${links}</div>` : ''}${q.note ? `<div class="cnote bad">${esc(q.note)}</div>` : ''}
    <div class="cq-a">${q.actions.map(a => `<button class="btn ${esc(a.tone)}" data-answer="${esc(a.id)}">${esc(a.label)}</button>`).join('')}</div></div>`;
  ($('#q-in') || $('.cq .btn.primary') || $('.cq [data-answer]')).focus({preventScroll: false});
}

async function answer(action) {
  const q = S.run && S.run.question; if (!q) return;
  const input = $('#q-in'), value = input ? input.value : '';
  if (input) input.value = '';
  S.answered = q.id; renderQuestion(null);
  const r = await send('/api/answer', {id: q.id, action, value});
  if (!r.ok) { S.answered = null; S.qkey = null; }
  tick();
}

function renderEnding(e) {
  const key = e ? JSON.stringify(e) : null;
  if (key === S.ending) return;
  S.ending = key;
  const el = $('#ending');
  if (!e) { el.innerHTML = ''; return; }
  const links = e.links.filter(l => safeUrl(l.url)).map(l => `<a class="btn primary" href="${esc(l.url)}" target="_blank" rel="noopener">${icon('arrow-top-right-on-square', 'sm')}${esc(l.label)}</a>`).join('');
  el.innerHTML = `<div class="cend ${e.ok ? 'good' : 'bad'}"><div class="ce-h">${icon(e.ok ? 'check' : 'exclamation-triangle')}<b>${esc(e.headline)}</b></div>
    ${e.detail ? `<p class="ce-d">${esc(e.detail)}</p>` : ''}${e.next ? `<p class="ce-n">${esc(e.next)}</p>` : ''}
    ${e.log ? `<p class="ce-l">${esc(e.log)}</p>` : ''}
    <div class="cq-a">${links}${e.actions.map(a => `<button class="btn ${esc(a.tone)}" data-end="${esc(a.id)}">${esc(a.label)}</button>`).join('')}</div></div>`;
}

/* ---------- what the owner does ---------- */
async function install() {
  const r = await send('/api/install', {commit: S.page.install.commit.value, repo: $('#f-repo').value.trim(),
    healthchecks: !!$('#f-healthchecks')?.checked, keep: !!$('#f-keep')?.checked, dry: $('#f-dry').checked});
  if (r.ok) watch();
}
async function look() { if ((await send('/api/uninstall', {confirm: false, repo: $('#f-repo').value.trim()})).ok) watch(); }
async function ended(id) {
  if (id === 'dismiss') { if ((await send('/api/dismiss', {})).ok) load(); }
  else if (id === 'uninstall') { if ((await send('/api/uninstall', {confirm: true, repo: ''})).ok) watch(); }
  else if (id === 'logs' || id === 'log') await send('/api/open', {what: id});
}
async function closeConsole() {
  const r = await send('/api/close', {});
  if (!r.ok) return;
  clearTimeout(S.timer); S.timer = null;
  $('#page').innerHTML = `<div class="empty">${esc(r.message)}</div>`; $('#close').hidden = true;
}
async function copy(btn) {
  const box = S.run.question.boxes[+btn.dataset.copy];
  try { await navigator.clipboard.writeText(box.value); btn.textContent = box.copied; setTimeout(() => { btn.textContent = box.copy; }, 1600); }
  catch (e) { toast(e.message, true); }
}

document.addEventListener('click', e => {
  const t = e.target.closest('button'); if (!t || t.disabled) return;
  if (t.dataset.answer) answer(t.dataset.answer);
  else if (t.dataset.copy !== undefined) copy(t);
  else if (t.dataset.end) ended(t.dataset.end);
  else if (t.dataset.action === 'install') install();
  else if (t.dataset.action === 'look') look();
  else if (t.dataset.action === 'close') closeConsole();
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Enter' || e.target.id !== 'q-in') return;
  e.preventDefault(); const go = $('.cq .btn.primary') || $('.cq [data-answer]'); if (go) go.click();
});

/* ---------- following the console ---------- */
function watch() { S.watching = true; busy(true); tick(); }
async function load() {
  clearTimeout(S.timer);
  try { const p = await api('GET', '/api/page'); renderPage(p); S.watching = p.busy; }
  catch (e) { toast(e.message, true); }
  S.timer = setTimeout(tick, S.watching ? POLL_MS : IDLE_MS);
}
async function tick() {
  clearTimeout(S.timer);
  if (!S.watching) { load(); return; }
  try {
    const r = await api('GET', '/api/run');
    renderRun(r.run);
    if (!r.busy) { S.watching = false; load(); return; }
  } catch (e) { toast(e.message, true); }
  S.timer = setTimeout(tick, POLL_MS);
}
load();
