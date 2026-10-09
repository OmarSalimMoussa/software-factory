'use strict';
/* Agents: aligned columns, a tree that slides open and shut, Stop with what it stops, History read-only and paged,
   Pause and Resume from the orchestrator's panel, and stopping the whole factory behind the code. */
const {drive, ready, heights} = require('./lib.cjs');

drive('agents', async t => {
  const p = await t.open(1440, 900, 'agents');
  const state = id => p.$eval(`[data-node="${id}"] > .node-card .state`, e => e.textContent.trim()).catch(() => 'gone');
  // the metadata columns line up down the tree, whatever the names
  const cols = await p.$$eval('.node-card', cs => cs.map(c => [...c.children].slice(3).map(x => Math.round(x.getBoundingClientRect().left))));
  for (let i = 0; i < 4; i++) { const xs = new Set(cols.map(c => c[i])); t.check(xs.size === 1, `column ${i} not aligned: ${[...xs]}`); }
  // collapsing slides: mid-way the children are partly shown, at the end they are gone
  const kidsH = () => p.$eval('[data-node="impl-sign-in-loop"] > .kids-wrap', e => e.getBoundingClientRect().height);
  const full = await kidsH();
  await p.click('[data-toggle="impl-sign-in-loop"]'); await p.waitForTimeout(110);
  const mid = await kidsH(); await p.waitForTimeout(400); const end = await kidsH();
  t.check(mid > 1 && mid < full, `no collapse motion: ${full} -> ${mid}`);
  t.check(end <= 1, 'children still showing after collapse: ' + end);
  await p.click('[data-toggle="impl-sign-in-loop"]'); await p.waitForTimeout(450);
  t.check(Math.abs(await kidsH() - full) <= 1, 'expand did not return to full height');
  await p.click('[data-tree="close"]'); await p.waitForTimeout(450);
  t.check((await p.$$('.node.closed')).length === 3, 'Collapse all did not close every parent');
  await p.click('[data-tree="open"]'); await p.waitForTimeout(450);
  // every running agent has a Stop button in one column; the retired one has none and is dimmed
  const xs = await p.$$eval('.node-card .kill', els => [...new Set(els.map(e => Math.round(e.getBoundingClientRect().left)))]); t.check(xs.length === 1, 'stop buttons not aligned: ' + xs);
  t.check((await p.$$('.node-card .kill')).length === (await p.$$('.node:not(.retired)')).length, 'stop buttons do not match running agents');
  t.check(await p.$('[data-node="qa-sec-1"].retired') && !(await p.$('[data-node="qa-sec-1"] > .node-card .kill')), 'retired agent offers Stop');
  t.check((await state('impl-project-names')).includes('deprioritized'), 'deprioritized not shown');
  t.check((await p.textContent('.capbars')).includes('1 deprioritized'), 'capacity does not count the deprioritized agent');
  await t.shot(p, 'agents-1440.png');
  // History: aligned columns, a broken row that says so, Show more, a read-only panel
  t.check((await p.$$('#history .hist-row')).length === 24 && await p.$('#history .row.broken'), 'History first page wrong');
  const hc = await p.$$eval('#history .hist-row', rs => [...new Set(rs.map(r => Math.round(r.children[2].getBoundingClientRect().left)))]); t.check(hc.length === 1, 'History columns not aligned');
  await p.click('[data-more="history"]'); await p.waitForTimeout(500);
  t.check((await p.$$('#history .hist-row')).length === 32 && !(await p.$('[data-more="history"]')), 'History Show more failed');
  await p.click('#history .hist-row'); await p.waitForTimeout(400);
  t.check(!(await p.$('#sheet [data-stop], #sheet [data-stopnow]')), 'a History record offers an action');
  t.check((await p.textContent('#sheet-body')).includes('can never be resumed'), 'History record does not say it is final');
  await p.keyboard.press('Escape'); await p.waitForTimeout(350);
  // stop a tester: the server lists what stops; the tester stops and its parent keeps working
  await p.click('[data-stop="qa-vis-1"]'); await p.waitForTimeout(400);
  t.check((await p.textContent('#sheet-title')) === 'Stop qa-visual?', 'no confirm for a tester');
  await p.click('#sheet-foot [data-stopnow="qa-vis-1"]'); await p.waitForTimeout(600);
  t.check((await state('qa-vis-1')).includes('stopped'), 'tester not stopped');
  t.check((await state('impl-sign-in-loop')).includes('working'), 'stopping a tester stopped its parent');
  const hs = [...new Set(await heights(p, '.node-card'))]; t.check(hs.length === 1, 'tree rows differ in height: ' + hs);
  const sq = await p.$eval('[data-node="qa-vis-1"] > .node-card .state .dot', e => { const cs = getComputedStyle(e); return [Math.round(e.getBoundingClientRect().width), cs.borderRadius]; });
  t.check(sq[0] <= 10 && sq[1] !== '50%', 'stopped marker is not a small square: ' + sq);
  // a parent: the confirm lists it and the one tester still running under it
  await p.click('[data-stop="impl-sign-in-loop"]'); await p.waitForTimeout(400);
  t.check((await p.$$('.stoplist > div')).length === 2, 'confirm should list the implementer and its running tester');
  await t.shot(p, 'stop-confirm-1440.png');
  await p.keyboard.press('Escape'); await p.waitForTimeout(350);
  // from an agent's panel, Keep running goes back to that panel
  await p.click('[data-agent="design-empty-states"] .nname'); await p.waitForTimeout(400);
  await p.click('#sheet-foot [data-stop="design-empty-states"]'); await p.waitForTimeout(400);
  await p.click('#sheet-foot [data-action="sheet-back"]'); await p.waitForTimeout(300);
  t.check((await p.textContent('#sheet-title')).trim() === 'Empty states', 'Keep running did not return to the agent');
  await p.keyboard.press('Escape'); await p.waitForTimeout(350);
  // Pause from the orchestrator's panel: the code, the steps, then the paused banner with Resume
  await p.click('[data-agent="orchestrator"] .nname'); await p.waitForTimeout(400);
  t.check(await p.$('#sheet-foot [data-action="pause"]') && await p.$('#sheet-foot [data-stop="orchestrator"]') && await p.$('#sheet-foot a[href*="claude.ai"]'), 'orchestrator panel lacks Pause, Stop or Open chat');
  await p.click('#sheet-foot [data-action="pause"]'); await p.waitForTimeout(400);
  await t.code(p); await p.waitForTimeout(400);
  t.check(await p.$('.steps li'), 'pausing shows no steps');
  await p.waitForFunction(() => (document.querySelector('.ro-banner') || {}).textContent?.includes('The factory is paused'), null, {timeout: 8000}).catch(() => t.errors.push('no paused banner'));
  await t.shot(p, 'paused-1440.png');
  await p.click('[data-action="resume"]'); await p.waitForTimeout(400); await t.code(p);
  await p.waitForFunction(() => !document.querySelector('.ro-banner'), null, {timeout: 8000}).catch(() => t.errors.push('banner stayed after Resume'));
  // stopping the whole factory needs the code; then the stopped banner offers Start
  await p.click('[data-stop="orchestrator"]'); await p.waitForTimeout(400);
  await p.click('#sheet-foot [data-stopnow="orchestrator"]'); await p.waitForTimeout(400);
  t.check(await p.$('#code-0'), 'no code asked to stop the factory');
  await t.code(p); await p.waitForTimeout(800);
  t.check(!(await p.$('.node-card .kill')), 'agents still stoppable after stopping the factory');
  t.check(await p.$('.ro-banner [data-action="start-orch"]'), 'no stopped-factory banner');
  await p.click('[data-action="start-orch"]'); await p.waitForTimeout(600);
  t.check((await state('orchestrator')).includes('working'), 'orchestrator did not start');
  await p.setViewportSize({width: 1024, height: 800}); await p.waitForTimeout(200);
  const th = [...new Set(await heights(p, '.node-card'))]; t.check(th.length === 1, '1024 tree rows differ: ' + th);
  await t.wide(p, '1024 agents');
  await p.close();
  const m = await t.open(390, 844, 'agents');
  await t.wide(m, '390 agents');
  const mh = [...new Set(await m.$$eval('#history .hist-row', rs => rs.map(r => r.clientHeight)))]; t.check(mh.length === 1, 'phone History rows differ: ' + mh);
  const kb = await m.$eval('.node-card .kill', e => { const r = e.getBoundingClientRect(); return [r.width, r.height]; }); t.check(kb[0] >= 24 && kb[1] >= 24, 'stop target under 24 px');
  await t.shot(m, 'agents-390.png');
});
