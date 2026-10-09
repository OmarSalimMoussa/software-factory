'use strict';
/* Shared by the dashboard drives: Playwright core, headless Chrome, a fresh live server per drive, and small checks.
   PLAYWRIGHT_CORE and CHROME override where they are found; SHOTS names a folder for screenshots. */
const path = require('path'), fs = require('fs'), os = require('os'), {spawn} = require('child_process');

function playwright() {
  if (process.env.PLAYWRIGHT_CORE) return require(process.env.PLAYWRIGHT_CORE);
  const base = path.join(os.homedir(), '.npm/_npx');
  for (const d of fs.existsSync(base) ? fs.readdirSync(base) : []) {
    const p = path.join(base, d, 'node_modules/playwright-core');
    if (fs.existsSync(p)) return require(p);
  }
  throw new Error('playwright-core was not found; set PLAYWRIGHT_CORE to its folder');
}
const CHROME = process.env.CHROME || '/usr/bin/google-chrome';

/* The real dashboard server on a seeded real-format state, with a fake warden; see tests/dashboard_server/live_server.py. */
async function liveServer() {
  const proc = spawn('python3', [path.join(__dirname, '..', 'dashboard_server', 'live_server.py'), '--port', '0'], {stdio: ['ignore', 'pipe', 'inherit']});
  const url = await new Promise((resolve, reject) => {
    let buf = '';
    proc.stdout.on('data', d => { buf += d; const m = buf.match(/listening on (\S+)/); if (m) resolve(m[1]); });
    proc.on('exit', c => reject(new Error('the live server exited with ' + c)));
  });
  const postJSON = (p, body) => fetch(url + p, {method: 'POST', body: JSON.stringify(body)}).then(r => r.json());
  return {url, stop: () => proc.kill(), set: body => postJSON('/__switch', body), reset: () => postJSON('/__reset', {}), calls: () => fetch(url + '/__calls').then(r => r.json())};
}

async function drive(name, fn) {
  const errors = [], shots = process.env.SHOTS;
  const pw = playwright(), srv = await liveServer();
  const browser = await pw.chromium.launch({executablePath: CHROME, args: ['--headless=new']});
  const t = {
    srv, errors,
    check: (cond, msg) => { if (!cond) errors.push(msg); },
    /* A page on its own context; admitted (with the owner cookie) unless told otherwise. */
    open: async (width, height, hash = 'home', {admit = true} = {}) => {
      const ctx = await browser.newContext({viewport: {width, height}});
      if (admit) await ctx.addCookies([{name: 'owner', value: '1', url: srv.url}]);
      const p = await ctx.newPage();
      const tag = `${width}`;
      p.on('pageerror', e => errors.push(`${tag} pageerror: ${e.message}`));
      p.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push(`${tag} console: ${m.text()}`); });
      await p.goto(srv.url + '/#' + hash);
      await p.addStyleTag({content: '.main{scroll-behavior:auto!important}'});
      if (admit) await ready(p);
      return p;
    },
    wide: async (p, tag) => { if (await p.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)) errors.push(tag + ': sideways scroll'); },
    shot: async (p, file, opts = {}) => { if (shots) { fs.mkdirSync(shots, {recursive: true}); await p.screenshot({path: path.join(shots, file), ...opts}); } },
    code: async (p, digits = '123456') => { for (let i = 0; i < 6; i++) await p.fill('#code-' + i, digits[i]); },
    go: async (p, page) => { await p.evaluate(pg => go(pg), page); await ready(p); }
  };
  try { await fn(t); } catch (e) { errors.push('DRIVER FAILED: ' + (e.stack || e.message)); }
  finally { await browser.close(); srv.stop(); }
  console.log(`${name}: ${errors.length ? '\n  ' + errors.join('\n  ') : 'NO_ERRORS'}`);
  if (errors.length) process.exitCode = 1;
}
/* A page is ready once its data is drawn: no loading line, and the live indicator past "Connecting". */
async function ready(p) {
  await p.waitForFunction(() => document.querySelector('#page') && !document.querySelector('#page .loading') && document.querySelector('#live-text').textContent !== 'Connecting', null, {timeout: 8000});
  await p.waitForTimeout(80);
}
const heights = (p, sel) => p.$$eval(sel, els => els.map(e => Math.round(e.getBoundingClientRect().height)));
const texts = (p, sel) => p.$$eval(sel, els => els.map(e => e.textContent.trim()));

module.exports = {drive, ready, heights, texts};
