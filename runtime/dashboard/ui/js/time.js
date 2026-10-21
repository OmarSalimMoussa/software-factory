'use strict';
/* Time: where a task's time goes, end to end. Wall time and runtime are told apart and never added; the server words
   both. The slowest tasks and the tools' time open in a sheet; a task's sheet tells its own timeline and runtime. */

PAGE.time = {
  url: () => '/api/time',
  render: env => {
    const w = partData(env, 'wall');
    return `<div class="page-h">${ph('Time', 'Where a task’s time goes, from approved to on staging and prod. Wall time is calendar time; runtime is the time its agents were doing something. The two are never added together.')}
    ${w ? `<div class="seg" role="group" aria-label="More">${w.lists.map(([k, l]) => `<button data-time-list="${esc(k)}">${esc(l)}</button>`).join('')}</div>` : ''}</div>
    ${part(env, 'wall', wallCard)}${part(env, 'runtime', runtimeCard)}${part(env, 'checks', checksCard)}`;
  }
};
const timeHead = (label, help, sub) => `<div class="sec-h"><div class="label">${esc(label)} ${hint(help)}</div>${sub ? `<span class="muted">${esc(sub)}</span>` : ''}</div>`;
function wallCard(w) {
  return `<div class="card time-card" id="time-wall">${timeHead('Wall time, median per step', w.hint, w.sub)}
    ${w.rows.map(r => `<div class="bar-row time-row${r.total ? ' total' : ''}"><div class="name"><span>${esc(r.label)}</span></div><div class="hbar"><i style="width:${r.pct}%;background:var(${r.total ? '--good' : '--accent'})"></i></div><span class="cost">${esc(r.median)}</span><span class="faint">${r.note ? esc(r.note) : ''}</span></div>`).join('')}</div>`;
}
/* A stacked bar of the parts, then each part with its colour and value. */
const splitBar = (parts, label) => `<div class="tok-bar" role="img" aria-label="${esc(label)}">${parts.map(p => `<i style="width:${p.pct}%;background:${cv(p.color)}"></i>`).join('')}</div>`;
const splitRows = parts => `<div class="tok-stats">${parts.map(p => `<div class="tok"><span class="k"><i style="background:${cv(p.color)}"></i>${esc(p.label)}</span><span class="v">${esc(p.value)}</span></div>`).join('')}</div>`;
const runGroup = g => `<div class="time-group"><div class="sec-h"><b>${esc(g.name)}</b><span class="muted">alive ${esc(g.alive)}</span></div>${splitBar(g.parts, g.name)}${splitRows(g.parts)}</div>`;
function runtimeCard(r) {
  return `<div class="card time-card" id="time-run">${timeHead('Runtime', r.hint, r.sub)}${r.empty ? `<div class="empty">${esc(r.empty)}</div>` : r.groups.map(runGroup).join('')}</div>`;
}
function checksCard(c) {
  return `<div class="card time-card" id="time-checks">${timeHead('Merged, until its check passed', 'Wall time a merge waits for the check that clears it. A check covers every merge waiting, so a regression tester walking counts for each of them.', c.sub)}
    ${c.kv.length ? `<div class="kv" style="grid-template-columns:repeat(3,minmax(0,1fr))">${c.kv.map(kvBox).join('')}</div>` : ''}
    ${c.split.map(s => `<div class="bar-row time-row"><div class="name"><i class="dot" style="background:${cv(s.color)}"></i><span>${esc(s.label)}</span></div><div class="hbar"><i style="width:${s.pct}%;background:${cv(s.color)}"></i></div><span class="cost">${esc(s.share)}</span><span class="faint">median ${esc(s.value)}</span></div>`).join('')}
    ${c.facts.map(f => `<div class="muted time-fact">${esc(f)}</div>`).join('')}</div>`;
}
SHEETS.timelist = {what: 'This list', url: id => '/api/time/' + encodeURIComponent(id), draw: (env, id) => sheetPart(env, 'list', L => [L.title, `<div class="muted">${esc(L.note)}</div>
  ${L.rows.length ? `<div class="card bars">${L.rows.map(r => `<div class="bar-row"${r.task ? ` data-task="${esc(r.task)}" style="cursor:pointer"` : ''}><div class="name"><span>${esc(r.name)}</span></div><div class="hbar"><i style="width:${r.pct}%;background:var(--accent)"></i></div><span class="cost">${esc(r.value)}</span>${r.detail ? `<div class="bar-detail faint">${esc(r.detail)}</div>` : ''}</div>`).join('')}</div>` : `<div class="empty card">${esc(L.empty)}</div>`}`, ''], 'Time')};
/* A task's own time, under its progress: its timeline in wall time, then its runtime, its tools one click away. */
function taskTimeHTML(t) {
  const steps = t.rows.map(r => `<div class="bar-row time-step"><div class="name"><span>${esc(r.label)}</span></div><div class="hbar"><i style="width:${r.pct}%;background:var(--accent)"></i></div><span class="cost">${esc(r.value)}${r.note ? ` <span class="faint">${esc(r.note)}</span>` : ''}</span></div>`).join('');
  const tools = t.tools ? `<div class="muted time-fact">Each tool’s time ${hint(t.tools)}</div>` : '';
  return `<div class="section" id="task-time"><div class="label">Wall time ${hint(t.hint)}</div><div class="muted">${esc(t.total)}</div><div class="card time-card">${steps}</div></div>
    <div class="section" id="task-runtime"><div class="label">Runtime ${hint(t.run_hint)}</div>${t.runtime ? `<div class="card time-card">${t.runtime.map(runGroup).join('')}${tools}</div>` : `<div class="muted">${esc(t.unknown)}</div>`}</div>`;
}
