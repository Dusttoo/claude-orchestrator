---
description: Run the independent review gates (code-review, then security-review when warranted) on a PR and merge it on green.
argument-hint: <pr-number>
---

Gate PR #$ARGUMENTS. Read `.orchestration/config.yaml` and validate it before
mutation:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/orchestration-engine.py validate-config
```

For `schema_version: 2`, use the configured transition plan for the target gate
or merge transition. Branch roles, evidence, approvals, CI categories, and
adapters come from:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/orchestration-engine.py adapter-plan --host claude <transition>
```

For legacy configs, use the existing review gates below.

0. **Open the ledger and build the round brief.** The failure ledger lives on
   disk, not in this conversation -- it survives compaction, normalizes component
   keys so a repeated defect actually accumulates strikes, and decides when the
   loop stops:

   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/review-ledger.py open <pr>
   ${CLAUDE_PLUGIN_ROOT}/scripts/review-ledger.py brief <pr>
   ```

   Paste the `brief` output verbatim into every reviewer's brief this round. It
   carries the round number, the scope mode, the round-aware uncertainty rule,
   and the open component keys the reviewer must reuse. Without it a reviewer
   assumes round 1 and reviews with full blocking authority.

1. **Code review.** Launch the `orchestration-code-reviewer` agent (a FRESH
   agent, no implementer context) on the PR. It must re-derive correctness, run
   the repo's review skill + self-checks, finish the full checklist/diff/adversarial
   matrix even after finding a blocker, batch all findings with stable
   `[component: ...]` keys, and end with `VERDICT: PASS` / `FAIL`.

2. **Security review.** Inspect the PR diff. If it touches any
   `security_required_when` trigger (auth, data isolation, migrations, payments,
   webhooks...), launch the `orchestration-security-reviewer` agent (another
   fresh agent). It must end with `VERDICT: PASS` / `FAIL`. If the diff has no
   security surface, note that and skip.

3. **Record the round.** Record every completed gate through the ledger, blocking
   and advisory findings alike:

   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/review-ledger.py record <pr> --gate code-review \
     --verdict FAIL --blocking "src/auth/session.ts:refreshToken" \
     --advisory "src/ui/Badge.tsx:Badge"
   ```

   Pass `--regression <key>` for any blocking finding the reviewer marked
   `REGRESSION`. The ledger increments strikes, auto-resolves components this gate
   no longer reports, demotes out-of-scope new findings in a frozen round, and
   returns `next_action`. Its `effective_verdict` governs, not the reviewer's
   claimed one.

4. **Act on `next_action`.**
   - `review` -> relay each blocking finding's exact wording to a fresh
     implementer to fix on the same branch, then re-run the failed gate. Relay by
     grep target/symbol, not by copying the reviewer's `file:line` (it drifts on
     rebase); label each as reviewer-reported so the implementer re-derives it.
     Advisory findings go to the PR body, never into the implementer's brief --
     widening the fix widens the next sweep.
   - `redesign` -> the second failure in one component. Launch the
     `orchestration-design-reviewer` scoped to the named component with a revised
     adversarial matrix; do not authorize another narrow patch. On its
     `VERDICT: PASS`, run `review-ledger.py redesign <pr> --key <key> --verdict PASS`.
   - `escalate-human` -> the round cap is spent with blocking findings still open.
     STOP: do not merge, do not run another round. Give the user
     `review-ledger.py handoff <pr>` with the PR link. A component that survives
     this many strikes is usually underspecified acceptance criteria, not
     stubborn code.
   - `gates-clear` -> necessary, not sufficient. Confirm the security gate
     actually ran if the diff triggers it, then continue below.

   Never merge while a blocking component is open.

5. **Merge on green.**
   - All gates `PASS` AND every required `verification:` is GREEN AND the
     configured target CI checks green -> carry the ledger's advisory findings
     into the PR body as follow-ups, record the marker
     (`${CLAUDE_PLUGIN_ROOT}/scripts/merge-guard.sh --record-green <pr> [result_file]`) and merge via
     `${CLAUDE_PLUGIN_ROOT}/scripts/merge-on-green.sh`. The script directly revalidates the active
     plugin version, exact PR head branch/sha, exact target base branch/sha, and
     marker freshness. The merge-guard hook is optional defense in depth.

Report each gate's verdict, the round number and scope mode, the blocking
findings (if any), the advisory findings carried to the PR body, and the merge
result. CI-green alone is NOT the gate -- the independent verdicts are mandatory.
