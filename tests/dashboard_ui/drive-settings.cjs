'use strict';
/* Settings: a control for each kind, changed rows with "was", Undo and Reset to default, the save bar, the coded save
   with its steps, Change repo sending the path (and refused while running), a conflict from another device. */
const {drive, settle, until, texts} = require('./lib.cjs');

drive('settings', async t => {
  const p = await t.open(1440, 900, 'settings');
  const bar = () => p.textContent('#savebar-n');
  t.check(await p.$('[data-row="models.intake"] [data-ddbtn]') && await p.$('[data-row="caps.designers"] .stepper') && await p.$('[data-row="budget.slice_memory_high"] input.txt')
    && await p.$('[data-row="sandbox.allowed_domains"] .listed') && await p.$('[data-row="prices"] .ro'), 'a control kind is missing');
  t.check(!(await p.$('[data-row="prices"] .reset, [data-row="prices"] [data-default]')), 'a read-only row can be changed');
  await p.click('[data-row="models.intake"] [data-ddbtn]'); await p.click('[data-ddopt][data-ddkind="model"][data-ddkey="models.intake"][data-v="Haiku 5.5"]');
  await p.click('[data-step="1"][data-key="caps.designers"]');
  t.check((await bar()).includes('2 changes'), 'savebar: ' + await bar());
  t.check((await p.textContent('[data-row="models.intake"] .was')).includes('was Sonnet 5.5 · medium'), 'no "was" on a changed model');
  await p.fill('[data-row="budget.slice_memory_high"] input', '10G'); await p.press('[data-row="budget.slice_memory_high"] input', 'Tab');
  await p.fill('[data-row="sandbox.allowed_domains"] [data-listadd]', 'example.org'); await p.press('[data-row="sandbox.allowed_domains"] [data-listadd]', 'Enter');
  t.check((await texts(p, '[data-row="sandbox.allowed_domains"] .listed .chip')).some(x => x.includes('example.org')), 'list item not added');
  t.check((await bar()).includes('4 changes'), 'text and list edits not counted: ' + await bar());
  await p.click('[data-listdel="sandbox.allowed_domains"][data-i="3"]');
  t.check((await bar()).includes('3 changes'), 'removing the added item did not undo it');
  // Reset to default: implementers are saved at 4, the default is 3
  await p.click('[data-default="caps.implementers"]');
  t.check(await p.inputValue('[data-setn="caps.implementers"]') === '3' && (await p.textContent('[data-row="caps.implementers"] .was')).includes('was 4'), 'Reset to default failed');
  await p.click('[data-resetkey="caps.implementers"]');
  t.check(await p.inputValue('[data-setn="caps.implementers"]') === '4', 'Undo failed');
  // the stepper keeps inside its range
  for (let i = 0; i < 6; i++) await p.click('[data-step="1"][data-key="caps.designers"]');
  t.check(await p.inputValue('[data-setn="caps.designers"]') === '4', 'stepper went past its range');
  await t.shot(p, 'settings-changed-1440.png');
  await p.click('[data-action="discard"]'); await settle(p);
  t.check(!(await p.$eval('#savebar', e => e.classList.contains('show'))), 'savebar still shows after Discard');
  // save: the review lists each change, the code, the steps, then the audit
  await p.click('[data-row="models.intake"] [data-ddbtn]'); await p.click('[data-ddopt][data-ddkind="model"][data-ddkey="models.intake"][data-v="Haiku 5.5"]');
  await p.click('[data-step="1"][data-key="caps.designers"]');
  await p.click('[data-action="review"]'); await settle(p);
  t.check((await p.textContent('#sheet-body .changes')).includes('Sonnet 5.5 · medium → Haiku 5.5 · medium'), 'review does not list the change');
  await t.code(p);
  t.check(await until(p, () => document.querySelector('#sheet-title').textContent === 'Saving settings' && document.querySelector('.steps li')), 'no save steps');
  await p.waitForFunction(() => document.querySelector('.audit') && document.querySelector('.audit').textContent.includes('Haiku 5.5'), null, {timeout: 8000}).catch(() => t.errors.push('audit did not record the save'));
  const save = (await t.srv.calls()).filter(c => c.method === 'POST' && c.path === '/api/settings').at(-1);
  t.check(save && JSON.stringify(save.body.changes) === '{"models.intake":{"model":"Haiku 5.5","effort":"medium"},"caps.designers":3}', 'save sent: ' + JSON.stringify(save && save.body.changes));
  t.check(!(await p.$eval('#savebar', e => e.classList.contains('show'))), 'savebar still shows after saving');
  // Change repo sends the path; the factory refuses while it runs, and says why (it is not a wrong code)
  await p.click('[data-action="change-repo"]'); await settle(p);
  await p.fill('#repo-path', '/srv/products/other-app'); await t.code(p); await settle(p);
  t.check((await p.textContent('#code-note')).includes('Stop the factory first'), 'repo refusal not shown: ' + await p.textContent('#code-note'));
  const repo = (await t.srv.calls()).filter(c => c.method === 'POST' && c.path === '/api/settings/repo').at(-1); t.check(repo && repo.body.path === '/srv/products/other-app', 'repo path not sent');
  await p.keyboard.press('Escape'); await settle(p);
  // an edit made against a value another device then changed is dropped, and the owner is told
  await p.click('[data-step="1"][data-key="rounds.qa"]');
  await t.srv.set({saved: {'rounds.qa': 6}});
  await p.evaluate(() => loadPage()); await settle(p);
  t.check(await p.inputValue('[data-setn="rounds.qa"]') === '6', 'the conflicting edit was kept');
  t.check((await p.textContent('#toasts')).includes('changed on another device'), 'no conflict toast');
  await p.close();
  const m = await t.open(390, 844, 'settings');
  await m.click('[data-step="1"][data-key="caps.designers"]'); await settle(m);
  await t.wide(m, '390 settings');
  const [sb, nav] = await m.evaluate(() => [document.querySelector('#savebar').getBoundingClientRect().bottom, document.querySelector('#bottomnav').getBoundingClientRect().top]);
  t.check(sb <= nav, 'phone: the save bar sits under the bottom bar');
  await t.shot(m, 'settings-390.png');
});
