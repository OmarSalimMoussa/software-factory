'use strict';
/* Agents: who is doing what, as a tree under the orchestrator; History of every retired agent; Stop, Pause and Resume. */

PAGE.agents = {
  url: () => '/api/agents',
  load: url => UI.histPages > 1 ? loadPaged(url, UI.histPages, 'history', 'rows', 'history_page') : fetchEnv('page', url),
  render: pAgents
};
const partData = (env, name) => { const p = env.parts[name]; return p && p.ok ? p.data : null; };
function pAgents(env) {
  const h = partData(env, 'header');
  return `<div class="page-h"><div><h1>Agents</h1>${h ? `<div class="sub">${esc(h.sub)}</div>` : ''}</div>
    ${part(env, 'legend', l => `<div class="legend">${l.map(x => `<span><i style="background:var(--f-${esc(x.family)})"></i>${esc(x.label)} · ${x.count}</span>`).join('')}</div>`, '')}</div>
    ${part(env, 'capacity', caps => `<div class="capbars">${caps.map(capCard).join('')}</div>`)}
    ${part(env, 'banner', b => b ? bannerHTML(b) : '')}
    <section class="section"><div class="sec-h"><div class="label">Hierarchy</div><div style="display:flex;gap:6px"><button class="btn sm quiet" data-tree="open">Expand all</button><button class="btn sm quiet" data-tree="close">Collapse all</button></div></div>
    ${part(env, 'tree', t => `<div class="card tree">${t ? node(t) : '<div class="empty">No orchestrator is running yet.</div>'}</div>`)}</section>
    <section class="section"><div class="sec-h"><div class="label">History ${hint('Agents that finished, that you stopped, or that crashed. Each leaves the live tree a few minutes after it ends. None can be resumed.')}</div></div>
    ${part(env, 'history', d => `<div class="card rows" id="history">${d.rows.length ? d.rows.map(r => r.broken ? brokenRow(r) : histRow(r)).join('') : '<div class="empty">No agent has ended yet.</div>'}${showMore('history', d.more)}</div>`)}</section>`;
}
const capCard = c => `<div class="card cap"><div style="display:flex;justify-content:space-between;align-items:baseline"><span class="label">${esc(c.label)}</span><span class="big" style="font-size:24px">${c.value}<small> / ${c.max}</small></span></div><div class="slots">${Array.from({length: Math.max(c.max, c.value)}, (_, i) => `<i class="${i < c.value ? 'on' : ''}"></i>`).join('')}</div><div class="faint" style="font-size:12px;min-height:16px">${c.note ? icon('clock', 'sm') + ' ' + esc(c.note) : ''}</div></div>`;
function bannerHTML(b) {
  const btn = {start: '<button class="btn primary" data-action="start-orch" data-write>Start the orchestrator</button>', resume: `<button class="btn primary" data-action="resume" data-write>${icon('play-circle', 'sm')}Resume the factory</button>`}[b.action] || '';
  return `<div class="ro-banner"><span class="ico">${icon(b.kind === 'paused' ? 'pause-circle' : 'stop-circle')}</span><div style="flex:1;min-width:0"><b>${esc(b.text)}</b> <span class="muted">${esc(b.sub)}</span></div>${btn}</div>`;
}
const MARK = {working: 'accent', done: 'good', waiting: 'warn', depri: 'depri', stop: 'stop'};
function node(a) {
  if (a.broken) return `<div class="node"><div class="node-card broken" role="status">${icon('exclamation-triangle', 'sm')}<div style="min-width:0"><div class="nname">This agent could not be read.</div><div class="nsub">${esc(a.id)} · ${esc(a.error)}</div></div></div></div>`;
  const kids = a.kids || [], id = esc(a.id);
  return `<div class="node ${UI.agentsClosed.has(a.id) ? 'closed' : ''} ${a.retired ? 'retired' : ''}" data-node="${id}"><div class="node-card" data-agent="${id}">
    ${kids.length ? `<button class="toggle" data-toggle="${id}" aria-label="Show or hide ${esc(a.name)}'s agents">${icon('chevron-down', 'sm')}</button>` : '<span></span>'}
    ${ava(a.family, a.initials)}
    <div style="min-width:0"><div class="nname">${esc(a.name)} <span class="faint" style="font-weight:500;font-size:12px">${esc(a.role)}${esc(a.kids_label)}</span></div><div class="nsub">${esc(a.doing)}</div></div>
    <span class="chip hide-sm">${esc(a.model)}</span><span class="state hide-sm"><i class="dot ${MARK[a.marker] || ''}"></i>${esc(a.state_word)}</span><span class="cost">${esc(a.cost)}</span>${a.can_stop ? `<button class="iconbtn kill" data-stop="${id}" aria-label="Stop ${esc(a.name)} and every agent under it" data-write>${icon('stop-circle', 'sm')}</button>` : '<span></span>'}</div>
    ${kids.length ? `<div class="kids-wrap"><div class="kids">${kids.map(node).join('')}</div></div>` : ''}</div>`;
}
const treeIds = (n, out = []) => { if (n && !n.broken && n.kids && n.kids.length) { out.push(n.id); n.kids.forEach(k => treeIds(k, out)); } return out; };
function syncTree() { $$('[data-node]').forEach(n => n.classList.toggle('closed', UI.agentsClosed.has(n.dataset.node))); }
function histRow(h) {
  return `<div class="row hist-row" data-hist="${esc(h.id)}">${ava(h.family, h.initials, 30)}<div style="min-width:0"><div class="tt" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(h.name)} <span class="faint" style="font-weight:500">${esc(h.role)}</span></div><div class="ts">${h.task ? pill(h.task, 'accent', 'mono') : ''}<span class="model">${esc(h.model)}</span><span class="why-sm">${chip(h.why)}</span></div></div>
    <div class="hc"><b>${esc(h.started)}</b><small>${esc(h.started_by)}</small></div><div class="hc"><b>${esc(h.ended)}</b><small>${esc(h.lasted)}</small></div><span class="why">${chip(h.why)}</span><span class="cost">${esc(h.cost)}</span></div>`;
}

/* ---------- panels: an agent, a History record (read-only) ---------- */
const onlyPart = env => Object.values(env.parts)[0];
function agentBody(a) {
  const life = a.life_note ? `<div class="ro-banner"><span class="ico">${icon(a.life_note.kind === 'depri' ? 'clock' : 'lock-closed')}</span><div>${esc(a.life_note.text)}</div></div>` : '';
  return `<div style="display:flex;gap:10px;align-items:center">${ava(a.family, a.initials)}<div><b>${esc(a.role)}</b><div class="muted">${esc(a.doing)}</div></div></div>
    <div class="kv">${a.kv.map(kvBox).join('')}</div>
    ${life}
    ${a.task ? `<button class="btn" data-task="${esc(a.task)}">${icon('queue-list', 'sm')}Open task ${esc(a.task)}</button>` : ''}
    ${a.kids.length ? `<div class="section"><div class="label">Under it</div><div class="card rows">${a.kids.map(k => `<div class="row" style="grid-template-columns:34px minmax(0,1fr) auto;min-height:52px" data-agent="${esc(k.id)}">${ava(k.family, k.initials, 28)}<span class="tt" style="font-weight:500">${esc(k.name)} <span class="faint">· ${esc(k.doing)}</span></span><span class="cost">${esc(k.cost)}</span></div>`).join('')}</div></div>` : ''}`;
}
function agentFoot(a, id) {
  const B = {
    stop: () => `<button class="btn danger" data-stop="${esc(id)}" data-write>${icon('stop-circle', 'sm')}Stop</button>`,
    'stop-factory': () => `<button class="btn danger" data-stop="${esc(id)}" data-write>${icon('stop-circle', 'sm')}Stop the factory</button>`,
    'pause-factory': () => `<button class="btn" data-action="pause" data-write>${icon('pause-circle', 'sm')}Pause the factory</button>`
  };
  const chat = a.chat_url ? `<a class="btn" href="${esc(a.chat_url)}" target="_blank" rel="noopener">${icon('chat-bubble-left-right', 'sm')}Open chat</a>` : `<button class="btn" disabled title="This session has no chat link">${icon('chat-bubble-left-right', 'sm')}Open chat</button>`;
  return a.actions.map(x => B[x] ? B[x]() : '').join('') + chat;
}
SHEETS.agent = {what: 'This agent', url: id => '/api/agent/' + encodeURIComponent(id), draw: (env, id) => { const p = onlyPart(env); return p && p.ok ? [p.data.name, agentBody(p.data), agentFoot(p.data, id)] : ['Agent', failedPart(p ? p.error : 'The answer was empty.'), '']; }};
const openAgent = id => openKeyed('agent', id);
function histBody(h) {
  return `<div style="display:flex;gap:10px;align-items:center">${ava(h.family, h.initials)}<div><b>${esc(h.role)}</b>${h.note ? `<div class="muted">${esc(h.note)}</div>` : ''}</div><span style="margin-left:auto">${chip(h.why)}</span></div>
    <div class="kv"><div><div class="l">Model</div><div style="font-weight:650;margin-top:4px">${esc(h.model)}</div></div><div><div class="l">Started</div><div style="font-weight:650;margin-top:4px">${esc(h.started)}</div><div class="l">${esc(h.started_by)}</div></div><div><div class="l">Ended</div><div style="font-weight:650;margin-top:4px">${esc(h.ended)}</div><div class="l">${esc(h.lasted)}</div></div><div><div class="l">Cost</div><div class="big">${esc(h.cost)}</div></div></div>
    ${h.task ? `<button class="btn" data-task="${esc(h.task)}">${icon('queue-list', 'sm')}Open task ${esc(h.task)}</button>` : ''}
    <div class="ro-banner"><span class="ico">${icon('lock-closed')}</span><div>Retired. This agent can never be resumed; its record is read-only.</div></div>`;
}
SHEETS.hist = {what: 'This record', url: id => '/api/history/' + encodeURIComponent(id), draw: (env, id) => { const p = onlyPart(env); return p && p.ok ? [p.data.name, histBody(p.data), ''] : ['Record', failedPart(p ? p.error : 'The answer was empty.'), '']; }};
const openHist = id => openKeyed('hist', id);

/* ---------- Stop: the server lists what stops; stopping the whole factory needs the code ---------- */
async function openStop(id) {
  let pv, ag;
  try { [pv, ag] = await Promise.all([fetchEnv('stop:' + id, `/api/agent/${encodeURIComponent(id)}/stop-preview`), fetchEnv('stopa:' + id, `/api/agent/${encodeURIComponent(id)}`)]); }
  catch (e) { loadFailed(e, 'Stopping', () => openStop(id)); return; }
  if (!pv || !ag) return;
  const p = onlyPart(pv), a = onlyPart(ag);
  if (!p.ok) { alertToast('stop:' + id, p.error); return; }
  const d = p.data;
  UI.stopping = {id, version: a.ok ? a.data.version : null, coded: d.coded, button: d.button, title: d.title, text: d.text};
  const keep = sheetOpen() ? 'sheet-back' : 'close-sheet';
  openSheet(d.title, `<div>${esc(d.text)}</div><div class="stoplist">${d.rows.map(x => `<div>${ava(x.family, x.initials, 28)}<span style="min-width:0"><b>${esc(x.name)}</b> <span class="faint">${esc(x.role)} · ${esc(x.model)}</span></span><span class="cost">${esc(x.cost)}</span></div>`).join('')}</div>`,
    `<button class="btn quiet" data-action="${keep}">Keep running</button><button class="btn danger" data-stopnow="${esc(id)}" data-write>${icon('stop-circle', 'sm')}${esc(d.button)}</button>`);
}
function stopNow(id, btn) {
  const s = UI.stopping; if (!s || s.id !== id) return;
  if (s.coded) codeSheet(s.title, `<div class="muted">${esc(s.text)}</div>`, s.button, (code, key) => post(`/api/agents/${encodeURIComponent(id)}/stop`, {version: s.version, code}, key), {lockExempt: true, icon: 'stop-circle'});
  else act(`/api/agents/${encodeURIComponent(id)}/stop`, {version: s.version}, {btn, icon: 'stop-circle', onOk: () => closeSheet()});
}
function pauseFactory() {
  codeSheet('Pause the factory', '<div class="muted">Every agent is benched, the orchestrator included. Nothing is lost, and everything comes back in order when you resume.</div>', 'Pause the factory',
    (code, key) => post('/api/factory/pause', {code}, key), {lockExempt: true, opTitle: 'Pausing the factory', icon: 'pause-circle'});
}
function resumeFactory() {
  codeSheet('Resume the factory', '<div class="muted">Every benched agent comes back in order, with its full context.</div>', 'Resume the factory',
    (code, key) => post('/api/factory/resume', {code}, key), {opTitle: 'Resuming the factory', icon: 'play-circle'});
}
