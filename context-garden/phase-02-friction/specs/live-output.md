# Live worker output

`claude -p --output-format stream-json` emits one JSON object per event. The runner should
support `claude.output_format: stream-json`; when set, `stdout.json` becomes a JSONL log and
`collect()` takes the final `result` event. The web UI task page then tails the log
(HTMX polling every few seconds, last N events, rendered as a compact timeline: tool
calls, text, errors), and the TUI shows the same in its detail pane.
