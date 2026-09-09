# CG-417 task snapshot measurement

This matched, bounded replay compares `f5767e103` (the immediate predecessor) with
`2cdbfdb0`, using the same disposable 100- and 1,000-task gardens and three serial
loopback `/board` requests plus three no-dispatch ticks per revision. The predecessor
is read with `git archive`; the replay changes neither another checkout nor a live garden.
It starts one Uvicorn process at a time and no worker workloads.

| tasks | revision | served scans (fresh, warm, warm) | served p50/p95 | tick scans | tick p50/p95 |
| ---: | --- | --- | ---: | --- | ---: |
| 100 | before | 1, 0, 0 | .0076/.0771s | 2, 2, 2 | .0659/.0668s |
| 100 | after | 1, 0, 0 | .0072/.0807s | 1, 1, 1 | .0354/.0375s |
| 1,000 | before | 1, 0, 0 | .0581/.4788s | 2, 2, 2 | .7209/.7809s |
| 1,000 | after | 1, 0, 0 | .0566/.3712s | 1, 1, 1 | .3891/.3901s |

The fresh served request parses one task snapshot; the following two requests reuse the
unchanged page discovery cache in both revisions, so CG-417 leaves served scan counts and
latency in the existing request-snapshot behavior. Its changed path is the quiet tick:
the post-reap scan falls from two to one at both fixture sizes. Every served sample remains
under the four-second tolerance.

Run the replay from this checkout after committing the source change:

```bash
timeout --signal=TERM --kill-after=10s 900 "$GARDEN_VALIDATION_RUNNER" -m garden.validation -- \
  .venv/bin/python docs/validation/cg417/reproduce.py --output /tmp/cg417-measure --samples 3
```

`report.json` is the recorded result. The script writes its complete disposable fixtures,
server logs, and raw per-request spans only beneath its requested output directory.
