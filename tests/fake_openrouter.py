#!/usr/bin/env python3
"""Fake the Codex/OpenRouter adapter boundary used by the CG-302 smoke test.

The selected adapter is still ``codex exec``; this executable stands in for Codex so the
test spends no provider tokens.  Its request contract is:

* argv starts with ``exec --json --skip-git-repo-check``;
* Codex config selects ``model_provider=\"openrouter\"`` and defines that provider's
  ``name``, ``base_url``, API-key environment variable, and ``wire_api=\"responses\"``;
* ``-m`` carries an OpenRouter model id and stdin carries the garden brief.

With those settings the real Codex CLI makes an OpenAI Responses-compatible request to
``<base_url>/responses``.  OpenRouter returns the Responses event stream; Codex, rather
than garden, drives tool calls and translates it into its documented ``--json`` JSONL.
This fake emits that translated response payload: ``thread.started``, an
``item.completed`` agent message containing the final ``GARDEN_RESULT``, and
``turn.completed`` usage.  ``Harness.parse`` then extracts the last agent message and
passes its marker to ``garden.brief.parse_result``.
"""

from __future__ import annotations

import json
import sys


def _config(args: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, arg in enumerate(args[:-1]):
        if arg != "-c":
            continue
        key, separator, value = args[index + 1].partition("=")
        if separator:
            values[key] = value.strip('"')
    return values


def main() -> int:
    args = sys.argv[1:]
    config = _config(args)
    required = {
        "model_provider": "openrouter",
        "model_providers.openrouter.name": "OpenRouter",
        "model_providers.openrouter.wire_api": "responses",
    }
    if args[:3] != ["exec", "--json", "--skip-git-repo-check"]:
        return 2
    if any(config.get(key) != value for key, value in required.items()):
        return 2
    if not config.get("model_providers.openrouter.base_url"):
        return 2
    if config.get("model_providers.openrouter.env_key") != "OPENROUTER_API_KEY":
        return 2
    if "-m" not in args or not sys.stdin.read().strip():
        return 2

    final = 'GARDEN_RESULT: {"status":"done","summary":"OpenRouter adapter smoke passed"}'
    events = [
        {"type": "thread.started", "thread_id": "openrouter-smoke"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": final}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 12, "cached_input_tokens": 2, "output_tokens": 5,
        }},
    ]
    for event in events:
        print(json.dumps(event))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
