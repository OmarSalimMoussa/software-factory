'use strict';
/* A tester run's replay page: a row for each item it tested, with its verdict. Opening one shows a step's expected
   and got, the steps, and the player. The tester's thoughts and actions, and the run as recorded, sit behind a
   button each, so the player keeps its size. */

const RP = {url: null, open: null, pick: {}, log: false, player: null, raw: null};

PAGE.replay = {
  url: () => UI.replay,
  render: env => part(env, 'replay', drawReplay),
  after: env => bindReplay(env)
};
function openReplay(url) { closeSheet(); UI.replay = url; go('replay', {back: true}); }

function rpState() {
  if (RP.url !== UI.replay) Object.assign(RP, {url: UI.replay, open: null, pick: {}, log: false});
  return RP;
}
const rpTone = (text, tone) => `<span style="color:${toneColor(tone)}">${esc(text)}</span>`;
const rpPick = r => RP.pick[r.key] != null ? RP.pick[r.key] : r.first;

function drawReplay(d) {
  const S = rpState();
  const head = `<div class="page-h"><div><h1>${esc(d.title)}</h1><div class="sub">${d.summary ? rpTone(d.summary.text, d.summary.tone) + ' · ' : ''}${esc(d.sub)}</div></div></div>`;
  if (d.says) return head + `<div class="card empty rp-says">${esc(d.says)}</div>`;
  if (d.empty) return head + `<div class="card empty rp-says">${esc(d.empty)}<button class="btn sm" data-rp="raw" aria-haspopup="dialog">${icon('video-camera', 'sm')}Recording</button></div>` + rawHTML(d);
  const open = d.rows.find(r => r.key === S.open);
  return head + `<div class="rp-rows">${d.rows.map((r, i) => rowHTML(d, r, i)).join('')}</div>` + (open ? logHTML(open) : '') + rawHTML(d);
}
function rowHTML(d, r, i) {
  const open = RP.open === r.key, s = r.steps[rpPick(r)];
  const body = open ? `<div class="rp-body">${saidHTML(s)}${stepsHTML(r)}${stageHTML(d)}</div>` : '';
  return `<div class="card rp-item"><button class="rp-row" data-rp="open" data-row="${i}" aria-expanded="${open}"><span style="color:${toneColor(r.tone)}">${icon(r.icon, 'lg')}</span><span class="rp-name">${esc(r.name)}</span>${r.severity ? pill(r.severity.text, r.severity.tone) : ''}<span class="rp-note">${esc(r.note)}</span>${icon('chevron-down', 'rp-chev')}</button>${body}</div>`;
}
const saidHTML = s => `<div class="rp-said" id="rp-said"><div class="rp-said-h">${esc(s.head)}</div><div class="rp-eg"><span>Expected</span><span>${esc(s.expected)}</span><span>Got</span><b style="color:${toneColor(s.tone)}">${esc(s.got)}</b></div>${s.hint ? `<div class="rp-hint">${esc(s.hint)}</div>` : ''}</div>`;
function stepsHTML(r) {
  const pick = rpPick(r);
  return `<div class="rp-steps" role="group" aria-label="Steps">${r.steps.map((s, j) => `<button class="rp-step" data-rp="step" data-step="${j}" aria-pressed="${j === pick}"><span style="color:${toneColor(s.tone)}">${icon(s.icon, 'sm')}</span>${s.n}. ${esc(s.label)}${s.rough ? '<i class="rp-rough" title="Took several tries"></i>' : ''}</button>`).join('')}</div>`;
}
function stageHTML(d) {
  const f = d.frames[0];
  return `<div class="rp-stage idle" style="--sw:${f.w};--sh:${f.h}">
    <div class="rp-screen"><div class="rp-frames"></div><div class="rp-secret">${esc(d.hidden_says)}</div><div class="rp-who">${esc(d.second_says)}</div>
      <div class="rp-flash"></div><div class="rp-boxes"></div><span class="rp-ripple"></span><span class="rp-cursor"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 2.5 19 12.2l-6.3 1.2 3.7 7.4-3 1.5-3.7-7.4L5 19.4Z"/></svg></span>
      <div class="rp-bubble" role="status"><i></i><i></i><span class="rp-bubble-text"></span></div><span class="rp-pill"></span></div>
    <img class="rp-still" alt="The screen at the end of this step"><div class="rp-who">${esc(d.second_says)}</div><div class="rp-skip">Didn't run</div><div class="rp-card"></div>
    <button class="rp-catch" data-rp="toggle" aria-label="Pause"></button>
    <button class="rp-watch" data-rp="watch">${icon('play')}Watch</button>
    <button class="rp-resume" data-rp="toggle" aria-label="Resume">${icon('play', 'lg')}</button>
    <div class="rp-done"><button data-rp="again">Watch again</button></div>
    <div class="rp-tools"><button data-rp="raw" aria-haspopup="dialog">${icon('video-camera', 'sm')}Recording</button><button data-rp="log" aria-expanded="${RP.log}" aria-controls="rp-log">${icon('list-bullet', 'sm')}Thoughts and actions</button></div>
  </div>`;
}
function logHTML(r) {
  const items = r.steps.map((s, j) => `<button class="rp-li rp-lh" data-rp="pick" data-step="${j}">${icon(s.icon, 'sm')}${esc(s.head)}</button>` + s.beats.map((b, k) =>
    `<button class="rp-li ${b.k === 'think' ? 'rp-thought' : 'rp-act'}${b.bad ? ' bad' : ''}" data-rp="go" data-step="${j}" data-beat="${k}">${b.k === 'think' ? '' : icon(b.icon, 'sm')}<span>${esc(b.log)}</span></button>`).join('')).join('');
  return `<aside class="rp-log${RP.log ? ' open' : ''}" id="rp-log" aria-label="The tester's thoughts and actions" aria-hidden="${!RP.log}"><div class="rp-log-h"><h2>Thoughts and actions</h2><button class="iconbtn" data-rp="log" aria-label="Close">${icon('x-mark')}</button></div><div class="rp-list">${items}</div></aside>`;
}
const rawHTML = d => `<div class="rp-raw" role="dialog" aria-modal="true" aria-label="Recording" hidden><div class="rp-raw-h"><div><b>Recording</b><span>The whole run, as it was recorded</span></div><button class="iconbtn" data-rp="raw-close" aria-label="Close">${icon('x-mark')}</button></div>
  <div class="rp-stage rp-rawstage" style="--sw:${d.frames[0].w};--sh:${d.frames[0].h}"><div class="rp-screen"><div class="rp-frames"></div><div class="rp-secret">${esc(d.hidden_says)}</div><div class="rp-who">${esc(d.second_says)}</div></div></div>
  <div class="rp-bar"><button class="iconbtn rp-rawplay" data-rp="raw-play" aria-label="Play"></button><input class="rp-slider" type="range" min="0" step="0.1" value="0" data-rp-seek aria-label="Time in the run"><span class="rp-time"></span></div></div>`;

/* ---------- the player on the open row ---------- */
function rpData() { const env = UI.data[UI.replay], p = env && env.parts.replay; return p && p.ok ? p.data : null; }
function bindReplay() {
  if (RP.player) RP.player.stop();
  RP.player = null; RP.raw = null;
  const d = rpData(), stage = $('#page .rp-body .rp-stage');
  if (!d || !stage) return;
  const r = d.rows.find(x => x.key === RP.open);
  rpSaidHeight(r);
  RP.player = new Player(stage, r, d, {step: j => rpStepShown(r, j), beat: (j, k) => rpBeatShown(j, k)});
  RP.player.select(rpPick(r));
}
/* The expected-and-got box takes the height of its tallest step, so moving between steps moves nothing below it. */
function rpSaidHeight(r) {
  const said = $('#rp-said'), probe = document.createElement('div');
  probe.style.cssText = `position:absolute;visibility:hidden;left:0;top:0;width:${said.offsetWidth}px`;
  said.parentNode.append(probe);
  RP.saidMin = Math.max(...r.steps.map(s => { probe.innerHTML = saidHTML(s); return probe.firstElementChild.offsetHeight; }));
  probe.remove();
  said.style.minHeight = RP.saidMin + 'px';
}
function rpStepShown(r, j) {
  RP.pick[r.key] = j;
  const said = $('#rp-said');
  if (said) { said.outerHTML = saidHTML(r.steps[j]); $('#rp-said').style.minHeight = RP.saidMin + 'px'; }
  $$('#page .rp-step').forEach(b => b.setAttribute('aria-pressed', String(+b.dataset.step === j)));
  $$('#rp-log .rp-li').forEach(li => { li.classList.toggle('here', +li.dataset.step === j); li.classList.remove('cur'); });
  rpFollow($(`#rp-log .rp-lh[data-step="${j}"]`));
}
function rpBeatShown(j, k) {
  $$('#rp-log .rp-li.cur').forEach(li => li.classList.remove('cur'));
  rpFollow($(`#rp-log .rp-li[data-step="${j}"][data-beat="${k}"]`));
}
/* Keeps the current line a third of the way down the list. */
function rpFollow(el) {
  if (!el) return;
  el.classList.add('cur');
  const list = el.closest('.rp-list');
  list.scrollTo({top: Math.max(0, el.offsetTop - list.clientHeight * 0.3), behavior: 'smooth'});
}

/* ---------- what the viewer does ---------- */
function replayClick(t) {
  const d = rpData(), what = t.dataset.rp, P = RP.player;
  if (!d) return;
  if (what === 'open') {
    const r = d.rows[+t.dataset.row];
    RP.open = RP.open === r.key ? null : r.key;
    render({keep: true});
  } else if (what === 'step' || what === 'pick') { if (P) P.select(+t.dataset.step); }
  else if (what === 'go') { if (P) P.playFrom(+t.dataset.step, +t.dataset.beat); }
  else if (what === 'watch') P.watch();
  else if (what === 'toggle') P.toggle();
  else if (what === 'again') P.again();
  else if (what === 'log') rpLog(!RP.log);
  else if (what === 'raw') rpRaw(true, d);
  else if (what === 'raw-close') rpRaw(false);
  else if (what === 'raw-play') RP.raw.toggle();
}
function rpLog(open) {
  RP.log = open;
  const log = $('#rp-log'); if (!log) return;
  log.classList.toggle('open', open); log.setAttribute('aria-hidden', String(!open));
  $$('#page [data-rp="log"][aria-controls]').forEach(b => b.setAttribute('aria-expanded', String(open)));
  if (open) rpFollow($('#rp-log .rp-li.cur') || $('#rp-log .rp-li.here'));
}
function rpRaw(open, d) {
  const box = $('#page .rp-raw'); if (!box) return;
  if (RP.player) RP.player.pause();
  if (RP.raw) RP.raw.pause();
  box.hidden = !open;
  if (!open) { RP.raw = null; return; }
  RP.raw = new RawPlayer(box, d); RP.raw.seek(0); $('.rp-rawplay', box).focus();
}
const replaySeek = v => { if (RP.raw) RP.raw.seek(+v); };
/* Escape closes the recording first, then the list; true when it closed one. */
function replayEscape() {
  if (UI.page !== 'replay') return false;
  if (RP.raw) { rpRaw(false); return true; }
  if (RP.log) { rpLog(false); return true; }
  return false;
}
