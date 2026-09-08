# CG-319 browser evidence

The disposable Now fixture was served at `http://localhost:8769`. The captures exercise
the retained `/now` page in light and dark at 1280px and in a 390 CSS-pixel iframe. The
narrow page measured `clientWidth=390` and `scrollWidth=390`.

Interaction checks covered Inbox navigation to `/now`, the hour/24-hour period controls,
the live EventSource target, and the `/now1` and `/now2` redirects. The fixture's quiet
state renders without running cards while retaining the next-work explanation.
