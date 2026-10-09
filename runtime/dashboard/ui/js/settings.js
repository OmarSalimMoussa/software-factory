'use strict';
/* Settings: every value by its kind of control; edits collect locally, and one coded save commits them all. */

PAGE.settings = {
  url: () => '/api/settings',
  load: async url => { const env = await fetchEnv('page', url); if (env) dropConflicts(env); return env; },
  render: pSettings,
  after: () => {
    updateSavebar();
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
  const head = `<div class="page-h">${ph('Settings', 'Defaults live in settings.toml in the factory repo. Nothing changes until you save. Saving needs your 6-digit code, commits the new settings to the factory repo, and reloads only the part of the factory that reads them; no agent is stopped. Every change is listed under Recent changes.')}</div>`;
  if (!d) return head + part(env, 'settings', () => '');
  return `${head}
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
  if (r.control === 'number') { let n = parseInt(v, 10); if (isNaN(n)) n = curOf(r); v = Math.max(r.low, Math.min(r.high, n)); }
  if (same(v, r.saved)) UI.edits.delete(key); else UI.edits.set(key, {value: v, base: JSON.stringify(r.saved)});
  const row = $(`[data-row="${CSS.escape(key)}"]`); if (row) row.outerHTML = setRow(r);
  updateSavebar();
}
const stepSetting = (key, dir) => { const r = rowOf(key); if (r) setVal(key, curOf(r) + dir * r.step); };
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
