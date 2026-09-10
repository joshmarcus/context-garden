# Documentation

Choose the shortest path for what you are doing:

| You want to… | Start here | Continue with |
| --- | --- | --- |
| Understand the product | [README](../README.md) | [Design and vocabulary](design.md) |
| Run a first project | [Getting started](getting-started.md) | [CLI guide](cli.md) |
| Operate or troubleshoot a garden | [Operations](operations.md) | [Worker recovery](worker-protocol.md#when-things-go-wrong) |
| Add a review perspective | [Persona reviews](personas.md) | [Review evidence](worker-protocol.md#review-evidence) |
| Contribute to context-garden | [Contributor guide](contributing.md) | [Architecture](architecture.md) and [test suites](test-suites.md) |
| Configure Codex | [Codex setup](codex.md) | [Worker protocol](worker-protocol.md) |

The README is the product overview, not a second operations manual. The getting-started
guide owns the first-project journey; the CLI guide is the task-oriented command reference;
and the operations guide owns steady-state operation and recovery. Architecture and worker
protocol pages describe internals and are linked from those guides instead of repeated.

Feature-specific references describe narrower supported workflows:

- [Release and rollback protocol](release-protocol.md)
- [Local and remote worker lifecycle](worker-protocol.md#variants-of-the-transport)
- [Host lifecycle](host-lifecycle.md) and [host identity boundary](host-identity-boundary.md)
- [GitHub Enterprise configuration](operations.md#github-enterprise)
- [Focused test selection](test-suites.md) and [worker CI](worker-ci.md)

Files under `docs/design/`, `docs/validation/`, and `docs/evidence/` are design history or
verification records. They may explain why a feature exists, but the guides above describe
the supported product. [The roadmap](roadmap.md) is explicitly forward-looking.
