# CG-479 web page load measurements

Measured on the assigned AWS worker with Python 3.12.14 and Playwright Chromium. The
disposable fixture contains 600 tasks, 1,200 runs (including one active local worker), and
5,000 events. Each row uses three warm samples; values are medians in milliseconds.

Reproduce with:

```bash
.venv/bin/python scripts/benchmark_web_pages.py --browser
```

The server measurement uses an in-process ASGI client. Browser response time is navigation
`requestStart` through `responseEnd`; browser render is `responseEnd` through
`domContentLoadedEventEnd`. Both measurements use localhost, so public-network transport is
deliberately excluded rather than mixed into application time. Cold means the first request
for that route in the ordered run; warm requests follow in the same process.

| Page | Server before | Server after | Browser response before | Browser response after | Browser render before | Browser render after |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Now | 809.6 | 381.5 | 992.1 | 670.6 | 38.6 | 42.0 |
| Inbox | 632.7 | 193.1 | 626.2 | 233.2 | 31.6 | 35.9 |
| Board | 609.7 | 251.6 | 1,347.6 | 458.7 | 105.9 | 35.6 |
| Task detail | 743.3 | 181.8 | 1,339.4 | 472.5 | 29.9 | 39.8 |
| Run detail | 650.8 | 164.2 | 576.8 | 260.4 | 25.1 | 26.7 |
| Config | 742.5 | 338.0 | 579.0 | 301.9 | 26.9 | 41.7 |

The profile attributed 13.6 seconds of the 12-request run to `Store.tasks`/`Store._scan`,
including 7,200 task YAML parses. Request-local snapshots had prevented duplicate scans
within one response, but every new request still reparsed all 600 task files. The change
keeps one metadata-validated parsed discovery snapshot and deep-copies it into each isolated
request. In-process writes invalidate eagerly; edits from another process change the file
fingerprint and are visible on the next request. A focused regression test covers both reuse
and external-edit invalidation.

The remaining largest costs are the full Inbox computation embedded in common page context,
the Now page's event/period aggregation, and large Board HTML (264 KB in this fixture).
Those paths retain current operational data and failure visibility; this change does not add
time-based response caching or hide rows. Public deployments should measure network transfer
separately because latency and compression depend on their proxy and link.
