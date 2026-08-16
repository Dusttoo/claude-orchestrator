---
description: Run the independent review gates (code-review, then security-review when warranted) on a PR and merge it on green.
argument-hint: <pr-number>
---

Gate PR #$ARGUMENTS. Read `.orchestration/config.yaml` and validate it before
mutation:

```bash
scripts/orchestration-engine.py validate-config
```

For `schema_version: 2`, use the configured transition plan for the target gate
or merge transition. Branch roles, evidence, approvals, CI categories, and
adapters come from:

```bash
scripts/orchestration-engine.py adapter-plan --host claude <transition>
```

For legacy configs, use the existing review gates below.

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

3. **Decision.**
   - Any `VERDICT: FAIL` -> maintain a cross-gate, cross-round failure ledger by
     `[component: ...]`, then relay the reviewer's exact wording of each blocking
     finding to a fresh implementer to fix on the same branch, then re-run the
     failed gate. Relay by grep target/symbol, not by copying the reviewer's
     `file:line` (it drifts on rebase); label each as reviewer-reported so the
     implementer re-derives it. The first failure in a component may receive a
     narrow fix. A second failure in the same component triggers the
     `orchestration-design-reviewer` and a revised adversarial matrix; do not
     authorize another code patch until the redesign passes. Re-run the complete
     gates after changes. Never merge on a FAIL.
   - All gates `PASS` AND every required `verification:` is GREEN AND the
     configured target CI checks green -> record the marker
     (`scripts/merge-guard.sh --record-green <pr> [result_file]`) and merge via
     `scripts/merge-on-green.sh`. The merge-guard hook blocks a direct merge with
     no recorded all-green marker and any merge target blocked by configuration.

Report each gate's verdict, the blocking findings (if any), and the merge result.
CI-green alone is NOT the gate -- the independent verdicts are mandatory.
