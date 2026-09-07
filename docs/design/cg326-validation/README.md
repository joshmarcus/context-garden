# CG-326 capture evidence

These PNGs are current-head renders of the seeded UI check's `/` page:

- `now-390-light.png` — light color scheme
- `now-390-dark.png` — dark color scheme

The check rendered each narrow page in a 600px outer browser viewport, inside a
390px iframe, and measured the embedded document before taking its screenshot.
For both palettes the recorded measurement was `clientWidth=390` and
`scrollWidth=390`; the PNGs are each 390 by 5400 pixels.

Command used:

```sh
LD_LIBRARY_PATH=/home/joshua/.local/share/garden/browser-runtime/usr/lib/x86_64-linux-gnu \
  PYTHONPATH=src .venv/bin/python -m garden.walkthrough --ui-check <capture-dir>
```

The browser-backed check remains strict: pages whose content itself overflowed
390px were reported as failed captures rather than being accepted as narrow
evidence.
