# Product specifications

This directory is the canonical product-repository entry point for context-garden's
specifications. Detailed product and phase specifications are authored in the configured Garden
workspace; this repository keeps the implementation-facing architecture and protocol documents
beside the code.

- [System design](../docs/design.md) explains the product idea and operating loop.
- [Implementation architecture](../docs/architecture.md) records current source boundaries.
- [Worker protocol](../docs/worker-protocol.md) defines dispatch, evidence, and recovery.
- [Specification audit](audit-2026-09-10.md) inventories both repositories, with status,
  ownership, and audited source revisions.

Mechanism contracts live next to their implementation documentation, including
[configuration](../docs/configuration.md), [branch ownership](../docs/branch-stack-ownership.md),
[host lifecycle](../docs/host-lifecycle.md), [release](../docs/release-protocol.md), and
[validation](../docs/test-suites.md).

Product specifications express durable intended behavior. Phase goals authorize bounded work;
task status records implementation progress and does not silently change a requirement. The
newest explicit owner decision governs a contradiction until the canonical spec is reconciled.
“Implemented” does not mean released, installed, deployed, or environment-verified.
