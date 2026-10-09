'use strict';
/* The search bar: one on every page, / focuses it, arrows and Enter pick, a setting jumps to its row, see-all goes to
   Library with the query shown; it holds still while the live counter changes; on a phone it opens over the top row. */
const {drive, ready} = require('./lib.cjs');

drive('search', async t => {
  const p = await t.open(1440, 900);
  const results = () => p.waitForFunction(() => !document.querySelector('#gs-pop').hidden, null, {timeout: 3000});
  for (const pg of ['home', 'work', 'agents', 'spend', 'library', 'settings', 'reflect', 'guard']) {
    await p.click(`#sidenav [data-go="${pg}"]`); await ready(p);
    t.check(!(await p.$$eval('#page input', els => els.filter(e => /search/i.test(e.placeholder + e.getAttribute('aria-label'))).length)), 'a page search box on ' + pg);
    t.check(await p.isVisible('#gq'), 'search bar missing on ' + pg);
  }
  t.check(!(await p.isVisible('#gs-open')) && !(await p.isVisible('.gs-close')), 'phone-only search buttons show on a laptop');
  await p.keyboard.press('/'); t.check(await p.evaluate(() => document.activeElement.id) === 'gq', '/ did not focus search');
  await p.keyboard.type('t-0017'); await results();
  t.check((await p.$$eval('#gs-pop mark', m => m.length)) > 0, 'matches are not marked');
  await t.shot(p, 'search-1440.png');
  await p.keyboard.press('Enter'); await p.waitForTimeout(500);
  t.check((await p.textContent('#sheet-title')).includes('Sign-in sends people back'), 'Enter did not open the task: ' + await p.textContent('#sheet-title'));
  await p.keyboard.press('Escape'); await p.waitForTimeout(350);
  await p.click('#gq'); await p.keyboard.type('qa rounds'); await results();
  const items = await p.$$eval('.gs-item .t', els => els.map(e => e.textContent)); const at = items.findIndex(x => x === 'QA rounds');
  t.check(at >= 0, 'setting not found: ' + items);
  for (let i = 0; i < at; i++) await p.keyboard.press('ArrowDown');
  await p.keyboard.press('Enter'); await ready(p); await p.waitForTimeout(200);
  t.check(await p.evaluate(() => location.hash) === '#settings' && await p.$('[data-row="rounds.qa"].flash'), 'setting jump failed');
  t.check((await p.textContent('.backlink')).includes('Back to'), 'no back link after a search jump');
  await p.click('#gq'); await p.keyboard.type('qa-visual'); await results();
  t.check((await p.textContent('#gs-pop')).toLowerCase().includes('agents'), 'agent group missing');
  await p.keyboard.press('Escape'); t.check(await p.$eval('#gs-pop', e => e.hidden), 'Escape did not close the list');
  await p.click('#gq'); await p.keyboard.type('r-2026'); await results();
  const all = await p.$('.gs-all');
  if (!all) t.errors.push('no see-all row'); else {
    const n = +(await all.textContent()).match(/\d+/)[0]; await all.click(); await ready(p); await p.waitForTimeout(300);
    const shown = +(await p.textContent('.results .group-h')).match(/\d+/)[0]; t.check(shown === n, `Library shows ${shown}, search said ${n}`);
    t.check(await p.$('.qchip'), 'query chip missing');
    await p.click('[data-action="lib-clear-q"]'); await p.waitForTimeout(500); t.check(!(await p.$('.qchip')), 'query not cleared');
  }
  await p.click('#prefs-btn'); await p.waitForTimeout(80); await p.click('#gq'); await p.waitForTimeout(80);
  t.check(await p.$eval('#prefs-menu', m => m.hidden), 'a menu stayed open after a click in search');
  await p.keyboard.type('zzzz'); await results();
  t.check((await p.textContent('#gs-pop')).includes('Nothing matches'), 'no empty message');
  await p.keyboard.press('Escape');
  // the bar holds still while the live counter changes
  const box = () => p.$eval('.gs-field', e => { const r = e.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.width)].join(); });
  const b0 = await box(); await p.evaluate(() => { UI.lastOk = Date.now() - 37000; }); await p.waitForTimeout(1100);
  t.check(/Updated \d+ s ago/.test(await p.textContent('#live-text')), 'counter did not change');
  const b1 = await box(); await p.evaluate(() => { UI.lastOk = Date.now() - 125000; }); await p.waitForTimeout(1100);
  t.check(await p.$('#live.stale'), 'old data not marked amber');
  t.check(b0 === b1 && b1 === await box(), 'search bar moved');
  await p.close();
  const m = await t.open(390, 844);
  t.check(!(await m.isVisible('#gq')), 'phone: field should start hidden');
  await m.click('#gs-open'); t.check(await m.isVisible('#gq'), 'phone: field did not open');
  await m.keyboard.type('billing'); await m.waitForFunction(() => !document.querySelector('#gs-pop').hidden);
  const r = await m.$eval('#gs-pop', e => { const b = e.getBoundingClientRect(); return [b.left, b.right]; }); t.check(r[0] >= 0 && r[1] <= 390, 'phone: list off screen ' + r);
  await t.wide(m, 'phone search');
  const over = await m.evaluate(() => { const b = document.querySelector('#gq').getBoundingClientRect(); const el = document.elementFromPoint(b.right - 4, b.top + b.height / 2); return el && el.closest('#gsearch') ? 'ok' : (el ? el.className : 'none'); });
  t.check(over === 'ok', 'phone: something draws over the field: ' + over);
  await t.shot(m, 'search-390.png');
  await m.click('[data-action="gs-close"]'); t.check(!(await m.isVisible('#gq')), 'phone: search did not close');
});
