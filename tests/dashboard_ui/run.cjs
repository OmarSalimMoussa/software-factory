'use strict';
/* Runs every dashboard drive, a few at a time, and prints each one's result. Exits non-zero if any drive found a problem.
   Usage: node tests/dashboard_ui/run.cjs [drive names...]; DRIVES_AT_ONCE sets how many run at once (3 unless set). */
const path = require('path'), fs = require('fs'), {spawn} = require('child_process');

const AT_ONCE = process.env.DRIVES_AT_ONCE === undefined ? 3 : Number(process.env.DRIVES_AT_ONCE);
if (!Number.isInteger(AT_ONCE) || AT_ONCE < 1) throw new Error(`DRIVES_AT_ONCE must be a whole number, 1 or more: ${process.env.DRIVES_AT_ONCE}`);
const all = fs.readdirSync(__dirname).filter(f => /^drive-.*\.cjs$/.test(f)).map(f => f.slice(6, -4)).sort();
const want = process.argv.slice(2).length ? process.argv.slice(2) : all;
const run = name => new Promise(resolve => {
  const t0 = Date.now(), proc = spawn(process.execPath, [path.join(__dirname, `drive-${name}.cjs`)], {stdio: ['ignore', 'pipe', 'pipe']});
  let out = '';
  proc.stdout.on('data', d => { out += d; }); proc.stderr.on('data', d => { out += d; });
  proc.on('exit', code => resolve({name, code, out: out.trim(), s: ((Date.now() - t0) / 1000).toFixed(1)}));
});
(async () => {
  const queue = [...want], results = [];
  await Promise.all(Array.from({length: AT_ONCE}, async () => { while (queue.length) results.push(await run(queue.shift())); }));
  results.sort((a, b) => a.name.localeCompare(b.name)).forEach(r => console.log(`${r.out}  (${r.s} s)`));
  const bad = results.filter(r => r.code !== 0);
  console.log(bad.length ? `FAILED: ${bad.map(r => r.name).join(', ')}` : `ALL ${results.length} DRIVES PASSED`);
  process.exitCode = bad.length ? 1 : 0;
})();
