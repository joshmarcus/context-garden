# CG-382 served Store replay

The replay makes two disposable gardens with 122 task files and retained histories of
1,546 and 6,000 runs. It serves each profile at a real loopback HTTP URL, samples Board
latency three times in each of three request/expiry intervals, records Store scans, YAML
parses and filesystem stats per request, and keeps two short-lived bounded 16 MiB workload
processes active. It also records a 404 task request and the succeeding Board recovery.

Run it from the reviewed checkout after committing the change:

```bash
python3 docs/validation/cg382/reproduce.py --output /tmp/cg382-replay --samples 3
```

`/tmp/cg382-replay/manifest.json` is the interaction manifest; its `before` source is an
archive of `HEAD^`, and its `after` source is the exact checkout head. The fixture and all
processes are temporary. The replay neither reads a production garden nor changes cache,
pressure, or scheduler-cap settings. Scan/parse reductions are measured; latency numbers
are descriptive distributions on the shared host, not a regression threshold.
