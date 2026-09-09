// Capture the served README fixture. Requires Playwright and its Chromium browser.
// node scripts/capture_readme.cjs [base URL] [output directory]
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const base = process.argv[2] || 'http://127.0.0.1:8876';
const output = process.argv[3] || path.join(__dirname, '../docs/screenshots');
const pages = [
  ['inbox-light', '/inbox', 'light'],
  ['trellis-light', '/trellis', 'light'],
  ['task-dark', '/tasks/DM-002', 'dark'],
  ['phase-light', '/phases/demo/p1', 'light'],
];
(async () => {
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, ...(process.env.README_BROWSER ? { executablePath: process.env.README_BROWSER } : {}) });
  const report = [];
  try {
    for (const [name, route, colorScheme] of pages) {
      const page = await browser.newPage({ viewport: { width: 1280, height: 1200 }, deviceScaleFactor: 1, colorScheme });
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      const response = await page.goto(base + route, { waitUntil: 'networkidle' });
      if (response.status() !== 200) throw new Error(`${route}: HTTP ${response.status()}`);
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: path.join(output, name + '.png') });
      const widths = await page.evaluate(() => ({ client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
      report.push({ name, route, colorScheme, viewport: { width: 1280, height: 1200 }, widths, errors });
      if (errors.length || widths.scroll > widths.client) throw new Error(`${route}: ${JSON.stringify({ widths, errors })}`);
      await page.close();
    }
    await fs.writeFile(path.join(output, 'capture.json'), JSON.stringify({ browser: browser.version(), pages: report }, null, 2) + '\n');
    console.log(JSON.stringify(report));
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
