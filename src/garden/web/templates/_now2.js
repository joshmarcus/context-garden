(() => {
  const root = document.getElementById('now2');
  const status = document.getElementById('n2-connection');
  const runs = document.getElementById('n2-runs');
  let anchor = Date.parse(root.dataset.serverNow), receipt = performance.now();
  let source, revision = 0, generation = 0, metric = 'total_cost', lastReceived = performance.now();
  const finishedSeen = new Map();
  const pendingRemoval = new Set();
  const serverNow = () => anchor + performance.now() - receipt;
  function duration(seconds) {
    if (seconds < 60) return seconds + 's';
    const pad = n => String(n).padStart(2, '0');
    if (seconds < 3600) return Math.floor(seconds / 60) + ':' + pad(seconds % 60);
    return Math.floor(seconds / 3600) + ':' + pad(Math.floor(seconds / 60) % 60) + ':' + pad(seconds % 60);
  }
  async function anchorClock() {
    const before = performance.now();
    try {
      const response = await fetch('/now2/time', {cache:'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      const after = performance.now();
      anchor = Date.parse(data.now) + (after-before)/2;
      receipt = after;
      clocks();
    } catch (_) { /* initial server timestamp remains an honest fallback */ }
  }
  function clocks() {
    root.querySelectorAll('[data-run-id]').forEach(card => {
      const start = Date.parse(card.dataset.startedAt);
      const end = Date.parse(card.dataset.finishedAt);
      const elapsed = Math.max(0, Math.floor(((Number.isFinite(end) ? end : serverNow())-start)/1000));
      card.querySelector('[data-clock]').textContent = Number.isFinite(elapsed) ? duration(elapsed) : 'Clock unavailable';
      const typical = Number(card.dataset.typicalSeconds);
      card.querySelector('[data-fill]').style.width = typical > 0 && Number.isFinite(elapsed) ? Math.min(100, elapsed / typical * 100) + '%' : '0%';
      card.querySelector('[data-overdue]').textContent = typical > 0 && elapsed > typical ? '· longer than usual' : '';
      if (card.dataset.status !== 'running') {
        if (!finishedSeen.has(card.id)) finishedSeen.set(card.id, performance.now());
        if (performance.now()-finishedSeen.get(card.id) >= 8000 && !card.contains(document.activeElement) && !card.querySelector('details[open]')) card.remove();
      }
    });
    for (const id of pendingRemoval) {
      const card = document.getElementById(id);
      if (!card) { pendingRemoval.delete(id); continue; }
      if (!card.contains(document.activeElement) && !card.querySelector('details[open]') && performance.now()-(finishedSeen.get(id) || 0) >= 8000) { card.remove(); pendingRemoval.delete(id); }
    }
    const quiet = document.querySelector('#now2-summary .n2-quiet');
    if (quiet) quiet.hidden = runs.children.length > 0;
    if (performance.now()-lastReceived > 45000) status.textContent = 'Updates disconnected · elapsed since start';
  }
  // Patch leaves in place. Disclosure state, focus, selection, and live clock nodes survive.
  function morph(old, fresh) {
    if (old.nodeType !== fresh.nodeType || old.nodeName !== fresh.nodeName) { old.replaceWith(fresh.cloneNode(true)); return; }
    if (old.nodeType === Node.TEXT_NODE) { if (old.data !== fresh.data) old.data = fresh.data; return; }
    if (old.nodeType !== Node.ELEMENT_NODE) return;
    if (old.matches('[data-clock],[data-fill],[data-overdue]')) return;
    for (const attr of [...old.attributes]) if (!fresh.hasAttribute(attr.name) && !(old.tagName === 'DETAILS' && attr.name === 'open')) old.removeAttribute(attr.name);
    for (const attr of [...fresh.attributes]) if (old.getAttribute(attr.name) !== attr.value && !(old.tagName === 'DETAILS' && attr.name === 'open')) old.setAttribute(attr.name, attr.value);
    const children = [...fresh.childNodes];
    children.forEach((child, i) => {
      const key = child.nodeType === 1 && (child.id || child.dataset.key);
      if (key) {
        const existing = [...old.childNodes].find(n => n.nodeType === 1 && (n.id || n.dataset.key) === key);
        if (existing && existing !== old.childNodes[i]) old.insertBefore(existing, old.childNodes[i] || null);
      }
      if (!old.childNodes[i]) old.appendChild(child.cloneNode(true));
      else morph(old.childNodes[i], child);
    });
    while (old.childNodes.length > children.length) old.lastChild.remove();
  }
  function pickMetric() {
    const picker = document.getElementById('n2-metric');
    if (picker) picker.value = metric;
    root.querySelectorAll('[data-metric]').forEach(el => el.hidden = el.dataset.metric !== metric);
  }
  function apply(payload, reset) {
    if (payload.revision <= revision) return;
    revision = payload.revision;
    const removed = reset ? [...runs.children].filter(el => !payload.fragments[el.id]).map(el => el.id) : payload.removed;
    for (const id of removed) {
      const el = document.getElementById(id);
      if (!el) continue;
      pendingRemoval.add(id);
      if (el.contains(document.activeElement) || el.querySelector('details[open]')) continue;
      if (el.dataset.status !== 'running' && performance.now()-(finishedSeen.get(id) || 0) < 8000) continue;
      el.remove();
    }
    let arrivals = 0;
    for (const [id, html] of Object.entries(payload.fragments)) {
      pendingRemoval.delete(id);
      const old = document.getElementById(id);
      const template = document.createElement('template');
      template.innerHTML = html;
      if (id.startsWith('run-')) {
        const fresh = template.content.firstElementChild;
        if (old) morph(old, fresh);
        else { runs.appendChild(fresh); if (fresh.getBoundingClientRect().top < innerHeight) fresh.classList.add('n2-arrive'); arrivals++; }
      } else if (old) {
        const wrapper = old.cloneNode(false); wrapper.appendChild(template.content); morph(old, wrapper);
      }
    }
    if (arrivals) document.getElementById('n2-announcement').textContent = arrivals + ' run updates';
    pickMetric(); clocks();
  }
  function connect() {
    if (source) source.close();
    revision = 0;
    const current = ++generation;
    const query = new URLSearchParams({window:document.getElementById('n2-window').value, phase:document.getElementById('n2-phase').value});
    source = new EventSource('/api/events/now2?' + query);
    const alive = () => { lastReceived = performance.now(); status.textContent = 'Live · connected'; };
    source.addEventListener('snapshot', e => { if(current !== generation) return; alive(); revision = 0; apply(JSON.parse(e.data), true); });
    source.addEventListener('fragments', e => { if(current !== generation) return; alive(); apply(JSON.parse(e.data), false); });
    source.addEventListener('heartbeat', alive);
    source.onerror = () => status.textContent = 'Updates disconnected · reconnecting';
    history.replaceState(null, '', '/now2?' + query);
  }
  root.addEventListener('change', e => { if (e.target.id === 'n2-metric') { metric = e.target.value; pickMetric(); } });
  document.getElementById('n2-window-form').addEventListener('submit', e => { e.preventDefault(); connect(); });
  document.getElementById('n2-motion').addEventListener('click', e => {
    const off = root.dataset.motion !== 'off'; root.dataset.motion = off ? 'off' : 'on';
    e.target.setAttribute('aria-pressed', String(off));
    try { localStorage.setItem('now2-motion', root.dataset.motion); } catch (_) {}
  });
  try { root.dataset.motion = localStorage.getItem('now2-motion') || 'on'; } catch (_) {}
  document.getElementById('n2-motion').setAttribute('aria-pressed', String(root.dataset.motion === 'off'));
  document.addEventListener('visibilitychange', clocks);
  window.addEventListener('pagehide', () => source && source.close());
  pickMetric(); clocks(); anchorClock(); connect(); setInterval(clocks, 1000);
})();
