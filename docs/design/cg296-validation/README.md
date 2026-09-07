# Operating UI validation

Disposable running application on port 8781, synthetic data only, watch disabled. 2026-09-06.

Observed browser interactions: Config observation feed selected quiet and remained quiet after reload; Costs group-by changed from activity to model with the breakdown updating automatically and no Filter button visible; Inbox worker question appeared once with its answer action. Config lists tick_interval only under restart-required settings. Automated web tests cover taskless kickoff questions appearing once, kickoff placement after approved tasks, missing-brief approval handling, and empty Inbox behavior.

Inspected Config, Costs, phase and task screenshots at 1280 and 390 CSS pixels in light and dark. Mobile content screenshots include scrolling below the long navigation. The capture fixture selects the application's existing data-theme CSS using a capture-only query parameter. Full-page Inbox captures have browser stitching repetition; viewport content captures are the reliable visual evidence. Files alongside this note are synthetic app captures, not operational snapshots.

Known pre-existing limitation: the phase progress chart exceeds a 390px viewport; mobile navigation also precedes the content in a long list. These layout issues are separate from the operating vocabulary and question changes. Capture scope follows affected pages, rather than the previous review's request to recapture every unrelated page. The shared rail Runs label was inspected across those pages. Harness-aware profile defaults remain CG-283; this PR changes display wording only.

A draft with placeholder criteria kept Approve disabled. Saving valid criteria originally left stale disabled controls; fixed by refreshing dependent page state after success. Repeated the browser journey: saved criteria appeared and Approve became enabled without manual reload or dispatch. Final targeted suite: 169 passed, lint passed.
