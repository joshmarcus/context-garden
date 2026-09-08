# CG-419 production worker verification

## Recorded environment

- Starting commit: `be22506bd00cce32fb0f3acba121a764bafb506c` (v0.3.0rc2)
- UID: `999`
- Python: `Python 3.12.3`
- Codex: `codex-cli 0.153.4`
- Codex login status: logged in using ChatGPT

## Focused check executed

Exact command:

```sh
.venv/bin/python -m pytest -q tests/test_managed_worker.py
```

Observed result:

```text
............                                                             [100%]
12 passed in 0.19s
```

These tests exercise the managed-worker consumer's resource reporting, exclusive host-slot locking, claim polling and single-run behavior, host-fact attribution, temporary-directory setup, environment allowlist composition, and low-resource handling. The concrete finding is that all 12 focused managed-worker tests passed on this worker.

## Production scope and remaining operator verification

This verification executed only the focused local test module. It does not establish live production properties such as a complete bootstrap replay, service startup and lifecycle behavior, network/firewall enforcement, remote controller connectivity, credential isolation, or remote branch publication; those remain for operator verification.

The initial attempt authenticated successfully but could not use tools because the standalone Codex executable lacked its Code Mode companion. The operator subsequently installed the complete official Codex 0.153.4 package after verifying its SHA256. This is a live repair, not evidence of an untouched final-bootstrap replay. Permanent bootstrap and controller URL fixes remain tracked by CG420.
