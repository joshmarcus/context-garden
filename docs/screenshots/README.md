# README screenshots

The main feature tour combines a real development garden with smaller examples. The app's templates, styles, and data are not changed for the live captures.

## Live development garden

The live images were captured from the locally running context-garden development garden on 2026-09-09. `now-light.png` was refreshed later that day from serving build `a0f69c6bfd98`; the three `board-*-light.png` images and the two Now comparison charts retain their earlier captures from build `8072091aea73`. Each image shows real task and run state at its capture time, which continues changing afterward. The refreshed Now image preserves the 1440 × 1000 light-mode viewport and last-24-hours filter.

Now uses its **last 24 hours** filter. The two comparison images are direct element screenshots of the “Runs by harness and model” and “By difficulty and model” sections lower down that page. They preserve the table labels, sample counts, missing values, and sparse-sample styling. These observations are not controlled model benchmarks or estimates for another project.

With a garden already running, use Node.js with the Playwright package available to its module resolver:

```bash
node scripts/capture_live_readme.cjs http://127.0.0.1:8765 docs/screenshots
```

The script only reads pages; it never submits a garden action. The selected Board routes name this development garden's product and phase. Change those routes if capturing another garden. It captures the page at 1440 × 1000, device scale 1, in light mode; the two chart images capture their whole elements. It waits for page load, fonts, and images, checks HTTP status, and rejects JavaScript errors or document-wide overflow. Board columns have their own horizontal scrolling region.

`capture-live.json` records the browser version, routes, timestamps, viewport widths, and chart selectors/bounds. The live server was viewed through Windows Edge, with the application running in WSL. Regeneration will naturally produce different tasks and numbers.

## Small example garden

The Inbox, Trellis, dark task, and phase images in the expandable section use a fictional Fieldnotes project. They show the app at source revision `6851d128`, captured on 2026-09-09. Tasks, run results, merge history, and $1.01 of recorded spend are sample data.

The fixture uses a temporary garden, a local bare Git remote, and an in-memory GitHub adapter. It serves with the scheduler loop off and automatic dispatch disabled. It does not invoke a model. Stop the server with Ctrl+C to remove the temporary garden.

From an activated Python environment with this project's dependencies installed:

```bash
PYTHONPATH=src python scripts/readme_demo.py
```

In a second terminal:

```bash
node scripts/capture_readme.cjs
```

These four images are viewport captures at 1280 × 1200, device scale 1. The task page uses dark mode; the others use light mode. `capture.json` records their browser, routes, themes, viewport, measured page widths, and JavaScript errors.

## Browser setup and inspection

Both scripts accept a base URL and output directory. `README_BROWSER` can point to an existing Chromium-compatible executable. Otherwise install Playwright's Chromium browser.

For example, install capture-only dependencies outside the repository with `npm install --prefix /tmp/garden-readme-browser playwright`, install its browser with `/tmp/garden-readme-browser/node_modules/.bin/playwright install chromium`, and set `NODE_PATH=/tmp/garden-readme-browser/node_modules` for the capture command. These Node dependencies are only for regenerating documentation screenshots; garden itself needs no Node build.

Read the PNGs back after capturing. Automated checks cannot establish whether they illustrate the accompanying text well or contain material unsuitable for a public README. Other images in this directory are historical captures.

The development-loop diagram is a separate, editable [SVG](../development-loop.svg), with an accessible title and description. It is not a UI screenshot.
