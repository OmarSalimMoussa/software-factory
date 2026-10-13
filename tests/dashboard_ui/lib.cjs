'use strict';
/* Shared by the dashboard drives: Playwright core, headless Chrome, a fresh live server per drive, and small checks.
   PLAYWRIGHT_CORE and CHROME override where they are found; SHOTS names a folder for screenshots; CPU_SLOWDOWN runs
   every page's CPU that many times slower, as on a slow runner. */
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

/* The install console on a temporary machine, every action dry; see tests/system/console_live.py. Its url is the
   console's one-time link. */
async function consoleServer() {
  const proc = spawn('python3', [path.join(__dirname, '..', 'system', 'console_live.py')], {stdio: ['ignore', 'pipe', 'inherit']});
  const url = await new Promise((resolve, reject) => {
    let buf = '';
    proc.stdout.on('data', d => { buf += d; const m = buf.match(/listening on (\S+)/); if (m) resolve(m[1]); });
    proc.on('exit', c => reject(new Error('the console exited with ' + c)));
  });
  return {url, stop: () => proc.kill()};
}

async function drive(name, fn, start = liveServer) {
  const errors = [], shots = process.env.SHOTS;
  const pw = playwright(), srv = await start();
  const browser = await pw.chromium.launch({executablePath: CHROME, args: ['--headless=new']});
  const t = {
    srv, errors, browser,
    check: (cond, msg) => { if (!cond) errors.push(msg); },
    /* A page on its own context; admitted (with the owner cookie) unless told otherwise. */
    open: async (width, height, hash = 'home', {admit = true} = {}) => {
      const ctx = await browser.newContext({viewport: {width, height}});
      if (admit) await ctx.addCookies([{name: 'owner', value: '1', url: srv.url}]);
      await ctx.addInitScript(countRequests);
      const p = await ctx.newPage();
      if (process.env.CPU_SLOWDOWN) await (await ctx.newCDPSession(p)).send('Emulation.setCPUThrottlingRate', {rate: Number(process.env.CPU_SLOWDOWN)});
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
    confirm: async p => { await p.click('#confirm-ok'); },
    go: async (p, page) => { await p.evaluate(pg => go(pg), page); await ready(p); }
  };
  try { await fn(t); } catch (e) { errors.push('DRIVER FAILED: ' + (e.stack || e.message)); }
  finally { await browser.close(); srv.stop(); }
  console.log(`${name}: ${errors.length ? '\n  ' + errors.join('\n  ') : 'NO_ERRORS'}`);
  if (errors.length) process.exitCode = 1;
}
/* Runs in each page before its own scripts: counts the requests the page has not yet acted on. A request counts until
   its answer is read (if the page reads it) and every reaction to it has run, so a request that the reaction starts
   is counted before the first one stops counting. */
function countRequests() {
  let open = 0;
  const send = window.fetch, close = () => setTimeout(() => { open--; }, 0);
  window.fetch = (...args) => {
    open++;
    return send(...args).then(res => {
      const read = res.json.bind(res);
      let reading = false;
      res.json = () => { reading = true; return read().finally(close); };
      setTimeout(() => { if (!reading) open--; }, 0);
      return res;
    }, e => { close(); throw e; });
  };
  Object.defineProperty(window, 'openRequests', {get: () => open});
}
/* The page is still: no request open and nothing moving for three frames in a row. A motion that never ends (a
   spinner) or only changes colour (a row's wash) does not count. */
async function settle(p) {
  await p.waitForFunction(token => {
    const moves = a => {
      const props = a instanceof CSSTransition ? [a.transitionProperty] : a.effect.getKeyframes().flatMap(k => Object.keys(k));
      return props.some(x => !/^(offset|easing|composite|computedOffset)$|color|background|shadow/i.test(x));
    };
    const moving = document.getAnimations().some(a => a.playState === 'running' && a.effect.getComputedTiming().endTime !== Infinity && moves(a));
    if (window.stillToken !== token) Object.assign(window, {stillToken: token, stillFrames: 0});
    window.stillFrames = window.openRequests || moving ? 0 : window.stillFrames + 1;
    return window.stillFrames >= 3;
  }, Math.random(), {polling: 'raf', timeout: 15000});
}
/* A page is ready once its data is drawn and it is still: something drawn and no loading line, the live indicator past
   "Connecting", no request open. A tab or filter keeps the old view drawn while its own loads: the open request is
   what tells them apart. */
async function ready(p) {
  await p.waitForFunction(() => document.querySelector('#page').childElementCount && !document.querySelector('#page .loading') && document.querySelector('#live-text').textContent !== 'Connecting' && window.openRequests === 0, null, {timeout: 15000});
  await settle(p);
}
/* Whether the page reaches a state within a few seconds: for a state that arrives on the server's own time, or one
   that shows only for a moment. */
const until = (p, fn, arg) => p.waitForFunction(fn, arg, {timeout: 8000}).then(() => true, () => false);
/* One edge or size of an element, read on every frame while act runs and the page settles (null while it is absent):
   the in-between steps of a motion. The page's motion plays ten times slower meanwhile, so a starved machine that
   draws few frames still draws some of them mid-way. */
async function frames(p, sel, side, act) {
  const cdp = await p.context().newCDPSession(p);
  await cdp.send('Animation.enable'); await cdp.send('Animation.setPlaybackRate', {playbackRate: 0.1});
  await p.evaluate(([sel, side]) => {
    const log = window.frameLog = [];
    const read = () => { const el = document.querySelector(sel); log.push(el ? el.getBoundingClientRect()[side] : null); window.frameTick = requestAnimationFrame(read); };
    read();
  }, [sel, side]);
  await act();
  await settle(p);
  const log = await p.evaluate(() => { cancelAnimationFrame(window.frameTick); return window.frameLog; });
  await cdp.send('Animation.setPlaybackRate', {playbackRate: 1}); await cdp.detach();
  return log;
}
const heights = (p, sel) => p.$$eval(sel, els => els.map(e => Math.round(e.getBoundingClientRect().height)));
const texts = (p, sel) => p.$$eval(sel, els => els.map(e => e.textContent.trim()));

module.exports = {drive, ready, settle, until, frames, heights, texts, consoleServer};
