'use strict';
/* Home: is prod safe, what should start next, what else needs me, and is the work moving. */

PAGE.home = {
  url: () => '/api/home',
  render: env => `${part(env, 'outage', o => o ? outageBand(o) : '')}
  ${part(env, 'upgrade', u => u ? upgradeBand(u) : '')}
  ${part(env, 'yolo', y => y ? yoloBand(y) : '')}
  ${part(env, 'envs', envsHTML)}
  ${propsSection(env)}
  ${needsSection(env)}
  <section class="section"><div class="sec-h"><div class="label">At a glance</div></div>${part(env, 'tiles', tilesHTML)}</section>
  ${goalSection(env)}
  <section class="section"><div class="sec-h"><div class="label">Working now</div><button class="btn sm quiet" data-go="agents">All agents${icon('chevron-right', 'sm')}</button></div>${part(env, 'working', workingHTML)}</section>
  ${orchestratorBox(env)}`,
  /* A settled card is the server's brief "Approved." moment, and a choosing slot is waiting on the orchestrator:
     look again soon, so the slot moves on without waiting a whole poll. */
  soon: env => {
    const p = env.parts.proposals, kinds = p && p.ok ? p.data.slots.map(s => s.kind) : [], u = env.parts.upgrade;
    if (u && u.ok && u.data && u.data.busy) return 3000;  // an upgrade moves step by step
    return kinds.includes('settled') ? 1600 : kinds.includes('choosing') ? 4000 : 0;
  }
};
const homePart = name => { const env = UI.data['/api/home'], p = env && env.parts[name]; return p && p.ok ? p.data : null; };

function envsHTML(e) {
  return `<div class="envs">${envCard(e.staging)}<div class="promote-col"><span class="arrow">${icon('chevron-right', 'lg')}</span><span class="ahead">${e.to_push ? `<b class="num" style="font-size:18px;color:var(--fg)">${e.to_push}</b><br>to push` : esc(e.step_word)}</span></div>${envCard(e.prod)}${laptopHTML(e.laptop)}</div>`;
}
function envCard(env) {
  const dot = esc(env.tone);
  return `<div class="card env"><div class="env-top"><i class="dot ${dot}"></i><span class="env-name">${esc(env.name)}</span><span class="chip mono" style="margin-left:auto">${esc(env.sha || 'unknown')}</span></div><div class="env-state" style="color:${cv(env.state_color)}">${esc(env.state_word)}</div>
    <div class="env-meta"><span class="dep">${icon('clock', 'sm')}deployed <b>${esc(env.deployed || 'unknown')}</b></span>${env.chips.map(chip).join('')}</div>
    <div class="env-links">${env.commit_url ? `<a href="${esc(env.commit_url)}" target="_blank" rel="noopener">${icon('code-bracket', 'sm')}Commit on GitHub</a>` : ''}</div></div>`;
}
function laptopHTML(l) {
  return `<div class="card vitals-mini"><div class="env-top"><span class="env-name">Laptop</span><span class="chip ${esc(l.tone)}" style="margin-left:auto">${esc(l.label)}</span></div>${l.rows.map(r => `<div class="vrow"><span>${esc(r.label)}</span>${r.pct == null ? '<div class="meter unknown"></div>' : `<div class="meter"><i style="width:${r.pct}%"></i></div>`}<span class="num">${esc(r.value)}</span></div>`).join('')}</div>`;
}

/* ---------- YOLO mode: the factory runs on its own until the owner switches it off ---------- */
const yoloBand = y => `<div class="card upgrade yolo" role="status"><span class="ico">${icon('moon')}</span><div style="min-width:0"><div class="o-t">${esc(y.title)}</div><div class="o-s">${esc(y.message)}</div></div><div class="o-a"><button class="btn sm" data-action="yolo-off" data-write>${esc(y.button)}</button></div></div>`;

/* ---------- the upgrade band: the upgrade under way, or how it ended ---------- */
function upgradeBand(u) {
  const acts = {
    resume: () => `<button class="btn sm" data-action="resume">${icon('play-circle', 'sm')}Restart the factory</button>`,
  };
  const log = u.log ? `<div class="o-s muted">The log: <span class="mono">${esc(u.log)}</span></div>` : '';
  return `<div class="card upgrade" data-state="${esc(u.state)}" role="status"><span class="ico">${icon(u.busy ? 'arrow-path' : 'arrow-up-circle')}</span><div style="min-width:0"><div class="o-t">${esc(u.title)}</div><div class="o-s">${esc(u.message)}</div>${log}</div><div class="o-a">${u.actions.map(a => acts[a] ? acts[a]() : '').join('')}</div></div>`;
}

/* ---------- the outage band: decided by the warden's probes, never by an agent ---------- */
function outageBand(o) {
  const acts = {
    watch: () => o.session ? `<button class="btn sm" data-agent="${esc(o.session)}">Watch the fix</button>` : '',
    stop: () => `<button class="btn sm danger" data-action="outage-stop" data-write>${icon('stop-circle', 'sm')}Stop the fix</button>`,
    push: () => `<button class="btn sm primary" data-action="promote" data-write>${icon('rocket-launch', 'sm')}Push to prod</button>`
  };
  return `<div class="card outage" role="alert"><span class="ico">${icon('signal-slash')}</span><div style="min-width:0"><div class="o-t">Prod is down <span class="muted" style="font-weight:500;font-size:13px">since ${esc(o.since)}</span></div><div class="probes" role="img" aria-label="${esc(o.probe_note)}">${o.probes.map(x => `<i class="${x === false ? 'x' : x == null ? 'u' : ''}"></i>`).join('')}<span>${esc(o.probe_note)}</span></div><div class="o-s">${boldOnly(o.message)}</div></div><div class="o-a">${o.actions.map(a => acts[a] ? acts[a]() : '').join('')}</div></div>`;
}

/* ---------- Proposed next: fixed-height slots, so nothing below moves when a card leaves ---------- */
function propsSection(env) {
  const d = homePartOf(env, 'proposals');
  return `<section class="section">
    <div class="sec-h"><div class="label">Proposed next ${d ? `<span class="n">${d.open} of ${d.max}</span>` : ''}${hint("The orchestrator's picks: the steps that take you closest to the goal. Nothing starts until you approve it. Only a clear prod outage skips you.")}</div>${d && d.approved_waiting ? `<button class="btn sm quiet" data-go="work">${d.approved_waiting} approved, waiting for a slot${icon('chevron-right', 'sm')}</button>` : ''}</div>
    ${part(env, 'proposals', p => `<div class="props" id="props">${p.slots.map(slotHTML).join('')}</div>`)}
  </section>`;
}
const homePartOf = (env, name) => { const p = env.parts[name]; return p && p.ok ? p.data : null; };
function slotHTML(s, i) {
  if (s.broken) return `<div class="card prop slot" data-flip="slot-${i}" role="status">${icon('exclamation-triangle')}This proposal could not be read: ${esc(s.error)}</div>`;
  if (s.kind === 'card') return propCard(s);
  if (s.kind === 'settled') return `<div class="card prop ${esc(s.look)}" data-flip="${esc(s.task)}" data-task="${esc(s.task)}"><button class="sk" data-task="${esc(s.task)}" aria-label="Open the proposal">${picture(s.picture, 'Sketch: now and after')}</button><div class="prop-main"><div class="prop-t">${esc(s.line)}</div><div class="result">${icon(s.icon, 'sm')}${esc(s.message)}</div></div><span></span></div>`;
  if (s.kind === 'choosing') return `<div class="card prop slot" data-flip="slot-${i}"><span class="spin"></span>The orchestrator is choosing the next proposal…</div>`;
  return `<div class="card prop slot" data-flip="slot-${i}">${s.message ? icon('check') + esc(s.message) : ''}</div>`;
}
function propCard(c) {
  const id = esc(c.task);
  return `<div class="card prop ${c.severe ? 'severe' : ''}" data-flip="${id}" data-task="${id}">
    <button class="sk" data-task="${id}" aria-label="See the sketch larger">${picture(c.picture, 'Sketch: now and after')}</button>
    <div class="prop-main">
      <div class="prop-top"><span class="prop-n">${c.number}</span>${c.goal_item ? `<span class="chip accent">${esc(c.goal_item)}</span>` : c.severe ? '<span class="chip bad">severe</span>' : ''}<button class="prop-t" data-task="${id}">${esc(c.line)}</button></div>
      <div class="prop-why">${esc(c.why)}</div>
      <div class="prop-chips">${dd('prop-run', c.task, c.run.value, c.run.options, 'The run', 0, true)}<span class="chip">${esc(c.testers)}</span><span class="chip">${esc(c.estimate)}</span>${c.run_changed ? pill('run changed by you', 'violet') : ''}</div>
    </div>
    <div class="prop-a"><button class="btn primary" data-prop-act="approve" data-id="${id}" data-write>${icon('check', 'sm')}Approve</button><button class="btn notnow" data-prop-act="later" data-id="${id}" data-write>Not now</button>
      <div class="order"><button class="btn sq" data-prop-move="-1" data-id="${id}" aria-label="Move up" data-write ${c.can_up ? '' : 'disabled'}>${icon('arrow-up', 'sm')}</button><button class="btn sq" data-prop-move="1" data-id="${id}" aria-label="Move down" data-write ${c.can_down ? '' : 'disabled'}>${icon('arrow-down', 'sm')}</button></div></div>
  </div>`;
}
const propCardOf = id => { const d = homePart('proposals'); return d ? d.slots.find(s => s.kind === 'card' && s.task === id) : null; };
const seenProposals = () => { const d = homePart('proposals'); return d ? d.slots.filter(s => s.kind === 'card').map(s => s.task) : []; };
/* The version comes from what the owner is looking at: the button's own panel, else the card on Home. */
function propVersion(id, btn) { if (btn && btn.dataset.version) return btn.dataset.version; const c = propCardOf(id); return c ? c.version : null; }
function propAct(id, what, btn, close) { act(`/api/proposals/${encodeURIComponent(id)}/${what}`, {version: propVersion(id, btn)}, {btn, icon: what === 'approve' ? 'check' : what === 'drop' ? 'x-mark' : 'clock', onOk: close ? () => closeSheet() : null}); }
function propMove(id, dir, btn) { act(`/api/proposals/${encodeURIComponent(id)}/move`, {version: propVersion(id), dir, seen_order: seenProposals()}, {btn, icon: dir < 0 ? 'arrow-up' : 'arrow-down'}); }
DDPICK['prop-run'] = (id, v) => act(`/api/proposals/${encodeURIComponent(id)}/run`, {version: propVersion(id), run: v}, {});

/* ---------- Also needs you: every other decision, each with a picture of what it asks ---------- */
function needsSection(env) {
  const l = homePartOf(env, 'needs');
  return `<section class="section"><div class="sec-h"><div class="label">Also needs you ${l ? `<span class="n">${l.count}</span>` : ''}</div></div>${part(env, 'needs', d => `<div class="needs" id="needs">${d.cards.length ? d.cards.map(needCard).join('') : `<div class="card need slot">${icon('check')} Nothing else needs you.</div>`}</div>`)}</section>`;
}
const needButton = (n, a, c) => {
  const look = `${a.icon ? icon(a.icon, 'sm') : ''}${esc(a.label)}`;
  if (a.key === 'brief') return n.brief_url ? `<button class="btn" data-brief="${esc(n.brief_url)}">${look}</button>` : `<button class="btn" disabled>${look}</button>`;
  return `<button class="btn${a.primary ? ' primary' : ''}" data-need-act="${esc(a.key)}" data-id="${esc(n.id)}" data-write ${c}>${look}</button>`;
};
const needButtons = (n, close) => n.actions.map(a => needButton(n, a, close ? 'data-close="1"' : '')).join('');
const goalChip = n => n.goal_item ? `<span class="chip accent">${esc(n.goal_item)}</span>` : '';
UI.answers = {};
function needCard(n) {
  if (n.broken) return `<div class="card need slot" role="status">${icon('exclamation-triangle')} This item could not be read: ${esc(n.error)}</div>`;
  if (n.done) return `<div class="card need pic done" style="grid-template-columns:44px minmax(0,1fr) auto"><span class="ico" style="background:var(--good-soft);color:var(--good)">${icon('check')}</span><div style="min-width:0"><div class="need-t">${esc(n.title)}</div><div class="need-s">${esc(n.done)}</div></div><div class="need-a">${icon('check', 'sm')}Done</div></div>`;
  const id = esc(n.id), pic = `<button class="sk" data-pic="${id}" aria-label="${esc(n.picture_label || 'See the picture')}">${picture(n.picture, n.picture_label)}</button>`;
  if (n.kind === 'spec') return `<div class="card need pic" data-need="${id}">${pic}<div class="prop-main"><div class="prop-top">${n.chips.slice(0, 1).map(chip).join('')}${goalChip(n)}</div><div class="prop-t" style="cursor:default">${esc(n.title)}</div><div class="need-s" style="margin:0">${n.chips.slice(1).map(chip).join('')}${n.comments ? `<span class="chip">${icon('chat-bubble-left-right', 'sm')}${esc(n.comments)}</span>` : ''}</div></div>
    <div class="need-a prop-a">${needButtons(n)}</div></div>`;
  if (n.kind === 'question') return `<div class="card need pic question" data-need="${id}">${n.picture ? pic : `<span class="sk blank" aria-hidden="true">${icon('chat-bubble-left-right')}</span>`}<div class="prop-main"><div class="prop-top">${n.chips.map(chip).join('')}${goalChip(n)}${n.task ? pill(n.task, 'accent', 'mono') : ''}</div><div class="prop-t" style="cursor:default">${esc(n.title)}</div><div class="prop-why">${esc(n.sub)}</div><label class="need-q"><input id="answer-${id}" data-answer="${id}" value="${esc(UI.answers[n.id] || '')}" placeholder="Your answer" aria-label="Your answer" autocomplete="off"></label></div>
    <div class="need-a prop-a">${needButtons(n)}</div></div>`;
  const lead = n.icon ? `<span class="sk blank" aria-hidden="true">${icon(n.icon)}</span>` : pic;
  return `<div class="card need pic decide" data-need="${id}">${lead}<div class="prop-main"><div class="prop-t" style="cursor:default">${esc(n.title)}</div><div class="prop-why" style="white-space:normal">${esc(n.sub)}</div><div class="need-s" style="margin:0">${n.task ? pill(n.task, 'accent', 'mono') : ''}${goalChip(n)}${n.chips.map(chip).join('')}</div></div>
    <div class="need-a prop-a">${needButtons(n)}</div></div>`;
}
const needOf = id => (homePart('needs') || {cards: []}).cards.find(n => n.id === id);
function needAct(id, what, btn, close) {
  const n = needOf(id); if (!n) return;
  const body = {version: n.version};
  if (what === 'answer') { body.text = (UI.answers[id] || '').trim(); if (!body.text) { alertToast('answer:' + id, 'Write an answer first.'); return; } }
  act(`/api/needs/${encodeURIComponent(id)}/${what}`, body, {btn, quiet: true, onOk: () => { if (what === 'answer') delete UI.answers[id]; if (close) closeSheet(); }});
}
function openPic(id) {
  const n = needOf(id); if (!n) return;
  if (n.kind === 'spec') openSheet(n.title, `<div class="ts">${n.chips.map(chip).join('')}${goalChip(n)}</div><div class="mock">${picture(n.picture, n.picture_label)}</div><div class="muted">The designer's mock-up of the finished screen, under the same goal as your first sign-off. The brief holds every state, in both themes.</div>`, needButtons(n, true));
  else openSheet(n.title, `<div class="mock">${picture(n.picture, n.picture_label)}</div><div>${esc(n.sub)}</div>`, needButtons(n, true));
}

/* ---------- tiles, goal, working now ---------- */
function tilesHTML(t) {
  const w = t.work, s = t.staging, r = t.requests, sp = t.spend;
  return `<div class="tiles">
      <button class="card tile" data-go="work"><div class="label">Work moving</div><div class="big">${w.count}</div>
        <div class="stackbar" aria-hidden="true">${w.stages.map(x => `<i style="flex:${x.count};background:${cv(x.color)}"></i>`).join('')}</div>
        <div class="mini-legend">${w.stages.map(x => `<span><i style="background:${cv(x.color)}"></i><b>${x.count}</b> ${esc(x.label)}</span>`).join('')}</div></button>
      <button class="card tile" data-go="work"><div class="label">On staging, not prod</div><div class="big">${s.count}</div><div class="foot"><span>${esc(s.foot)}</span></div></button>
      <button class="card tile" data-go="work" data-tab="requests"><div class="label">Requests waiting</div><div style="display:flex;align-items:center;gap:14px"><span id="req-ring">${ring(r.count / r.batch, 54, 'batch')}</span><div class="big"><span id="req-count">${r.count}</span><small> / ${r.batch}</small></div></div><div class="foot"><span>triage in <b class="num" id="triage-cd" data-at="${esc(r.triage_at || '')}" style="color:var(--fg)">${r.triage_at ? countdown(r.triage_at) : 'unknown'}</b></span><span>${esc(r.foot_right)}</span></div></button>
      <button class="card tile" data-go="spend"><div class="label">Spend today</div><div class="big" id="spend-today">${money0(sp.today)}<small> / ${money0(sp.budget)}</small></div>${spark(sp.spark)}<div class="foot"><span>${esc(sp.foot)}</span></div></button>
    </div>`;
}
function goalSection(env) {
  const g = homePartOf(env, 'goal');
  return `<section class="section"><div class="sec-h"><div class="label">Goal</div>${g ? `<button class="btn sm quiet" data-open-doc="${esc(g.doc)}">${icon('document-text', 'sm')}${esc(g.doc)}</button>` : ''}</div>${part(env, 'goal', d => `<div class="card goals">${d.items.length ? d.items.map(goalRow).join('') : '<div class="empty">The goal has no items yet.</div>'}</div>`)}</section>`;
}
const goalRow = g => g.broken ? `<div class="goal"><span class="gid">${icon('exclamation-triangle', 'sm')}</span><span class="gname">This item could not be read: ${esc(g.error)}</span><span></span><span></span></div>` : `<div class="goal"><span class="gid">${esc(g.id)}</span><span class="gname">${esc(g.name)}</span><div class="gbar"><i class="${g.progress >= 1 ? 'done' : ''}" style="width:${g.progress * 100}%"></i></div><span class="gstate">${esc(g.state)}</span></div>`;
/* Under Working now, so a message box appearing or going never moves anything above it. */
function orchestratorBox(env) {
  const p = env.parts.message;
  if (p && p.ok && !p.data) return '';
  return `<section class="section"><div class="sec-h"><div class="label">Message the orchestrator</div>${p && p.ok ? `<button class="btn sm quiet" data-agent="${esc(p.data.session)}">See the thread${icon('chevron-right', 'sm')}</button>` : ''}</div>${part(env, 'message', m => `<div class="card">${msgBox(m)}</div>`)}</section>`;
}
function workingHTML(list) {
  if (!list.length) return '<div class="card empty">Nobody is working right now.</div>';
  return `<div class="now">${list.map(a => a.broken ? `<div class="card nowcard" role="status">${icon('exclamation-triangle')}<span class="s">${esc(a.error)}</span></div>` : `<button class="card nowcard" data-agent="${esc(a.session)}">${ava(a.family, a.initials)}<span style="min-width:0"><div class="t">${esc(a.name)}</div><div class="s"><i class="dot ${a.dot}"></i>${esc(a.doing)}</div></span></button>`).join('')}</div>`;
}
