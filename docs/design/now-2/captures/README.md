# Now 2 browser evidence

Captured with Windows Edge headless from a copy of the committed mock and local
plate assets on 2026-09-06 UTC. The HTML still identifies the operational snapshot
as 2026-09-06T02:34:47+00:00; its elapsed clocks replay from that timestamp.

- `light-1280.png`: 1280 × 2400. Inspected: readable Now and Next columns, wrapping
  titles/reasons, visible elapsed clocks and explicit missing-process labels.
  The bottom of this viewport reaches the phase and outcomes headings, not the
  metric matrix or atlas.
- `light-390-diagnostic.png`: 390 × 2400 output with right-side clipping. Retained
  as a diagnostic, not approval of the mobile layout. The actual CSS viewport
  width could not be measured before WSL interop stopped launching Edge.
- Dark screenshots and DOM measurements: no output; Windows process launch failed
  with `WSL ... ERROR: UtilAcceptVsock:271: accept4 failed 110`.

No persona verdict, full-page inspection, keyboard interaction, or dark-mode
approval is implied by these images. Capture follow-up belongs to the existing
CG-315 browser-capture work, not another parallel implementation in this design.

## Reproduce

From the assigned product worktree, copy the HTML to
`/mnt/c/Users/joshm/AppData/Local/Temp/now2/mock/now-2.html` and the existing
`src/garden/web/static/plates/` assets to the sibling `now2/plates/` directory.
Preserve that relative layout: the mock's image URLs are `../plates/...`.

```bash
"/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" \
  --headless=new --disable-gpu --hide-scrollbars \
  --window-size=1280,2400 \
  --screenshot="C:\Users\joshm\AppData\Local\Temp\now2\light-1280.png" \
  "file:///C:/Users/joshm/AppData/Local/Temp/now2/mock/now-2.html"
```

Repeat with `--window-size=390,2400` and a distinct output filename. For dark,
add `--force-dark-mode`. Before claiming phone evidence, measure `innerWidth`
and `document.documentElement.scrollWidth`; screenshot pixel dimensions alone
do not prove a 390px CSS viewport. Capture below-fold regions separately or use
a browser full-page screenshot, then inspect the matrix and every atlas state.
