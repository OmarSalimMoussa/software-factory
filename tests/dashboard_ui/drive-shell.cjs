'use strict';
/* The shell: every page draws at 1440, 1024 and 390 with no sideways scroll; preferences, keyboard, hints, the More sheet. */
const {drive, ready} = require('./lib.cjs');

drive('shell', async t => {
  const PAGES = ['home', 'work', 'agents', 'spend', 'library', 'settings', 'reflect', 'guard'];
  for (const [w, h] of [[1440, 900], [1024, 800]]) {
    const p = await t.open(w, h);
    t.check(await p.$$eval('[data-action="promote"]', els => els.length) === 1, `${w}: more than one Push to prod`);
    t.check((await p.textContent('#repo')) === 'acme-app', `${w}: repo name`);
    t.check(await p.$('.env-links a[href$="/commit/9f8e7d1"]'), `${w}: no commit link on prod`);
    t.check(!(await p.textContent('#page')).includes('Prototype'), `${w}: prototype words left`);
    for (const pg of PAGES) {
      await p.click(`#sidenav [data-go="${pg}"]`); await ready(p);
      await t.wide(p, `${w} ${pg}`);
      t.check(await p.evaluate(() => location.hash) === '#' + pg, `${w}: hash for ${pg}`);
      await t.shot(p, `${pg}-${w}.png`);
    }
    await p.close();
  }
  const p = await t.open(1440, 900);
  // preferences: the theme menu opens, sets dark, and closes on a click elsewhere
  await p.click('#prefs-btn'); t.check(!(await p.$eval('#prefs-menu', m => m.hidden)), 'prefs menu did not open');
  await p.click('#prefs-menu [data-theme-set="dark"]');
  t.check(await p.evaluate(() => document.documentElement.dataset.theme) === 'dark', 'theme not set');
  await p.mouse.click(300, 30); t.check(await p.$eval('#prefs-menu', m => m.hidden), 'prefs menu did not close');
  await t.shot(p, 'home-1440-dark.png');
  await p.click('#prefs-btn'); await p.click('#prefs-menu [data-theme-set="light"]'); await p.mouse.click(300, 30);
  // keyboard: g then a letter
  await p.keyboard.press('g'); await p.keyboard.press('s'); await ready(p);
  t.check(await p.evaluate(() => location.hash) === '#spend', 'g s did not open Spend');
  await p.click('#prefs-btn'); await p.click('[data-action="help"]'); await p.waitForTimeout(350);
  t.check((await p.textContent('#sheet-title')) === 'Keyboard shortcuts', 'no shortcuts sheet');
  await p.keyboard.press('Escape'); await p.waitForTimeout(350);
  // every hint on Settings opens inside its column, with no sideways scroll
  await p.click('#sidenav [data-go="settings"]'); await ready(p);
  for (const hb of (await p.$$('.hint-btn')).slice(0, 30)) {
    await hb.click(); await p.waitForTimeout(20);
    const [pl, pr, ml, mr] = await p.evaluate(() => { const pop = document.querySelector('.hint-pop:not([hidden])'), m = document.querySelector('#main').getBoundingClientRect(); const r = pop.getBoundingClientRect(); return [r.left, r.right, m.left, m.right]; });
    if (pl < ml || pr > mr) t.errors.push(`hint outside its column: ${pl}-${pr} vs ${ml}-${mr}`);
    await t.wide(p, 'hint open'); await p.keyboard.press('Escape');
  }
  const hb = await p.$eval('.hint-btn', e => { const r = e.getBoundingClientRect(); return [r.width, r.height]; }); t.check(hb[0] >= 24 && hb[1] >= 24, 'hint target under 24 px');
  await p.close();
  // phone: the bottom bar, the More sheet, every page without sideways scroll
  const m = await t.open(390, 844);
  t.check(!(await m.isVisible('#sidenav')), 'phone: sidebar shows');
  for (const pg of ['work', 'agents', 'spend', 'home']) { await m.click(`#bottomnav [data-go="${pg}"]`); await ready(m); await t.wide(m, '390 ' + pg); await t.shot(m, `${pg}-390.png`); }
  for (const pg of ['library', 'settings', 'reflect', 'guard']) {
    await m.click('#bottomnav [data-go="more"]'); await m.waitForTimeout(350);
    await m.click(`#sheet [data-go="${pg}"]`); await ready(m); await t.wide(m, '390 ' + pg); await t.shot(m, `${pg}-390.png`);
  }
  await m.click('.hint-btn'); await t.wide(m, '390 hint');
});
