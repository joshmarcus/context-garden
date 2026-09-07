# Review-loop retrospective

CG-300 and CG-323 stopped at the former four-round hard cap despite concrete, actionable
findings. They are implementation/recovery loops, not evidence that review should be skipped;
the new unlimited setting keeps their current-head review and merge gates in place while the
soft threshold makes their cost and evidence visible.

CG-297 repeatedly mishandled child CSS combinators. This is an implementation defect with a
repeated unaddressed finding, and remains subject to existing stall handling rather than being
treated as a reason to buy unbounded identical retries.

CG-339 repaired evidence placeholders but retained label-only recovery validation. This is a
newly discovered validation defect; CG-339 remains the prevention work for proportional
real-application evidence.

CG-358 met functional criteria but was held for a blanket missing-capture finding. This is
stale or missing infrastructure evidence, not a weakened functional claim; CG-323's preflight
and CG-339's evidence work are the linked prevention paths.

CG-365 reached approval after four rounds with only stale description evidence left to rewrite.
This is a description-only correction, which the reviewer can apply directly without another
worker revise round.

The measurable follow-up is one friction record per task/loop episode after
`review.friction_after`: it includes round count, work/revise/review cost, head lineage,
classified cause and actionable evidence. CG-374 owns broader routine-recovery classification,
CG-372 review-admission fairness, CG-323 worker preflight and CG-339 proportional evidence;
the signal links to those tasks instead of filing a new ticket on each scheduler tick.
