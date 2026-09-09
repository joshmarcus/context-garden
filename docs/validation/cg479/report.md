# CG-479 web page load measurements

Measured on the assigned AWS worker with Python 3.12.14 and Playwright Chromium. The
disposable fixture contains 600 tasks, 1,200 runs (including one active local worker), and
5,000 events. Warm values are medians of three samples in milliseconds.

Reproduce with:

```bash
PLAYWRIGHT_BROWSERS_PATH=/var/lib/garden-worker/browsers \
  .venv/bin/python scripts/benchmark_web_pages.py --browser
```

The command measures both modes against the same fixture. `before` invalidates parsed task
discovery between requests, reproducing the old cross-request behavior; `after` retains the
metadata-validated discovery snapshot. Each route gets a newly created app and Store for its
cold request. Its warm requests then use that same process. This avoids treating the first
navigation to a later route as cold after an earlier route has already populated discovery.

## Server response

The server measurement uses an in-process ASGI client. Cold results are single samples;
warm results are medians.

| Page | Before cold | After cold | Before warm | After warm |
| --- | ---: | ---: | ---: | ---: |
| Now | 1,014.5 | 909.4 | 830.6 | 447.2 |
| Inbox | 631.2 | 899.4 | 738.6 | 188.6 |
| Board | 595.9 | 709.6 | 659.6 | 330.2 |
| Task detail | 775.4 | 803.9 | 657.4 | 180.6 |
| Run detail | 571.7 | 865.3 | 734.2 | 162.8 |
| Config | 671.1 | 989.0 | 835.1 | 378.5 |

Cold response time does not consistently improve because both modes must parse the fixture
on their first request. The focused improvement is visible on warm navigation: response
medians fell 46% for Now, 74% for Inbox, 50% for Board, 73% for task detail, 78% for run
detail, and 55% for Config.

## Browser response and rendering

Browser response is navigation `requestStart` through `responseEnd`; render is `responseEnd`
through `domContentLoadedEventEnd`. Cold is the first navigation against a new app; warm is
the median of the following three navigations.

| Page | Response before cold | Response after cold | Response before warm | Response after warm |
| --- | ---: | ---: | ---: | ---: |
| Now | 1,041.3 | 1,120.4 | 999.3 | 501.9 |
| Inbox | 745.8 | 750.3 | 661.7 | 197.6 |
| Board | 716.0 | 891.7 | 774.0 | 410.0 |
| Task detail | 781.5 | 656.0 | 861.8 | 447.8 |
| Run detail | 688.2 | 863.8 | 655.8 | 171.9 |
| Config | 805.7 | 799.9 | 759.4 | 266.5 |

| Page | Render before cold | Render after cold | Render before warm | Render after warm |
| --- | ---: | ---: | ---: | ---: |
| Now | 194.1 | 208.8 | 19.8 | 28.5 |
| Inbox | 39.9 | 19.2 | 21.8 | 28.4 |
| Board | 30.5 | 29.3 | 65.2 | 65.6 |
| Task detail | 47.8 | 21.0 | 27.3 | 33.3 |
| Run detail | 17.1 | 34.1 | 27.0 | 22.6 |
| Config | 18.3 | 18.7 | 25.2 | 25.3 |

Both measurements use localhost. Public-network transport is deliberately excluded rather
than mixed into application time; deployments should measure it separately because latency
and compression depend on their proxy and link.

## Profile and remaining costs

The original profile attributed 13.6 seconds of a 12-request run to `Store.tasks` and
`Store._scan`, including 7,200 task YAML parses. Request-local snapshots had prevented
duplicate scans within one response, but every new request still reparsed all 600 task files.
The change keeps one parsed discovery snapshot and deep-copies it into each isolated request.
In-process writes invalidate eagerly. External edits are detected by metadata, and discovery
is bracketed by pre/post fingerprints so a file edit during parsing forces a retry before the
snapshot is published.

The remaining largest costs are the full Inbox computation embedded in common page context,
the Now page's event/period aggregation, and large Board HTML (264 KB in this fixture). Those
paths retain current operational data and failure visibility; this change does not add
time-based response caching or hide rows.
