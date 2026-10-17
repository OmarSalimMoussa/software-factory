'use strict';
/* Self-reflect: the open count and the bars per day, Open as the first view, an entry marked fixed leaving it for
   Fixed with Reopen, and the state filter and the kind filter together. */
const {drive, settle, until} = require('./lib.cjs');

drive('reflect', async t => {
  const p = await t.open(1440, 900, 'reflect');
  const open = () => p.textContent('.rf-open .big');
  await t.shot(p, 'reflect.png');
  t.check(await open() === '30' && (await p.$$('.rf-bars i')).length === 14, 'no open count or no bar per day');
  t.check(await p.$('[data-rstate="open"][aria-pressed="true"]') && await p.$('.entry.rf .chip:text-is("came back")'), 'Open is not the first view, or nothing came back');
  const first = await p.getAttribute('.entry.rf [data-to="fixed"]', 'data-rmark');
  await p.click(`.entry.rf [data-rmark="${first}"][data-to="fixed"]`);
  t.check(await until(p, n => document.querySelector('.rf-open .big')?.textContent === n, '29') && !(await p.$(`[data-rmark="${first}"]`)), 'a fixed entry stayed open');
  await p.click('[data-rstate="fixed"]'); await settle(p);
  t.check(await p.$(`[data-rmark="${first}"][data-to="open"]`) && (await p.textContent('.entry.rf .rf-state')) === 'Fixed', 'Fixed does not list it with Reopen');
  await p.click('[data-rkind="miss"]'); await settle(p);
  t.check((await p.$$('.entry.rf')).length <= 1, 'the kind filter shows entries of another state');
  await t.wide(p, 'reflect');
  await p.close();
});
