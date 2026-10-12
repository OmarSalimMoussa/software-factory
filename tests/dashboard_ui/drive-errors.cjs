'use strict';
/* Broken states: device admission; the warden unreachable (persistent toast, amber since-when, page marked old, writes
   off, nothing moves); a 500 with Retry; a failed part; a refusal; a stale panel; wrong code then lock; an operation
   across a reload and one that fails; an API version change; toasts bottom right, and full width on a phone. */
const {drive, ready, settle, until, heights} = require('./lib.cjs');

drive('errors', async t => {
  const posts = async () => (await t.srv.calls()).filter(c => c.method === 'POST').length;
  const toastText = p => p.$$eval('#toasts .toast', els => els.map(e => e.textContent).join(' | '));
  // the first visit asks for the code once; a wrong code says how many tries are left
  const d = await t.open(1440, 900, 'home', {admit: false});
  await d.waitForSelector('#code-0');
  t.check((await d.textContent('#sheet-title')) === 'Sign in this device', 'no device sign-in');
  t.check(await d.$eval('#sheet-x', e => e.hidden), 'the sign-in panel can be closed');
  await d.keyboard.press('Escape'); await settle(d);
  t.check(await d.$eval('#sheet', e => e.classList.contains('open')), 'Escape closed the sign-in panel');
  await t.code(d, '111111'); await settle(d);
  t.check((await d.textContent('#code-note')).includes('Wrong code, 4 tries left.'), 'wrong device code: ' + await d.textContent('#code-note'));
  await t.code(d); await d.waitForSelector('#props', {timeout: 8000}).catch(() => {}); await ready(d);
  t.check(await d.$('#props') && (await d.context().cookies()).some(c => c.name === 'owner'), 'the device was not signed in');
  await d.close();

  // the warden goes away: a persistent toast, the indicator says since when, the page is marked old, writes are off, nothing moves
  const p = await t.open(1440, 900);
  const box = () => p.$eval('.gs-field', e => { const r = e.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.width)].join(); });
  const where = () => p.$eval('#props', e => Math.round(e.getBoundingClientRect().top));
  const b0 = await box(), w0 = await where(), h0 = await heights(p, '#props > .card');
  await t.srv.set({down: true}); await p.evaluate(() => poll()); await settle(p);
  const warden = await p.$('.toast.alert[data-key="warden"]');
  t.check(warden && (await warden.textContent()).includes("Can't reach the warden since 14:02"), 'no persistent warden toast');
  t.check(warden && (await warden.getAttribute('role')) === 'alert', 'the warden toast is not an alert');
  t.check((await p.textContent('#live-text')) === "Can't reach the warden since 14:02" && await p.$('#live.down'), 'live indicator does not say since when');
  t.check(await p.$('#page.old'), 'the page is not marked old');
  t.check(b0 === await box() && w0 === await where() && h0.join() === (await heights(p, '#props > .card')).join(), 'something moved when the warden went away');
  const n0 = await posts();
  await p.click('[data-prop-act="approve"][data-id="t-0027"]'); await settle(p);
  t.check(await posts() === n0, 'a write was sent while the warden was away');
  t.check((await toastText(p)).includes('nothing can change now'), 'no reason given for the write being off');
  t.check(await p.$('[data-prop-act="approve"].writes-off'), 'write buttons not marked off');
  const r = await warden.boundingBox(), vw = 1440, vh = 900;
  t.check(Math.abs(r.x + r.width - (vw - 24)) <= 2 && r.y + r.height <= vh - 24 + 2, `toasts are not bottom right: ${JSON.stringify(r)}`);
  await t.shot(p, 'warden-down-1440.png');
  await t.srv.set({down: false}); await p.evaluate(() => poll());
  t.check(await until(p, () => !document.querySelector('.toast.alert[data-key="warden"]') && document.querySelector('#toasts').textContent.includes('The warden answers again')), 'the warden toast stayed');
  await settle(p);
  t.check(!(await p.$('#page.old')) && !(await p.$('#live.down')), 'still marked old after the warden came back');
  // the browser itself loses the server: the same, with the browser's own time
  await p.route('**/api/**', route => route.abort()); await p.evaluate(() => poll()); await settle(p);
  t.check(/^Can't reach the warden since \d\d:\d\d$/.test(await p.textContent('#live-text')), 'a lost connection is not shown: ' + await p.textContent('#live-text'));
  await p.unroute('**/api/**'); await p.evaluate(() => poll()); await settle(p);
  // the dashboard's stand-in holds its address while the factory upgrades: the warden toast says its sentence instead
  const upgrading = 'The factory is upgrading: starting the services. This page comes back by itself when it is done.';
  const standIn = {ok: false, warden: {ok: false, since: null, note: upgrading}, error: upgrading, reason: upgrading};
  await p.route('**/api/**', route => route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify(standIn)}));
  await p.evaluate(() => poll()); await settle(p);
  t.check((await p.textContent('.toast.alert[data-key="warden"]')) === upgrading, 'the upgrade is not said: ' + await toastText(p));
  await p.unroute('**/api/**'); await p.evaluate(() => poll()); await settle(p);
  // a 500 on one page: an error toast with Retry; once fixed, Retry draws it; the same failure again counts up
  await t.srv.set({fail_path: '/api/spend'});
  await p.click('#sidenav [data-go="spend"]'); await settle(p);
  const fail = await p.$('.toast.alert[data-key="load:This page"]');
  t.check(fail && (await fail.textContent()).includes('This page could not be loaded') && await fail.$('[data-t="retry"]'), 'no load-failure toast with Retry');
  await t.srv.set({fail_path: null}); await p.click('.toast.alert[data-key="load:This page"] [data-t="retry"]'); await ready(p);
  t.check(await p.$('#plan'), 'Retry did not draw the page');
  await t.srv.set({fail_path: '/api/spend'}); await p.evaluate(() => loadPage()); await p.evaluate(() => loadPage()); await settle(p);
  t.check((await p.$$('.toast.alert[data-key="load:This page"]')).length === 1 && (await p.textContent('.toast.alert[data-key="load:This page"]')).includes('×2'), 'a repeated failure stacked instead of counting');
  t.check(await p.$('#plan'), 'a failed refresh wiped the last good page');
  await t.srv.set({fail_path: null});
  // one part fails: its card says so, a toast names it, the rest draws
  await t.srv.set({fail_part: ['goal']}); await t.go(p, 'home');
  const goal = await p.$eval('.goals, .part-failed', () => document.querySelector('.part-failed') ? document.querySelector('.part-failed').textContent : '');
  t.check(goal.includes('This part could not be drawn.') && goal.includes('The goal could not be read'), 'failed part not drawn as such: ' + goal);
  t.check((await toastText(p)).includes('The goal could not be read'), 'no toast names the failed part');
  t.check(await p.$('.tiles') && await p.$('#props'), 'the rest of the page did not draw');
  await t.shot(p, 'part-failed-1440.png');
  await t.srv.set({fail_part: []}); await p.evaluate(() => loadPage()); await settle(p);
  // a picture the server refuses says so in its place
  await p.route('**/api/picture/**', route => route.fulfill({status: 404, body: ''}));
  await p.reload(); await ready(p);
  t.check(await until(p, () => { const gone = document.querySelectorAll('#props .pic-gone'); return gone.length === 3 && gone[0].textContent.includes('The picture could not be shown.'); }), 'a refused picture is not said');
  await p.unroute('**/api/picture/**'); await p.reload(); await ready(p);
  // a refusal shows the server's reason, and nothing changed first
  await t.srv.set({refuse: {'/api/proposals': 'Proposals are frozen while you review the goal.'}});
  await p.click('[data-prop-act="approve"][data-id="t-0027"]'); await settle(p);
  t.check((await toastText(p)).includes('Proposals are frozen'), 'refusal reason not shown');
  t.check(!(await p.$('#props .prop.done')) && await p.$('[data-prop-act="approve"][data-id="t-0027"]'), 'the card changed on a refusal');
  await p.click('[data-prop-act="approve"][data-id="t-0027"]'); await settle(p);
  t.check((await p.$$('.toast.alert')).filter(Boolean).length >= 1 && (await toastText(p)).includes('×2'), 'a repeated refusal stacked');
  await t.srv.set({refuse: {}});
  // a stale panel: the task moved on; Approve is refused with "This changed" and the panel stays
  await p.click('[data-flip="t-0027"] .prop-t'); await settle(p);
  await t.srv.set({bump: 't-0027'});
  await p.click('#sheet-foot [data-prop-act="approve"]'); await settle(p);
  t.check((await toastText(p)).includes('This changed; here it is now.') && await p.$eval('#sheet', e => e.classList.contains('open')), 'stale action not refused in place');
  await p.keyboard.press('Escape'); await settle(p);
  // wrong codes count down, then lock with a time; a locked sheet opens locked; Pause is never locked
  await t.go(p, 'guard');
  await p.click('.rule .switch'); await settle(p);
  for (const left of [4, 3, 2, 1]) {
    await t.code(p, '000000'); await settle(p);
    const note = await p.textContent('#code-note');
    t.check(note.includes(`Wrong code, ${left} ${left === 1 ? 'try' : 'tries'} left.`), `tries left ${left}: ${note}`);
  }
  await t.code(p, '000000'); await settle(p);
  t.check(/Locked after too many wrong codes\. Try again at \d\d:\d\d\./.test(await p.textContent('#code-note')) && await p.$eval('#code-0', e => e.disabled), 'no lock after five wrong codes');
  await t.shot(p, 'locked-1440.png');
  await p.keyboard.press('Escape'); await settle(p);
  await p.evaluate(() => loadShell()); await settle(p);
  await p.click('.rule .switch'); await settle(p);
  t.check((await p.textContent('#code-note')).includes('Coded actions are locked until') && await p.$eval('#code-0', e => e.disabled), 'a coded sheet does not open locked');
  await p.keyboard.press('Escape'); await settle(p);
  await t.go(p, 'agents');
  await p.click('[data-agent="orchestrator"] .nname'); await settle(p); await p.click('#sheet-foot [data-action="pause"]'); await settle(p);
  t.check(!(await p.$eval('#code-0', e => e.disabled)), 'Pause is blocked by the lock');
  await p.keyboard.press('Escape'); await p.keyboard.press('Escape'); await settle(p);
  await p.close();

  // an operation survives a reload, and a failing one says which step failed
  await t.srv.reset(); await t.srv.set({op_ms: 1200});
  const o = await t.open(1440, 900, 'settings');
  await o.click('[data-step="1"][data-key="caps.designers"]'); await o.click('[data-action="review"]'); await settle(o);
  await t.code(o);
  t.check(await until(o, () => document.querySelector('.steps li')), 'no steps before the reload');
  await o.reload(); await ready(o);
  await o.waitForFunction(() => document.querySelector('#sheet-title').textContent === 'Saving settings' && document.querySelector('.steps li'), null, {timeout: 4000}).catch(() => t.errors.push('the operation did not come back after a reload'));
  await o.waitForFunction(() => document.querySelector('#toasts').textContent.includes('The new values are in effect.'), null, {timeout: 9000}).catch(() => t.errors.push('the operation did not finish after a reload'));
  await settle(o);
  await t.srv.set({op_fail: true, op_ms: 400});
  await o.click('#top-promote'); await settle(o); await t.code(o);
  await o.waitForFunction(() => document.querySelector('#toasts').textContent.includes('failed at'), null, {timeout: 6000}).catch(() => t.errors.push('a failed operation did not say so'));
  t.check((await toastText(o)).includes('Pushing to prod failed at “Redeploying prod at a1b2c3d”. The deploy ended failure: https://github.invalid/r'), 'failed step not named: ' + await toastText(o));
  t.check(await o.$('.steps li.failed') && await o.$('#sheet-foot [data-action="close-sheet"]'), 'the failed step is not marked in the panel');
  await t.shot(o, 'op-failed-1440.png');
  await o.close();

  // the factory was updated underneath: the page says so instead of drawing a mismatched page
  const u = await t.open(1440, 900);
  await t.srv.set({api: 2}); await u.evaluate(() => poll()); await settle(u);
  t.check((await toastText(u)).includes('The factory was updated. Reload to see it.') && await u.$('.toast [data-t="reload"]'), 'no reload prompt on an API change');
  await u.close();

  // degenerate states each say one sentence: no GOAL.md, nobody working
  await t.srv.reset(); await t.srv.set({scenario: 'no-goal'});
  const g = await t.open(1440, 900);
  t.check((await g.textContent('#page')).includes('GOAL.md is missing, so the goal cannot be drawn.'), 'a missing GOAL.md is not said');
  t.check((await g.textContent('#page')).includes('Nobody is working right now.'), 'an empty Working now is not said');
  await g.close();

  // phone: error toasts run full width above the bottom bar
  await t.srv.reset();
  const m = await t.open(390, 844);
  await t.srv.set({down: true}); await m.evaluate(() => poll()); await settle(m);
  const mr = await m.$eval('.toast.alert', e => { const b = e.getBoundingClientRect(); return [b.left, b.right, b.bottom]; });
  const navTop = await m.$eval('#bottomnav', e => e.getBoundingClientRect().top);
  t.check(Math.round(mr[0]) === 8 && Math.round(mr[1]) === 382 && mr[2] <= navTop, 'phone toast not full width above the bottom bar: ' + mr + ' / ' + navTop);
  await t.wide(m, '390 warden down');
  await t.shot(m, 'warden-down-390.png');
});
