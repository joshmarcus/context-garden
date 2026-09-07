# CG377 scoped validation / CG376 friction evidence

The integration with CG323 and CG339 uses the same explicit head-bound plan in worker
briefs, precheck capture selection and review evidence. Plans name configured checks and
applicability reasons. The final preflight capture check follows the plan as well; it no
longer reclassifies a backend action as requiring screenshots after planning selected none.
Review reads only completed current-head check artifacts. Shared UI cannot pass by treating
missing capture inventory as an empty requirement. Unknown UI paths require bounded
inspection or a reasoned scope expansion. A failed initial diff inspection is carried as
an inspection requirement; the mechanical precheck supplies actionable feedback.

The runner-boundary regression records the actual submitted UI-check specifications:

| Change | Capture pages requested |
| --- | --- |
| backend control action | none |
| criteria parser | none |
| task page implementation | task |
| shared base template | all affected walkthrough consumers |

This eliminates the previous blanket fourteen-page requirement for backend/parser cases
and requests one page for the one-page case. These are measured fixture request counts,
not observed production savings. At two widths/two themes, a fourteen-page baseline has
56 images versus zero for backend/parser or four for one page; shared UI retains broad
consumer coverage. Actual review rounds avoided, model spend saved, and production latency
impact remain unknown until the deployed loop runs. Link these measurements to CG376's
review-loop friction signal; do not equate fewer required captures with proven cost savings.

Integration validation: 134 tests passed, one browser-dependent case skipped and one
initial inspection-error regression failed in the first bounded run. The defect was fixed;
all seven focused inspection/plan/runner-boundary cases then passed at103.2MiB peak/noSwap.
The first combined run peaked at299.5MiB/noSwap. Exact final CI remains the full-suite gate.
CG339's nine-flow served HTTP replay remains required for relevant lifecycle behavior;
this change does not replace phase-wide walkthroughs or claim visual-browser verification.
