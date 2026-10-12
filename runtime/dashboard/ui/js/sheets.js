'use strict';
/* Side panels that stack (the one underneath steps left), the code sheet, operations and Push to prod. */

let sheetCleanup = null;
const sheetStack = [];
const sheetOpen = () => $('#sheet').classList.contains('open');
function openSheet(title, body, foot, fresh) {
  closePops();
  if (sheetOpen() && !fresh) { addGhost(); sheetStack.push({title: $('#sheet-title').textContent, body: [...$('#sheet-body').childNodes], foot: [...$('#sheet-foot').childNodes], footHidden: $('#sheet-foot').hidden, cleanup: sheetCleanup, scroll: $('#sheet-body').scrollTop, key: $('#sheet').dataset.key || ''}); sheetCleanup = null; slideIn(); }
  else if (!sheetOpen() || fresh) { dropStack(); $$('.sheet-ghost').forEach(g => g.remove()); }
  $('#sheet-back').hidden = !sheetStack.length;
  $('#sheet').dataset.key = '';
  $('#sheet-x').hidden = false;
  $('#sheet-title').textContent = title; $('#sheet-body').innerHTML = body; $('#sheet-foot').innerHTML = foot || ''; $('#sheet-foot').hidden = !foot; $('#sheet').classList.add('open'); $('#scrim').classList.add('open'); document.body.classList.add('sheet-open'); $('#sheet-body').scrollTop = 0; markWrites();
  setTimeout(() => ($('#sheet-body input:not([disabled])') || $('#sheet-x')).focus(), 60);
}
function dropStack() { while (sheetStack.length) { const x = sheetStack.pop(); if (x.cleanup) x.cleanup(); } }
function closeSheet() {
  if (UI.device) return;
  $('#sheet').classList.remove('open'); $('#scrim').classList.remove('open'); document.body.classList.remove('sheet-open'); if (sheetCleanup) { sheetCleanup(); sheetCleanup = null; } dropStack(); $('#sheet-back').hidden = true; $('#sheet').dataset.key = '';
  $$('.sheet-ghost').forEach(g => { g.classList.add('gone'); setTimeout(() => g.remove(), 340); });
  setTimeout(flushDirty, 0);
}
/* A ghost is a picture of the panel underneath: a copy without ids (so lookups always find the live panel), shifted by its depth. */
function addGhost() {
  const g = document.createElement('div'); g.className = 'sheet open sheet-ghost'; g.setAttribute('aria-hidden', 'true');
  [...$('#sheet').children].forEach(c => { const k = c.cloneNode(true); k.removeAttribute('id'); k.querySelectorAll('[id]').forEach(x => x.removeAttribute('id')); g.append(k); });
  g.dataset.level = sheetStack.length; g.style.setProperty('--d', 0); $('#sheet').before(g); requestAnimationFrame(() => requestAnimationFrame(placeGhosts));
}
function placeGhosts() { const gs = $$('.sheet-ghost:not(.gone)'); gs.forEach((g, i) => g.style.setProperty('--d', Math.min(3, gs.length - i))); }
function slideIn() { const r = isPhone(); $('#sheet').animate([{transform: r ? 'translateY(28px)' : 'translateX(48px)', opacity: .55}, {transform: 'none', opacity: 1}], {duration: 320, easing: 'cubic-bezier(.2,.8,.2,1)'}); }
function popTo(level) { while (sheetStack.length > level) sheetBack(); }
/* Opening what a panel in the stack already shows returns to it rather than stacking a copy. */
function stackHas(key) { if (sheetOpen() && $('#sheet').dataset.key === key) return sheetStack.length; return sheetStack.findIndex(x => x.key === key); }
/* Moving the saved nodes back, rather than re-rendering them, keeps their listeners: a code entry left half-typed is still live. */
function sheetBack() {
  const x = sheetStack.pop(); if (!x) return;
  if (sheetCleanup) sheetCleanup(); sheetCleanup = x.cleanup;
  $('#sheet-title').textContent = x.title; $('#sheet-body').replaceChildren(...x.body); $('#sheet-foot').replaceChildren(...x.foot); $('#sheet-foot').hidden = x.footHidden; $('#sheet').dataset.key = x.key || '';
  $('#sheet-body').scrollTop = x.scroll; $('#sheet-back').hidden = !sheetStack.length; $('#sheet-back').hidden ? $('#sheet-x').focus() : $('#sheet-back').focus();
  const g = $$('.sheet-ghost:not(.gone)').at(-1); if (g) { g.remove(); placeGhosts(); const r = isPhone(); $('#sheet').animate([{transform: r ? 'translateY(-12px) scale(.97)' : 'translateX(-28px)', filter: 'brightness(.97)'}, {transform: 'none', filter: 'none'}], {duration: 300, easing: 'cubic-bezier(.2,.8,.2,1)'}); }
}

/* ---------- keyed panels: each kind knows its endpoint and how to draw it, so an open panel refreshes on every poll ---------- */
const SHEETS = {};
async function openKeyed(kind, id) {
  const key = kind + ':' + id, at = stackHas(key);
  if (at === sheetStack.length && sheetOpen()) return;
  if (at >= 0) { popTo(at); return; }
  const S = SHEETS[kind];
  let env;
  try { env = await fetchEnv('sheet:' + key, S.url(id)); } catch (e) { loadFailed(e, S.what, () => openKeyed(kind, id)); return; }
  if (!env) return;
  const [title, body, foot] = S.draw(env, id);
  openSheet(title, body, foot); $('#sheet').dataset.key = key;
  if (S.after) S.after(env, id);
}
const busySheet = () => anyPop() || ($('#sheet-body').contains(document.activeElement) && typing(document.activeElement));
async function refreshSheet(names) {
  const key = $('#sheet').dataset.key || '';
  if (!sheetOpen() || !key) return;
  const i = key.indexOf(':'), kind = key.slice(0, i), id = key.slice(i + 1), S = SHEETS[kind];
  if (!S || (names && !names.includes('all') && !names.includes(kind) && !names.includes(key))) return;
  let env;
  try { env = await fetchEnv('sheet:' + key, S.url(id)); } catch (e) { loadFailed(e, S.what, null); return; }
  if (!env || $('#sheet').dataset.key !== key || busySheet()) return;
  const [title, body, foot] = S.draw(env, id), top = $('#sheet-body').scrollTop;
  $('#sheet-title').textContent = title; $('#sheet-body').innerHTML = body; $('#sheet-foot').innerHTML = foot || ''; $('#sheet-foot').hidden = !foot;
  $('#sheet-body').scrollTop = top; markWrites();
  if (S.after) S.after(env, id);
}
/* A panel whose part failed still opens, and says what failed. */
function sheetPart(env, name, draw, title) {
  const p = env.parts[name];
  if (!p || !p.ok) return [title, failedPart(p ? p.error : `The answer had no ${name}.`), ''];
  return draw(p.data);
}

/* ---------- the code sheet: six boxes; wrong code, locked, refused and failed each say what happened ---------- */
function codeSheet(title, intro, okWord, send, opts = {}) {
  const lockedUntil = UI.shell && UI.shell.code.locked_until && !opts.lockExempt ? UI.shell.code.locked_until : null;
  const off = !!lockedUntil || !!opts.blocked;
  const note = lockedUntil ? `Coded actions are locked until ${lockedUntil}.` : opts.blocked ? opts.blocked : 'The code from your authenticator app.';
  openSheet(title, `${intro}<div class="section"><div class="label">Your 6-digit code</div><div class="code-boxes" id="code">${Array.from({length: 6}, (_, i) => `<input id="code-${i}" maxlength="1" inputmode="numeric" autocomplete="one-time-code" aria-label="Digit ${i + 1}" ${off ? 'disabled' : ''}>`).join('')}</div><div class="code-note ${off ? 'bad' : ''}" id="code-note">${esc(note)}</div></div>`,
    `${opts.device ? '' : '<button class="btn quiet" data-action="close-sheet">Cancel</button>'}<button class="btn primary" id="code-ok" ${off ? 'disabled' : ''}>${esc(okWord)}</button>`, opts.fresh);
  if (opts.device) $('#sheet-x').hidden = true;
  const boxes = $$('#code input'), ok = $('#code-ok'), noteEl = $('#code-note');
  const say = (text, bad) => { noteEl.textContent = text; noteEl.className = 'code-note' + (bad ? ' bad' : ''); };
  let key = newKey(), sent = '';
  const submit = async () => {
    const v = boxes.map(b => b.value).join('');
    if (v.length < 6) { say('Enter all six digits.', true); return; }
    if (UI.down && !opts.device) { say(`Can't reach the warden since ${UI.down}. Your code was not sent.`, true); return; }
    if (v !== sent) { key = newKey(); sent = v; }
    boxes.forEach(b => b.disabled = true); ok.disabled = true; say('Checking…');
    let ans;
    try { ans = await send(v, key); }
    catch (e) {
      boxes.forEach(b => b.disabled = false); ok.disabled = false;
      if (e instanceof NeedsDevice) { UI.device = false; closeSheet(); askDevice(); return; }
      say(e instanceof TimedOut ? 'No answer came in time, so it may have gone through. Trying again is safe: it is never done twice.' : e instanceof ApiDown ? "The warden did not answer, so nothing changed. Try again." : `That failed: ${e.message} Nothing changed.`, true);
      return;
    }
    if (!ans.ok) {
      const c = ans.code || {};
      sent = ''; boxes.forEach(b => { b.value = ''; b.disabled = false; }); ok.disabled = false;
      if (c.locked_until) { UI.shell && (UI.shell.code.locked_until = c.locked_until); boxes.forEach(b => b.disabled = true); ok.disabled = true; say(`Locked after too many wrong codes. Try again at ${c.locked_until}.`, true); return; }
      if (c.tries_left != null) { const box = $('#code'); box.classList.remove('shake'); void box.offsetWidth; box.classList.add('shake'); boxes[0].focus(); say(ans.reason, true); return; }
      boxes[0].focus(); say(ans.reason || 'The factory refused that.', true); return;
    }
    UI.device = false; closeSheet();
    if (ans.message) toast(ans.message, opts.icon);
    if (opts.onOk) opts.onOk(ans);
    if (ans.op) runOp(ans.op, opts.opTitle || title);
    refreshParts(ans.refresh || []);
  };
  boxes.forEach((b, i) => {
    b.addEventListener('input', () => { b.value = b.value.replace(/\D/g, '').slice(-1); if (b.value && i < 5) boxes[i + 1].focus(); if (boxes.every(x => x.value)) submit(); });
    b.addEventListener('keydown', e => { if (e.key === 'Backspace' && !b.value && i > 0) boxes[i - 1].focus(); if (e.key === 'Enter') submit(); });
    b.addEventListener('paste', e => { const t = (e.clipboardData.getData('text') || '').replace(/\D/g, '').slice(0, 6); if (!t) return; e.preventDefault(); t.split('').forEach((c, j) => boxes[j] && (boxes[j].value = c)); (boxes[t.length] || boxes[5]).focus(); if (t.length === 6) submit(); });
  });
  ok.addEventListener('click', submit);
}
/* The first visit asks for the code once; the device then carries the owner cookie. */
function askDevice() {
  if (UI.device) return;
  closeSheet(); UI.device = true; clearTimeout(UI.pollT);
  codeSheet('Sign in this device', '<div class="muted">Enter the 6-digit code from your authenticator app. This browser then stays signed in to the dashboard.</div>', 'Sign in',
    (code, key) => post('/api/device', {code}, key), {device: true, lockExempt: true, fresh: true, onOk: () => { start(); }});
}

/* ---------- operations: long actions show their steps, and survive a reload ---------- */
const OPS = 'ef-ops';
// telemetry: expected — storage a private window refuses holds no operation to follow
function openOps() { try { return JSON.parse(sessionStorage.getItem(OPS) || '[]'); } catch (e) { return []; } }
// telemetry: expected — without storage an operation is followed only until the page reloads
function keepOps(list) { try { sessionStorage.setItem(OPS, JSON.stringify(list)); } catch (e) { } }
const holdOp = (id, title) => keepOps(openOps().filter(o => o.id !== id).concat([{id, title}]));
const forgetOp = id => keepOps(openOps().filter(o => o.id !== id));
function drawSteps(op) {
  const ul = document.getElementById('steps-' + op.id); if (!ul) return;
  $('#sheet-title').textContent = op.title;
  const mark = m => m === 'spin' ? '<span class="spin"></span>' : m === 'ring' ? '<span class="dot ring"></span>' : icon(m, 'sm');
  ul.innerHTML = op.steps.map(s => `<li class="${s.look.cls}">${mark(s.look.mark)}<span>${esc(s.text)}${s.detail ? `<span class="detail">${esc(s.detail)}</span>` : ''}</span></li>`).join('');
}
async function runOp(id, title, quiet) {
  holdOp(id, title);
  if (!quiet) { openSheet(title, `<ul class="steps" id="steps-${esc(id)}"></ul>`, '', true); $('#sheet').dataset.key = 'op:' + id; }
  for (;;) {
    let env;
    try { env = await fetchEnv('op:' + id, '/api/ops/' + encodeURIComponent(id)); }
    catch (e) { if (e instanceof ApiDown) { wardenDown(e.since); await sleep(2000); continue; } forgetOp(id); loadFailed(e, title, null); return; }
    if (!env) { await sleep(700); continue; }
    const p = env.parts.op;
    if (!p.ok) { forgetOp(id); alertToast('op:' + id, `${title}: ${p.error}`); return; }
    const op = p.data; drawSteps(op);
    if (op.state === 'running') { await sleep(700); continue; }
    forgetOp(id);
    const result = op.result || {};
    if (op.state === 'done') {
      setTimeout(() => { if ($('#sheet').dataset.key === 'op:' + id) closeSheet(); }, 450);
      toast(result.message || `${op.title}: done`);
    } else {
      const bad = op.steps.find(s => s.state === 'failed');
      alertToast('op:' + id, `${op.title} failed${bad ? ` at “${bad.text}”` : ''}.${bad && bad.detail ? ' ' + bad.detail : ''}`);
      if ($('#sheet').dataset.key === 'op:' + id) { $('#sheet-foot').innerHTML = '<button class="btn" data-action="close-sheet">Close</button>'; $('#sheet-foot').hidden = false; }
    }
    refreshParts(result.refresh || ['all', 'shell']);
    return;
  }
}

/* ---------- what a change was built from: a spec, or a fix with its cause and its QA ---------- */
function builtFromHTML(o, taskId) {
  if (!o) return `<div class="muted">No record of how this was built.</div>`;
  if (o.kind === 'spec') return `<div class="ts"><span class="chip accent">spec ${esc(o.spec)}</span>${o.approved_by ? `<span class="chip">approved by ${esc(o.approved_by)}</span>` : ''}</div><div>${esc(o.summary)}</div>${o.picture ? `<div class="mock">${picture(o.picture, 'The spec\'s mock-up')}</div>` : ''}<div style="display:flex;gap:8px;flex-wrap:wrap">${o.art_url ? `<a class="btn sm" href="${esc(o.art_url)}" target="_blank" rel="noopener">${icon('arrow-top-right-on-square', 'sm')}Open spec artifact</a>` : ''}<button class="btn sm" data-open-doc="${esc(o.spec)}">${icon('book-open', 'sm')}Spec in Library</button></div>`;
  const qa = o.qa, shots = qa && qa.shots && qa.shots.length ? `<div class="label">QA screenshots</div><div class="shots-row">${qa.shots.map(s => `<figure class="shot">${picture(s.url, s.tester)}<figcaption><span>${esc(s.tester)}</span><span style="color:${toneColor(s.tone)}">${esc(s.verdict)}</span></figcaption></figure>`).join('')}</div>` : '';
  const qaLine = !qa ? '' : qa.tone ? `<div class="chip ${esc(qa.tone)}" style="align-self:flex-start">${esc(qa.note)}</div>` : qa.note ? `<div class="muted" style="font-size:12px">${esc(qa.note)}</div>` : '';
  return `<div class="ts"><span class="chip">${esc(o.rung_label)}</span><span class="chip mono">${esc(taskId)}</span></div><div class="fact"><b>Cause</b><span class="mono">${esc(o.cause)}</span></div><div class="fact"><b>Fix</b><span>${esc(o.fix)}</span></div>${qaLine}${shots}<button class="btn sm" style="align-self:flex-start" data-open-doc="${esc(taskId)}">${icon('book-open', 'sm')}Task file in Library</button>`;
}

/* ---------- Push to prod ---------- */
const envBox = (label, env, accent) => `<div class="card" ${accent ? 'style="border-color:var(--accent)"' : ''}><div class="label">${label}</div><div class="mono" style="font-size:16px;font-weight:650;margin-top:6px">${esc(env.sha || 'unknown')}</div><div class="muted" style="font-size:12px">deployed ${esc(env.deployed || 'unknown')}${env.healthy_for ? ` · healthy for ${esc(env.healthy_for)}` : ''}</div><div class="env-links" style="margin-top:8px">${env.run_url ? `<a href="${esc(env.run_url)}" target="_blank" rel="noopener">${icon('rocket-launch', 'sm')}Deployment</a>` : ''}${env.commit_url ? `<a href="${esc(env.commit_url)}" target="_blank" rel="noopener">${icon('code-bracket', 'sm')}GitHub</a>` : ''}</div></div>`;
async function openPromote() {
  let env;
  try { env = await fetchEnv('sheet:push', '/api/push'); } catch (e) { loadFailed(e, 'Push to prod', openPromote); return; }
  if (!env) return;
  const p = env.parts.push;
  if (!p.ok) { openSheet('Push to prod', failedPart(p.error), ''); return; }
  const d = p.data;
  UI.origins = Object.fromEntries(d.tasks.map(t => [t.id, t.built_from]));
  if (d.prod_runs_staging) { openSheet('Push to prod', `<div class="card empty" style="min-height:120px">${icon('check')} Prod already runs what staging runs.</div>${d.previous_prod ? `<button class="btn" data-action="promote-back" data-write data-sha="${esc(d.previous_prod)}">Push prod back to ${esc(d.previous_prod)}</button>` : ''}`, ''); return; }
  codeSheet('Push to prod', `<div class="diff">${envBox('Prod now', d.prod)}<span style="color:var(--faint);display:grid;place-items:center">${icon('chevron-right')}</span>${envBox('Staging', d.staging, true)}</div>
    ${d.tasks.length ? `<div class="section"><div class="label">What prod gains <span class="n">${d.gains}</span> ${hint('Open a change to see what it was built from. Prod is redeployed at exactly the commit staging runs.')}</div><div class="card rows" style="overflow:hidden">${d.tasks.map(t => `<button class="origin-row" data-origin="${esc(t.id)}" aria-expanded="false" aria-controls="origin-${esc(t.id)}"><span class="tt" style="font-weight:500">${esc(t.title)}</span>${chip(t.chip)}<span class="chev faint">${icon('chevron-right', 'sm')}</span></button><div class="origin" id="origin-${esc(t.id)}" hidden></div>`).join('')}</div></div>` : ''}`,
  'Push to prod', (code, key) => post('/api/push', {expected_staging_sha: d.expected_staging_sha, code}, key), {opTitle: 'Pushing to prod', icon: 'rocket-launch', blocked: d.blocked});
}
function openPushBack(sha) {
  codeSheet('Push prod back', `<div class="muted">Prod is redeployed at ${esc(sha)}, the commit it ran before.</div>`, 'Push back', (code, key) => post('/api/push/back', {expected_sha: sha, code}, key), {opTitle: 'Pushing prod back', icon: 'arrow-path'});
}

function openMore() { openSheet('More', `<div class="more-grid">${UI.pages.slice(4).map(([id, n, ic]) => `<button data-go="${id}" data-close="1">${icon(ic)}${esc(n)}</button>`).join('')}</div><div class="section"><div class="label">Theme</div><div class="seg">${['system', 'light', 'dark'].map(t => `<button data-theme-set="${t}" aria-pressed="${theme() === t}">${t[0].toUpperCase() + t.slice(1)}</button>`).join('')}</div></div>`, '', true); }
function openHelp() { openSheet('Keyboard shortcuts', `<div class="card rows">${UI.pages.map(([, n, , k]) => `<div class="row" style="grid-template-columns:minmax(0,1fr) auto;min-height:44px;cursor:default"><span>${esc(n)}</span><span><span class="chip mono">g</span> <span class="chip mono">${esc(k)}</span></span></div>`).join('')}<div class="row" style="grid-template-columns:minmax(0,1fr) auto;min-height:44px;cursor:default"><span>Search everything</span><span class="chip mono">/</span></div><div class="row" style="grid-template-columns:minmax(0,1fr) auto;min-height:44px;cursor:default"><span>Close a panel or menu</span><span class="chip mono">Esc</span></div></div>`, ''); }
