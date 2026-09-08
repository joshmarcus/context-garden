# CG-319 browser evidence

The disposable Now fixture was served at `http://localhost:8769`. The captures exercise
the retained `/now` page in light and dark at 1280px and in a 390 CSS-pixel iframe. The
narrow page measured `clientWidth=390` and `scrollWidth=390`.

`replay.py` records the reviewed head and interaction details in
`interaction-manifest.json`. It clicks Inbox navigation to `/now`, switches to the 24-hour
period, observes the live `/now/stream` EventSource target, and follows the `/now1` and `/now2`
redirects. It then finishes every synthetic run and verifies that the quiet state renders
without running cards while retaining the next-work explanation.
