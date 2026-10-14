'use strict';
/* Work: what is queued and in what order, requests by inbox, parked work, what is done and checks; and each task's
   own panel. */

PAGE.work = {
  url: () => `/api/work?tab=${encodeURIComponent(UI.workTab)}` + (UI.workTab === 'requests' ? `&inbox=${encodeURIComponent(UI.reqInbox)}&outcome=${encodeURIComponent(UI.reqOut)}` : UI.workTab === 'done' ? `&view=${encodeURIComponent(UI.doneView)}` : ''),
  load: url => {
    const pages = {requests: UI.reqPages, done: UI.donePages}[UI.workTab];
    return pages > 1 ? loadPaged(url, pages, UI.workTab) : fetchEnv('page', url);
  },
  render: pWork,
  tabLabel: () => { const env = UI.data[PAGE.work.url()], p = env && env.parts.tabs; const t = p && p.ok ? p.data.find(x => x[0] === UI.workTab) : null; return t ? t[1] : null; }
};
function pWork(env) {
  const h = env.parts.header && env.parts.header.ok ? env.parts.header.data : null, tabs = env.parts.tabs;
  return `<div class="page-h"><div><h1>Work</h1>${h ? `<div class="sub">${esc(h.sub)}</div>` : ''}</div>
</div>
    ${tabs && tabs.ok ? `<div class="tabs" role="tablist">${tabs.data.map(([id, n, c]) => `<button role="tab" data-worktab="${esc(id)}" aria-selected="${UI.workTab === id}">${esc(n)}<span class="c">${c}</span></button>`).join('')}</div>` : failedPart(tabs ? tabs.error : 'The answer had no tabs.')}
    ${part(env, UI.workTab, WORK_TAB[UI.workTab])}
    ${h && h.triage_line ? `<div class="triage-line">${icon('clock', 'sm')}<span>${esc(h.triage_line)}</span></div>` : ''}`;
}
const WORK_TAB = {
  tasks: d => `<div class="groups" id="taskrows">${d.groups.map(groupHTML).join('')}</div>`,
  requests: d => `<div class="toolbar"><div class="pillset">${d.inboxes.map(k => `<button class="pill" data-inbox="${esc(k.key)}" aria-pressed="${UI.reqInbox === k.key}">${esc(k.label)} <span class="c">${k.count}</span></button>`).join('')}</div>
      <div class="seg" role="group" aria-label="Outcome">${d.outcomes.map(([k, l]) => `<button data-out="${esc(k)}" aria-pressed="${UI.reqOut === k}">${esc(l)}</button>`).join('')}</div></div>
      <div class="card rows">${d.rows.length ? d.rows.map(r => r.broken ? brokenRow(r) : reqRow(r)).join('') : '<div class="empty">No requests match.</div>'}${showMore('requests', d.more)}</div>`,
  parked: d => `<div class="card rows">${d.rows.length ? d.rows.map(p => p.broken ? brokenRow(p) : `<div class="row req-row" data-open-doc="${esc(p.id)}"><span class="faint">${icon('clock', 'sm')}</span><div style="min-width:0"><div class="tt">${esc(p.title)}</div><div class="ts">${esc(p.why)} · ${esc(p.requests)}</div></div><span class="c-hide faint">${esc(p.date)}</span><span class="c-hide"></span><button class="btn sm" data-unpark="${esc(p.id)}" data-write>Unpark</button></div>`).join('') : '<div class="empty">Nothing parked.</div>'}</div>`,
  done: d => `${d.master ? masterHTML(d.master) : ''}
      <div class="toolbar"><div class="seg" role="group" aria-label="Show">${d.views.map(([k, l]) => `<button data-done-view="${esc(k)}" aria-pressed="${d.view === k}">${esc(l)}</button>`).join('')}</div><span class="faint">${esc(d.note)}</span></div>
      <div class="card rows">${d.rows.length ? d.rows.map(d.view === 'tasks' ? doneRow : commitRow).join('') : `<div class="empty">${esc(d.empty)}</div>`}${showMore('done', d.more)}</div>`,
  checks: d => `<div class="legend">${d.legend.map(l => `<span><i style="background:${toneColor(l.tone)}"></i>${esc(l.label)}</span>`).join('')}</div>
      <div class="card rows">${d.rows.length ? d.rows.map(c => c.broken ? brokenRow(c) : `<div class="row check-row" data-open-doc="${esc(c.id)}"><div><div class="tt">${esc(c.merge)}</div><div class="ts"><span class="chip mono">${esc(c.sha)}</span>${esc(c.age)}</div></div><div class="scen">${c.results.map(x => `<i class="${x.cls}" title="${esc(x.scenario)}"></i>`).join('')}</div><div class="status" style="justify-content:flex-end"><i class="dot ${c.tone}"></i>${esc(c.summary)}</div></div>`).join('') : '<div class="empty">No check of the default branch has run yet.</div>'}</div>`
};
function groupHTML(g) {
  return `<section class="card rows group" data-group="${esc(g.key)}"><div class="group-h"><span class="gtitle">${esc(g.title)}<span class="n">${g.count}</span></span>${g.hint ? hint(g.hint, true) : ''}</div>${g.rows.length ? g.rows.map(r => r.broken ? brokenRow(r) : taskRow(r, g.flip)).join('') : `<div class="empty">${esc(g.empty)}</div>`}</section>`;
}
function taskRow(t, flip) {
  const id = esc(t.id);
  const r = t.rank != null ? `<span class="rank">${t.rank}</span>` : `<span class="rank ${t.rank_mark === '!' ? 'sev' : ''}">${esc(t.rank_mark || '')}</span>`;
  const action = {
    approve: () => `<button class="btn sm primary boost" data-prop-act="approve" data-id="${id}" data-write>Approve</button>`,
    order: () => `<span class="order boost" style="display:flex;gap:6px"><button class="btn sq" data-appr-move="-1" data-id="${id}" aria-label="Start sooner" data-write ${t.can_up ? '' : 'disabled'}>${icon('arrow-up', 'sm')}</button><button class="btn sq" data-appr-move="1" data-id="${id}" aria-label="Start later" data-write ${t.can_down ? '' : 'disabled'}>${icon('arrow-down', 'sm')}</button></span>`,
    boost: () => `<button class="btn sm boost" data-boost="${id}" aria-pressed="false" title="Propose it next" data-write>${icon('arrow-up-circle', 'sm')}Boost</button>`,
    unboost: () => `<button class="btn sm primary boost" data-unboost="${id}" aria-pressed="true" title="Unboost" data-write>${icon('arrow-up-circle', 'sm')}Boosted</button>`
  }[t.action];
  return `<div class="row task-row ${t.boosted ? 'boosted' : ''}" data-task="${id}" data-flip="${flip}-${id}">${r}
    <div style="min-width:0"><div class="tt">${esc(t.title)}</div><div class="ts">${chip(t.severity)}${t.surfaces.map(s => `<span class="chip">${esc(s)}</span>`).join('')}<span>${esc(t.requests)}</span></div></div>
    <span class="c-goal">${t.goal ? `<span class="gid" style="font-size:13px">${esc(t.goal)}</span>` : ''}</span>
    <span class="rung c-rung">${t.rung === null ? '' : `<b>${t.rung}</b>`}${esc(t.rung_word)}</span>
    <span class="status c-status"><i class="dot ${t.status.tone}"></i>${esc(t.status.word)}</span>
    <span class="cost c-cost">${t.cost === '$0' ? '<span class="faint">$0</span>' : esc(t.cost)}</span>${action ? action() : '<span></span>'}</div>`;
}
function masterHTML(m) {
  const b = m.button;
  return `<div class="card master"><div style="min-width:0"><div class="label">${esc(m.label)}</div><div><span class="mono">${esc(m.sha)}</span> · ${esc(m.github)}</div><div class="faint">${esc(m.slot)}</div></div>
    <button class="btn" ${b.url ? `data-slot-open="${esc(b.url)}" data-write` : 'disabled'} title="${esc(b.title)}">${esc(b.label)}${b.url ? ' ↗' : ''}</button></div>`;
}
const doneRow = t => `<div class="row done-row" data-task="${esc(t.id)}"><div style="min-width:0"><div class="tt">${esc(t.title)}</div><div class="ts">${esc(t.sub)}</div></div>
    <span class="stages">${t.stages.map(s => `<span class="chip ${s.tone}" title="${esc(s.title)}">${esc(s.label)}</span>`).join('')}</span></div>`;
const commitRow = c => `<div class="row commit-row" ${c.task ? `data-task="${esc(c.task)}"` : ''}><span class="chip mono">${esc(c.sha)}</span><div style="min-width:0"><div class="tt">${esc(c.subject)}</div>${c.note ? `<div class="ts">${esc(c.note)}</div>` : ''}</div><span class="faint c-hide">${esc(c.age)}</span></div>`;
async function openSlot(btn) {
  // Opened on the press, as a popup opened after an answer may be blocked; a retry from the toast opens its own.
  const tab = window.open('', '_blank');
  const go = ans => {
    const to = tab && !tab.closed ? tab : window.open(ans.open, '_blank');
    if (!to) { alertToast('slot-open', 'The browser blocked the new tab. Press Open again.'); return; }
    to.opener = null; if (to === tab) tab.location.href = ans.open;
  };
  const ans = await act(btn.dataset.slotOpen, {}, {btn, onOk: go});
  if (tab && !(ans && ans.ok)) tab.close();
}
function reqRow(r) {
  const o = r.outcome;
  return `<div class="row req-row" data-open-doc="${esc(r.id)}">${ava(r.family, r.initial, 26)}
    <div style="min-width:0"><div class="tt">${r.severe ? '<span class="chip bad">severe</span> ' : ''}${esc(r.text)}</div><div class="ts"><span class="mono">${esc(r.id)}</span>${esc(r.age)}</div></div>
    <span class="c-hide">${pill(o.text, o.tone, o.mono ? 'mono' : '')}</span><span class="c-hide faint">${esc(r.from)}</span>
    ${r.can_boost ? `<button class="btn sm" data-boostreq="${esc(r.id)}" data-write>${icon('arrow-up-circle', 'sm')}Boost</button>` : '<span></span>'}</div>`;
}
const workGroup = key => { const env = UI.data[PAGE.work.url()], p = env && env.parts.tasks; return p && p.ok ? p.data.groups.find(g => g.key === key) : null; };
function apprMove(id, dir, btn) {
  const g = workGroup('approved');
  act(`/api/approved/${encodeURIComponent(id)}/move`, {dir, seen_order: g ? g.rows.filter(r => !r.broken).map(r => r.id) : []}, {btn, icon: dir < 0 ? 'arrow-up' : 'arrow-down'});
}
function boostTask(id, on, btn, close) {
  act(`/api/tasks/${encodeURIComponent(id)}/${on ? 'boost' : 'unboost'}`, {}, {btn, icon: 'arrow-up-circle', onOk: close ? () => closeSheet() : null});
}

/* ---------- the task panel ---------- */
const BIG = /^[≈$\d]/;
const kvBox = k => `<div><div class="l">${esc(k.label)}</div>${BIG.test(String(k.value)) ? `<div class="big">${esc(k.value)}</div>` : `<div style="font-weight:650;margin-top:4px">${esc(k.value)}</div>`}</div>`;
const testerCard = x => `<div class="tester"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><b>${esc(x.name)}</b><span class="chip ${x.tone}">${esc(x.state)}</span></div><div class="faint" style="font-size:12px">${esc(x.model)}${x.note ? ' · ' + esc(x.note) : ''}</div>${x.shots.length ? `<div class="shots">${x.shots.map(u => picture(u, x.name + ' evidence')).join('')}</div>` : ''}${x.replay ? `<button class="btn sm" style="align-self:flex-start" data-replay="${esc(x.replay)}">${icon('play-circle', 'sm')}Replay</button>` : ''}</div>`;
function taskBody(t) {
  const p = t.proposal;
  return `${p ? `<div class="mock">${picture(p.picture, 'Sketch: now and after')}</div><div><div class="label">Why now</div><div style="margin-top:4px">${esc(p.why)}</div></div>` : ''}
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><span class="status"><i class="dot ${t.status.tone}"></i>${esc(t.status.word)}</span>${t.chips.map(chip).join('')}</div>
    <div class="kv">${t.kv.map(kvBox).join('')}</div>
    <div class="section"><div class="label">${esc(t.route.label)}</div><div class="ladder">${t.route.steps.map((x, i) => `<div class="${i === t.route.current ? 'on' : ''}">${esc(x)}</div>`).join('')}</div>${t.route.cause ? `<div class="muted" style="font-size:12px">Cause: <span class="mono">${esc(t.route.cause)}</span></div>` : ''}</div>
    <div class="section"><div class="label">Progress</div><div class="ladder track">${t.progress.stages.map((x, i) => `<div class="${i <= t.progress.at ? 'on' : ''}">${esc(x)}</div>`).join('')}</div></div>
    <div class="section"><div class="label">Testers <span class="n">${esc(t.testers.note)}</span></div>${t.testers.rows.length ? `<div class="testers">${t.testers.rows.map(testerCard).join('')}</div>` : `<div class="empty card" style="min-height:64px">${esc(t.testers.empty)}</div>`}</div>
    ${t.built_from ? `<div class="section"><div class="label">Built from</div><div class="card" style="overflow:hidden"><div class="origin" style="border:0">${builtFromHTML(t.built_from, t.id)}</div></div></div>` : ''}
    <div class="section"><div class="label">Requests</div>${t.requests.length ? `<div class="card rows">${t.requests.map(r => `<div class="row" style="grid-template-columns:minmax(0,1fr) auto;min-height:48px" data-open-doc="${esc(r.id)}"><span class="tt" style="font-weight:500">${esc(r.text)}</span><span class="faint">${esc(r.age)}</span></div>`).join('')}</div>` : '<div class="muted">No requests linked yet.</div>'}</div>
    <div class="section"><div class="label">Log</div>${t.log.length ? `<ul class="timeline">${t.log.map(l => `<li><i class="dot accent"></i><span>${esc(l.text)}</span><span class="faint" style="font-size:12px">${esc(l.at)}</span></li>`).join('')}</ul>` : '<div class="muted">Nothing is logged yet.</div>'}</div>
    <div class="section"><div class="label">Branch</div>${t.branch ? `<div><span class="chip mono">${esc(t.branch)}</span></div>` : '<div class="muted">No branch yet.</div>'}</div>
    ${t.commits.length ? `<div class="section"><div class="label">Commits</div><div class="card rows">${t.commits.map(c => `<div class="row commit-row" style="cursor:default"><span class="chip mono">${esc(c.sha)}</span><div class="tt" style="font-weight:500">${esc(c.subject)}</div><span class="faint c-hide">${esc(c.note || '')}</span></div>`).join('')}</div></div>` : ''}`;
}
function taskFoot(t) {
  const id = esc(t.id), v = `data-version="${esc(t.version)}" data-close="1" data-write`;
  const B = {
    library: () => `<button class="btn quiet" data-open-doc="${id}">${icon('book-open', 'sm')}In Library</button>`,
    drop: () => `<button class="btn quiet danger" data-drop="${id}" ${v}>Drop</button>`,
    later: () => `<button class="btn" data-prop-act="later" data-id="${id}" ${v}>Not now</button>`,
    approve: () => `<button class="btn primary" data-prop-act="approve" data-id="${id}" ${v}>${icon('check', 'sm')}Approve</button>`,
    boost: () => `<button class="btn primary" data-boost="${id}" ${v}>${icon('arrow-up-circle', 'sm')}Boost</button>`,
    unboost: () => `<button class="btn primary" data-unboost="${id}" ${v}>${icon('arrow-up-circle', 'sm')}Unboost</button>`
  };
  return t.actions.map(a => B[a] ? B[a]() : '').join('');
}
SHEETS.task = {what: 'This task', url: id => '/api/task/' + encodeURIComponent(id), draw: (env, id) => sheetPart(env, 'task', t => [t.sheet_title, taskBody(t), taskFoot(t)], 'Task ' + id)};
const openTask = id => openKeyed('task', id);
