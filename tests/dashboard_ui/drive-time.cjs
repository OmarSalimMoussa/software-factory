'use strict';
/* Time: wall time per step and runtime in cards of their own, the merges' wait, the slowest tasks and the tools' time
   one click away, and a task's own timeline beside its runtime, unknown where it was not measured. */
const {drive, settle, texts} = require('./lib.cjs');

drive('time', async t => {
  const p = await t.open(1440, 900, 'time');
  const steps = await texts(p, '#time-wall .bar-row .name');
  t.check(steps.length === 11 && steps[0] === 'Approved, until on staging', 'wall steps: ' + steps.join(' | '));
  t.check((await p.textContent('#time-wall .bar-row.total .cost')) === '10 h 6 min', 'end-to-end median not drawn');
  t.check((await p.textContent('#time-wall')).includes('no task yet'), 'a step no task reached is not said');
  const wall = await p.textContent('#time-wall'), run = await p.textContent('#time-run');
  t.check(!wall.includes('Model thinking') && run.includes('Model thinking') && run.includes('Testers and critics'), 'wall time and runtime are not apart');
  t.check((await p.textContent('#time-checks')).includes('2 of 4 merges waited through a failed or blocked check'), 'merge checks not told');
  await t.shot(p, 'time-1440.png');
  await p.click('[data-time-list="slowest"]'); await settle(p);
  t.check((await p.textContent('#sheet-title')) === 'Slowest tasks', 'slowest tasks did not open');
  const slow = await texts(p, '#sheet-body .bar-row .cost');
  t.check(slow.join(',') === '1 d 0 h,13 h 36 min,6 h 36 min,6 h 6 min', 'slowest not slowest first: ' + slow.join(','));
  await t.shot(p, 'time-slowest-1440.png');
  await p.click('#sheet-body [data-task="t-0012"]'); await settle(p);
  t.check(await p.isVisible('#sheet-back') && (await p.textContent('#task-time')).includes('1 d 0 h from approved to on staging'), 'a slow task did not open with its timeline');
  t.check((await p.textContent('#task-runtime')).includes('Designer and implementer'), 'a task does not show its runtime');
  await p.$eval('#task-time', e => e.scrollIntoView()); await t.shot(p, 'time-task-1440.png');
  await p.click('#sheet-back'); await settle(p);
  await p.click('[data-action="close-sheet"]'); await settle(p);
  await p.click('[data-time-list="tools"]'); await settle(p);
  t.check((await texts(p, '#sheet-body .bar-row .name'))[0] === 'browser_take_screenshot', 'tool time not biggest first');
  await p.click('[data-action="close-sheet"]'); await settle(p);
  await p.evaluate(() => openTask('t-0015')); await settle(p);
  t.check((await p.textContent('#task-runtime')).includes('Unknown'), 'a task never measured does not say its runtime is unknown');
  await p.close();
  const m = await t.open(390, 844, 'time');
  await t.wide(m, '390 time');
  await m.click('[data-time-list="slowest"]'); await settle(m);
  await t.wide(m, '390 slowest');
  await t.shot(m, 'time-390.png');
});
