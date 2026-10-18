'use strict';
/* Agents: aligned columns, a tree that slides open and shut, Stop with what it stops, History read-only and paged,
   Pause and Resume from the orchestrator's panel, and stopping the whole factory behind the code. */
const {drive, settle, until, frames, heights, texts} = require('./lib.cjs');

drive('agents', async t => {
  const p = await t.open(1440, 900, 'agents');
  const state = id => p.$eval(`[data-node="${id}"] > .node-card .state`, e => e.textContent.trim()).catch(() => 'gone');
  // the metadata columns line up down the tree, whatever the names
  const cols = await p.$$eval('.node-card', cs => cs.map(c => [...c.children].slice(3).map(x => Math.round(x.getBoundingClientRect().left))));
  for (let i = 0; i < 4; i++) { const xs = new Set(cols.map(c => c[i])); t.check(xs.size === 1, `column ${i} not aligned: ${[...xs]}`); }
  // collapsing slides: mid-way the children are partly shown, at the end they are gone
  const kidsH = () => p.$eval('[data-node="impl-sign-in-loop"] > .kids-wrap', e => e.getBoundingClientRect().height);
  const full = await kidsH();
  const seen = await frames(p, '[data-node="impl-sign-in-loop"] > .kids-wrap', 'height', () => p.click('[data-toggle="impl-sign-in-loop"]'));
  const end = await kidsH();
  t.check(seen.some(h => h > 1 && h < full), `no collapse motion: ${full} -> ${seen}`);
  t.check(end <= 1, 'children still showing after collapse: ' + end);
  await p.click('[data-toggle="impl-sign-in-loop"]'); await settle(p);
  t.check(Math.abs(await kidsH() - full) <= 1, 'expand did not return to full height');
  await p.click('[data-tree="close"]'); await settle(p);
  t.check((await p.$$('.node.closed')).length === 3, 'Collapse all did not close every parent');
  await p.click('[data-tree="open"]'); await settle(p);
  // every running agent has a Stop button in one column; the retired one has none and is dimmed
  const xs = await p.$$eval('.node-card .kill', els => [...new Set(els.map(e => Math.round(e.getBoundingClientRect().left)))]); t.check(xs.length === 1, 'stop buttons not aligned: ' + xs);
  t.check((await p.$$('.node-card .kill')).length === (await p.$$('.node:not(.retired)')).length, 'stop buttons do not match running agents');
  t.check(await p.$('[data-node="qa-sec-1"].retired') && !(await p.$('[data-node="qa-sec-1"] > .node-card .kill')), 'retired agent offers Stop');
  t.check((await state('impl-project-names')).includes('deprioritized'), 'deprioritized not shown');
  t.check((await p.textContent('.capbars')).includes('1 deprioritized'), 'capacity does not count the deprioritized agent');
  await t.shot(p, 'agents-1440.png');
  // History: aligned columns, a broken row that says so, ten to a page with Newer and Older, a read-only panel
  const histRows = () => p.$$eval('#history > .row', rs => rs.map(r => r.textContent.trim().slice(0, 40)));
  const first = await histRows(), label = async () => (await texts(p, '.pager .faint'))[0];
  t.check(first.length === 10 && await p.$('#history .row.broken'), 'History first page wrong: ' + first.length);
  const hc = await p.$$eval('#history .hist-row', rs => [...new Set(rs.map(r => Math.round(r.children[2].getBoundingClientRect().left)))]); t.check(hc.length === 1, 'History columns not aligned');
  t.check(/^1–10 of \d+$/.test(await label()) && await p.$('.pager [data-hist-page]:disabled'), 'History pager wrong on the first page: ' + await label());
  const shows = start => until(p, s => (document.querySelector('.pager .faint')?.textContent || '').startsWith(s), start);
  await p.click('.pager [data-hist-page]:last-child');
  t.check(await shows('11–20 of') && (await histRows())[0] !== first[0], 'Older did not show the next ten: ' + await label());
  await p.click('.pager [data-hist-page]:nth-child(2)');
  t.check(await shows('1–10 of') && (await histRows())[0] === first[0], 'Newer did not come back: ' + await label());
  await p.click('#history .hist-row'); await settle(p);
  t.check(!(await p.$('#sheet [data-stop], #sheet [data-stopnow]')), 'a History record offers an action');
  t.check((await p.textContent('#sheet-body')).includes('can never be resumed'), 'History record does not say it is final');
  await p.keyboard.press('Escape'); await settle(p);
  // a message from a working session's panel: its thread above, the warden's word in a toast, the box cleared
  await p.click('[data-agent="impl-sign-in-loop"] .nname'); await settle(p);
  t.check((await p.textContent('#sheet-body .thread')).includes('Check Safari first'), 'no thread in the panel');
  await p.fill('#sheet-body [data-msg="impl-sign-in-loop"]', 'Look at Safari first, please.');
  await p.click('#sheet-body [data-send-msg="impl-sign-in-loop"]');
  t.check(await until(p, () => document.querySelector('#toasts').textContent.includes('as soon as its current turn ends')), 'no toast for the message');
  await settle(p);
  t.check(await p.inputValue('#sheet-body [data-msg="impl-sign-in-loop"]') === '', 'the message box was not cleared');
  const sent = (await t.srv.calls()).filter(c => c.path === '/api/agent/message').at(-1);
  t.check(sent && sent.body.session === 'impl-sign-in-loop' && sent.body.text === 'Look at Safari first, please.', 'the message was not sent');
  await p.keyboard.press('Escape'); await settle(p);
  // a benched session comes back with the message; a subagent has no box, and Open chat says why it is grey
  await p.click('[data-agent="impl-project-names"] .nname'); await settle(p);
  t.check((await p.textContent('#sheet-body [data-send-msg="impl-project-names"]')).includes('Bring back with this message'), 'a benched panel lacks Bring back');
  await p.keyboard.press('Escape'); await settle(p);
  await p.click('[data-agent="qa-ui-1"] .nname'); await settle(p);
  t.check(!(await p.$('#sheet-body [data-msg]')), 'a subagent offers a message box');
  t.check(await p.$eval('#sheet-foot [data-chat-note]', e => e.disabled && e.title === "A subagent runs inside its parent's session: open the parent's chat."), 'Open chat does not say why it is grey');
  await p.keyboard.press('Escape'); await settle(p);
  // stop a tester: the server lists what stops; the tester stops and its parent keeps working
  await p.click('[data-stop="qa-vis-1"]'); await settle(p);
  t.check((await p.textContent('#sheet-title')) === 'Stop qa-visual?', 'no confirm for a tester');
  await p.click('#sheet-foot [data-stopnow="qa-vis-1"]'); await settle(p);
  t.check((await state('qa-vis-1')).includes('stopped'), 'tester not stopped');
  t.check((await state('impl-sign-in-loop')).includes('working'), 'stopping a tester stopped its parent');
  const hs = [...new Set(await heights(p, '.node-card'))]; t.check(hs.length === 1, 'tree rows differ in height: ' + hs);
  const sq = await p.$eval('[data-node="qa-vis-1"] > .node-card .state .dot', e => { const cs = getComputedStyle(e); return [Math.round(e.getBoundingClientRect().width), cs.borderRadius]; });
  t.check(sq[0] <= 10 && sq[1] !== '50%', 'stopped marker is not a small square: ' + sq);
  // a parent: the confirm lists it and the one tester still running under it
  await p.click('[data-stop="impl-sign-in-loop"]'); await settle(p);
  t.check((await p.$$('.stoplist > div')).length === 2, 'confirm should list the implementer and its running tester');
  await t.shot(p, 'stop-confirm-1440.png');
  await p.keyboard.press('Escape'); await settle(p);
  // from an agent's panel, Keep running goes back to that panel
  await p.click('[data-agent="design-empty-states"] .nname'); await settle(p);
  await p.click('#sheet-foot [data-stop="design-empty-states"]'); await settle(p);
  await p.click('#sheet-foot [data-action="sheet-back"]'); await settle(p);
  t.check((await p.textContent('#sheet-title')).trim() === 'Clearer empty states on Projects and Invoices', 'Keep running did not return to the agent');
  await p.keyboard.press('Escape'); await settle(p);
  // Pause from the orchestrator's panel: one click, the steps, then the paused banner with Resume
  await p.click('[data-agent="orchestrator"] .nname'); await settle(p);
  t.check(await p.$('#sheet-foot [data-action="pause"]') && await p.$('#sheet-foot [data-stop="orchestrator"]') && await p.$('#sheet-foot a[href*="claude.ai"]'), 'orchestrator panel lacks Pause, Stop or Open chat');
  await p.click('#sheet-foot [data-action="pause"]'); await settle(p);
  await t.confirm(p);
  t.check(await until(p, () => document.querySelector('.steps li')), 'pausing shows no steps');
  await p.waitForFunction(() => (document.querySelector('.ro-banner') || {}).textContent?.includes('The factory is paused'), null, {timeout: 8000}).catch(() => t.errors.push('no paused banner'));
  await t.shot(p, 'paused-1440.png');
  await p.click('[data-action="resume"]'); await settle(p); await t.confirm(p);
  await p.waitForFunction(() => !document.querySelector('.ro-banner'), null, {timeout: 8000}).catch(() => t.errors.push('banner stayed after Resume'));
  // stopping the whole factory asks to confirm it first; then the stopped banner offers Start
  await p.click('[data-stop="orchestrator"]'); await settle(p);
  await p.click('#sheet-foot [data-stopnow="orchestrator"]'); await settle(p);
  t.check(await p.$('#confirm-ok') && !(await p.$('#code-0')), 'stopping the factory is not confirmed first, without a code');
  await t.confirm(p);
  t.check(await until(p, () => document.querySelector('.ro-banner [data-action="start-orch"]')), 'no stopped-factory banner');
  await settle(p);
  t.check(!(await p.$('.node-card .kill')), 'agents still stoppable after stopping the factory');
  await p.click('[data-action="start-orch"]');
  // a stopped orchestrator never comes back: the start is a new session at the root of the tree
  const started = await until(p, () => { const n = document.querySelector('.tree > .node'), s = n && n.querySelector(':scope > .node-card .state'); return s && n.dataset.node !== 'orchestrator' && s.textContent.includes('working'); });
  await settle(p);
  t.check(started, 'orchestrator did not start: ' + await p.$eval('.tree > .node', e => e.dataset.node));
  await p.setViewportSize({width: 1024, height: 800}); await settle(p);
  const th = [...new Set(await heights(p, '.node-card'))]; t.check(th.length === 1, '1024 tree rows differ: ' + th);
  await t.wide(p, '1024 agents');
  await p.close();
  const m = await t.open(390, 844, 'agents');
  await t.wide(m, '390 agents');
  const mh = [...new Set(await m.$$eval('#history .hist-row', rs => rs.map(r => r.clientHeight)))]; t.check(mh.length === 1, 'phone History rows differ: ' + mh);
  const kb = await m.$eval('.node-card .kill', e => { const r = e.getBoundingClientRect(); return [r.width, r.height]; }); t.check(kb[0] >= 24 && kb[1] >= 24, 'stop target under 24 px');
  await t.shot(m, 'agents-390.png');
  await m.close();
  // a benched implementer's panel says what it waits on, from its first draw and unmoved by a refresh: its merge's
  // check, or, on hold, the task it waits for, which opens on a click
  await t.srv.reset(); await t.srv.set({scenario: 'waits'});
  const w = await t.open(1440, 900, 'agents');
  await w.click('[data-agent="impl-staging-down"] .nname'); await settle(w);
  const said = await texts(w, '#sheet-body .wait-line');
  t.check(said.length === 1 && /^Check failed on [0-9a-f]{7}, 1 h ago: phone-lists$/.test(said[0]), 'a merged implementer does not say its check failed: ' + said);
  const top = () => w.$eval('#sheet-body .wait-line', e => Math.round(e.getBoundingClientRect().top));
  const before = await top();
  await w.evaluate(() => refreshSheet(['all'])); await settle(w);
  t.check(await top() === before, 'the check line moved when the panel refreshed');
  await t.shot(w, 'agent-merged-1440.png');
  await w.keyboard.press('Escape'); await settle(w);
  await w.click('[data-agent="impl-project-names"] .nname'); await settle(w);
  t.check((await texts(w, '#sheet-body .wait-task')).join() === 'Staging is down after the last deploy', 'an implementer on hold does not name the task it waits for');
  t.check((await texts(w, '#sheet-body .wait-line')).join() === said.join(), 'an implementer on hold does not say how the check of the task it waits for stands');
  const tw = await w.$eval('#sheet-body .wait-task', e => e.getBoundingClientRect().height); t.check(tw >= 24, 'the task it waits for is a target under 24 px');
  await t.shot(w, 'agent-on-hold-1440.png');
  await w.setViewportSize({width: 390, height: 844}); await settle(w);
  await t.wide(w, '390 panel of an implementer on hold'); await t.shot(w, 'agent-on-hold-390.png');
  await w.click('#sheet-body .wait-task'); await settle(w);
  t.check((await w.textContent('#sheet-title')).includes('Staging is down after the last deploy'), 'the task it waits for did not open');
});
