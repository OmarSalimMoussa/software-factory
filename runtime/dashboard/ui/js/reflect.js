'use strict';
/* Self-reflect: what agents wrote about the factory itself. Open entries lead; the owner marks one fixed or ignored. */

PAGE.reflect = {
  url: () => `/api/reflect?state=${encodeURIComponent(UI.reflectState)}&kind=${encodeURIComponent(UI.reflectKind)}`,
  load: url => UI.reflectPages > 1 ? loadPaged(url, UI.reflectPages, 'reflect', 'entries') : fetchEnv('page', url),
  render: env => `<div class="page-h">${ph('Self-reflect', 'What agents wrote about the factory itself. Mark an entry fixed once a change addresses it, or ignore it when it needs none. One that comes back opens again.')}</div>
    ${part(env, 'reflect', d => `${reflectHealth(d.health)}
    <div class="seg" role="group" aria-label="State">${d.states.map(s => `<button data-rstate="${esc(s.key)}" aria-pressed="${UI.reflectState === s.key}">${esc(s.label)} <span class="faint">${s.count}</span></button>`).join('')}</div>
    <div class="kinds">${d.kinds.map(k => `<button class="card kind" data-rkind="${esc(k.key)}" aria-pressed="${UI.reflectKind === k.key}"><span class="state"><i class="dot" style="background:${toneColor(k.tone)}"></i>${esc(k.key)}</span><span class="big">${k.count}</span></button>`).join('')}</div>
    ${UI.reflectKind !== 'all' ? `<div class="toolbar"><button class="pill" data-rkind="all" aria-pressed="true">${esc(UI.reflectKind)} ${icon('x-mark', 'sm')}</button></div>` : ''}
    <div class="entries">${d.entries.length ? d.entries.map(reflectEntry).join('') : '<div class="card empty" style="min-height:120px">Nothing here.</div>'}${showMore('reflect', d.more)}</div>`)}`
};
function reflectHealth(h) {
  return `<div class="card rf-health"><div class="rf-open"><span class="big">${h.open}</span><span class="muted">open</span></div>
    <div class="rf-chart"><div class="rf-cap"><span>New each day</span><span class="muted">${esc(h.caption)}</span></div>
    <div class="rf-bars" role="img" aria-label="New entries each day: ${esc(h.caption)}">${h.bars.map(b => `<i style="height:${b.share * 100}%" title="${b.count}"></i>`).join('')}</div>
    <div class="rf-axis faint"><span>${esc(h.from)}</span><span>${esc(h.to)}</span></div></div></div>`;
}
function reflectEntry(r) {
  if (r.broken) return `<div class="card">${brokenRow(r)}</div>`;
  const acts = r.actions.map(a => `<button class="btn sm" data-rmark="${esc(r.id)}" data-to="${esc(a.state)}">${esc(a.label)}</button>`).join('');
  return `<div class="card entry rf"><div class="e-side"><span class="e-date">${esc(r.when)}</span></div>
    <div style="min-width:0"><div class="meta">${pill(r.kind, r.tone)}<span class="who">${esc(r.who)}</span><span>${esc(r.where)}</span>${r.repeats > 1 ? pill(r.repeats + ' times', 'muted') : ''}${r.came_back ? pill('came back', 'warn') : ''}</div>
    <div class="txt">${esc(r.text)}</div>${r.note ? `<div class="rf-note muted">${esc(r.note)}</div>` : ''}</div>
    <div class="acts">${r.state ? `<span class="rf-state">${esc(r.state)}</span>` : ''}${acts}</div></div>`;
}
