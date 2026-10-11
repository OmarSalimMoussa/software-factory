'use strict';
/* The Claude account section: first on Settings, no token drawn, the add form refusing an empty token in place and then
   adding one, Switch running as an operation to its end, Remove asking first; aligned controls, and a phone's width. */
const {drive, settle, until} = require('./lib.cjs');

drive('accounts', async t => {
  const p = await t.open(1440, 900, 'settings');
  const first = await p.evaluate(() => { const a = document.querySelector('.acct-card'), r = document.querySelector('.repo-card'); return !!a && !!r && !!(a.compareDocumentPosition(r) & Node.DOCUMENT_POSITION_FOLLOWING); });
  t.check(first, 'the Claude account section is not at the top of Settings');
  t.check((await p.textContent('[data-row="acct:main"]')).includes('active') && !(await p.$('[data-row="acct:main"] [data-acct-use], [data-row="acct:main"] [data-acct-remove]')), 'the active account is not marked, or can be switched to or removed');
  t.check(!(await p.textContent('#page')).includes('sk-ant'), 'a token is drawn');
  // an empty token is refused, in place, and the name typed stays
  await p.fill('#acct-name', 'spare'); await p.click('[data-acct-add]'); await settle(p);
  t.check((await p.textContent('#acct-note')).includes("Paste the account's token."), 'empty token not refused: ' + await p.textContent('#acct-note'));
  t.check(await p.inputValue('#acct-name') === 'spare', 'the name typed was lost');
  await p.fill('#acct-token', 'sk-ant-oat01-typed-in-the-form-9z9z'); await p.press('#acct-token', 'Enter');
  await p.waitForSelector('[data-row="acct:spare"]', {timeout: 8000}).catch(() => t.errors.push('the added account is not listed'));
  t.check((await p.textContent('[data-row="acct:spare"]') || '').includes('…9z9z'), 'the added account does not show its last 4');
  t.check(await p.inputValue('#acct-token') === '' && !(await p.textContent('#page')).includes('typed-in-the-form'), 'the token stayed on the page');
  // controls: every target at least 24 px, and the add button the height and line of the fields
  const small = await p.$$eval('[data-acct-use], [data-acct-remove], [data-acct-add], .acct-fields input', els => els.filter(e => { const r = e.getBoundingClientRect(); return r.width < 24 || r.height < 24; }).length);
  t.check(small === 0, small + ' account controls are under 24 px');
  const [field, button] = await p.evaluate(() => ['#acct-token', '[data-acct-add]'].map(s => { const r = document.querySelector(s).getBoundingClientRect(); return [Math.round(r.top), Math.round(r.height)]; }));
  t.check(field[0] === button[0] && field[1] === button[1], `the add button is off the fields' line: ${field} vs ${button}`);
  await t.shot(p, 'accounts-1440.png');
  // switch: the operation's steps, then work is the active account
  await p.click('[data-row="acct:work"] [data-acct-use]');
  t.check(await until(p, () => document.querySelector('#sheet-title').textContent === 'Switching the Claude account' && document.querySelector('.steps li')), 'the switch shows no steps');
  t.check(await until(p, () => document.querySelector('#toasts').textContent.includes('The agents now use work.')), 'no toast once the switch is done');
  await p.waitForFunction(() => { const r = document.querySelector('[data-row="acct:work"]'); return r && r.textContent.includes('active') && !document.querySelector('#sheet.open'); }, null, {timeout: 10000}).catch(() => t.errors.push('the switch did not finish'));
  const use = (await t.srv.calls()).filter(c => c.method === 'POST' && c.path === '/api/accounts/work/use');
  t.check(use.length === 1 && Object.keys(use[0].body).join() === 'idempotency_key', 'the switch was not sent once, with its key alone');
  // remove asks first, then main is gone
  await p.click('[data-row="acct:main"] [data-acct-remove]'); await settle(p);
  t.check((await p.textContent('#sheet-title')) === 'Remove main?', 'no question before Remove');
  await p.click('#sheet-foot [data-acct-remove-now]'); await settle(p);
  t.check(!(await p.$('[data-row="acct:main"]')), 'main was not removed');
  await p.close();
  const m = await t.open(390, 844, 'settings');
  await t.wide(m, '390 accounts');
  const [fw, cw] = await m.evaluate(() => [document.querySelector('#acct-token').getBoundingClientRect().width, document.querySelector('.acct-card').getBoundingClientRect().width]);
  t.check(fw > cw / 2, 'phone: the token field is not given the card\'s width');
  await t.shot(m, 'accounts-390.png');
});
