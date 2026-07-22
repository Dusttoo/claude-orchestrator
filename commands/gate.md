---
description: Run the independent review gates (code-review, then security-review when warranted) on a PR and merge it on green.
argument-hint: <pr-number>
---

Gate PR #$ARGUMENTS. Read `.orchestration/config.yaml` for the gate sequence,
CI check names, and merge strategy.

1. **Code review.** Launch the `orchestration-code-reviewer` agent (a FRESH
   agent, no implementer context) on the PR. It must re-derive correctness, run
   the repo's review skill + self-checks, and end with `VERDICT: PASS` / `FAIL`.

2. **Security review.** Inspect the PR diff. If it touches any
   `security_required_when` trigger (auth, data isolation, migrations, payments,
   webhooks...), launch the `orchestration-security-reviewer` agent (another
   fresh agent). It must end with `VERDICT: PASS` / `FAIL`. If the diff has no
   security surface, note that and skip.

3. **Decision.**
   - Any `VERDICT: FAIL` -> relay the exact blocking findings to a fresh
     implementer to fix on the same branch, then re-run the failed gate. Never
     merge on a FAIL.
   - All gates `PASS` AND every required `verification:` is GREEN AND the
     integration CI checks green -> record the marker
     (`scripts/merge-guard.sh --record-green <pr> [result_file]`) and merge via
     `scripts/merge-on-green.sh`. The merge-guard hook blocks a direct merge with
     no recorded all-green marker and any direct merge to the production branch.

Report each gate's verdict, the blocking findings (if any), and the merge result.
CI-green alone is NOT the gate -- the independent verdicts are mandatory.
