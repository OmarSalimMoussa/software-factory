'use strict';
/* Work: back links and browser history restore tab, filter and scroll; stacked panels; the task panel; boost, approved
   order, requests with a broken row and Show more, parked, done with Open, checks and the last triage line. */
const {drive, ready, settle, until, texts} = require('./lib.cjs');

drive('work', async t => {
  const scrollTop = p => p.evaluate(() => document.querySelector('#main').scrollTop);
  for (const [w, h] of [[1440, 900], [390, 844]]) {
    const tag = s => `${w}: ${s}`;
    const p = await t.open(w, h, 'work');
    await p.click('[data-worktab="requests"]'); await ready(p);
    await p.click('[data-inbox="users"]'); await settle(p);
    await p.evaluate(() => document.querySelector('#main').scrollTop = 300); await settle(p);
    const before = await scrollTop(p);
    // a request opens over the list, and the list stays where it was
    await p.$eval('.req-row[data-open-doc]', e => e.click()); await settle(p);
    t.check(await p.$eval('#sheet', e => e.classList.contains('open')), tag('request did not open in a panel'));
    t.check(await p.evaluate(() => location.hash) === '#work', tag('left the Work page'));
    await p.keyboard.press('Escape'); await settle(p);
    t.check(await scrollTop(p) === before, tag('scroll lost after the panel'));
    // to Library from the panel, then back with the Back link: tab, filter and scroll come back
    await p.$eval('.req-row[data-open-doc]', e => e.click()); await settle(p);
    await p.click('[data-lib-open]'); await ready(p);
    t.check((await p.textContent('.backlink')).includes('Back to Work › Requests'), tag('back link: ' + await p.textContent('.backlink')));
    await p.click('.backlink'); await settle(p);
    t.check(await p.evaluate(() => location.hash) === '#work', tag('back link did not return'));
    t.check(await p.$('[data-worktab="requests"][aria-selected="true"]'), tag('tab not restored'));
    t.check(await p.$('[data-inbox="users"][aria-pressed="true"]'), tag('filter not restored'));
    t.check(await scrollTop(p) === before, tag('scroll not restored: ' + await scrollTop(p)));
    // the browser's back button does the same
    await p.$eval('.req-row[data-open-doc]', e => e.click()); await settle(p);
    await p.click('[data-lib-open]'); await ready(p);
    await p.goBack(); await settle(p);
    t.check(await p.$('[data-inbox="users"][aria-pressed="true"]'), tag('browser back lost the filter'));
    // stacked panels: the task, then its file; Back returns to the task
    await p.click('[data-worktab="tasks"]'); await ready(p);
    await p.$eval('[data-task="t-0017"]', e => e.click()); await settle(p);
    await p.click('#sheet-foot [data-open-doc="t-0017"]'); await settle(p);
    t.check(!(await p.$eval('#sheet-back', e => e.hidden)), tag('no Back in a stacked panel'));
    await p.click('#sheet-back'); await settle(p);
    t.check((await p.textContent('#sheet-title')).includes('Sign-in sends people back'), tag('panel Back did not return to the task'));
    await p.keyboard.press('Escape'); await settle(p);
    await t.wide(p, tag('work'));
    await p.close();
  }

  const p = await t.open(1440, 900, 'work');
  // the task panel: status, numbers, route, progress, testers with their evidence, requests, log and branch
  await p.$eval('[data-task="t-0017"]', e => e.click()); await settle(p);
  const body = await p.textContent('#sheet-body');
  for (const s of ['QA, round 2 of 4', 'Route', 'Progress', 'Testers', '4 required', 'Log', 'Boosted by you', 'Branch', 'worktree-t-0017', 'Login loop on Safari']) t.check(body.includes(s), 'task panel lacks: ' + s);
  t.check((await p.$$('#sheet-body .tester')).length === 4, 'task panel should show 4 testers');
  t.check((await p.$$('#sheet-body .tester .shots img')).length === 2, 'tester evidence missing');
  t.check(!(await p.$eval('#sheet-body', e => e.scrollWidth > e.clientWidth)), 'task panel wider than its column');
  await t.shot(p, 'task-1440.png');
  await p.keyboard.press('Escape'); await settle(p);
  // groups are separate cards with title bands, and status dots stay small
  t.check((await p.$$('#taskrows > .group')).length >= 3, 'fewer than 3 task groups');
  t.check(await p.$eval('.group-h', e => getComputedStyle(e).backgroundColor) !== 'rgb(255, 255, 255)', 'group header is white');
  t.check(await p.$$eval('.task-row .status .dot', els => Math.max(...els.map(e => e.getBoundingClientRect().width))) <= 10, 'a status dot is oversized');
  t.check((await p.textContent('.triage-line')).includes('Last triage, 2 h ago'), 'no last-triage line');
  // Boost: the row moves to the top of the queue, washed; Unboost puts it back
  const queue = () => p.$$eval('[data-group="queue"] .task-row', els => els.map(e => e.dataset.task));
  const q0 = await queue();
  await p.click('[data-boost="t-0030"]'); await settle(p);
  const q1 = await queue();
  t.check(q1[0] === 't-0030', 'boosted task not first: ' + q1);
  t.check(await p.$('[data-flip="row-t-0030"].boosted'), 'boosted row not marked');
  t.check(await p.$('[data-flip="row-t-0030"].wash'), 'boosted row not washed');
  await p.click('[data-unboost="t-0030"]'); await settle(p);
  t.check((await queue()).join() === q0.join(), 'unboost did not restore the order');
  // approve two, then move the second up: the server gets the order shown
  await t.go(p, 'home');
  await p.click('[data-prop-act="approve"][data-id="t-0027"]'); await settle(p);
  await p.click('[data-prop-act="approve"][data-id="t-0024"]'); await settle(p);
  await t.go(p, 'work');
  t.check((await p.textContent('#taskrows')).includes('Approved, starting in this order'), 'no approved group');
  await p.click('[data-appr-move="-1"][data-id="t-0024"]'); await settle(p);
  const order = await p.$$eval('[data-flip^="appr-"]', els => els.map(e => e.dataset.flip));
  t.check(order[0] === 'appr-t-0024', 'approved reorder failed: ' + order);
  const mv = (await t.srv.calls()).filter(c => c.path === '/api/approved/t-0024/move').at(-1);
  t.check(mv && JSON.stringify(mv.body.seen_order) === '["t-0027","t-0024"]', 'approved move did not send what the device saw');
  t.check((await texts(p, '[data-group="approved"] .status'))[0].includes('1st in line'), 'approved status word missing');
  // Requests: a broken row says so; Show more loads the next page; the outcome filter asks the server
  await p.click('[data-worktab="requests"]'); await ready(p);
  t.check((await p.textContent('.row.broken')).includes('This row could not be read'), 'broken request row not drawn');
  t.check((await p.$$('.req-row, .row.broken')).length === 25, 'first page should hold 25 rows');
  await p.click('[data-more="requests"]'); await settle(p);
  t.check((await p.$$('.req-row, .row.broken')).length === 41, 'Show more did not load the rest: ' + (await p.$$('.req-row, .row.broken')).length);
  t.check(!(await p.$('[data-more="requests"]')), 'Show more stayed after the last page');
  await p.click('[data-out="noise"]'); await settle(p);
  t.check((await texts(p, '.req-row .c-hide .chip')).every(x => x === 'noise'), 'outcome filter not applied');
  await p.click('[data-out="all"]'); await settle(p);
  await p.click('[data-boostreq="r-2026-10-09-1042"]'); await settle(p);
  t.check(!(await p.$('[data-boostreq="r-2026-10-09-1042"]')), 'a boosted request still offers Boost');
  // Parked: Unpark asks the server and the row goes
  await p.click('[data-worktab="parked"]'); await ready(p);
  await p.click('[data-unpark="t-0011"]');
  t.check(await until(p, () => document.querySelector('#toasts').textContent.includes('Back in the queue')), 'no unpark toast');
  await settle(p);
  t.check(!(await p.$('[data-unpark="t-0011"]')), 'unparked row still listed');
  // Checks: strips and a legend
  await p.click('[data-worktab="checks"]'); await ready(p);
  t.check((await p.$$('.scen i')).length === 80, 'check strips wrong');
  t.check((await p.$$('.legend span')).length === 3, 'check legend wrong');
  // Done: each finished task with how far it reached; its commits; Open opens slot 0's sign-in link in a new tab
  await p.click('[data-worktab="done"]'); await ready(p);
  t.check((await p.$$('.done-row')).length === 4, 'the done tasks are not listed');
  t.check((await p.$$eval('.done-row:first-child .stages .chip', els => els.map(e => e.className))).join() === 'chip good,chip good,chip good,chip ', 'the stages are wrong');
  await t.shot(p, 'work-done-1440.png');
  await p.context().route('http://127.0.0.1:8100/**', r => r.fulfill({body: 'signed in'}));
  const [slot] = await Promise.all([p.context().waitForEvent('page'), p.click('[data-slot-open]')]);
  t.check(await slot.waitForURL('http://127.0.0.1:8100/sign-in/once').then(() => true, () => false), 'Open did not open slot 0 signed in');
  await slot.close();
  await p.click('[data-done-view="commits"]'); await ready(p);
  t.check((await p.$$('#page .commit-row')).length === 11, 'the commits are not listed');
  await p.$eval('#page .commit-row[data-task="t-0013"]', e => e.click()); await settle(p);
  t.check(await until(p, () => document.querySelectorAll('#sheet .commit-row').length === 2), 'the task panel does not list its commits');
  await p.keyboard.press('Escape'); await settle(p);
  const phone = await t.open(390, 844, 'work');
  await phone.click('[data-worktab="done"]'); await ready(phone);
  await t.wide(phone, 'done 390');
  await t.shot(phone, 'work-done-390.png');
  // no stray scroll bars: only the page scrolls
  const bars = () => p.evaluate(() => [...document.querySelectorAll('*')].filter(e => e.id !== 'main' && e.id !== 'sheet-body' && ((/(auto|scroll)/.test(getComputedStyle(e).overflowX) && e.scrollWidth > e.clientWidth) || (/(auto|scroll)/.test(getComputedStyle(e).overflowY) && e.scrollHeight > e.clientHeight))).map(e => e.className));
  await p.click('[data-worktab="tasks"]'); await ready(p);
  const sb = await bars(); t.check(!sb.length, 'stray scroll bars on Work: ' + sb);
  await p.setViewportSize({width: 1024, height: 800}); await settle(p);
  await t.wide(p, '1024 work');
  await t.shot(p, 'work-1024.png');
});
