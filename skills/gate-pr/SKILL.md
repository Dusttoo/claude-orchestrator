---
name: gate-pr
description: Run the independent review gates on an existing PR and merge it only after code-review, required security review, configured verification, and CI are green. Use when the user asks to gate a PR, review and merge a PR through the orchestration pipeline, run merge-on-green, or run the equivalent of the Claude /gate command.
---

# Gate a PR through merge-on-green

Run the review gates on one existing PR and merge it only after every required
gate is green. This is the natural-language entry point for Codex and the same
workflow as the Claude Code `/gate` command.

## Plugin paths

The role briefs and scripts below live in this plugin, not necessarily in the
target repository. Resolve these paths from this skill file before executing or
reading them:

- `../../agents/orchestration-code-reviewer.md`
- `../../agents/orchestration-design-reviewer.md`
- `../../agents/orchestration-implementer.md`
- `../../agents/orchestration-security-reviewer.md`
- `../../scripts/context_pipeline.py`
- `../../scripts/api_agent.py`
- `../../scripts/merge-guard.sh`
- `../../scripts/merge-on-green.sh`
- `../../scripts/orchestration-engine.py`
- `../../scripts/review-ledger.py`
- `../../scripts/run-gates.sh`
- `../../scripts/run-verification.sh`

Execute scripts by absolute path while keeping the target repository as the
working directory.

## Procedure

Before each `code-reviewer` or `security-reviewer` pass, resolve its route with
`scripts/context_pipeline.py route --config .orchestration/config.yaml --role
<role>`. Desktop routes use fresh native agents. API routes build their request
with `context_pipeline.py payload --config ... --role <role>` and use the
`api_agent.py run --request -` adapter with the ticket and a stable run id.
Desktop fallback is allowed only before provider
acknowledgement; submitted, timed-out, or uncertain work must be reconciled
instead of duplicated.

1. Read `.orchestration/config.yaml` and run
   `orchestration-engine.py validate-config`. For `schema_version: 2`, use
   `orchestration-engine.py adapter-plan --host codex <transition>` for the
   configured gate or merge transition; branch roles, evidence, approvals, CI
   categories, and adapters come from that plan. For legacy configs, continue
   with the existing review gates below.
2. Open the durable review ledger and build this round's brief:
   `review-ledger.py open <pr>` then `review-ledger.py brief <pr>`. The
   failure ledger lives on disk, not in this conversation -- it survives
   compaction,
   normalizes component keys so a repeated defect actually accumulates strikes,
   and decides when the loop stops. Paste the `brief` output verbatim into every
   reviewer pass this round: it carries the round number, the scope mode, the
   round-aware uncertainty rule, and the open component keys to reuse. Without it
   a reviewer assumes round 1 and reviews with full blocking authority.
3. Run the code-review gate as a fresh review pass. If the host supports
   subagents, launch one with `orchestration-code-reviewer.md`; otherwise apply
   that brief yourself without using the implementer's reasoning as evidence.
   Generate and pass the raw unified base-to-head git diff as the default and
   authoritative code input, alongside only the ticket, configured rules docs,
   stable repository map, and round brief. Do not pass a full-codebase index;
   additional source is allowed only for a named verification or regression.
   The reviewer must finish the full checklist, diff, and adversarial matrix even
   after finding a blocker, then return only concise structured review JSON.
   Explanations belong only to findings; each finding has a stable component key.
4. Inspect the PR diff against `security_required_when`. If any trigger matches,
   run a fresh security-review pass using `orchestration-security-reviewer.md`
   with the same raw unified diff and diff-isolated context.
   If nothing matches, record that the security gate was skipped because the diff
   has no configured security surface.
5. Record every completed gate through the ledger, blocking and advisory findings
   alike: `review-ledger.py record <pr> --gate code-review --result
   .orchestration/.review-results/code-review.json`. The validated JSON carries
   disposition, severity, regression, and explanation. The ledger increments strikes, auto-resolves components this gate
   no longer reports, demotes out-of-scope new findings in a frozen round, and
   returns `next_action`. Its `effective_verdict` governs, not the claimed one.
6. Act on `next_action`. Never merge while a blocking component is open.
   - `review` -- relay the reviewer's exact wording of each blocking finding to a
     fresh implementer to fix on the same branch, then re-run the failed gate.
     Relay by grep target or symbol, not by copying the reviewer's `file:line`
     (it drifts once the branch moves); label each finding as reviewer-reported
     so the implementer re-derives it rather than trusting it. Advisory findings
     go to the PR body, never into the implementer's brief -- widening the fix
     widens the next sweep. Then re-run each complete gate, not only the prior
     finding.
   - `redesign` -- the second failure in one component. Run
     `orchestration-design-reviewer.md` scoped to the named component against the
     root design and a revised adversarial matrix; no further implementation
     starts until it returns `VERDICT: PASS`, then record
     `review-ledger.py redesign <pr> --key <key> --verdict PASS`.
   - `escalate-human` -- the round cap is spent with blocking findings still open.
     STOP: do not merge and do not run another round. Give the user
     `review-ledger.py handoff <pr>` with the PR link. A component that survives
     this many strikes is usually underspecified acceptance criteria, not
     stubborn code.
   - `gates-clear` -- necessary, not sufficient. Confirm the security gate
     actually ran if the diff triggers it, then continue.
7. For each configured `verification:` entry whose `when:` applies to the target,
   run `run-verification.sh <name>`. A GREEN result file is required; RED or a
   missing result file blocks the merge.
8. Confirm every required configured target CI check is green.
9. Carry the ledger's advisory findings into the PR body as follow-ups, then
   record the all-green marker with `merge-guard.sh --record-green <pr>
   [result_file]`, then merge with `merge-on-green.sh <pr> <branch> all-green
   <verify_path>`. The merge script itself revalidates the active plugin version,
   exact PR head branch/sha, exact target base branch/sha, and marker freshness;
   never treat host hook registration as required evidence.

Report each gate verdict, the round number and scope mode, any blocking findings,
the advisory findings carried to the PR body, verification result paths, CI
status, and the merge result. CI-green alone is not the gate; the independent
verdicts are mandatory.
