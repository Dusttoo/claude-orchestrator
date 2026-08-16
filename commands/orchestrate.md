---
description: Run one ticket end-to-end through the orchestration pipeline (implement -> code-review -> security-review -> verify -> merge-on-green).
argument-hint: <ticket-id or description>
---

Drive ONE unit of work through the full pipeline. Target: `$ARGUMENTS`.

Read `.orchestration/config.yaml` first. Workflow procedures and reusable safety
tests are plugin-owned; the target repository supplies configuration, rules, and
project-specific acceptance criteria/tests.
Validate config before mutation:

```bash
scripts/orchestration-engine.py validate-config
```

For `schema_version: 2`, branch roles, transition guards, approvals, CI
categories, ticket adapters, and target branches are policy from the configured
workflow. Use `scripts/orchestration-engine.py adapter-plan --host claude
<transition>` whenever the repo declares this ticket flow as explicit
transitions. For legacy configs, keep the existing implement -> review ->
security -> verify -> merge-on-green flow below.

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
   description you could write a failing test from). If it is too thin, scope it
   or push back BEFORE cutting a branch. If `ticket.kind == none`, treat
   `$ARGUMENTS` as the spec.

2. **Pre-implementation gates.** Before cutting a branch or editing production
   code, create an adversarial test matrix. Every row names the attack/failure
   mode, setup/input, invariant, test layer, and falsifying assertion. Cover all
   relevant parser/interpreter syntax (including shell wrappers, substitutions,
   heredocs, redirections, and pipelines), ignored/untracked files, failed Git
   or other inspection, partial execution, cleanup recovery, permissions,
   concurrency, retries, and hostile input. N/A requires a reason. For planned
   security-sensitive infrastructure, launch a fresh
   `orchestration-design-reviewer`; it must define the trust boundary and
   impossible guarantees, reject fragile designs, audit the matrix, and return
   `VERDICT: PASS` before implementation.

3. **Implement.** Launch the `orchestration-implementer` agent with
   `isolation: "worktree"`, passing the ticket body + the click-path. Use the
   configured source and target branch roles. In legacy configs, this is one
   agent, one ticket, one worktree, one branch, one PR to the legacy configured
   target branch. Wait for its structured report (PR number, branch,
   worktree, SELF_CHECK).

4. **Gate.** Run the gate pipeline on the resulting PR -- invoke
   `/orchestration:gate <pr>` (code-review, then security-review when the diff
   hits a `security_required_when` trigger). Both must end `VERDICT: PASS`.
   - Reviewers finish the entire checklist and adversarial sweep and batch all
   findings, even after the first blocker. Maintain a failure ledger by each
   finding's `[component: ...]` key across all gates and rounds. The first component
     failure may return to a fresh implementer. A second failure in that same
     component triggers the design reviewer and a revised adversarial matrix;
     do not authorize another narrow patch. Re-run complete gates. Do NOT merge.

5. **Verify (when configured).** For each entry in the config `verification:`
   block whose `when:` includes this configured target, run it on the rebased
   branch: `scripts/run-verification.sh <name>`. It writes a
   sha-stamped GREEN result file on success (RED = no file = do not merge). If
   the change has a user-visible surface, also run the `orchestration-visual-qa`
   agent against the `Reachable via:` click-path; it must end `VERDICT: PASS`.

6. **Merge on green.** Only after every gate PASSES, every required verification
   is GREEN, and the configured target CI checks are green:
   record the marker with `scripts/merge-guard.sh --record-green <pr>
   [result_file]` (pass the verification result file so the marker is validated
   against the PR head), then merge with `scripts/merge-on-green.sh <pr> <branch>
   all-green <verify_path>`. The merge-guard hook enforces this mechanically.

7. **Close the loop.** If `ticket.kind != none`, transition the ticket. Confirm
   the work actually landed on the configured target branch.
   Report what merged + the click path.

Respect `concurrency_max`: at most that many heavy verification chains at once,
on non-conflicting areas of the codebase. Dual gates on ONE PR run sequentially.
