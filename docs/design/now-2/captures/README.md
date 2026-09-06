# Now 2 browser evidence

Windows Edge rendered a Windows-side copy of the committed HTML and local plate
assets. All four full-page captures were inspected in consecutive 2000px strips,
including Now, Next, phase specimens, the default matrix and all ten atlas states.

| Capture | CSS viewport / document width | Full image | Theme media query |
|---|---|---|---|
| [Light desktop](light-1280-full.png) | 1280 / 1280 | 1280 × 5533 | light |
| [Dark desktop](dark-1280-full.png) | 1280 / 1280 | 1280 × 5533 | dark |
| [Light phone](light-390-full.png) | 390 / 390 | 390 × 10980 | light |
| [Dark phone](dark-390-full.png) | 390 / 390 | 390 × 10980 | dark |

Desktop: separate Now/Next columns, wrapped titles and reasons, readable clocks
and process uncertainty. Phase plates and outcomes balance below the work. The
matrix's best/worst marks, n and faint provisional backgrounds are visible in
both themes. Atlas treatments distinguish a finish from a merge, and a healthy
quiet interval from failure or missing data.

Phone: navigation, controls, run metadata and reasons wrap inside the page.
Phase plates and atlas states stack without clipping. The model matrix extends
inside its own horizontal scroll region, with a visible scroll instruction;
values and sample counts remain legible. No document-level horizontal overflow
was measured. The long page preserves every active run rather than hiding work.

These captures cover the default metric and window. They do not establish
all metric/window combinations or complete keyboard/reduced-motion behavior.
Persona verdicts and responses are linked from the design document.

## Reproduce

Copy `src/garden/web/static/mock/now-2.html` to
`C:\Users\joshm\AppData\Local\Temp\now2-revision\mock\now-2.html`, and copy the
existing `static/plates` directory to the sibling `now2-revision\plates` folder.
The relative image paths must remain `../plates/...`.

Use the product overview's Windows Edge headless recipe. For reliable viewport
measurement and below-fold captures, launch through Windows PowerShell:

```powershell
Start-Process 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe' -ArgumentList '--headless=new --disable-gpu --hide-scrollbars --remote-debugging-port=9335 --user-data-dir=C:\Users\joshm\AppData\Local\Temp\now2-revision\profile-cg314-final about:blank'
```

Then run [capture-edge.ps1](capture-edge.ps1) in Windows PowerShell. This artifact
uses .NET's WebSocket client, with no installed browser library. It sets
`Emulation.setDeviceMetricsOverride` to 1280/390 × 2400 at scale 1, emulates the
light/dark preference, prints `innerWidth`, `scrollWidth`, height and theme,
expands the paint viewport to the measured document height, then
calls `Page.captureScreenshot` for the full document and consecutive strips.
It uses the same Edge engine and Windows-side assets as the overview recipe;
media emulation replaces the separate `--force-dark-mode` launch. The HTML's
recorded state stays at 2026-09-06T02:34:47+00:00; clocks replay from that anchor.
The script is a task-specific evidence recipe, not the CG-315 capture pipeline.
