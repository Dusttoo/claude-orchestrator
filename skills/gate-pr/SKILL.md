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
- `../../agents/orchestration-implementer.md`
- `../../agents/orchestration-security-reviewer.md`
- `../../scripts/merge-guard.sh`
- `../../scripts/merge-on-green.sh`
- `../../scripts/orchestration-engine.py`
- `../../scripts/run-gates.sh`
- `../../scripts/run-verification.sh`

Execute scripts by absolute path while keeping the target repository as the
working directory.

## Procedure

1. Read `.orchestration/config.yaml` and run
   `orchestration-engine.py validate-config`. For `schema_version: 2`, use
   `orchestration-engine.py adapter-plan --host codex <transition>` for the
   configured gate or merge transition; branch roles, evidence, approvals, CI
   categories, and adapters come from that plan. For legacy configs, continue
   with the existing review gates below.
2. Run the code-review gate as a fresh review pass. If the host supports
   subagents, launch one with `orchestration-code-reviewer.md`; otherwise apply
   that brief yourself without using the implementer's reasoning as evidence.
   The output must end in `VERDICT: PASS` or `VERDICT: FAIL`.
3. Inspect the PR diff against `security_required_when`. If any trigger matches,
   run a fresh security-review pass using `orchestration-security-reviewer.md`.
   If nothing matches, record that the security gate was skipped because the diff
   has no configured security surface.
4. On any `VERDICT: FAIL`, do not merge. Relay the reviewer's exact wording of each
   blocking finding to a fresh implementer to fix on the same branch, then re-run
   the failed gate. Relay by grep target or symbol, not by copying the reviewer's
   `file:line` (it drifts once the branch moves); label each finding as
   reviewer-reported so the implementer re-derives it rather than trusting it.
5. For each configured `verification:` entry whose `when:` applies to the target,
   run `run-verification.sh <name>`. A GREEN result file is required; RED or a
   missing result file blocks the merge.
6. Confirm every required configured target CI check is green.
7. Record the all-green marker with `merge-guard.sh --record-green <pr>
   [result_file]`, then merge with `merge-on-green.sh <pr> <branch> all-green
   <verify_path>`.

Report each gate verdict, any blocking findings, verification result paths, CI
status, and the merge result. CI-green alone is not the gate; the independent
verdicts are mandatory.
