# README screenshots

These images show the app at source revision `6851d128`, captured on 2026-09-09 with a fictional Fieldnotes project. Tasks, run results, merge history, and $1.01 of recorded spend are sample data, not production results or pricing claims. The app's templates and styles are unmodified.

The fixture uses a temporary garden, a local bare Git remote, and an in-memory GitHub adapter. It serves with the scheduler loop off and automatic dispatch disabled. It does not invoke a model. Stop the server with Ctrl+C to remove the temporary garden.

From an activated Python environment with this project's dependencies installed:

```bash
PYTHONPATH=src python scripts/readme_demo.py
```

In a second terminal, use Node.js with the Playwright package available to Node's module resolver and a Chromium browser installed:

```bash
node scripts/capture_readme.cjs
```

For example, install capture-only dependencies outside the repository with `npm install --prefix /tmp/garden-readme-browser playwright`, install its browser with `/tmp/garden-readme-browser/node_modules/.bin/playwright install chromium`, and set `NODE_PATH=/tmp/garden-readme-browser/node_modules` for the capture command. These Node dependencies are only for regenerating documentation screenshots; garden itself needs no Node build.

The capture script accepts a base URL and output directory as positional arguments. `README_BROWSER` can point to an existing Chromium-compatible executable. These captures used Windows Edge with the fixture served from WSL. `capture.json` records the browser version, routes, themes, viewport, measured page widths, and JavaScript errors.

All five images are viewport captures at 1280 × 1200, device scale 1. The task page uses dark mode; the other images use light mode. The script waits for network idle and fonts, verifies HTTP 200, and fails on page errors or horizontal overflow. Read the PNGs back after capturing: automated checks do not establish that the screenshots illustrate the accompanying text well.

Only the five images linked from the main README are refreshed here. Other images in this directory are historical captures.
