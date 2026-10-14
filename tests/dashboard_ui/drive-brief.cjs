'use strict';
/* A task's brief: Open brief on Home shows the designer's page in its frame beside the owner's threads, each thread's
   text highlighted in the page; selecting text in the frame offers Comment, and the comment joins the threads;
   Resolve closes one; a refresh leaves the frame as it was; at phone width the threads sit under the page. */
const {drive, ready, settle, until, texts} = require('./lib.cjs');

drive('brief', async t => {
  const p = await t.open(1440, 900, 'home');
  await p.click('[data-brief]'); await ready(p);
  t.check((await texts(p, '.brief-h h1'))[0] === 'Partner sign-in from the import step', 'the brief page did not open');
  const frame = () => p.frames().find(f => f.url().includes('/api/brief-page/'));
  t.check(await until(p, () => !!document.querySelector('#brief-frame')), 'no brief frame');
  await p.waitForFunction(() => document.querySelector('#brief-frame').contentWindow !== null);
  const f = await (async () => { for (let i = 0; i < 50 && !frame(); i++) await p.waitForTimeout(100); return frame(); })();
  t.check(!!f, 'the frame never loaded the brief');
  await f.waitForSelector('h1');
  t.check((await f.textContent('h1')).includes('Partners sign in'), 'the brief is not the designer\'s page');
  t.check((await texts(p, '.brief-t')).length === 2, 'the two threads are not listed');
  t.check(await f.waitForFunction(() => CSS.highlights.get('brief-note').size === 2).then(() => true, () => false),
    'the two threads\' text is not highlighted in the page');

  await f.evaluate(() => {
    const r = document.createRange(); r.selectNodeContents(document.querySelector('p.muted'));
    getSelection().removeAllRanges(); getSelection().addRange(r); document.dispatchEvent(new MouseEvent('mouseup'));
  });
  await p.waitForSelector('#brief-cmt:not([hidden])');
  await p.click('#brief-cmt');
  t.check((await texts(p, '.brief-t.compose q'))[0].startsWith('The form keeps what'), 'the comment does not quote the selection');
  await p.fill('#brief-note', 'Say what happens to a half-typed form.');
  await p.click('[data-brief-save]');
  t.check(await until(p, () => document.querySelector('#toasts').textContent.includes('Comment saved')), 'no saved toast');
  await settle(p);
  t.check((await texts(p, '.brief-t')).length === 3, 'the new comment is not listed');
  t.check((await texts(p, '[data-brief-act]')).some(x => x.includes('Send feedback · 2')), 'Send feedback does not count the new comment');

  await f.evaluate(() => { window.stillHere = true; });
  await p.click('[data-brief-resolve="c2"]');
  t.check(await until(p, () => document.querySelector('#toasts').textContent.includes('Resolved')), 'no resolved toast');
  await settle(p);
  t.check(await p.$('[data-brief-t="c2"].done'), 'the resolved thread is not shown as done');
  t.check(await f.evaluate(() => window.stillHere === true), 'a refresh reloaded the brief');
  t.check(await f.waitForFunction(() => CSS.highlights.get('brief-note').size + CSS.highlights.get('brief-note-on').size === 2)
    .then(() => true, () => false), 'the highlights do not follow the threads: the new one in, the resolved one out');
  await t.shot(p, 'brief-1440.png');

  const phone = await t.open(390, 844, 'home');
  await phone.click('[data-brief]'); await ready(phone);
  const [frameBox, threadsBox] = await Promise.all(['.brief-frame', '.brief-threads'].map(s => phone.$eval(s, e => e.getBoundingClientRect().top)));
  t.check(threadsBox > frameBox, 'at phone width the threads do not sit under the page');
  await t.wide(phone, 'brief 390');
  await t.shot(phone, 'brief-390.png');
});
