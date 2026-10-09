'use strict';
/* Stacked panels: the one underneath steps 28 px left and moves smoothly; a link to a panel already in the stack
   returns to it; the exposed edge brings a panel back; no document links to itself; a gone document says so. */
const {drive, ready} = require('./lib.cjs');

drive('stack', async t => {
  const p = await t.open(1440, 900, 'work');
  await p.click('[data-worktab="requests"]'); await ready(p);
  await p.$eval('.req-row[data-open-doc="r-1024"]', e => e.click()); await p.waitForTimeout(500);
  t.check(!(await p.$('#sheet-body [data-open-doc="r-1024"]')), 'the open request links to itself');
  t.check(await p.$('#sheet-body [data-open-doc="t-0017"]'), 'no link to the task');
  const sheetLeft = await p.$eval('#sheet', e => e.getBoundingClientRect().left);
  await p.click('#sheet-body [data-open-doc="t-0017"]'); await p.waitForTimeout(90);
  const midLeft = await p.$eval('.sheet-ghost', e => e.getBoundingClientRect().left); await p.waitForTimeout(450);
  const ghostLeft = await p.$eval('.sheet-ghost', e => e.getBoundingClientRect().left);
  t.check(Math.round(sheetLeft - ghostLeft) === 28, `the panel underneath should sit 28 px left, got ${sheetLeft - ghostLeft}`);
  t.check(midLeft > ghostLeft, 'the panel underneath did not move smoothly');
  t.check(!(await p.$('#sheet-body [data-open-doc="t-0017"]')), 'the open task file links to itself');
  t.check(await p.$$eval('[id="sheet-body"]', els => els.length) === 1, 'duplicate panel ids');
  await t.shot(p, 'stack-1-1440.png');
  const reqLink = await p.$eval('#sheet-body [data-open-doc^="r-"]:not([data-open-doc="r-1024"])', e => e.dataset.openDoc);
  await p.click(`#sheet-body [data-open-doc="${reqLink}"]`); await p.waitForTimeout(500);
  const depths = await p.$$eval('.sheet-ghost', gs => gs.map(g => g.style.getPropertyValue('--d')));
  t.check(depths.join() === '2,1', 'ghost depths: ' + depths);
  await t.shot(p, 'stack-2-1440.png');
  await p.click('#sheet-body [data-open-doc="t-0017"]'); await p.waitForTimeout(450);
  t.check((await p.$$('.sheet-ghost')).length === 1 && (await p.textContent('#sheet-title')).includes('Sign-in sends people back'), 're-opening a stacked panel did not return to it');
  await p.mouse.click(Math.round(ghostLeft) + 8, 450); await p.waitForTimeout(450);
  t.check((await p.$$('.sheet-ghost')).length === 0 && (await p.textContent('#sheet-title')).includes('Sign in keeps looping'), 'the edge did not bring the panel back: ' + await p.textContent('#sheet-title'));
  await p.click('#sheet-body [data-open-doc="t-0017"]'); await p.waitForTimeout(450); await p.click('#sheet-x'); await p.waitForTimeout(450);
  t.check(!(await p.$$eval('.sheet-ghost', gs => gs.length)), 'layers left after closing');
  // an open panel refreshes on the poll, by its key
  await p.$eval('[data-open-doc="r-1024"]', e => e.click()); await p.waitForTimeout(400);
  await p.$eval('#sheet-body [data-open-doc="t-0017"]', e => e.click()); await p.waitForTimeout(400);
  const n0 = (await t.srv.calls()).filter(c => c.path === '/api/doc/t-0017').length;
  await p.evaluate(() => poll()); await p.waitForTimeout(400);
  t.check((await t.srv.calls()).filter(c => c.path === '/api/doc/t-0017').length === n0 + 1, 'the open panel was not refreshed by the poll');
  await p.keyboard.press('Escape'); await p.keyboard.press('Escape'); await p.waitForTimeout(400);
  // a document that is gone opens and says so
  await p.evaluate(() => openDoc('r-9999')); await p.waitForTimeout(400);
  t.check((await p.textContent('#sheet-body')).includes('This file is gone.'), 'a gone document did not say so');
  await p.close();
  const m = await t.open(390, 844, 'work');
  await m.click('[data-worktab="requests"]'); await ready(m);
  await m.$eval('.req-row[data-open-doc="r-1024"]', e => e.click()); await m.waitForTimeout(450);
  await m.click('#sheet-body [data-open-doc="t-0017"]'); await m.waitForTimeout(500);
  const [st, gt] = await m.evaluate(() => [document.querySelector('#sheet').getBoundingClientRect().top, document.querySelector('.sheet-ghost').getBoundingClientRect().top]);
  t.check(gt < st, 'phone: the panel underneath is not visible above');
  await t.wide(m, 'phone stack');
  await t.shot(m, 'stack-390.png');
});
