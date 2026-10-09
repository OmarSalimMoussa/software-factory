'use strict';
/* Spend: plan limits with reset time and age (warning colours near the limit, unknown when stale), the stacked chart
   with square joins and the budget on the axis, ranges and slices with an unknown price. */
const {drive, ready} = require('./lib.cjs');

drive('spend', async t => {
  const p = await t.open(1440, 900, 'spend');
  const plan = await p.textContent('#plan');
  t.check(/5-hour window\s*62%/.test(plan) && /This week\s*41%/.test(plan), 'plan numbers missing: ' + plan.slice(0, 160));
  t.check(plan.includes('resets in 2 h') && plan.includes('read 3 min ago'), 'no reset time or reading age');
  const svg = '.chart svg[aria-label^="Spend per day"]';
  t.check(!(await p.$$eval(`${svg} rect[rx]`, els => els.length)), 'segments are rounded');
  t.check((await p.$$eval(`${svg} clipPath`, els => els.length)) === 14, 'bars not clipped');
  const [ly, tickX, budX] = await p.$eval(svg, s => { const tx = [...s.querySelectorAll('text')]; const bud = tx.find(x => x.style.fill.includes('warn')); const tick = tx.find(x => x.textContent === '$0'); return [+bud.getAttribute('y'), +tick.getAttribute('x'), +bud.getAttribute('x')]; });
  t.check(ly >= 24, 'budget label at the top edge: y=' + ly);
  t.check(tickX === budX, 'budget label not in line with the axis labels');
  t.check((await p.textContent('.hero-num')).includes('2 calls with unknown price'), 'unknown-price calls not said');
  await t.shot(p, 'spend-1440.png');
  await p.click('[data-range="7"]'); await p.waitForTimeout(500);
  t.check((await p.textContent('.hero-num .label')) === 'Last 7 days' && (await p.$$eval(`${svg} clipPath`, els => els.length)) === 7, 'range 7 not drawn');
  await p.click('[data-range="1"]'); await p.waitForTimeout(500);
  t.check((await p.textContent('.hero-num .label')) === 'Today', 'range Today not drawn');
  // agents that spent in the last 14 days fill more than one page
  await p.click('[data-range="14"]'); await p.waitForTimeout(500);
  await p.click('[data-slice="agent"]'); await p.waitForTimeout(500);
  t.check((await p.$$('.bars .bar-row')).length === 25 && await p.$('[data-more="slice"]'), 'agent slice first page wrong');
  await p.click('[data-more="slice"]'); await p.waitForTimeout(500);
  t.check((await p.$$('.bars .bar-row')).length > 25, 'slice Show more failed');
  await p.click('[data-slice="model"]'); await p.waitForTimeout(500);
  t.check((await p.textContent('.bars')).includes('unknown'), 'a model with no price is not shown as unknown');
  await p.close();
  await t.srv.set({scenario: 'near-limit'});
  const n = await t.open(1440, 900, 'spend');
  t.check(await n.$('#plan .plan-meter i.warn') && await n.$('#plan .plan-meter i.bad'), 'no warning colours near the limit');
  await n.close();
  await t.srv.set({scenario: 'stale-plan'});
  const s = await t.open(1440, 900, 'spend');
  const st = await s.textContent('#plan');
  t.check(!/\d+%/.test(st) && st.includes('unknown') && st.includes('No reading for 52 min'), 'a stale reading still shows numbers: ' + st.slice(0, 160));
  await t.shot(s, 'spend-stale-1440.png');
  await s.close();
  const m = await t.open(390, 844, 'spend');
  await t.wide(m, '390 spend');
});
