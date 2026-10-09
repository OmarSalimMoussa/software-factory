'use strict';
/* Self-reflect: what agents wrote about the factory itself. Read-only here; the owner fixes the factory by hand. */

PAGE.reflect = {
  url: () => `/api/reflect?kind=${encodeURIComponent(UI.reflectKind)}`,
  load: url => UI.reflectPages > 1 ? loadPaged(url, UI.reflectPages, 'reflect', 'entries') : fetchEnv('page', url),
  render: env => `<div class="page-h">${ph('Self-reflect', 'What agents wrote about the factory itself. Agents can only add entries; nobody edits them here. You change the factory by hand, and only while it is fully stopped with every agent down.')}</div>
    <div class="ro-banner"><span class="ico">${icon('lock-closed')}</span><div style="flex:1;min-width:200px"><b>Read-only.</b> <span class="muted">Fix these by hand, only with the factory stopped and every agent down.</span></div></div>
    ${part(env, 'reflect', d => `<div class="kinds">${d.kinds.map(k => `<button class="card kind" data-rkind="${esc(k.key)}" aria-pressed="${UI.reflectKind === k.key}"><span class="state"><i class="dot" style="background:${toneColor(k.tone)}"></i>${esc(k.key)}</span><span class="big">${k.count}</span></button>`).join('')}</div>
    ${UI.reflectKind !== 'all' ? `<div class="toolbar"><button class="pill" data-rkind="all" aria-pressed="true">${esc(UI.reflectKind)} ${icon('x-mark', 'sm')}</button></div>` : ''}
    <div class="entries">${d.entries.length ? d.entries.map(r => r.broken ? `<div class="card">${brokenRow(r)}</div>` : `<div class="card entry ro"><div class="e-side"><span class="e-date">${esc(r.when)}</span></div><div style="min-width:0"><div class="meta">${pill(r.kind, r.tone)}<span class="who">${esc(r.who)}</span><span>${esc(r.where)}</span>${r.repeats > 1 ? pill(r.repeats + ' times', 'muted') : ''}</div><div class="txt">${esc(r.text)}</div></div></div>`).join('') : '<div class="card empty" style="min-height:120px">Nothing yet.</div>'}${showMore('reflect', d.more)}</div>`)}`
};
