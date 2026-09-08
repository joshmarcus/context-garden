# CG-356 served onboarding recovery replay

The replay at `c910cac39d559e924a26f47bee08d8591a20dd7c` starts a disposable HTTP
application around the real `onboard_project` implementation and drives its recovery
endpoints over loopback. The durable
[`replay/interaction-manifest.json`](replay/interaction-manifest.json) records every
request, response, and observation.

- Empty: the initial served state has no product and no tasks.
- Failure: rejected planner provenance returns the recovery explanation, leaves the
  pre-existing garden byte-for-byte unchanged, and leaves no product or imported task.
- Recovery: a subsequent served retry creates the listed onboarding files and one draft
  task only.
- Collision: another onboarding attempt returns a collision response without changing the
  completed garden.

Reproduce from the recorded source with:

```sh
PYTHONPATH=src .venv/bin/python docs/validation/cg356/reproduce.py \
  --output docs/validation/cg356/replay/interaction-manifest.json \
  --head c910cac39d559e924a26f47bee08d8591a20dd7c
```

This is lifecycle interaction evidence, not a rendered-appearance change or screenshot
claim.
