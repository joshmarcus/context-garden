# Independent-project onboarding demonstration

This record covers the repeatable part of stabilization outcome 1. It deliberately does
not claim real-user adoption: no named existing project or authorized maintainer was
provided for CG-340.

## Result

- **Repeatable compatibility: PASS.** A disposable POSIX shell repository is created from
  scratch, onboarded through the product API, given a grounded draft from its checked-in
  TODO, approved through the scheduler's normal brief gate, changed, checked, reviewed by
  deterministic fixture assertions, merged, and tested again on its main branch.
- **Real-user adoption: UNPROVEN.** The fixture is not a real existing repository, and its
  scripted review is not maintainer acceptance. A named project, authorization to change
  it, and that project's maintainer review are still required.

## Replay and artifacts

Run:

```console
PYTHONPATH=src .venv/bin/python scripts/run_onboarding_demo.py \
  --output docs/stabilization/artifacts/cg-340-fixture.json
```

The committed [fixture audit](artifacts/cg-340-fixture.json) records the generated setup,
grounded task provenance, approval status, implementation commit, changed files, scripted
review result, final behavior checks, and explicit unverified requirements. The run uses a
temporary directory, makes no network or model calls, and edits no generated configuration;
the audit records the configuration hash and confirms it remains unchanged after onboarding.

Automated interaction is distinguished from human interaction throughout: `make setup`,
`make test`, `make lint`, `git diff --cached --check`, and the final test after merge are
process checks. The review is a scripted scope-and-behavior assertion. There was no external
maintainer interaction.

## Observed journey

1. Project setup discovers `make setup`, `make test`, and `make lint` from the fixture's
   Makefile.
2. Planning produces `Reject blank names`, grounded in `TODO.md`; graph validation has no
   findings and scheduler approval moves the complete draft to `ready`.
3. Implementation changes only `greet.sh` and `test.sh`. A blank name exits 2 with
   `name must not be blank`, while a supplied name still prints its greeting.
4. The scripted fixture review accepts only after the behavior checks, syntax check, clean
   staged diff, and exact changed-file scope pass.
5. The accepted branch is merged with `--no-ff`; `make test` passes on main.

## Requirements not verified

- Onboarding a named existing repository with its owner's authorization.
- A real maintainer understanding the plan and accepting the resulting change.
- A hosted pull-request review or a real-model worker run for that project.

Those items require the owner to identify an authorized target and maintainer; fixture
evidence cannot turn them into a pass.
