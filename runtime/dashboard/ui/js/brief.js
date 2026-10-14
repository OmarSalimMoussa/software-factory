/* A task's brief: the designer's page in a frame of its own, and the owner's comment threads beside it. Selecting
   text in the brief offers Comment; each thread shows the text it quotes, the designer's replies, and Resolve. The
   frame stays put while the threads refresh, so a poll never reloads the brief under the owner's eyes. */
PAGE.brief = {
  url: () => UI.brief,
  render: env => part(env, 'brief', drawBrief),
  update: env => briefUpdate(env)
};
const BR = {sel: null, compose: null, draft: '', active: null, missing: [], data: null};
function openBrief(url) {
  closeSheet(); Object.assign(BR, {sel: null, compose: null, draft: '', active: null, missing: []});
  UI.brief = url; go('brief', {back: true});
}

function briefHead(d) {
  const vs = d.versions.length > 1 ? `<div class="seg" role="group" aria-label="Version">${d.versions.map(v => `<button data-brief-v="${esc(v.url)}" aria-pressed="${v.on}" title="${esc(v.when)}">${esc(v.label)}</button>`).join('')}</div>` : '';
  const c = d.card ? `<button class="btn" data-brief-act="${esc(d.card.feedback_url)}" data-v="${esc(d.card.version)}" data-write>${icon('chat-bubble-left-right', 'sm')}${esc(d.card.feedback_label)}</button><button class="btn primary" data-brief-act="${esc(d.card.go_url)}" data-v="${esc(d.card.version)}" data-write>${icon('check', 'sm')}Go</button>` : '';
  return `<div class="page-h brief-h"><div><h1>${esc(d.title)}</h1><div class="sub">${esc(d.sub)}</div></div><div class="brief-acts">${vs}${c}</div></div>`;
}
function briefThreadHTML(t) {
  const gone = BR.missing.includes(t.id) ? `<span class="chip">on ${esc(t.on)}</span>` : '';
  return `<div class="brief-t${BR.active === t.id ? ' on' : ''}${t.can_resolve ? '' : ' done'}" data-brief-t="${esc(t.id)}"><q>${esc(t.quote)}</q>${gone}<div><b>You</b> <span class="faint">${esc(t.when)}</span><div>${esc(t.note)}</div></div>${t.replies.map(r => `<div class="brief-r"><b>${esc(r.who)}</b> <span class="faint">${esc(r.when)}</span><div>${esc(r.text)}</div></div>`).join('')}<div class="brief-tf"><span class="chip ${esc(t.tone)}">${esc(t.state)}</span>${t.can_resolve ? `<button class="btn sm" data-brief-resolve="${esc(t.id)}" data-write>Resolve</button>` : ''}</div></div>`;
}
function briefThreadsHTML(d) {
  const draft = BR.compose ? `<div class="brief-t compose"><q>${esc(BR.compose.quote)}</q><textarea id="brief-note" rows="3" aria-label="Your comment" placeholder="Your comment">${esc(BR.draft)}</textarea><div class="brief-tf"><button class="btn sm" data-brief-cancel>Cancel</button><button class="btn sm primary" data-brief-save data-write>Comment</button></div></div>` : '';
  const rows = [...d.threads.filter(t => t.can_resolve), ...d.threads.filter(t => !t.can_resolve)];
  return draft + (rows.length ? rows.map(briefThreadHTML).join('') : (draft ? '' : '<div class="muted brief-none">Select text in the brief to comment on it.</div>'));
}
function drawBrief(d) {
  BR.data = d;
  if (d.empty) return briefHead(d) + `<div class="card empty">${esc(d.empty)}</div>`;
  return briefHead(d) + `<div class="brief"><div class="brief-frame"><iframe id="brief-frame" src="${esc(d.frame)}" title="The brief" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe><button class="btn sm primary brief-cmt" id="brief-cmt" hidden data-brief-cmt>${icon('chat-bubble-left-right', 'sm')}Comment</button></div><aside class="brief-threads" id="brief-threads">${briefThreadsHTML(d)}</aside></div>`;
}
/* A refresh that shows the same version redraws the head and the threads only; the frame and its scroll stay. */
function briefUpdate(env) {
  const p = env.parts.brief, f = $('#brief-frame');
  if (!p || !p.ok || !f || f.getAttribute('src') !== p.data.frame) return false;
  BR.data = p.data;
  $('.brief-h').outerHTML = briefHead(p.data);
  briefRedraw(); briefSendThreads();
  return true;
}
function briefRedraw() { const box = $('#brief-threads'); if (box && BR.data) box.innerHTML = briefThreadsHTML(BR.data); }

const briefTheme = () => document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
function briefTell(m) { const f = $('#brief-frame'); if (f && f.contentWindow) f.contentWindow.postMessage(Object.assign({brief: true}, m), '*'); }
function briefSendThreads() {
  if (!BR.data) return;
  const live = BR.data.threads.filter(t => t.can_resolve).map(t => ({id: t.id, quote: t.quote, prefix: t.prefix, suffix: t.suffix}));
  briefTell({type: 'threads', threads: live, active: BR.active});
}
function briefPlaceComment() {
  const b = $('#brief-cmt'), f = $('#brief-frame');
  if (!b || !f) return;
  b.hidden = !BR.sel;
  if (!BR.sel) return;
  b.style.top = Math.max(4, BR.sel.y - 36) + 'px';
  b.style.left = Math.max(4, Math.min(f.clientWidth - b.offsetWidth - 4, BR.sel.x - b.offsetWidth)) + 'px';
}
window.addEventListener('message', e => {
  const f = $('#brief-frame');
  if (!f || e.source !== f.contentWindow || !e.data || !e.data.brief) return;
  const m = e.data;
  if (m.type === 'ready') { briefTell({type: 'theme', theme: briefTheme()}); briefSendThreads(); }
  if (m.type === 'anchored') { BR.missing = m.missing || []; briefRedraw(); }
  if (m.type === 'select') { BR.sel = m.quote ? m : null; briefPlaceComment(); }
  if (m.type === 'focus') { BR.active = m.id; briefRedraw(); briefSendThreads(); const el = $(`[data-brief-t="${CSS.escape(m.id)}"]`); if (el) el.scrollIntoView({block: 'nearest'}); }
});
document.addEventListener('input', e => { if (e.target.id === 'brief-note') BR.draft = e.target.value; });
document.addEventListener('click', e => {
  const t = e.target.closest('[data-brief-v],[data-brief-act],[data-brief-resolve],[data-brief-cmt],[data-brief-cancel],[data-brief-save],[data-brief-t]');
  if (!t || UI.page !== 'brief' || !BR.data) return;
  const d = t.dataset;
  if (d.briefV) { Object.assign(BR, {sel: null, active: null, missing: []}); UI.brief = d.briefV; loadPage(); return; }
  if (d.briefAct) { act(d.briefAct, {version: d.v}, {btn: t, quiet: true}); return; }
  if (d.briefResolve) { e.stopPropagation(); act(BR.data.resolve_url, {thread: d.briefResolve}, {btn: t}); return; }
  if (d.briefCmt !== undefined) { BR.compose = BR.sel; BR.sel = null; briefPlaceComment(); briefTell({type: 'clear'}); briefRedraw(); const n = $('#brief-note'); if (n) n.focus(); return; }
  if (d.briefCancel !== undefined) { BR.compose = null; BR.draft = ''; briefRedraw(); return; }
  if (d.briefSave !== undefined) {
    const note = BR.draft.trim();
    if (!note) { alertToast('brief-note', 'Write your comment first.'); return; }
    const c = BR.compose;
    act(BR.data.comment_url, {version: BR.data.shown, quote: c.quote, prefix: c.prefix, suffix: c.suffix, note}, {btn: t, onOk: () => { BR.compose = null; BR.draft = ''; }});
    return;
  }
  if (d.briefT) { BR.active = d.briefT; briefRedraw(); briefSendThreads(); briefTell({type: 'scroll', id: d.briefT}); }
});
