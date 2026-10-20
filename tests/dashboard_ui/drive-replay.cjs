'use strict';
/* A tester run's replay: opened from the task panel; a row opens on its failed step; picking a step moves nothing;
   Watch plays the title card, the thought and the actions over the frames; the stage pauses and resumes; the list
   follows the player; the recording plays and hides a secret's stretch; a step that did not run says so; what the
   second browser showed is named. A check shows each run's screenshots, the second browser's named, and a run with
   no steps marked plays as its recording. */
const {drive, ready, settle} = require('./lib.cjs');

const wait = (p, fn, arg, ms = 20000) => p.waitForFunction(fn, arg, {timeout: ms}).then(() => true, () => false);
const box = (p, sel) => p.$eval(sel, e => { const r = e.getBoundingClientRect(); return [Math.round(r.top), Math.round(r.height)]; });

drive('replay', async t => {
  for (const [w, h] of [[1440, 900], [390, 844]]) {
    const tag = s => `${w}: ${s}`;
    const p = await t.open(w, h, 'work');
    await p.click('[data-worktab="tasks"]'); await ready(p);
    await p.$eval('[data-task="t-0017"]', e => e.click()); await settle(p);
    await p.click('#sheet-body [data-replay*="qa-ui-function"]'); await ready(p);
    t.check(await p.evaluate(() => location.hash) === '#replay', tag('the replay did not open as a page'));
    t.check((await p.textContent('#page .sub')).startsWith('1 of 2 failed'), tag('summary: ' + await p.textContent('#page .sub')));
    t.check(await p.$$eval('.rp-row', rs => rs.length) === 2, tag('want a row for each item'));
    t.check((await p.textContent('.rp-row')).includes('Failed at step 2'), tag('row note: ' + await p.textContent('.rp-row')));
    await t.wide(p, tag('replay'));

    // a row opens on its failed step, the screen still and Watch on it
    await p.click('.rp-row'); await settle(p);
    t.check((await p.textContent('#rp-said')).includes('Step 2 · Sign in with a test address'), tag('said: ' + await p.textContent('#rp-said')));
    t.check(await p.$eval('.rp-step[data-step="1"]', e => e.getAttribute('aria-pressed')) === 'true', tag('the failed step is not picked'));
    t.check(await p.isVisible('.rp-still') && await p.isVisible('.rp-watch'), tag('idle: want the still and Watch'));
    const stage = await box(p, '.rp-stage');
    await p.click('.rp-step[data-step="0"]'); await settle(p);
    t.check((await p.textContent('#rp-said')).includes('Step 1 · Open the sign-in page'), tag('picking a step did not show it'));
    t.check(JSON.stringify(await box(p, '.rp-stage')) === JSON.stringify(stage), tag('the stage moved when a step was picked'));
    await t.wide(p, tag('replay, open'));

    if (w === 1440) {
      // Watch: the title card, then the thought typing out, then the first action's tag
      await p.click('.rp-watch');
      t.check(await wait(p, () => document.querySelector('.rp-card.on')?.textContent === 'Step 1 of 2 · Open the sign-in page'), tag('no title card'));
      t.check(await wait(p, () => document.querySelector('.rp-bubble.on .rp-bubble-text')?.textContent.startsWith("I'll open the")), tag('no thought'));
      await t.shot(p, 'replay-thought.png');
      t.check(await wait(p, () => document.querySelector('.rp-pill.on')?.textContent === 'Opened app.example.test/sign-in'), tag('no action tag'));
      t.check(await p.$eval('.rp-frames img', i => i.complete && i.naturalWidth === 1280), tag('the frame did not draw'));
      // the stage pauses on a click and resumes from where it was
      await p.click('.rp-catch');
      t.check(await p.$eval('.rp-stage', e => e.classList.contains('paused')) && await p.isVisible('.rp-resume'), tag('did not pause'));
      await p.click('.rp-resume');
      t.check(await p.$eval('.rp-stage', e => e.classList.contains('playing')), tag('did not resume'));
      // the list opens over the page and marks the line on screen
      await p.click('.rp-tools [data-rp="log"]'); await settle(p);
      t.check(await p.$eval('#rp-log', e => e.classList.contains('open')), tag('the list did not open'));
      t.check(await wait(p, () => !!document.querySelector('#rp-log .rp-li.cur:not(.rp-lh)')), tag('the list marks no line'));
      // a line of the list plays from there: the typing shows a key at a time where the field is
      await p.click('#rp-log .rp-li[data-step="1"][data-beat="1"]');
      t.check(await wait(p, () => /^Typing “friend@/.test(document.querySelector('.rp-pill.on')?.textContent || '')), tag('no typing'));
      await t.shot(p, 'replay-typing.png');
      const cursor = await p.$eval('.rp-cursor', e => [parseFloat(e.style.left), parseFloat(e.style.top)]);
      t.check(cursor[0] === 50 && cursor[1] === 41.25, tag('the cursor is not on the field: ' + cursor));
      await p.keyboard.press('Escape'); await settle(p);
      t.check(!(await p.$eval('#rp-log', e => e.classList.contains('open'))), tag('Escape did not close the list'));
      // the recording: the whole run, a secret's stretch hidden
      await p.click('.rp-tools [data-rp="raw"]'); await settle(p);
      t.check(await p.isVisible('.rp-raw'), tag('the recording did not open'));
      t.check(await p.$eval('.rp-stage', e => !e.classList.contains('playing')), tag('the replay kept playing under the recording'));
      await p.$eval('.rp-slider', s => { s.value = '8.8'; s.dispatchEvent(new Event('input', {bubbles: true})); });
      t.check(await p.isVisible('.rp-raw .rp-secret'), tag('the secret stretch is not hidden'));
      t.check((await p.textContent('.rp-time')) === '0:09 / 0:09', tag('time: ' + await p.textContent('.rp-time')));
      t.check(await p.textContent('.rp-raw .rp-who') === 'Second browser' && await p.isVisible('.rp-raw .rp-who'), tag('the second browser is not named on its screen'));
      await p.$eval('.rp-slider', s => { s.value = '0'; s.dispatchEvent(new Event('input', {bubbles: true})); });
      t.check(!(await p.isVisible('.rp-raw .rp-who')), tag('the first browser is named the second'));
      await p.click('.rp-rawplay');
      t.check(await wait(p, () => +document.querySelector('.rp-slider').value > 1), tag('the recording did not play'));
      await p.keyboard.press('Escape'); await settle(p);
      t.check(!(await p.isVisible('.rp-raw')), tag('Escape did not close the recording'));
      await t.shot(p, 'replay-wide.png');
    }

    // a step that did not run says so, with nothing to watch
    await p.click('.rp-row'); await settle(p);
    await p.click('.rp-item:nth-child(2) .rp-row'); await settle(p);
    // a step walked in the second browser says so, and so does its picture
    await p.click('.rp-step[data-step="0"]'); await settle(p);
    t.check((await p.textContent('#rp-said')).includes('Step 1 · Open the profile · second browser'), tag('said: ' + await p.textContent('#rp-said')));
    t.check(await p.isVisible('.rp-body .rp-stage > .rp-who'), tag("the second browser's picture is not named"));
    await p.click('.rp-step[data-step="1"]'); await settle(p);
    t.check(await p.isVisible('.rp-skip') && !(await p.isVisible('.rp-watch')), tag('a step that did not run offers Watch'));
    t.check((await p.textContent('#rp-said')).includes("Step 2 · Change the name: didn't run"), tag('said: ' + await p.textContent('#rp-said')));
    await t.wide(p, tag('replay, second item'));
    await t.shot(p, `replay-${w}.png`);
    await p.click('.backlink'); await settle(p);
    t.check(await p.evaluate(() => location.hash) === '#work', tag('the back link did not return to Work'));

    await p.click('[data-worktab="checks"]'); await ready(p);
    await p.click('.check-row[data-open-doc="c-20261009T140500Z-a1b2c3d"]'); await settle(p);
    t.check(await p.$$eval('#sheet-body .doc-proof .tester', els => els.length) === 2, tag('the check shows no testers'));
    t.check(await p.$$eval('#sheet-body .doc-proof .shots a img', els => els.length) === 3, tag('the check shows no screenshots'));
    t.check(await p.$$eval('#sheet-body .doc-proof .shot-note', els => els.map(e => e.textContent).join()) === 'Second browser', tag("the second browser's screenshot is not named"));
    t.check((await p.textContent('#sheet-body .doc-proof')).includes('No replay: its tester is not known.'), tag('an unknown tester is not said'));
    await t.wide(p, tag('check'));
    await t.shot(p, `check-${w}.png`);
    await p.click('#sheet-body .doc-proof [data-replay]'); await ready(p);
    t.check((await p.textContent('#page .rp-says')).includes('plays as one recording'), tag('no recording offered'));
    await p.click('#page .rp-says [data-rp="raw"]'); await settle(p);
    t.check(await p.isVisible('.rp-raw'), tag('the recording of a run with no steps did not open'));
    await p.keyboard.press('Escape'); await settle(p);
  }
});
