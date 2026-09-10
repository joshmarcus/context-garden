# Final maintainer check of ONTOLOGY.md

Read-only review; do not edit files. At source commit
`bcc8419c2dde10ff022621a8a73d37bebdb8d1bc`, verify that `ONTOLOGY.md` now correctly:

1. models phase persona reviews without requiring a task document or PR;
2. includes `waiting` and `env_error` run states and distinguishes lifecycle completion from
   archive eligibility;
3. makes task/event and harness/model links optional and treats usage as one open mapping;
4. does not invent per-run uniqueness for resolved workload authorities; and
5. retains the workload-identity authority model requested by the first review.

Also report any remaining concrete source contradiction that makes the ontology unsafe as a
canonical reference. Return concise Markdown and end with:

`ONTOLOGY_REVIEW: {"verdict":"approve"|"request_changes","reviewed_head":"bcc8419c2dde10ff022621a8a73d37bebdb8d1bc","summary":"<summary>","findings":[]}`
