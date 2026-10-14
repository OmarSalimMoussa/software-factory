'use strict';
/* Spend: the plan's own limits first, then dollars at API prices against the daily budget, by family, agent, task and model. */

PAGE.spend = {
  url: () => `/api/spend?range=${UI.spendRange}&slice=${encodeURIComponent(UI.sliceKey)}`,
  load: url => UI.slicePages > 1 ? loadPaged(url, UI.slicePages, 'slice') : fetchEnv('page', url),
  render: env => {
    const sl = partData(env, 'slice');
    return `<div class="page-h">${ph('Spend', 'The plan limits are what really limits the factory: they come from the orchestrator after each reply. The dollars below are what each call would cost at API prices, priced from the agents’ transcripts. A call to a model with no price set counts as unknown.')}
    <div class="seg" role="group" aria-label="Range">${[[1, 'Today'], [7, '7 days'], [14, '14 days']].map(([d, l]) => `<button data-range="${d}" aria-pressed="${UI.spendRange === d}">${l}</button>`).join('')}</div></div>
    ${part(env, 'plan', planCard)}
    <div class="spend-hero">${part(env, 'hero', heroHTML)}${part(env, 'chart', chartHTML)}</div>
    ${part(env, 'tokens', tokensHTML)}
    <section class="section"><div class="sec-h">${sl ? `<div class="tabs" role="tablist" style="border:0">${sl.tabs.map(([k, l]) => `<button role="tab" data-slice="${esc(k)}" aria-selected="${UI.sliceKey === k}">${esc(l)}</button>`).join('')}</div>` : ''}</div>
    ${part(env, 'slice', s => `<div class="card bars">${s.rows.length ? barList(s.rows) : '<div class="empty">Nothing was spent in this range.</div>'}${showMore('slice', s.more)}</div>`)}</section>`;
  }
};
/* The windows come from the orchestrator's status line; a stale reading arrives without numbers and says how old it is. */
function planCard(P) {
  const w = x => x.pct == null
    ? `<div class="plan-w"><div class="pw-top"><span class="name">${esc(x.name)}</span><span class="pct faint">unknown</span></div><div class="plan-meter unknown"></div><div class="when">${esc(x.note || '')}</div></div>`
    : `<div class="plan-w"><div class="pw-top"><span class="name">${esc(x.name)}</span><span class="pct">${Math.round(x.pct)}%</span></div><div class="plan-meter" role="img" aria-label="${esc(x.name)}: ${Math.round(x.pct)}% used"><i class="${esc(x.tone || '')}" style="width:${Math.min(100, x.pct)}%"></i></div><div class="when">${esc(x.resets)}</div></div>`;
  return `<div class="card plan" id="plan"><div class="who"><div class="label">Plan limits ${hint('The plan’s own windows, the same numbers /usage shows. The orchestrator reads them after each reply.')}</div><small>${esc(P.age)}</small></div>${P.windows.map(w).join('')}</div>`;
}
function heroHTML(h) {
  const pct = h.budget_pct;
  return `<div class="card hero-num"><div class="label">${esc(h.label)}</div><div class="big" id="spend-total">${money(h.total)}</div>
    ${pct == null ? '<div class="budget meter unknown"></div>' : `<div class="budget"><i style="width:${Math.min(100, pct)}%"></i></div>`}<div class="muted" style="display:flex;justify-content:space-between"><span>${esc(h.today)}</span><span>${pct == null ? 'unknown' : pct + '%'}</span></div>
    <div class="kv" style="grid-template-columns:1fr 1fr">${h.kv.map(kvBox).join('')}</div>
    ${h.unknown ? `<div class="unknown">${icon('exclamation-triangle', 'sm')}${esc(h.unknown)}</div>` : ''}</div>`;
}
function chartHTML(c) {
  return `<div class="card chart"><div class="sec-h"><div class="label">By family, per day</div><div class="legend">${c.families.map(f => `<span><i style="background:${cv(f.color)}"></i>${esc(f.label)}</span>`).join('')}<span><i style="background:none;border-top:2px dashed var(--warn);height:0;border-radius:0"></i>${esc(c.budget_label)}</span></div></div>${stackChart(c)}</div>`;
}
/* Segments are square; each bar is clipped once to a shape rounded only at its top, so the joins between families stay flat. */
function stackChart(c) {
  const W = 640, H = 220, pl = 56, pb = 24, pt = 8, mx = c.max || 1, days = c.days, r = 3;
  const bw = (W - pl) / Math.max(1, days.length), y = v => pt + (H - pt - pb) * (1 - v / mx);
  const ticks = c.ticks.map(v => `<line class="gridl" x1="${pl}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text x="${pl - 8}" y="${y(v) + 4}" text-anchor="end">$${v}</text>`).join('');
  const bars = days.map((d, i) => {
    const x = pl + i * bw + bw * .18, w = bw * .64, top = y(d.total), base = y(0), rr = Math.min(r, (base - top) / 2);
    let acc = 0;
    const segs = d.values.map((v, j) => { const y0 = y(acc), y1 = y(acc + v); acc += v; return `<rect x="${x}" y="${y1}" width="${w}" height="${Math.max(0, y0 - y1)}" fill="${cv(c.families[j].color)}"><title>${esc(c.families[j].label)}: $${v.toFixed(2)}</title></rect>`; }).join('');
    const unk = d.unknown ? `<text class="unk" x="${x + w / 2}" y="${top - 4}" text-anchor="middle">?<title>${esc(d.unknown)}</title></text>` : '';
    return `<clipPath id="sbar-${i}"><path d="M${x} ${base}V${top + rr}Q${x} ${top} ${x + rr} ${top}H${x + w - rr}Q${x + w} ${top} ${x + w} ${top + rr}V${base}Z"/></clipPath><g clip-path="url(#sbar-${i})">${segs}</g>${unk}`;
  }).join('');
  const labels = days.map((d, i) => d.label ? `<text x="${pl + i * bw + bw / 2}" y="${H - 6}" text-anchor="middle">${esc(d.label)}</text>` : '').join('');
  const budget = c.budget == null ? '' : `<line x1="${pl}" x2="${W}" y1="${y(c.budget)}" y2="${y(c.budget)}" stroke="var(--warn)" stroke-dasharray="4 4"/><text x="${pl - 8}" y="${y(c.budget) + 4}" text-anchor="end" style="fill:var(--warn);font-weight:600">$${c.budget}</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Spend per day by family, with the daily budget">${ticks}${bars}${budget}${labels}</svg>`;
}
function barList(rows) {
  const mx = Math.max(0, ...rows.filter(r => !r.broken && r.value != null).map(r => r.value)) || 1;
  return rows.map(r => r.broken ? brokenRow(r) : `<div class="bar-row"><div class="name"><i class="dot" style="background:${cv(r.color)}"></i><span>${esc(r.name)}</span>${r.tag ? chip(r.tag) : ''}</div><div class="hbar">${r.value == null ? '' : `<i style="width:${r.value / mx * 100}%;background:${cv(r.color)}"></i>`}</div><span class="cost">${money(r.value)}</span>${r.detail ? `<div class="bar-detail faint">${esc(r.detail)}</div>` : ''}</div>`).join('');
}
/* What the range's input was made of, the cache's share first: the server counts and words it all. */
function tokensHTML(t) {
  return `<div class="card tokens"><div class="sec-h"><div class="label">${esc(t.label)}</div><span class="muted">${esc(t.hits || t.calls)}</span></div>
    <div class="tok-stats">${t.stats.map(s => `<div class="tok"><span class="k"><i class="tok-${esc(s.key)}"></i>${esc(s.label)}</span><span class="v">${esc(s.value)}</span></div>`).join('')}</div>
    <div class="tok-bar" role="img" aria-label="${esc(t.hits || t.calls)}">${t.bar.map(b => `<i class="tok-${esc(b.key)}" style="width:${b.pct}%"></i>`).join('')}</div></div>`;
}
