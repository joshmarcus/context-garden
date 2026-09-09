// Capture selected views from a running garden without submitting any actions.
// node scripts/capture_live_readme.cjs [base URL] [output directory]
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const base = process.argv[2] || 'http://127.0.0.1:8765';
const output = process.argv[3] || path.join(__dirname, '../docs/screenshots');
const pages = [
  ['now-light', '/now?window=24h'],
  ['board-backlog-light', '/board?view=backlog&product=context-garden'],
  ['board-list-light', '/board?view=list&product=context-garden&phase=phase-06'],
  ['board-columns-light', '/board?product=context-garden'],
];
(async () => {
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, ...(process.env.README_BROWSER ? { executablePath: process.env.README_BROWSER } : {}) });
  const report = [];
  try {
    for (const [name, route] of pages) {
      const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1, colorScheme: 'light' });
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      // Now has a persistent event stream, so networkidle is not a readiness signal.
      const response = await page.goto(base + route, { waitUntil: 'load', timeout: 60000 });
      if (response.status() !== 200) throw new Error(`${route}: HTTP ${response.status()}`);
      await page.evaluate(async () => {
        await document.fonts.ready;
        await Promise.all([...document.images].map(img => img.decode().catch(() => {})));
      });
      const widths = await page.evaluate(() => ({ client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
      if (errors.length || widths.scroll > widths.client) throw new Error(`${route}: ${JSON.stringify({ widths, errors })}`);
      await page.screenshot({ path: path.join(output, name + '.png') });
      report.push({ name, route, capturedAt: new Date().toISOString(), viewport: { width: 1440, height: 1000 }, widths, errors });
      if (name === 'now-light') {
        for (const [index, chart] of ['now-run-costs-light', 'now-outcomes-light'].entries()) {
          const selector = '#period-body .tiers';
          const section = page.locator(selector).nth(index);
          await section.screenshot({ path: path.join(output, chart + '.png') });
          report.push({ name: chart, route, capturedAt: new Date().toISOString(), selector, index, bounds: await section.boundingBox(), errors });
        }
      }
      await page.close();
      console.log(`Captured ${name}`);
    }
    await fs.writeFile(path.join(output, 'capture-live.json'), JSON.stringify({ browser: browser.version(), colorScheme: 'light', source: 'Live context-garden development garden', pages: report }, null, 2) + '\n');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
