# Multiplayer HTTP authorization

Multiplayer mode authenticates every non-public HTTP request as a garden member installation.
The route policy is deliberately split between admission and projection: middleware resolves
resource ownership and rejects direct access outside the principal's projects, while collection
handlers construct their response only from the principal's visible project set.

| Read surface | Principal policy | Response boundary |
| --- | --- | --- |
| health, favicon, plate assets | public | contains no garden records |
| config | administrator | garden-wide operational configuration |
| board, inbox, events, costs, runs, trellis, herbarium, Now and their partials/stream | authenticated project reader | tasks, events, runs, costs, products and aggregates are projected to visible projects |
| task, run, operation, investigation, transcript and capture detail | authenticated reader of the owning task's project | the task ID is resolved before admission; aliases and artifacts inherit that ownership |
| phase pages and phase documents | authenticated reader of the named project | the project path is resolved before admission |
| API task and decision collections | authenticated project reader | rows are projected to visible tasks/projects |
| worker/controller status, maintenance, diagnostics and API worker/event collections | administrator | operational garden-wide data is not project-attributable |
| framework docs, design browser, trials | administrator | currently garden-wide; these surfaces do not yet provide a complete project projection |

An `all` visibility principal can read every project. An `assigned` principal can read only the
explicit project keys in the private member registry; an empty assignment has no project read
access. Mutations remain separately authorized as administrator operations or work owned by the
authenticated member. Legacy single-user mode does not install this policy.
