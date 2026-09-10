# OpenRouter adapter spike (CG-302)

## Decision

Use the built-in `codex` harness with an OpenRouter `base_url`; do not add a distinct
`harnesses.openrouter` implementation. A configured instance has this shape:

```yaml
harness: codex
harnesses:
  codex:
    base_url: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY
    models:
      easy: openai/gpt-5.2-codex
      medium: openai/gpt-5.2-codex
      hard: openai/gpt-5.2-codex
```

Garden turns `base_url` into a named Codex model-provider configuration using the
Responses wire API. Codex remains the existing OpenAI-compatible CLI and owns the agent
loop, tool execution, and provider stream. Garden continues to own only process launch,
JSONL normalization, and `GARDEN_RESULT` parsing.

This avoids a second implementation of Codex command construction, resume behavior,
sandbox flags, JSONL events, usage accounting, and failure classification. It also keeps
OpenRouter credentials in the existing worker environment allowlist boundary. The tradeoff
is deliberate: OpenRouter is a provider selection, not a separately schedulable harness.

## Fake and result contract

[`tests/fake_openrouter.py`](../tests/fake_openrouter.py) is the executable contract fixture.
It validates the `codex exec --json` argv, provider settings, model, and stdin brief, then
emits representative Codex JSONL. The final `item.completed` agent message contains the
one-line `GARDEN_RESULT`; `Harness.parse()` selects that message and delegates the marker
to `parse_result()`. `test_fake_openrouter_smoke` executes this boundary end to end without
a live provider or provider spend.

## Follow-ups and open questions

### CG-213 — harness configuration

CG-213 should promote the prototype `harnesses.codex.base_url` and `api_key_env` fields into
supported configuration: document and validate them, and ensure the named key is admitted
through `worker_env.pass`. It should decide whether to reject a non-HTTPS URL outside tests
and how configured OpenRouter prices override Codex defaults. Live provider compatibility,
authentication errors, quota errors, and resume behavior remain CG-213 integration work;
this spike deliberately makes no paid request.

### CG-230 — member syntax

CG-230 should reuse ordinary pool member syntax: `codex:<model>`. It should not introduce
`openrouter:<model>`, because that would falsely identify the provider as a harness and
would create a second pause/quota bucket for the same Codex CLI. If one pool must mix
direct OpenAI Codex and OpenRouter-backed Codex concurrently, the current single `codex`
configuration cannot express per-member provider settings; CG-230 must either rule that
out or add named harness instances/aliases before defining the member syntax.
