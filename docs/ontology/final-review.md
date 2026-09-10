Request changes.

The worktree version addresses all five requested semantic points, including phase-scoped persona reviews, `waiting`/`env_error`, optional links, open usage mappings, and non-unique resolved authorities. However:

- `ONTOLOGY.md` is not present in commit `bcc8419c2dde10ff022621a8a73d37bebdb8d1bc`; it is untracked in the worktree, so that commit cannot be verified as containing this ontology.
- The ER diagram declares `RUN ||--|| USAGE`, implying a mandatory one-to-one entity, contradicting the text and implementation where `usage` is an optional open mapping stored directly on `Run` ([ONTOLOGY.md](/home/joshua/work/worktrees/CG-532/ONTOLOGY.md:47)).

ONTOLOGY_REVIEW: {"verdict":"request_changes","reviewed_head":"bcc8419c2dde10ff022621a8a73d37bebdb8d1bc","summary":"Requested semantic corrections are present in the untracked worktree file, but the reviewed commit does not contain ONTOLOGY.md and its diagram contradicts the open optional Run.usage mapping.","findings":["ONTOLOGY.md is absent from the specified commit.","The RUN-to-USAGE ER cardinality incorrectly implies a mandatory one-to-one entity rather than an optional open mapping on Run."]}