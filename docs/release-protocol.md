# Release protocol

Releases are deliberate, human-approved publications. A merged pull request records a
possible upgrade for a running garden; it does not create a package, publish a release, or
move a tag.

## Candidate to release

1. Choose an immutable commit and set the matching package version in `pyproject.toml`.
2. Create the annotated `v<version>` tag at that exact commit. Never force-move a published
   tag.
3. Write release notes, build the source artifact (and any prebuilt artifacts), and record
   each local path and SHA-256 in an artifact manifest. When no prebuilt artifact is supplied,
   set `prebuilt: absent`; source installs remain supported.
4. Make a candidate manifest with `kind: draft` or `kind: prerelease`, the exact commit, the
   repository, release notes, successful exact-commit CI links, and the artifact manifest.
5. Run `garden release validate --manifest path/to/candidate.yaml`. For GitHub Enterprise,
   pass `--github-host forge.example.com` (and optionally `--repository owner/repo`). The
   command only reads local files and Git metadata; it makes no network request and never
   publishes anything.
6. After a reviewer approves the candidate and validation succeeds, manually create the
   corresponding draft or prerelease in the configured GitHub/GitHub Enterprise repository.
   Upload only the digest-checked artifacts.

Example candidate manifest:

```yaml
version: 0.1.0
tag: v0.1.0
commit: 899b2c0cda5a311f6e498b75929ca6b58a524eab
kind: prerelease
repository: joshmarcus/context-garden
notes: v0.1.0-notes.md
artifacts: v0.1.0-artifacts.yaml
ci:
  - status: success
    commit: 899b2c0cda5a311f6e498b75929ca6b58a524eab
    url: https://github.com/joshmarcus/context-garden/actions/runs/123
```

The artifact manifest contains the same version and full commit, at least one file with a
SHA-256 digest, and `prebuilt: included` or `prebuilt: absent`.

## Rollback

Do not rewind scheduler state and do not change a published tag. Withdraw or mark the
published release as superseded in the forge, then publish a new version and immutable tag
from a corrective commit through the same candidate process. Operators who need to return a
running garden to an earlier build use the existing pinned-install maintenance/upgrade flow;
that does not alter release history or task state.
