# The review loop

The gates decide whether a PR is safe to land. The review *loop* is what happens
when they say no: fix, re-review, repeat. This document is about making that loop
terminate without lowering the bar.

## Why loops ran long

A review loop with no termination condition runs until something external stops
it. The original design had exactly one exit -- unanimous `VERDICT: PASS` -- and
four properties that kept pushing it away from that exit:

1. **A fresh reviewer every round.** No memory of what was already adjudicated,
   by design, so the author's narrative can never contaminate the gate. The cost
   is that each round is an independent draw from the space of possible findings.
2. **A fresh broad hunt every round**, over a diff that grows with each fix. More
   unrelated surface to find something new in, every time.
3. **No severity floor.** "There is no minor, merge anyway" made a note about
   premature abstraction as load-bearing as an IDOR.
4. **A doubt rule calibrated for round 1**, applied identically at round 8: *a
   false FAIL costs one loop; a false PASS ships a bug.* True the first time,
   misleading the eighth.

Together those make P(some blocking finding) roughly constant round over round.
That is a process with no absorbing state, and 10+ rounds is its expected tail,
not an anomaly.

The old strike-based redesign escalation was supposed to break exactly this cycle, and
it almost never fired. It counted failures per `[component: ...]` key -- a
free-text string each fresh reviewer invented. `auth/sessionStore` in round 1 and
`session-refresh` in round 4 are the same defect wearing two names, so the strike
never landed. The ledger holding those counts also lived in the orchestrator's
context window, which compacts on precisely the long tickets that need it.

## What makes it converge

**The blocking set must shrink monotonically.** That is the property everything
else serves.

- **Round 1 -- full authority.** Sweep the whole diff; every defect class may
  block. Thoroughness is free here, and a defect not raised now loses blocking
  authority later, so there is pressure to be exhaustive exactly once.
- **Round 2+ -- scope freeze.** Inspect the repair delta, every open component,
  and affected callers/trust boundaries. What may block is also narrow: open
  ledger components, regressions in the delta, and security or data-loss findings.
  Anything else newly noticed becomes advisory.
- **Blocking vs advisory.** Correctness, security, uncovered acceptance criteria,
  disabled tests, cross-surface disagreement, and root-cause suppression block.
  Dead weight, naming, and "while I was in here" are recorded and carried to the
  PR body. They are not discarded -- they are just not merge blockers.
- **Round-aware doubt.** Rounds 1-2 keep block-on-doubt. From round 3 a false
  FAIL no longer costs one loop, it costs every remaining one, so the reviewer
  files uncertain findings as advisory and names the evidence that would settle
  them.
- **Keys that are derived, not invented.** `[component: <path>:<symbol>]`, with
  line numbers stripped (they drift on rebase) and free-text subsystem names
  rejected. `review-ledger.py` normalizes them so one repair brief cannot split a
  repeated defect into multiple identities.
- **Separate caps end both loops.** `max_design_rounds` (default 5) counts
  pre-code design verdicts. `max_repair_cycles` (default 2) counts explicit
  repair reports, not review passes, so concurrent code and security gates
  consume one attempt together. Neither cap permits a merge with blockers.
- **A repair is a checkable artifact.** `repair-brief` emits one deduplicated set
  of finding IDs. `record-repair` requires root cause, change, and verification
  for every ID. `complete-repair-review` closes an attempt only after every
  required gate reviews that exact head.

## The ledger

`scripts/review-ledger.py` owns this state on disk, under
`.orchestration/.review-ledger/pr-<n>.json` (gitignore it). It is host-neutral:
the Claude commands and the Codex skills drive the same script.

```bash
review-ledger.py open <pr>                    # once per PR
review-ledger.py brief <pr>                   # paste into every reviewer brief
review-ledger.py record <pr> --gate code-review \
  --result .orchestration/.review-results/code-review.json
review-ledger.py repair-brief <pr>
review-ledger.py record-repair <pr> --report .orchestration/.review-results/repair.json
review-ledger.py complete-repair-review <pr>
review-ledger.py metrics <pr>
review-ledger.py status <pr>                  # strikes, open set, next action
review-ledger.py redesign <pr> --key <key> --verdict PASS
review-ledger.py handoff <pr>                 # the human escalation report
```

Pre-code design uses the same durable store keyed by ticket/change identifier:

```bash
review-ledger.py design-open <ticket>
review-ledger.py design-record <ticket> --verdict FAIL --evidence <artifact>
review-ledger.py design-handoff <ticket>
```

`record` is where the mechanics live. It increments strikes, auto-resolves any
component this gate no longer reports (a completed re-run that stays silent is
the evidence a fix held), demotes out-of-scope new findings in a frozen round,
and returns `next_action`:

| `next_action` | meaning |
|---|---|
| `review` | generate one repair brief, record the repair, re-run all required gates |
| `redesign` | a finding survived a completed repair -- scoped design gate |
| `escalate-human` | cap spent with findings open -- stop, hand over `handoff` |
| `gates-clear` | necessary, not sufficient; confirm the security gate ran |

The repair report must cover the exact open finding set. A repaired head cannot
complete until every required gate records, and no third repair starts after the
configured cap.
And the ledger's `effective_verdict` governs, not the reviewer's claimed one --
a round whose findings were all demoted is a PASS with advisories attached.

## The security gate is exempt

The scope freeze narrows what the *code* reviewer may block on. It does not apply
to `orchestration-security-reviewer`. A leak found in round 4 blocks exactly as
hard as one found in round 1, and `record` never demotes a security-gate finding.
Convergence is a scheduling concern; it is not a reason to ship a data leak.

## Tuning

`max_design_rounds: 5` lets architecture converge before implementation. Keep
`max_repair_cycles: 2` unless measured closure data proves another value safer.

Resist raising it as a reflex. A ticket that repeatedly burns the cap is usually
telling you the acceptance criteria are too vague to test against -- the same
signal a component collecting strikes gives. Scope the ticket harder instead;
that is cheaper than another three rounds.
