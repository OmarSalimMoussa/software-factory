'use strict';
/* Settings: every value by its kind of control; edits collect locally, and one coded save commits them all. */

PAGE.settings = {
  url: () => '/api/settings',
  load: async url => { const env = await fetchEnv('page', url); if (env) dropConflicts(env); return env; },
  render: pSettings,
  after: () => {
    updateSavebar();
    const n = $('#acct-name'), k = $('#acct-token'); if (n) n.value = UI.acct.name; if (k) k.value = UI.acct.token;
    const r = UI.flashRow && $(`[data-row="${CSS.escape(UI.flashRow)}"]`);
    if (r) { r.scrollIntoView({block: 'center'}); r.classList.add('flash'); UI.flashRow = null; }
  }
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const setData = () => { const env = UI.data['/api/settings']; return env ? partData(env, 'settings') : null; };
const rowOf = key => { const d = setData(); return d ? d.groups.flatMap(g => g.rows).find(r => r.key === key) : null; };
const curOf = r => UI.edits.has(r.key) ? UI.edits.get(r.key).value : r.saved;
const showVal = (r, v) => r.control === 'model' ? `${v.model} · ${v.effort}` : r.control === 'list' ? v.join(', ') : String(v);
function pSettings(env) {
  const d = partData(env, 'settings');
  const head = `<div class="page-h">${ph('Settings', 'Defaults come with the factory you installed. Nothing changes until you save. Saving needs your 6-digit code, keeps your values on this computer, and reloads only the part of the factory that reads them; no agent is stopped. Every change is listed under Recent changes.')}</div>`;
  if (!d) return head + acctSection(env) + part(env, 'settings', () => '');
  return `${head}${acctSection(env)}
    <div class="card repo-card"><div style="flex:1;min-width:0"><div class="label">Repo</div><div class="repo-path">${esc(d.repo.path)}</div></div><button class="btn" data-action="change-repo" data-write>Change repo</button></div>
    <div class="set-grid">${d.groups.map(g => `<div class="card set-card"><div class="sec-h"><div class="label">${esc(g.title)} ${g.hint ? hint(g.hint) : ''}</div></div>${g.rows.map(r => r.broken ? brokenRow(r) : setRow(r)).join('')}</div>`).join('')}
      <div class="card set-card"><div class="sec-h"><div class="label">Recent changes</div></div><ul class="audit">${d.audit.length ? d.audit.map(a => `<li><span>${esc(a.what)}</span><span class="faint">${a.sha ? `<span class="mono">${esc(a.sha)}</span> · ` : ''}${esc(a.ago)}</span></li>`).join('') : '<li><span class="muted">No setting has changed yet.</span></li>'}</ul></div></div>
    <div class="savebar" id="savebar"><span id="savebar-n"></span><button class="btn sm quiet" data-action="discard">Discard</button><button class="btn sm primary" data-action="review" data-write>Review and save</button></div>`;
}
function setRow(r) {
  const cur = curOf(r), changed = !same(cur, r.saved), key = esc(r.key), ro = r.control === 'readonly';
  const ctl = {
    model: () => dd('model', r.key, cur.model, r.options.models, r.label + ' model', 132) + dd('effort', r.key, cur.effort, r.options.efforts, r.label + ' effort', 104),
    number: () => `<div class="stepper"><button data-step="-1" data-key="${key}" aria-label="Less">−</button><input data-setn="${key}" value="${esc(cur)}" inputmode="numeric" aria-label="${esc(r.label)}"><button data-step="1" data-key="${key}" aria-label="More">+</button></div>`,
    text: () => `<input class="txt" data-sett="${key}" value="${esc(cur)}" aria-label="${esc(r.label)}" autocomplete="off" spellcheck="false">`,
    list: () => `<div class="listed">${cur.map((x, i) => `<span class="chip">${esc(x)}<button data-listdel="${key}" data-i="${i}" aria-label="Remove ${esc(x)}">${icon('x-mark', 'sm')}</button></span>`).join('')}<input data-listadd="${key}" placeholder="Add, then Enter" aria-label="Add to ${esc(r.label)}" autocomplete="off" spellcheck="false"></div>`,
    readonly: () => `<span class="ro">${esc(showVal(r, r.value))}</span>`
  }[r.control]();
  const toDefault = !ro && !same(cur, r.default) ? `<button class="linkbtn" data-default="${key}">Reset to default</button>` : '';
  return `<div class="set-row ${changed ? 'changed' : ''} ${r.control === 'list' ? 'list' : ''}" data-row="${key}"><div><div class="k">${esc(r.label)}${r.hint ? ' ' + hint(r.hint) : ''}</div>${changed ? `<div class="was">was ${esc(showVal(r, r.saved))}</div>` : ''}${r.pending ? `<div class="help">${esc(r.pending)}</div>` : ''}${toDefault}</div><div class="ctl">${ctl}${ro ? '' : `<button class="iconbtn reset" data-resetkey="${key}" aria-label="Undo this change" title="Undo">${icon('arrow-path', 'sm')}</button>`}</div></div>`;
}
function setVal(key, v) {
  const r = rowOf(key); if (!r) return;
  /* Exactly what was typed goes to the warden, which says what is wrong with it: never clamped, rounded or dropped here. */
  if (r.control === 'number') { const text = String(v).trim(), n = Number(text); v = text !== '' && Number.isFinite(n) ? n : text; }
  if (same(v, r.saved)) UI.edits.delete(key); else UI.edits.set(key, {value: v, base: JSON.stringify(r.saved)});
  const row = $(`[data-row="${CSS.escape(key)}"]`); if (row) row.outerHTML = setRow(r);
  updateSavebar();
}
/* The +/- buttons stop at the range the server sent; only typing can go past it, for the warden to refuse. */
const stepSetting = (key, dir) => { const r = rowOf(key); if (r) setVal(key, Math.max(r.low, Math.min(r.high, curOf(r) + dir * r.step))); };
const resetSetting = key => { const r = rowOf(key); if (r) setVal(key, r.saved); };
const defaultSetting = key => { const r = rowOf(key); if (r) setVal(key, r.default); };
DDPICK.model = (key, v) => { const r = rowOf(key); if (r) setVal(key, {...curOf(r), model: v}); };
DDPICK.effort = (key, v) => { const r = rowOf(key); if (r) setVal(key, {...curOf(r), effort: v}); };
function listAdd(key, input) { const r = rowOf(key), v = input.value.trim(); if (!r || !v) return; const cur = curOf(r); if (!cur.includes(v)) setVal(key, cur.concat([v])); const next = $(`[data-listadd="${CSS.escape(key)}"]`); if (next) next.focus(); }
function listDel(key, i) { const r = rowOf(key); if (r) setVal(key, curOf(r).filter((_, j) => j !== i)); }
function updateSavebar() {
  const n = UI.edits.size, bar = $('#savebar');
  document.body.classList.toggle('has-savebar', !!bar && n > 0);
  if (!bar) return;
  bar.classList.toggle('show', n > 0); $('#savebar-n').textContent = n + (n === 1 ? ' change, not saved' : ' changes, not saved');
}
/* An edit made against a value that has since changed on another device is dropped, and the owner is told. */
function dropConflicts(env) {
  const d = partData(env, 'settings'); if (!d) return;
  const rows = new Map(d.groups.flatMap(g => g.rows).filter(r => !r.broken).map(r => [r.key, r]));
  [...UI.edits].forEach(([k, e]) => {
    const r = rows.get(k);
    if (r && JSON.stringify(r.saved) === e.base) return;
    UI.edits.delete(k);
    alertToast('conflict:' + k, r ? `${r.label} changed on another device, so your edit was dropped. It is now ${showVal(r, r.saved)}.` : `A setting you changed is gone, so your edit was dropped.`);
  });
}
function reviewSettings() {
  if (!UI.edits.size) return;
  const items = [...UI.edits].map(([k, e]) => [rowOf(k), e.value]).filter(([r]) => r);
  codeSheet(`Save ${items.length} change${items.length === 1 ? '' : 's'}`, `<ul class="changes">${items.map(([r, v]) => `<li><span>${esc(r.label)}</span><b>${esc(showVal(r, r.saved))} → ${esc(showVal(r, v))}</b></li>`).join('')}</ul>`, 'Save',
    (code, key) => post('/api/settings', {changes: Object.fromEntries(items.map(([r, v]) => [r.key, v])), code}, key), {opTitle: 'Saving settings', onOk: () => { UI.edits.clear(); updateSavebar(); }});
}
function changeRepo() {
  const d = setData(); if (!d) return;
  codeSheet('Change repo', `<label class="search" style="max-width:none">${icon('book-open', 'sm')}<input id="repo-path" value="${esc(d.repo.path)}" aria-label="Repo path" autocomplete="off" spellcheck="false"></label><div class="muted" style="font-size:12px">The orchestrator must be stopped first. The factory works on one repo at a time.</div>`, 'Save',
    (code, key) => post('/api/settings/repo', {path: $('#repo-path').value.trim(), code}, key), {opTitle: 'Changing the repo'});
}

/* The Claude account section: the server lists the accounts and words every label. Switch runs as an operation, Remove
   asks first, and Add account sends the token once; the page never draws a token back. */
UI.acct = {name: '', token: ''};
const acctData = () => { const env = UI.data['/api/settings']; return env ? partData(env, 'accounts') : null; };
const acctRow = r => `<div class="set-row" data-row="acct:${esc(r.name)}"><div class="acct-id"><span class="k">${esc(r.name)}</span>${r.chip ? pill(r.chip, 'good') : ''}<span class="help"><span class="mono">${esc(r.token)}</span> · ${esc(r.added)}</span></div><div class="ctl">${r.switch ? `<button class="btn sm" data-acct-use="${esc(r.switch.url)}" aria-label="${esc(r.switch.aria)}" data-write>${icon('arrow-path', 'sm')}${esc(r.switch.label)}</button>` : ''}${r.remove ? `<button class="btn sm quiet" data-acct-remove="${esc(r.name)}" data-write>${esc(r.remove.label)}</button>` : ''}</div></div>`;
function acctSection(env) {
  return part(env, 'accounts', a => `<div class="card set-card acct-card"><div class="sec-h"><div class="label">${esc(a.title)} ${hint(a.hint)}</div></div>
    ${a.rows.map(r => r.broken ? brokenRow(r) : acctRow(r)).join('')}${a.empty ? `<div class="set-row"><span class="muted">${esc(a.empty)}</span></div>` : ''}
    <div class="acct-add"><div class="k">${esc(a.add.title)}</div><div class="acct-fields">
      <label class="a-nl" for="acct-name">${esc(a.add.name)}</label><input class="a-ni" id="acct-name" data-acct="name" autocomplete="off" spellcheck="false" aria-describedby="acct-name-hint"><span class="help a-nh" id="acct-name-hint">${esc(a.add.name_hint)}</span>
      <label class="a-tl" for="acct-token">${esc(a.add.token)}</label><input class="a-ti" id="acct-token" type="password" data-acct="token" autocomplete="off" spellcheck="false" aria-describedby="acct-token-hint"><span class="help a-th" id="acct-token-hint">${esc(a.add.token_hint)}</span>
      <button class="btn primary a-b" data-acct-add data-write>${esc(a.add.button)}</button></div>
      <div class="code-note" id="acct-note" role="status"></div></div></div>`);
}
const useAccount = (url, btn) => act(url, {}, {btn, icon: 'arrow-path', opTitle: acctData().op_title});
function removeAccount(name) {
  const r = acctData().rows.find(x => x.name === name); if (!r || !r.remove) return;
  openSheet(r.remove.title, `<div class="muted">${esc(r.remove.body)}</div>`, `<button class="btn quiet" data-action="close-sheet">${esc(r.remove.keep)}</button><button class="btn danger" data-acct-remove-now="${esc(r.remove.url)}" data-write>${esc(r.remove.label)}</button>`);
}
async function addAccount(btn) {
  if (UI.down) { writesOff(); return; }
  const note = $('#acct-note'), say = (text, bad) => { note.textContent = text; note.className = 'code-note' + (bad ? ' bad' : ''); };
  btn.disabled = true; say('');
  let ans;
  try { ans = await post(acctData().add.url, {name: $('#acct-name').value, token: $('#acct-token').value}); }
  catch (e) {
    if (e instanceof NeedsDevice) askDevice();
    else if (e instanceof TimedOut) say('No answer came in time, so it may have been added: check the list before you try again.', true);
    else if (e instanceof ApiDown) { wardenDown(e.since, e.note); say('The warden did not answer, so nothing changed. Try again.', true); }
    else say(`That failed: ${e.message} Nothing changed.`, true);
    return;
  } finally { if (btn.isConnected) btn.disabled = false; }
  if (!ans.ok) { say(ans.reason, true); return; }
  UI.acct = {name: '', token: ''};
  /* Left focused, a field holds the page's redraw back; the new account shows once it is let go. */
  ['#acct-name', '#acct-token'].forEach(s => { const f = $(s); f.value = ''; f.blur(); });
  toast(ans.message); refreshParts(ans.refresh);
}
