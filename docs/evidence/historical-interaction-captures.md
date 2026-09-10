# Historical interaction capture inventory

The receipts below remain immutable evidence for the source revision named in each file.
Their task-specific replay scripts were retired: current behavior is covered by focused,
disposable served-app tests instead. The tests do not refresh or reinterpret historical
captures.

| Historical task | Receipt and recorded source/environment | Preserved behavior | Current reproduction |
| --- | --- | --- | --- |
| CG-381 | [affected Inbox receipt](cg381/affected-inbox/interaction.json), head `8dcb5de9b4462a90d2bab6fd0578b9451efcfc5e`, disposable Garden on an ephemeral loopback port; its viewport evidence records 1280px and framed 390px light/dark captures. | Inbox shows queued review and a deployment recovery as information rather than owner decisions; an invalid resume fails, while the deployment resume removes only that recovery card. | `tests/test_historical_interactions.py::test_cg381_inbox_ownership_and_deployment_recovery_are_served` uses `garden.qa.sandbox.start(..., watch=False)` and real HTTP requests. |
| CG-410 | [manual Inbox receipt](cg410-manual-inbox-interaction.json), head `d2d2ce76aded9c22e64cec2a71d981f68a303ab2`, disposable local Garden on an ephemeral loopback port; named image artifacts preserve 1280px and 390px Inbox/task views. | An eligible manual task can be claimed despite exhausted automated capacity, exposes its packet, rejects a stale claim, and remains recoverable through malformed then valid completion. | `tests/test_historical_interactions.py::test_cg410_manual_claim_stale_take_and_completion_are_served` uses the same maintained disposable server helper and real HTTP requests. |
| CG-455 | [Now receipt](../design/captures/cg455/interaction.json), head `cd2680d059747bad1b20ef121c85d5ab7164d692`, disposable Garden on an ephemeral loopback port; its viewport evidence records 1280px and framed 390px light/dark captures. | The Now summary handles populated, missing-fragment, recovered, sparse, and paused readings, including the recorded browser refresh sequence. | Focused Now route and refresh coverage remains in `tests/test_now1.py` and `tests/test_web.py`; this task leaves the task-specific browser journey as historical evidence rather than a maintained workflow. |

The retired scripts were `scripts/replay_cg381_inbox.py`, `scripts/capture_cg410.py`, and
`scripts/replay_cg455_now.py`. Those names are historical provenance only, not commands to
run. Current checks create their own temporary Garden and never rely on live state or the
captured artifact paths.
