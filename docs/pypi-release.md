# PyPI release

PyPI publication extends the existing reviewed GitHub release process in
[the release protocol](release-protocol.md). It never runs for a merge to `main` or for a
prerelease.

## One-time owner setup

Create the `context-garden` project on PyPI with a pending Trusted Publisher (or add the
publisher to the existing project) using these exact values:

- PyPI project: `context-garden`
- GitHub owner/repository: `joshmarcus/context-garden`
- Workflow: `publish-pypi.yml`
- Environment: `pypi`

In the GitHub repository, create the `pypi` environment and restrict deployment to protected
release tags. PyPI supplies the upload authority through GitHub Actions OIDC; do not add an API
token to the repository.

## Publish a release

1. Set the same new, unused version in `pyproject.toml` and `garden.__version__`, then pass
   normal review and exact-head CI.
2. Follow the release protocol to create the immutable annotated `v<version>` tag and publish
   a non-prerelease GitHub release for that exact commit.
3. The `publish-pypi` workflow checks out the tag, requires its name to match the package
   version, builds the wheel and source distribution once, checks their metadata, and verifies
   clean non-editable installs from both artifacts. The publish job downloads those same build
   artifacts and uses the `pypi` environment's Trusted Publisher.
4. Confirm the workflow's environment deployment, artifact hashes on
   `https://pypi.org/project/context-garden/<version>/`, and a fresh install:

   ```bash
   python -m venv /tmp/context-garden-pypi-check
   /tmp/context-garden-pypi-check/bin/python -m pip install context-garden==<version>
   /tmp/context-garden-pypi-check/bin/python -c "import garden; print(garden.__version__)"
   /tmp/context-garden-pypi-check/bin/garden --help
   ```

   Use a disposable directory appropriate to the platform; on Windows, context-garden remains
   supported through WSL.

PyPI does not permit replacing a version's files. On a retry, the workflow compares every
published filename and SHA-256 hash with the artifacts it built. It exits successfully only
when they match exactly, and fails rather than overwriting or misreporting different files.
Corrections always receive a new version and tag. Existing source-install and rollback paths
remain those in the release protocol.
