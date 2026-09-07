(async () => {
  const checks = [];
  function check(ok, name) { if (!ok) throw new Error(name); checks.push(name); }
  const card = document.querySelector('[data-run-id]');
  const original = {start:card.dataset.startedAt, end:card.dataset.finishedAt, typical:card.dataset.typicalSeconds};
  const now = Date.parse((await (await fetch('/now2/time')).json()).now);
  for (const [seconds, expected] of [[0,'0s'],[59,'59s'],[60,'1:00'],[3599,'59:59'],[3600,'1:00:00']]) {
    card.dataset.startedAt = new Date(now-seconds*1000).toISOString();
    card.dataset.finishedAt = new Date(now).toISOString();
    document.dispatchEvent(new Event('visibilitychange'));
    check(card.querySelector('[data-clock]').textContent === expected, 'clock ' + expected);
  }
  card.dataset.typicalSeconds = '120';
  document.dispatchEvent(new Event('visibilitychange'));
  check(card.querySelector('[data-overdue]').textContent.includes('longer than usual'), 'plain overdue label');
  check(card.querySelector('[data-fill]').style.width === '100%', 'typical fill is capped');
  card.dataset.typicalSeconds = '';
  document.dispatchEvent(new Event('visibilitychange'));
  check(card.querySelector('[data-overdue]').textContent === '', 'missing typical is not overdue');
  card.dataset.startedAt = new Date(now-42000).toISOString(); card.dataset.finishedAt = '';
  const localNow = Date.now; Date.now = () => localNow()+10800000;
  document.dispatchEvent(new Event('visibilitychange'));
  check(/^4[1-3]s$/.test(card.querySelector('[data-clock]').textContent), 'server anchor ignores skewed browser wall clock: ' + card.querySelector('[data-clock]').textContent);
  Date.now = localNow;
  card.dataset.startedAt = original.start; card.dataset.finishedAt = original.end; card.dataset.typicalSeconds = original.typical;
  const picker = document.querySelector('#n2-metric');
  for (const option of picker.options) {
    picker.value = option.value; picker.dispatchEvent(new Event('change',{bubbles:true}));
    check(document.querySelectorAll('[data-metric]:not([hidden])').length === 1, 'one matrix: ' + option.value);
  }
  picker.value='total_cost'; picker.dispatchEvent(new Event('change',{bubbles:true}));
  const detail=card.querySelector('details'); detail.open=true; detail.querySelector('summary').focus();
  document.querySelector('#n2-window').value='24h';
  document.querySelector('#n2-window-form').requestSubmit();
  window.n2BrowserCheck = {detail, summary:detail.querySelector('summary')};
  const table=document.querySelector('[data-metric]:not([hidden]) .n2-table'); table.scrollLeft=120;
  if (innerWidth===390) check(table.scrollLeft>0, 'phone table scrolls independently');
  check(document.documentElement.scrollWidth===innerWidth, 'no document overflow');
  check(!performance.getEntriesByType('resource').some(e=>/\/partials\/|\/api\/decisions/.test(e.name)), 'no page or notification polling');
  card.classList.add('n2-arrive');
  if (document.querySelector('#n2-motion').getAttribute('aria-pressed') !== 'true') document.querySelector('#n2-motion').click();
  check(getComputedStyle(card).animationName==='none', 'motion off');
  card.classList.remove('n2-arrive');
  return JSON.stringify({passed:checks, width:innerWidth});
})()
