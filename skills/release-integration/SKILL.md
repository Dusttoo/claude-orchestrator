---
name: release-integration
description: Release the integration branch to production and immediately back-merge production into integration so release ancestry is preserved. Use when the user asks to release, ship integration to production, run a release PR, or run the equivalent of the Claude /release command.
---

# Release integration to production

Ship the integration branch to production and immediately record the required
back-merge. This treats the squash merge and back-merge as one indivisible flow,
so the next release PR does not inherit avoidable ancestry conflicts.

## Plugin paths

The scripts below live in this plugin, not necessarily in the target repository.
Resolve these paths from this skill file before executing them:

- `../../scripts/merge-on-green.sh`
- `../../scripts/merge-guard.sh`
- `../../scripts/run-verification.sh`

Execute scripts by absolute path while keeping the target repository as the
working directory.

## Procedure

1. Read `.orchestration/config.yaml` for `integration_branch`,
   `production_branch`, merge strategy, and production CI check names.
2. Open or locate the release PR from integration to production. If the user
   provides a PR number, use it; otherwise create one.
3. Wait for every configured production CI check to be green and confirm the PR
   head is the current integration tip. If integration moved after the release PR
   was cut, say which commits are not in this release.
4. Surface that the release PR is ready for the human-gated production squash
   merge. Do not bypass branch protection.
5. Immediately after the production squash lands, back-merge production into
   integration:
   ```bash
   git fetch origin <production> <integration>
   git checkout -b chore/back-merge-after-<pr> origin/<integration>
   git merge -s ours origin/<production> -m "chore: merge <production> back into <integration> after release #<pr>"
   git diff origin/<integration> --stat
   git merge-base --is-ancestor origin/<production> HEAD
   ```
6. Add one small real doc change in a second commit so paths-filtered CI has a
   file change to evaluate. Push, open a PR to integration, and merge it only
   after the required check posts green.
7. Verify `git merge-base --is-ancestor origin/<production>
   origin/<integration>` passes after the back-merge PR lands.

Report what shipped, the production PR, the back-merge PR, and the final ancestry
check result.
