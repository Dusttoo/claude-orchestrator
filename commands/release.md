---
description: Ship the integration branch to production AND do the required back-merge as one flow, so the back-merge can never be dropped.
argument-hint: [release-pr-number]
---

Release the integration branch to production. Read `.orchestration/config.yaml`
for `integration_branch`, `production_branch`, and the CI check names. This
command treats the squash-merge and the back-merge as ONE indivisible flow --
the back-merge being skipped is the classic post-release failure (it surfaces
days later as phantom conflicts on the next release).

1. **Open / locate the release PR** (`integration -> production`). If
   `$ARGUMENTS` is a PR number, use it; otherwise open one. It must run the full
   production CI checks (`ci_checks_production`, including the e2e suite). Wait
   for ALL of them green. Confirm the PR head == the current integration tip
   (no stale-head: a release PR whose head lags omits recent merges).

2. **Squash-merge to production.** This is the deliberate human-gated step:
   surface that the release PR is green and ready, and have the human squash it
   (production is typically admin-enforced). Do not bypass branch protection.

3. **Immediately back-merge production -> integration.** The instant the squash
   lands, run the merge-guard-safe back-merge:
   ```
   git fetch origin <production> <integration>
   git checkout -b chore/back-merge-after-<pr> origin/<integration>
   git merge -s ours origin/<production> -m "chore: merge <production> back into <integration> after release #<pr>"
   git diff origin/<integration> --stat        # MUST be empty (tree unchanged)
   git merge-base --is-ancestor origin/<production> HEAD && echo OK
   ```
   Then add ONE small real doc-diff (a release-note line in a tracked `.md`) in a
   second commit -- a zero-file PR never triggers a paths-filtered CI check and
   sits BLOCKED forever. Push, open a PR to the integration branch, merge it
   once the required check posts green.

   `-s ours` keeps the integration tree byte-for-byte and only records the
   ancestry. Safe because the integration branch is always a superset of the
   production squash (nothing lands on production outside release squashes).

4. **Verify.** `git merge-base --is-ancestor origin/<production> origin/<integration>`
   must pass after the back-merge merges. Report what shipped and confirm the
   ancestry is recorded so the next release PR is clean.

If hotfixes landed on the integration branch AFTER the release PR was cut, they
are NOT in the release -- they need their own release. Say so explicitly.
