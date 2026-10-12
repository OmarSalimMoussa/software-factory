// The QA recorder: `factory qa-browser` runs it in every page of a tester's browser before the page's own scripts.
// It writes what the tester did to the console as `[qa-rec] {json}` lines, which the run's trace keeps on the
// browser's clock: where each click landed, which field took the typing, and the elements a tester's own script
// looked up. It only listens, and never records the characters typed, which may be a secret.
(() => {
  if (window.__qaRecorder) return;
  window.__qaRecorder = true;
  const now = () => Math.round(performance.timeOrigin + performance.now());
  const box = (el) => {
    if (!el || typeof el.getBoundingClientRect !== 'function') return null;
    const r = el.getBoundingClientRect();
    return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
  };
  const label = (el) => {
    if (!el || !el.getAttribute) return '';
    const own = el.getAttribute('aria-label') || el.getAttribute('name') || el.getAttribute('id') || '';
    return (own || (el.textContent || '').replace(/\s+/g, ' ').trim()).slice(0, 60);
  };
  const emit = (event) => {
    const line = Object.assign({ t: now(), vw: innerWidth, vh: innerHeight,
      sx: Math.round(scrollX), sy: Math.round(scrollY) }, event);
    console.info('[qa-rec] ' + JSON.stringify(line));
  };
  emit({ k: 'page' });
  addEventListener('pointerdown', (e) => emit({ k: 'down', x: Math.round(e.clientX), y: Math.round(e.clientY),
    box: box(e.target), name: label(e.target) }), true);
  addEventListener('focusin', (e) => emit({ k: 'focus', box: box(e.target), name: label(e.target) }), true);
  addEventListener('keydown', (e) => emit({ k: 'key', key: e.key.length === 1 ? 'char' : e.key, box: box(e.target) }), true);
  let lastInput = 0;
  addEventListener('input', (e) => {
    const t = performance.now();
    if (t - lastInput > 400) { lastInput = t; emit({ k: 'input', box: box(e.target), name: label(e.target) }); }
  }, true);
  let lastScroll = 0;
  addEventListener('scroll', () => {
    const t = performance.now();
    if (t - lastScroll > 120) { lastScroll = t; emit({ k: 'scroll' }); }
  }, true);
  addEventListener('resize', () => emit({ k: 'resize' }));
  // Reads: the elements a tester's own page script looked up. Its frames carry no web address; the page's own code
  // always runs from one, so a lookup with no http frame past this file's is the tester's.
  const fromTester = () => {
    const frames = (new Error().stack || '').split('\n').slice(3);
    return frames.length > 0 && !frames.some((f) => /https?:\/\//.test(f));
  };
  let reads = 0;
  const watch = (proto, fn) => {
    const original = proto[fn];
    proto[fn] = function (...args) {
      const found = original.apply(this, args);
      try {
        if (reads < 400 && fromTester()) {
          const list = found && typeof found.length === 'number' && !found.tagName ? Array.from(found) : [found];
          const shown = list.filter(Boolean).slice(0, 12);
          if (shown.length) {
            reads += 1;
            emit({ k: 'read', how: fn, what: String(args[0]).slice(0, 80), n: list.length,
              boxes: shown.map(box), names: shown.map(label) });
          }
        }
      // telemetry: expected — a lookup the recorder cannot describe is left out; the page's own call still answers
      } catch (e) { /* the recorder never breaks the page */ }
      return found;
    };
  };
  watch(Document.prototype, 'querySelector');
  watch(Document.prototype, 'querySelectorAll');
  watch(Element.prototype, 'querySelector');
  watch(Element.prototype, 'querySelectorAll');
  watch(Document.prototype, 'getElementById');
})();
