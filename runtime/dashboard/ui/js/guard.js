'use strict';
/* Guard: the laptop's vitals and Docker disk, the slots, what was refused today, and every rule with its switch. */

PAGE.guard = {
  url: () => '/api/guard',
  render: env => `<div class="page-h"><div><h1>Guard</h1></div></div>
    ${part(env, 'vitals', v => `<div class="gauges ${env.parts.docker ? 'five' : ''}">${v.map(gauge).join('')}${dockerCard(env.parts.docker)}</div>`)}
    <section class="section"><div class="sec-h"><div class="label">Slots</div></div>${part(env, 'slots', s => s.length ? `<div class="slots-row">${s.map(slotCard).join('')}</div>` : '<div class="card empty">No slot is set up.</div>')}</section>
    <section class="section"><div class="sec-h"><div class="label">Most refusals today</div></div>${part(env, 'refusals', refusalBars)}</section>
    <section class="section"><div class="sec-h"><div class="label">Rules ${hint('Switching a rule on or off needs your 6-digit code.')}</div></div>${part(env, 'rules', rs => `<div class="rules">${rs.map(ruleCard).join('')}</div>`)}</section>`
};
const meterHTML = (pct, tone) => pct == null ? '<div class="meter unknown"></div>' : `<div class="meter"><i style="width:${pct}%;background:${toneColor(tone)}"></i></div>`;
const gauge = g => `<div class="card gauge"><div class="label">${esc(g.label)}</div><div class="big" style="font-size:24px">${esc(g.value)}</div>${meterHTML(g.pct, g.tone)}</div>`;
function dockerCard(p) {
  if (!p) return '';
  if (!p.ok) return failedPart(p.error, 'card gauge');
  const d = p.data;
  if (!d) return `<div class="card gauge"><div class="label">Docker disk</div><div class="big faint" style="font-size:24px">unknown</div>${meterHTML(null)}</div>`;
  return `<div class="card gauge"><div class="label">Docker disk</div><div class="big" style="font-size:24px">${esc(d.used)}<small> / ${esc(d.budget)}</small></div>${meterHTML(d.pct, d.tone)}</div>`;
}
const slotCard = s => `<div class="card slot"><div style="display:flex;justify-content:space-between"><span class="label">Slot ${s.n}</span><span class="state"><i class="dot ${s.up ? 'good' : 'ring'}"></i>${s.up ? 'up' : 'free'}</span></div><b>${s.who ? esc(s.who) : ''}</b><span class="muted" style="font-size:12px">${s.note ? esc(s.note) : ''}${s.port ? ` · :${s.port}` : ''}</span></div>`;
function refusalBars(rs) {
  if (!rs.length) return '<div class="card empty">Nothing was refused today.</div>';
  const mx = Math.max(...rs.map(r => r.count)) || 1;
  return `<div class="card bars">${rs.map(r => `<div class="bar-row"><div class="name"><span class="mono" style="font-size:12px">${esc(r.rule)}</span></div><div class="hbar"><i style="width:${r.count / mx * 100}%;background:var(--accent)"></i></div><span class="cost">${r.count}</span></div>`).join('')}</div>`;
}
const ruleCard = r => r.broken ? `<div class="card rule">${brokenRow(r)}</div>` : `<div class="card rule"><div><div class="rn">${esc(r.id)}${r.hint ? ' ' + hint(r.hint, true) : ''}</div><div class="rr">${esc(r.today)}</div></div><button class="switch" role="switch" aria-checked="${r.on}" data-rule="${esc(r.id)}" data-on="${r.on}" aria-label="${esc(r.id)}" data-write></button></div>`;
function ruleToggle(id, on) {
  codeSheet(`${on ? 'Switch off' : 'Switch on'} ${id}`, `<div class="muted">${on ? 'Agents will no longer be stopped by this rule until you switch it back on.' : 'The rule applies again to every agent at once.'}</div>`, on ? 'Switch off' : 'Switch on',
    (code, key) => post(`/api/rules/${encodeURIComponent(id)}`, {on: !on, code}, key), {icon: 'shield-check'});
}
