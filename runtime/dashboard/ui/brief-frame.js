/* Runs inside a designer's brief, in its sandboxed frame (runtime/dashboard/brief_frame.py adds it to every brief).
   It draws the brief's Mermaid diagrams, follows the dashboard's theme, tells the brief page what the owner selects,
   and highlights the text each comment quotes. It speaks to the page by messages alone, and the page checks that
   each one comes from this frame. */
(() => {
  const CONTEXT = 32;
  const MERMAID = 'https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js';
  const post = m => parent.postMessage(Object.assign({brief: true}, m), '*');
  let ranges = {};

  function drawDiagrams() {
    if (!document.querySelector('pre.mermaid')) return;
    const s = document.createElement('script');
    s.src = MERMAID;
    s.onload = () => {
      window.mermaid.initialize({startOnLoad: false, theme: document.documentElement.dataset.theme === 'dark' ? 'dark' : 'default'});
      window.mermaid.run();
    };
    document.head.append(s);
  }

  // The body's text in tree order, as a Range's text reads it, with where each text node starts in it.
  function textMap() {
    const nodes = [], walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let text = '', n;
    while ((n = walker.nextNode())) { nodes.push([n, text.length]); text += n.data; }
    return {text, nodes};
  }
  function offset(container, at) {
    const r = document.createRange();
    r.setStart(document.body, 0); r.setEnd(container, at);
    return r.toString().length;
  }
  function point(map, at) {
    let found = map.nodes[0];
    for (const entry of map.nodes) { if (entry[1] <= at) found = entry; else break; }
    return found ? [found[0], Math.min(at - found[1], found[0].data.length)] : null;
  }
  // Where a comment's text is now: the place its text matches with the most of what came before and after it.
  function locate(map, t) {
    let best = -1, score = -1, from = map.text.indexOf(t.quote);
    while (from >= 0) {
      const before = map.text.slice(Math.max(0, from - t.prefix.length), from);
      const after = map.text.slice(from + t.quote.length, from + t.quote.length + t.suffix.length);
      const s = (before === t.prefix ? 2 : 0) + (after === t.suffix ? 1 : 0);
      if (s > score) { best = from; score = s; }
      from = map.text.indexOf(t.quote, from + 1);
    }
    if (best < 0) return null;
    const a = point(map, best), b = point(map, best + t.quote.length);
    if (!a || !b) return null;
    const r = document.createRange(); r.setStart(a[0], a[1]); r.setEnd(b[0], b[1]);
    return r;
  }

  function highlight(threads, active) {
    const map = textMap(), missing = [];
    ranges = {};
    for (const t of threads) { const r = locate(map, t); if (r) ranges[t.id] = r; else missing.push(t.id); }
    if (window.CSS && CSS.highlights && window.Highlight) {
      CSS.highlights.set('brief-note', new Highlight(...Object.entries(ranges).filter(([id]) => id !== active).map(([, r]) => r)));
      CSS.highlights.set('brief-note-on', new Highlight(...(ranges[active] ? [ranges[active]] : [])));
    }
    post({type: 'anchored', missing});
  }

  function selected() {
    const sel = getSelection();
    if (!sel.rangeCount || sel.isCollapsed || !sel.toString().trim()) { post({type: 'select', quote: ''}); return; }
    const r = sel.getRangeAt(0), text = textMap().text;
    const start = offset(r.startContainer, r.startOffset), end = offset(r.endContainer, r.endOffset);
    const box = r.getBoundingClientRect();
    post({type: 'select', quote: text.slice(start, end), prefix: text.slice(Math.max(0, start - CONTEXT), start),
          suffix: text.slice(end, end + CONTEXT), x: box.right, y: box.top});
  }

  function clicked(e) {
    const caret = document.caretRangeFromPoint ? document.caretRangeFromPoint(e.clientX, e.clientY) : null;
    if (!caret) return;
    const hit = Object.entries(ranges).find(([, r]) => r.isPointInRange(caret.startContainer, caret.startOffset));
    if (hit) post({type: 'focus', id: hit[0]});
  }

  const style = document.createElement('style');
  style.textContent = '::highlight(brief-note){background-color:rgba(234,179,8,.32)}::highlight(brief-note-on){background-color:rgba(234,179,8,.7)}';
  document.head.append(style);
  window.addEventListener('message', e => {
    if (e.source !== parent || !e.data || !e.data.brief) return;
    const m = e.data;
    if (m.type === 'theme') document.documentElement.dataset.theme = m.theme;
    if (m.type === 'threads') highlight(m.threads, m.active);
    if (m.type === 'scroll' && ranges[m.id]) ranges[m.id].startContainer.parentElement.scrollIntoView({block: 'center', behavior: 'smooth'});
    if (m.type === 'clear') getSelection().removeAllRanges();
  });
  document.addEventListener('mouseup', () => setTimeout(selected, 0));
  document.addEventListener('keyup', e => { if (e.shiftKey || e.key.startsWith('Arrow')) selected(); });
  document.addEventListener('click', clicked);
  drawDiagrams();
  post({type: 'ready'});
})();
