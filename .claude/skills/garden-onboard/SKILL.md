---
name: garden-onboard
description: Analyse an existing repository interactively and draft a context garden from its docs, environment and backlog. Use when the user says "onboard this project", "/garden-onboard", or "make a garden for this repo".
---

# garden-onboard

Run `garden onboard <path-or-url> --into <garden-directory>`, then review the generated
`<product>/docs/onboarding.md` with the person. Edit the draft product, principles, setup,
phase goals and tasks based on its explicit inferences and open decisions. Never inspect or
copy `.env` or credential values. Run `garden validate` and `garden doctor`, and approve tasks
individually only after their acceptance criteria and reading lists are correct.
