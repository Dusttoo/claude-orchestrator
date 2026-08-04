---
description: Run one ticket end-to-end through the orchestration pipeline (implement -> code-review -> security-review -> verify -> merge-on-green).
argument-hint: <ticket-id or description>
---

Drive ONE unit of work through the full pipeline. Target: `$ARGUMENTS`.

Read `.orchestration/config.yaml` and `.orchestration/ORCHESTRATION.md` first for
this repo's branch model, gate sequence, concurrency cap, and ticket system.

**You are a lossy relay -- do not hand down stale facts.** Every hop from an
agent's report into a brief into a durable doc can drop the uncertainty marker.
Before you write anything into a brief or a durable artifact:
- Put a **grep target or symbol**, never a `file:line`, in a brief -- line numbers
  drift the moment a branch moves; a grep target is self-verifying.
- Never hand over a **selector, snippet, or query you did not run**. State intent +
  constraint and let the implementer write and verify the code.
- **Label provenance** on every relayed claim (`verified now` / `reported by
  <agent>, unverified` / `from the ticket, unverified`) and tell the receiver to
  verify and report what they drop.
- Two agents agreeing is verification **only if the second named the evidence it
  looked at** -- otherwise it is transitive trust.
- **Re-derive before any durable write** (CLAUDE.md, ticket, PR body): open the
  file, now, on this branch. Anything learned before a merge you performed is
  stale.

Steps:

1. **Scope check.** If `ticket.kind != none`, confirm the ticket is Ready (a
   description you could write a failing test from). If it's too thin, scope it
   or push back BEFORE cutting a branch. If `ticket.kind == none`, treat
   `$ARGUMENTS` as the spec.

2. **Implement.** Launch the `orchestration-implementer` agent with
   `isolation: "worktree"`, passing the ticket body + the click-path. One agent,
   one ticket, one worktree, one branch, one PR to the integration branch. Wait
   for its structured report (PR number, branch, worktree, SELF_CHECK).

3. **Gate.** Run the gate pipeline on the resulting PR -- invoke
   `/orchestration:gate <pr>` (code-review, then security-review when the diff
   hits a `security_required_when` trigger). Both must end `VERDICT: PASS`.
   - On any `VERDICT: FAIL`: relay the blocking findings back to a fresh
     implementer agent to fix, then re-gate. Do NOT merge.

4. **Verify (when configured).** For each entry in the config `verification:`
   block whose `when:` includes this target (the integration branch here), run
   it on the rebased branch: `scripts/run-verification.sh <name>`. It writes a
   sha-stamped GREEN result file on success (RED = no file = do not merge). If
   the change has a user-visible surface, also run the `orchestration-visual-qa`
   agent against the `Reachable via:` click-path; it must end `VERDICT: PASS`.

5. **Merge on green.** Only after every gate PASSES, every required verification
   is GREEN, and the integration CI checks (`ci_checks_integration`) are green:
   record the marker with `scripts/merge-guard.sh --record-green <pr>
   [result_file]` (pass the verification result file so the marker is validated
   against the PR head), then merge with `scripts/merge-on-green.sh <pr> <branch>
   all-green <verify_path>`. The merge-guard hook enforces this mechanically.

6. **Close the loop.** If `ticket.kind != none`, transition the ticket. Confirm
   the work actually landed (`git cat-file -e origin/<integration>:<a new file>`).
   Report what merged + the click path.

Respect `concurrency_max`: at most that many heavy verification chains at once,
on non-conflicting areas of the codebase. Dual gates on ONE PR run sequentially.
