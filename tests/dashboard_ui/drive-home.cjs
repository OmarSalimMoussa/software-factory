'use strict';
/* Home: proposals in fixed slots (no layout shift on approve or choosing), needs with pictures and a question,
   the outage band, and Push to prod with its steps. */
const {drive, settle, until, heights} = require('./lib.cjs');

drive('home', async t => {
  const tops = p => heights(p, '#props > .card');
  const lastCall = async path => (await t.srv.calls()).filter(c => c.method === 'POST' && c.path === path).at(-1);
  const p = await t.open(1440, 900);
  t.check((await p.$$('#props .prop:not(.slot)')).length === 3, 'expected 3 proposals');
  t.check(await p.$('#page [data-msg="orchestrator"]') && await p.$('#page [data-agent="orchestrator"]'), 'no box addressed to the orchestrator on Home');
  t.check((await p.$$('#props .prop .sk img')).length === 3, 'a proposal lacks its sketch');
  t.check((await p.textContent('#props')).includes('People see their credit once'), 'goal line missing');
  t.check((await p.$$('#needs .need.pic .sk img')).length === 2, 'the spec and decide cards lack pictures');
  t.check(await p.$('#needs .need.question input[data-answer="n3"]'), 'the question card has no answer field');
  const sizes = [...new Set(await heights(p, '#props .prop, #needs .need'))]; t.check(sizes.length === 1, 'Home cards differ in height: ' + sizes);
  const bw = await p.$$eval('.prop-a .btn:not(.sq)', els => [...new Set(els.map(e => Math.round(e.getBoundingClientRect().width) + 'x' + Math.round(e.getBoundingClientRect().height)))]); t.check(bw.length === 1, 'Home action buttons differ: ' + bw);
  await t.shot(p, 'home-full-1440.png', {fullPage: true});
  // reorder: the server is told the order this device showed, and the card moves when it answers
  const v24 = await p.evaluate(() => propCardOf('t-0024').version);
  await p.click('[data-prop-move="-1"][data-id="t-0024"]'); await settle(p);
  t.check((await p.$eval('#props .prop .prop-t', e => e.textContent)).includes('account menu'), 'reorder failed');
  const mv = await lastCall('/api/proposals/t-0024/move');
  t.check(mv && JSON.stringify(mv.body.seen_order) === '["t-0027","t-0024","t-0025"]' && mv.body.dir === -1, 'move did not send what the device saw: ' + JSON.stringify(mv && mv.body));
  t.check(mv && mv.headers['x-software'] === '1' && typeof mv.body.idempotency_key === 'string' && mv.body.version === v24, 'a write lacks its header, key or the version the card showed');
  // the run: changed through the dropdown, the server's new estimate drawn
  const est0 = await p.evaluate(() => propCardOf('t-0025').estimate);
  await p.click('[data-flip="t-0025"] [data-ddbtn]'); await p.click('[data-ddopt][data-ddkind="prop-run"][data-ddkey="t-0025"][data-v="2"]'); await settle(p);
  const card = await p.textContent('[data-flip="t-0025"]'); const est1 = await p.evaluate(() => propCardOf('t-0025').estimate);
  t.check(card.includes('run changed by you') && /^≈ \$\d/.test(est1) && est1 !== est0 && card.includes(est1), `run change not drawn (${est0} -> ${est1}): ` + card.slice(0, 200));
  // approve: the approved state in place, then choosing, then a refill, with no height change
  const h0 = await tops(p);
  await p.click('[data-prop-act="approve"][data-id="t-0024"]');
  t.check(await until(p, () => document.querySelector('#props .prop.done')), 'no approved state');
  t.check((await tops(p)).join() === h0.join(), 'layout shift on approve');
  await p.waitForFunction(() => document.querySelector('#props').textContent.includes('choosing'), null, {timeout: 4000}).catch(() => t.errors.push('no choosing placeholder'));
  t.check((await tops(p)).join() === h0.join(), 'layout shift while choosing');
  await p.waitForFunction(() => document.querySelector('#props').textContent.includes('dark mode'), null, {timeout: 8000}).catch(() => t.errors.push('no refill with t-0018'));
  t.check((await p.textContent('.sec-h')).includes('1 approved, waiting for a slot'), 'approved count missing on Home');
  // Not now, then nothing else is worth proposing
  await p.click('[data-prop-act="later"][data-id="t-0027"]');
  await p.waitForFunction(() => document.querySelector('#props').textContent.includes('Nothing else worth proposing'), null, {timeout: 10000}).catch(() => t.errors.push('no "nothing else" slot'));
  t.check((await tops(p)).join() === h0.join(), 'layout shift after Not now');
  // the proposal panel: the sketch large, with Approve, which closes it once the server agrees
  await p.click('[data-flip="t-0025"] .prop-t'); await settle(p);
  t.check(await p.$('#sheet-body .mock img'), 'no big sketch in the panel');
  t.check((await p.textContent('#sheet-body')).includes('Log'), 'the task panel has no log');
  await p.click('#sheet-foot [data-prop-act="approve"]'); await settle(p);
  t.check(!(await p.$eval('#sheet', e => e.classList.contains('open'))), 'the panel stayed open after Approve');
  // the spec card's picture opens in a panel
  await p.click('#needs [data-pic="n1"]'); await settle(p);
  t.check((await p.textContent('#sheet-body')).includes('Second sign-off'), 'spec picture panel missing');
  await p.keyboard.press('Escape'); await settle(p);
  // a question is answered from its card; an empty answer is not sent
  await p.click('[data-need-act="answer"][data-id="n3"]'); await settle(p);
  t.check(!(await lastCall('/api/needs/n3/answer')), 'an empty answer was sent');
  const v3 = await p.evaluate(() => needOf('n3').version);
  await p.fill('input[data-answer="n3"]', 'Resume from the last full part'); await p.click('[data-need-act="answer"][data-id="n3"]'); await settle(p);
  const ans = await lastCall('/api/needs/n3/answer'); t.check(ans && ans.body.text === 'Resume from the last full part' && ans.body.version === v3, 'answer not sent');
  t.check((await p.textContent('#needs')).includes('Your answer is on its way'), 'the answered card did not say so');
  await p.click('[data-need-act="extend"][data-id="n2"]'); await settle(p);
  t.check((await p.textContent('#needs')).includes('Two more rounds granted'), 'Extend not shown done');
  await t.wide(p, '1440 home');
  // Push to prod: both builds, what prod gains with what each was built from, then the steps until prod answers
  await p.click('#top-promote'); await settle(p);
  t.check((await p.textContent('#sheet-title')) === 'Push to prod' && await p.$('#sheet-body .diff'), 'no push panel');
  t.check((await p.$$('.origin-row')).length === 4, 'push panel should list 4 changes');
  await p.click('[data-origin="t-0012"]'); await settle(p); t.check(await p.$('#origin-t-0012 .mock img'), 'spec built-from has no picture');
  await p.click('[data-origin="t-0013"]'); await settle(p); t.check((await p.$$('#origin-t-0013 .shot img')).length === 3, 'fix built-from has no QA shots');
  await t.shot(p, 'push-1440.png');
  await t.code(p);
  t.check(await until(p, () => document.querySelector('.steps li')), 'no steps shown for the push');
  await p.waitForFunction(() => document.querySelector('#pill-prod').textContent.includes('a1b2c3d'), null, {timeout: 8000}).catch(() => t.errors.push('prod pill did not move to a1b2c3d'));
  const push = await lastCall('/api/push'); t.check(push && push.body.expected_staging_sha === 'a1b2c3d' && push.body.code === '123456', 'push did not send the expected staging build');
  await p.close();

  // the outage band: probes, the fix the factory started, Watch and Stop; the stopped fix waits first in Proposed next
  await t.srv.reset(); await t.srv.set({scenario: 'outage'});
  const o = await t.open(1440, 900);
  t.check(await o.$('.outage'), 'no outage band');
  t.check((await o.$$('.probes i.x')).length === 2, 'probe strip wrong');
  t.check(await o.$('#pill-prod .dot.bad'), 'prod pill not red');
  await t.shot(o, 'outage-1440.png');
  await o.click('.outage [data-agent="run-t-0031"]'); await settle(o);
  t.check((await o.textContent('#sheet-title')) === 'Prod answers again', 'Watch the fix did not open the fix');
  await o.keyboard.press('Escape'); await settle(o);
  await o.click('[data-action="outage-stop"]'); await settle(o);
  t.check((await o.$eval('#props .prop .prop-t', e => e.textContent)).includes('Prod answers again'), 'stopped fix not first');
  t.check((await o.textContent('.outage')).includes('You stopped the fix'), 'band did not say the fix stopped');
  await o.close();

  // phone: fixed slots there too
  await t.srv.reset();
  const m = await t.open(390, 844);
  await t.wide(m, '390 home');
  const mh0 = await tops(m);
  await m.click('[data-prop-act="approve"][data-id="t-0027"]');
  t.check(await until(m, () => document.querySelector('#props .prop.done')), 'phone: no approved state');
  t.check((await tops(m)).join() === mh0.join(), 'phone layout shift on approve');
  await m.waitForFunction(() => document.querySelector('#props').textContent.includes('choosing'), null, {timeout: 4000}).catch(() => t.errors.push('phone: no choosing'));
  t.check((await tops(m)).join() === mh0.join(), 'phone layout shift while choosing');
  await m.evaluate(() => document.querySelector('#props').scrollIntoView()); await t.shot(m, 'props-390.png');
  await m.evaluate(() => document.querySelector('#needs').scrollIntoView()); await t.shot(m, 'needs-390.png');
});
