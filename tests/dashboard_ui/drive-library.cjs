'use strict';
/* Library: resizable panes kept in the browser, server-side filters, the reader with front matter, tables and
   links, Show more, and the phone reader in a panel. */
const {drive, ready, settle, texts} = require('./lib.cjs');

drive('library', async t => {
  const p = await t.open(1440, 900, 'library');
  const w = sel => p.$eval(sel, e => Math.round(e.getBoundingClientRect().width));
  const drag = async (sel, dx) => { const r = await p.$eval(sel, e => { const b = e.getBoundingClientRect(); return [b.x + b.width / 2, b.y + b.height / 2]; }); await p.mouse.move(r[0], r[1]); await p.mouse.down(); await p.mouse.move(r[0] + dx / 2, r[1], {steps: 4}); await p.mouse.move(r[0] + dx, r[1], {steps: 4}); await p.mouse.up(); await settle(p); };
  const f0 = await w('.filters'), r0 = await w('.results');
  await drag('[data-split="r"]', 120);
  const r1 = await w('.results'); t.check(Math.abs(r1 - r0 - 120) <= 3, `results did not grow by 120: ${r0} -> ${r1}`);
  await drag('[data-split="f"]', 60);
  const f1 = await w('.filters'); t.check(Math.abs(f1 - f0 - 60) <= 3, `filters did not grow by 60: ${f0} -> ${f1}`);
  t.check(Math.abs(await w('.results') - r1) <= 3, 'results moved when the filters grew');
  await t.shot(p, 'library-resized-1440.png');
  await p.click('[data-librange="d7"]'); await ready(p);
  t.check(Math.abs(await w('.results') - r1) <= 3, 'size lost on re-render');
  await p.reload(); await ready(p);
  t.check(Math.abs(await w('.results') - r1) <= 3 && Math.abs(await w('.filters') - f1) <= 3, 'sizes not kept after a reload');
  await p.focus('[data-split="r"]'); for (let i = 0; i < 60; i++) await p.keyboard.press('Shift+ArrowRight');
  t.check(await w('.reader') >= 318, 'reader squeezed under 320: ' + await w('.reader'));
  await p.dblclick('[data-split="r"]'); await settle(p);
  t.check(Math.abs(await w('.results') - r0) <= 3, `reset failed: ${await w('.results')} vs ${r0}`);
  // the server filters: a status from the dropdown, then the reader shows the check's table with its alignment
  await p.click('.filters .fgroup:nth-of-type(3) [data-ddbtn]'); await p.click('[data-ddopt][data-ddkind="lib-status"][data-v="fail"]'); await settle(p);
  t.check((await texts(p, '.results .group-h .label'))[0] === '2 files', 'status filter not applied: ' + await texts(p, '.results .group-h .label'));
  await p.waitForSelector('#reader table');
  t.check((await p.$$eval('#reader th', els => els.map(e => e.style.textAlign))).join() === 'left,center,right', 'table alignment lost');
  t.check(await p.$('#reader .fm-grid .chip.bad'), 'result chip not coloured');
  const lastLib = async () => (await t.srv.calls()).filter(c => c.path === '/api/library').at(-1).query;
  t.check((await lastLib()).status === 'fail', 'the status filter was not sent');
  await p.click('[data-action="lib-reset"]'); await settle(p);
  await p.uncheck('#kind-request'); await settle(p);
  t.check(!(await texts(p, '.res .ts .chip.mono')).some(x => x.startsWith('r-')), 'unchecked requests still listed');
  t.check(!(await lastLib()).kinds.includes('request'), 'kinds not sent');
  await p.check('#kind-request'); await settle(p);
  // selecting a file changes only the reader; a coloured front-matter chip; links open other files, never this one
  const top = await p.evaluate(() => document.querySelector('#main').scrollTop);
  await p.$eval('[data-libsel="t-0017"]', e => e.click()).catch(async () => { await p.click('[data-more="library"]'); await settle(p); await p.$eval('[data-libsel="t-0017"]', e => e.click()); });
  await p.waitForFunction(() => document.querySelector('#reader .dh-kind') && document.querySelector('#reader').textContent.includes('t-0017'));
  t.check(await p.$('#reader .fm-grid .chip.warn'), 'severity not coloured');
  t.check(!(await p.$('#reader [data-open-doc="t-0017"]')), 'the task file links to itself');
  t.check(await p.evaluate(() => document.querySelector('#main').scrollTop) === top, 'the page moved when a file was picked');
  // Show more loads the next page of results
  await p.click('[data-action="lib-reset"]'); await settle(p);
  t.check((await p.$$('.res')).length === 25, 'first page of results should hold 25');
  await p.click('[data-more="library"]'); await settle(p);
  t.check((await p.$$('.res')).length === 50, 'Show more did not add a page');
  // a spec carries the link to its brief
  await p.evaluate(() => openDoc('s-0012')); await settle(p);
  t.check(await p.$('#reader .spec-full [data-brief^="/api/brief/"]'), 'spec has no brief link');
  // narrower: one divider and no sideways scroll; phone: none, and the reader opens in a panel
  await p.setViewportSize({width: 1100, height: 800}); await settle(p);
  t.check(!(await p.$eval('[data-split="f"]', e => !!e.offsetParent)), 'filters divider shown at 1100');
  await t.wide(p, '1100 library');
  await p.setViewportSize({width: 390, height: 844}); await settle(p);
  t.check(!(await p.evaluate(() => [...document.querySelectorAll('[data-split]')].some(e => e.offsetParent))), 'dividers shown on a phone');
  await p.$eval('.res', e => e.click()); await settle(p);
  t.check(await p.$eval('#sheet', e => e.classList.contains('open')), 'phone: the reader did not open in a panel');
  await t.wide(p, '390 library');
});
