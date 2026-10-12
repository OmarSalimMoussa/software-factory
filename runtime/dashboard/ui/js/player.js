'use strict';
/* The replay's player. It plays a tester's run from the trace's own frames on the run's own clock, and draws each beat
   over the screen: a thought typed beside the cursor, the cursor gliding to a click, a tag naming the action. The
   server sends every word, place and time; this file keeps the time and moves things. */

/* The run's frames on one screen: the frame shown at a second of the run, and a stretch of it played in real time.
   A frame is loaded once and kept, so playing it again never waits. */
class Screen {
  constructor(host, frames, hidden) {
    this.host = host; this.frames = frames; this.hidden = hidden;
    this.kept = new Map(); this.raf = 0; this.shown = -1;
  }
  index(t) {
    let lo = 0, hi = this.frames.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (this.frames[mid].t <= t) lo = mid; else hi = mid - 1; }
    return lo;
  }
  img(i) {
    let im = this.kept.get(i);
    if (!im) { im = new Image(); im.alt = ''; im.src = this.frames[i].url; this.kept.set(i, im); }
    return im;
  }
  warm(from, to) { for (let i = this.index(from); i < this.frames.length && this.frames[i].t <= to; i++) this.img(i); }
  show(t) {
    this.host.classList.toggle('secret', this.hidden.some(([a, b]) => t >= a && t <= b));
    const i = this.index(t);
    if (i === this.shown) return;
    this.shown = i;
    const f = this.frames[i], im = this.img(i);
    this.host.style.setProperty('--w', f.w); this.host.style.setProperty('--h', f.h);
    /* A frame still loading leaves the one before it up, so the screen never goes blank. */
    const put = () => { if (this.shown === i && this.host.isConnected) this.host.querySelector('.rp-frames').replaceChildren(im); };
    if (im.complete) put(); else im.addEventListener('load', put, {once: true});
  }
  play(from, to, done) {
    this.stop();
    const start = performance.now();
    const tick = now => {
      if (!this.host.isConnected) return;
      const t = Math.min(to, from + (now - start) / 1000);
      this.show(t);
      if (t >= to) { this.raf = 0; done(); return; }
      this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }
  stop() { if (this.raf) cancelAnimationFrame(this.raf); this.raf = 0; }
}

/* One item's steps, played on the stage. `hooks.step(j)` and `hooks.beat(j, k)` tell the page what is on screen. */
class Player {
  constructor(stage, row, data, hooks) {
    this.stage = stage; this.row = row; this.P = data.pace; this.hooks = hooks;
    this.screen = new Screen($('.rp-screen', stage), data.frames, data.hidden);
    this.timers = []; this.token = 0; this.cur = row.first; this.beat = 0; this.first = row.first;
    this.idle = true; this.playing = false; this.done = false;
    /* How far the run has played since play was pressed: a play-through never goes back before it. */
    this.head = null;
    this.cx = 50; this.cy = 50;
    stage.style.setProperty('--glide', this.P.glide + 'ms'); stage.style.setProperty('--fade', this.P.fade + 'ms');
  }
  el(name) { return $('.rp-' + name, this.stage); }
  step() { return this.row.steps[this.cur]; }
  after(ms, fn) { const tok = this.token; this.timers.push(setTimeout(() => { if (tok === this.token && this.stage.isConnected) fn(); }, ms)); }
  stop() { this.token += 1; this.timers.forEach(clearTimeout); this.timers = []; this.screen.stop(); }
  seek(t) { const to = Math.max(0, this.head == null ? t : Math.max(t, this.head)); this.screen.show(to); this.head = to; }
  mode() {
    const c = this.stage.classList, s = this.step();
    c.toggle('idle', this.idle); c.toggle('playing', this.playing); c.toggle('done', this.done);
    c.toggle('paused', !this.idle && !this.done && !this.playing); c.toggle('ran', s.beats.length > 0);
    c.toggle('has-still', !!s.still);
    this.el('catch').setAttribute('aria-label', this.playing ? 'Pause' : 'Resume');
  }

  select(j) {
    this.stop(); this.head = null;
    Object.assign(this, {idle: true, playing: false, done: false, beat: 0});
    this.show(j); this.clear(); this.mode();
  }
  show(j) {
    this.cur = j;
    const s = this.step(), still = this.el('still'), segs = s.beats.filter(b => b.seg);
    if (s.still) still.src = s.still; else still.removeAttribute('src');
    if (segs.length) this.screen.warm(segs[0].seg[0], segs[segs.length - 1].seg[1]);
    this.hooks.step(j);
  }
  watch() { this.first = this.cur; this.startStep(this.cur, 0, false); }
  again() { this.startStep(this.first, 0, false); }
  toggle() {
    if (this.playing) { this.pause(); return; }
    if (this.idle) { this.watch(); return; }
    if (this.done) { this.again(); return; }
    this.playing = true; this.mode(); this.run(this.beat);
  }
  pause() { if (!this.playing) return; this.stop(); this.playing = false; this.fadePill(); this.mode(); }
  /* From a line of the list: a beat right after a thought starts at the thought, so the action has its reason. */
  playFrom(j, k) {
    const beats = this.row.steps[j].beats;
    this.first = j; this.startStep(j, k > 0 && beats[k].k !== 'think' && beats[k - 1].k === 'think' ? k - 1 : k, false);
  }

  startStep(i, at, followOn) {
    this.stop();
    if (!followOn) this.head = null;
    const steps = this.row.steps;
    let n = i;
    while (n < steps.length && !steps[n].beats.length) n += 1;
    if (n >= steps.length) { this.playing = false; this.done = true; this.mode(); return; }
    this.show(n);
    const s = steps[n], from = n === i ? at : 0, first = s.beats.slice(from).find(b => b.t != null);
    if (first) this.seek(first.seg[0] - 0.15);
    Object.assign(this, {idle: false, playing: true, done: false, beat: from});
    this.clear(); this.mode();
    this.cursor(true);
    const card = this.el('card');
    card.textContent = s.card; card.classList.add('on'); card.classList.remove('fade');
    /* A title card names the step, then fades before its first beat. */
    this.after(this.P.card, () => card.classList.add('fade'));
    this.after(this.P.card + this.P.fade, () => { card.classList.remove('on', 'fade'); this.run(from); });
  }
  run(i) {
    const P = this.P, beats = this.step().beats;
    if (i >= beats.length) {
      const later = this.row.steps.slice(this.cur + 1).some(s => s.beats.length);
      this.fadeOut();
      if (later) { this.after(P.between * 2, () => this.startStep(this.cur + 1, 0, true)); return; }
      this.after(P.fade, () => { this.playing = false; this.done = true; this.clear(); this.mode(); });
      return;
    }
    const b = beats[i], next = ms => this.after(ms + P.between, () => this.run(i + 1));
    const clear = then => { this.fadePill(); this.boxes(null); this.after(P.fade, then); };
    const onward = () => clear(() => this.after(P.between, () => this.run(i + 1)));
    this.beat = i; this.hooks.beat(this.cur, i);
    if (b.k === 'think') {
      /* The thought types out beside the cursor at reading pace, then stays while it is read. */
      this.fadePill(); this.bubble(b.say, 0);
      const tick = c => { this.bubble(b.say, c); if (c < b.say.length) this.after(P.char, () => tick(c + 1)); };
      this.after(P.char, () => tick(1));
      next(P.char * b.say.length + P.read);
      return;
    }
    this.fadeOut();
    if (b.k !== 'go') { this.screen.stop(); this.seek(b.t - 0.1); }
    if (b.x != null && (b.k === 'click' || b.k === 'type' || b.k === 'miss')) { this.pointAt(b, next, onward); return; }
    if (b.k === 'miss') { this.pill(b.say, b.icon, true); this.after(P.miss, onward); return; }
    if (b.k === 'shot') {
      this.flash(); this.pill(b.say, b.icon);
      this.after(P.shot, onward);
      return;
    }
    this.pill(b.say, b.icon);
    /* A read outlines what the tester's script looked at: the whole part dashed, the piece it used solid. */
    this.boxes(b);
    if (b.seg[1] > b.seg[0]) { this.after(P.before, () => this.playSeg(b.seg, () => this.after(P.after, onward))); return; }
    this.after(P.pill, onward);
  }
  /* Playwright scrolls its target into view, then moves the pointer, then acts. So the run plays up to just before
     the action first (any scroll shows), and only then does the cursor glide to where it landed. */
  pointAt(b, next, onward) {
    const P = this.P, at = b.down != null ? b.down : b.t, before = at - P.freeze / 1000;
    const landed = () => {
      if (b.k === 'miss') { this.ripple(true); this.pill(b.say, b.icon, true); this.after(P.miss, onward); return; }
      this.ripple(false);
      const rest = [before, b.seg[1]];
      if (b.k === 'click') { this.pill(b.say, b.icon); this.after(P.before, () => this.playSeg(rest, () => next(P.after))); return; }
      /* The typing shows in the tag beside the cursor, a key at a time. */
      b.keys.forEach((said, c) => this.after(P.type_lead * Math.min(c, 1) + P.type_char * c, () => this.pill(said, b.icon)));
      const typed = P.type_lead + P.type_char * (b.keys.length - 1) + P.type_rest;
      this.after(typed, () => { this.pill(b.say, b.icon); this.after(P.before, () => this.playSeg(rest, () => next(P.after))); });
    };
    const glide = () => { this.screen.stop(); this.seek(before); this.cursor(true, b.x, b.y); this.after(P.glide, landed); };
    if (before - b.t > 0.08) this.playSeg([b.t, before], glide); else glide();
  }
  playSeg(seg, then) {
    const from = this.head == null ? seg[0] : Math.max(seg[0], this.head);
    if (seg[1] - from <= 0) { then(); return; }
    this.head = from;
    this.screen.play(from, seg[1], () => { this.head = Math.max(this.head, seg[1]); then(); });
  }

  clear() {
    ['bubble', 'pill', 'card', 'flash', 'ripple'].forEach(n => this.el(n).classList.remove('on', 'fade', 'bad'));
    this.boxes(null); this.cursor(false);
  }
  cursor(on, x, y) {
    if (x != null) { this.cx = x; this.cy = y; }
    const c = this.el('cursor'); c.classList.toggle('on', on); c.style.left = this.cx + '%'; c.style.top = this.cy + '%';
    this.el('ripple').style.left = this.cx + '%'; this.el('ripple').style.top = this.cy + '%';
  }
  /* Thoughts and tags share one spot beside the cursor, on the side away from the nearest edges. */
  near(el) {
    const right = this.cx > 58, up = this.cy >= 55;
    el.style.left = right ? '' : (this.cx + 2.2) + '%'; el.style.right = right ? (100 - this.cx + 2.2) + '%' : '';
    el.style.top = up ? '' : (this.cy + 4.5) + '%'; el.style.bottom = up ? (100 - this.cy + 4.5) + '%' : '';
    el.classList.toggle('to-left', right); el.classList.toggle('to-top', up);
  }
  bubble(text, shown) {
    const el = this.el('bubble'); this.near(el);
    $('.rp-bubble-text', el).textContent = text.slice(0, shown); el.classList.add('on'); el.classList.remove('fade');
  }
  pill(text, ic, bad) {
    const el = this.el('pill'); this.near(el);
    el.innerHTML = icon(ic) + `<span>${esc(text)}</span>`; el.classList.add('on'); el.classList.toggle('bad', !!bad); el.classList.remove('fade');
  }
  fadeEl(el) {
    if (!el.classList.contains('on') || el.classList.contains('fade')) return;
    el.classList.add('fade');
    this.after(this.P.fade, () => { if (el.classList.contains('fade')) el.classList.remove('on', 'fade'); });
  }
  fadePill() { this.fadeEl(this.el('pill')); }
  fadeOut() { this.fadePill(); this.fadeEl(this.el('bubble')); }
  ripple(bad) { const r = this.el('ripple'); r.classList.toggle('bad', bad); this.once(r); }
  flash() { this.once(this.el('flash')); }
  /* Plays an element's one-off animation from its start; it takes its class off itself when the animation ends.
     Reading offsetWidth between the class going and coming makes the browser start the animation again. */
  once(el) {
    el.classList.remove('on'); void el.offsetWidth; el.classList.add('on');
    el.addEventListener('animationend', () => el.classList.remove('on'), {once: true});
  }
  boxes(b) {
    const at = box => `left:${box[0]}%;top:${box[1]}%;width:${box[2]}%;height:${box[3]}%`;
    this.el('boxes').innerHTML = b ? (b.outer ? `<i class="outer" style="${at(b.outer)}"></i>` : '') + (b.inner || []).map(x => `<i style="${at(x)}"></i>`).join('') : '';
  }
}

/* The whole run as it was recorded, at its own speed, with a slider to move through it. */
class RawPlayer {
  constructor(box, data) {
    this.box = box; this.seconds = data.seconds; this.t = 0; this.playing = false;
    this.screen = new Screen($('.rp-screen', box), data.frames, data.hidden);
    this.slider = $('.rp-slider', box); this.slider.max = String(this.seconds);
  }
  clock(t) { const s = Math.round(t); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }
  draw() {
    this.slider.value = String(this.t);
    $('.rp-time', this.box).textContent = this.clock(this.t) + ' / ' + this.clock(this.seconds);
    $('.rp-rawplay', this.box).innerHTML = icon(this.playing ? 'pause' : 'play');
    $('.rp-rawplay', this.box).setAttribute('aria-label', this.playing ? 'Pause' : 'Play');
  }
  seek(t) { this.screen.stop(); this.t = Math.min(this.seconds, Math.max(0, t)); this.screen.show(this.t); if (this.playing) this.play(); else this.draw(); }
  play() {
    if (this.t >= this.seconds) this.t = 0;
    this.playing = true; this.draw();
    const start = performance.now(), from = this.t, run = (this.run || 0) + 1;
    this.run = run;
    this.screen.play(from, this.seconds, () => { this.playing = false; this.t = this.seconds; this.draw(); });
    /* The slider follows the screen; a later play or seek starts its own follower and this one ends. */
    const follow = now => {
      if (!this.playing || run !== this.run || !this.box.isConnected) return;
      this.t = Math.min(this.seconds, from + (now - start) / 1000); this.draw(); requestAnimationFrame(follow);
    };
    requestAnimationFrame(follow);
  }
  pause() { this.screen.stop(); this.playing = false; this.draw(); }
  toggle() { if (this.playing) this.pause(); else this.play(); }
}
