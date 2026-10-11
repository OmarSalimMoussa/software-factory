'use strict';
/* The install console: its one-time link admits this browser alone; a dry install draws every step as a row, asks each
   question in place under its step (the 6-digit code's secret, the GitHub and Claude tokens), shows the keys to add by
   hand as the installer's own lines, keeps a typed secret off the page, and finishes; at desktop and phone width, in
   both themes, with 24 px targets. */
const {drive, consoleServer} = require('./lib.cjs');

const SECRET = 'sk-ant-oat01-typed-in-the-console-5e5e';
const ASKED = ['authenticator', 'GitHub token', 'claude setup-token', 'name for this Claude account'];

async function dryInstall(t, p, tag) {
  if (!(await p.isChecked('#f-dry'))) await p.click('.cf-switch');
  t.check((await p.textContent('#f-go')) === 'Dry run', `${tag}: the button does not say Dry run`);
  await p.click('#f-go');
  await p.waitForSelector('.crow', {timeout: 8000});
  const asked = [];
  while (asked.length < 10) {
    await p.waitForSelector('.cq, .cend', {timeout: 20000});
    if (await p.$('.cend')) break;
    const title = await p.textContent('#q-title');
    asked.push(title);
    t.check(await p.$('.crow[data-state="running"] .cq'), `${tag}: "${title}" is not drawn under the step that asks it`);
    if (await p.$('#q-in')) {
      await p.fill('#q-in', (await p.getAttribute('#q-in', 'type')) === 'password' ? SECRET : '');
      await p.press('#q-in', 'Enter');
    } else {
      if (asked.length === 1) {
        await p.click('.cq [data-copy]');
        await p.waitForFunction(() => document.querySelector('.cq [data-copy]').textContent === 'Copied', null, {timeout: 3000})
          .catch(async () => t.errors.push(`${tag}: Copy did not copy: ${await p.textContent('#toasts')}`));
      }
      await p.click('.cq .btn.primary');
    }
  }
  t.check(ASKED.every((want, i) => (asked[i] || '').includes(want)) && asked.length === ASKED.length, `${tag}: asked ${JSON.stringify(asked)}`);
  await p.waitForSelector('.cend', {timeout: 20000});
  t.check((await p.textContent('.cend')).includes('The dry run finished. Nothing changed.'), `${tag}: the run did not finish: ${await p.textContent('.cend')}`);
  const secrets = await p.$$eval('.crow', els => els.map(e => e.textContent).find(t => t.includes('Keep the secrets')) || '');
  t.check(secrets.includes('Add this as a deploy key to acme/shop') && secrets.includes('FACTORY_READ_KEY'), `${tag}: the keys to add by hand are not the step's own lines`);
  const states = await p.$$eval('.crow', els => els.map(e => e.dataset.state));
  t.check(states.length > 20 && states.every(s => s === 'done'), `${tag}: the steps are not all done: ${states.join(',')}`);
  t.check(!(await p.evaluate(() => document.body.innerText)).includes(SECRET), `${tag}: the typed secret is on the page`);
}

async function targets(t, p, tag) {
  const small = await p.$$eval('button:not([hidden]), input.cf-in, summary, .cf-check, .cf-switch, a.btn', els => els
    .filter(e => e.offsetParent !== null).filter(e => { const r = e.getBoundingClientRect(); return r.width < 24 || r.height < 24; })
    .map(e => e.outerHTML.slice(0, 60)));
  t.check(small.length === 0, `${tag}: targets under 24 px: ${small.join(' | ')}`);
}

drive('console', async t => {
  const ctx = await t.browser.newContext({viewport: {width: 1440, height: 900}});
  await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], {origin: new URL(t.srv.url).origin});
  const p = await ctx.newPage();
  p.on('pageerror', e => t.errors.push(`pageerror: ${e.message}`));
  p.on('console', m => { if (m.type() === 'error') t.errors.push(`console: ${m.text()}`); });
  p.on('response', async r => { if (r.url().includes('/api/') && (await r.text().catch(() => '')).includes(SECRET)) t.errors.push(`${r.url()} sent the typed secret back`); });
  await p.goto(t.srv.url);
  await p.waitForSelector('#f-go', {timeout: 8000});
  t.check(new URL(p.url()).search === '', 'the one-time token stayed in the address bar');
  t.check((await p.textContent('#status')).includes('Not installed'), 'the status does not say it is not installed');
  t.check((await p.textContent('.cf-commit')).startsWith("0123456 The drive's newest commit"), 'the commit it installs is not shown');
  t.check(await p.inputValue('#f-repo') !== '', 'the product checkout is not filled in');
  await targets(t, p, '1440');
  await dryInstall(t, p, '1440');
  await t.shot(p, 'console-1440.png', {fullPage: true});

  // the link works once: another browser is turned away
  const other = await (await t.browser.newContext()).newPage();
  await other.goto(t.srv.url);
  t.check((await other.textContent('body')).includes('used already'), 'the link let a second browser in');

  // both themes draw from the dashboard's tokens
  const bg = () => p.evaluate(() => getComputedStyle(document.body).backgroundColor);
  await p.emulateMedia({colorScheme: 'light'}); const light = await bg();
  await p.emulateMedia({colorScheme: 'dark'}); const dark = await bg();
  t.check(light !== dark, `the dark theme is not drawn: ${light} vs ${dark}`);
  await t.shot(p, 'console-1440-dark.png');
  await p.emulateMedia({colorScheme: 'light'});

  // a phone's width: the finished run goes, and a new dry install runs the same way
  await p.setViewportSize({width: 390, height: 844});
  await p.click('.cend [data-end="dismiss"]');
  await p.waitForFunction(() => document.querySelector('#run-sec').hidden, null, {timeout: 8000});
  await t.wide(p, '390 console');
  await targets(t, p, '390');
  await dryInstall(t, p, '390');
  await t.wide(p, '390 console run');
  await t.shot(p, 'console-390.png', {fullPage: true});
}, consoleServer);
