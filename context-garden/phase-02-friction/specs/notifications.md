# Notifications

When a task transitions to `in_review`, `failed`, or `changes_requested` with the revision
cap hit, the garden should tell the human. Config `notify.command` runs a shell command with
`GARDEN_TASK_ID`, `GARDEN_STATUS`, `GARDEN_MESSAGE`, `GARDEN_PR` in its environment (so
`terminal-notifier`, `osascript`, `notify-send`, or a curl to Slack all work). The web UI
shows the same events in an inbox strip. Keep it to one hook; no notification library.
