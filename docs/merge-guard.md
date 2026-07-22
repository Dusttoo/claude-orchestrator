# The merge-guard

The merge-guard turns "never merge on red" from a discipline the orchestrator is
*asked* to follow into a mechanism it *cannot* bypass. It is a Claude Code
`PreToolUse` hook on the `Bash` tool: before any shell command runs, the guard
inspects it, and a merge that has not earned an all-green marker is vetoed before
it executes.

## Why a hook, not an instruction

An agent told "do not merge until the gates pass" will, eventually, merge before
the gates pass. Not from malice, from a wrong assumption: a check that returned
empty read as success, a branch protection that was not enforced, a race between
a background CI watcher and the merge command. When that happens at 3am in an
unattended run, an instruction provides no backstop.

A hook does. The guard sits between the model's intent and the command's
execution. It does not matter what the agent believes about the gate state; the
guard re-derives it from a marker file and the live PR head, and returns exit 2
(block) with a reason the model then reads. The reason is fed back, so the agent
learns *why* it was stopped and what to do instead, rather than silently failing.

## Hook contract

```
stdin  : the PreToolUse JSON (.tool_name, .tool_input.command)
exit 0 : ALLOW  (not a merge, or a valid green marker exists)
exit 2 : BLOCK  (stderr is fed back to the model as the reason)
```

The guard fires only on a literal `gh pr merge`. It parses the command's argv
shape (via `shlex`) so that a commit body, a `--body` string, or a shell comment
that merely *contains* the words "gh pr merge" is not mistaken for a merge. Only
a command whose first three tokens are `gh pr merge` is treated as one.

## What it blocks, and why

1. **A merge with no all-green marker.** The default. A marker is written only by
   `--record-green`, which the orchestrator calls after every gate passes and CI
   is green. No marker means the gates have not been recorded as passed.

2. **A merge whose marker SHA does not match the PR head.** A marker is stamped
   with the exact commit it was recorded against. If the branch moved since
   (a rebase, a new push), the marker is stale and the gates must be re-run
   against the new head. A moved branch invalidates earlier gate runs by
   definition.

3. **A merge whose marker is older than the freshness window**
   (`MERGE_GUARD_MAX_AGE_SECONDS`, default 3600). Even when the SHA still
   matches, an hours-old marker means CI state and the base branch have moved on;
   re-gate. This closes the gap where a marker is recorded, the run stalls for
   hours, then a merge fires against a world that has changed.

4. **Any direct merge to the production branch, and any `--squash`.** Releases go
   through the human-gated `/release` flow, never a direct agent merge. The
   production branch is sacred; the guard refuses to let an agent touch it
   directly regardless of markers.

## Recording a marker

```
merge-guard.sh --record-green <pr> [result_file]
```

The orchestrator calls this only after all gates PASS and CI is green. With no
`result_file`, it stamps a marker for the PR's current head. With a
`result_file` from `run-verification.sh`, it additionally requires that the file
is `GREEN` and its embedded SHA matches the PR head, so a marker cannot be
recorded unless the heavy verification actually ran on this exact commit. This
folds the strong, artifact-backed proof into the same mechanism that otherwise
trusts the orchestrator's assertion.

`--clear <pr>` drops a marker (e.g. after a rebase); `merge-on-green.sh` clears
the spent marker automatically after a successful merge.

## Fail-closed

The precise argv parse uses `python3`. If `python3` is unavailable, the guard
does **not** fall through to allowing the command, which would silently disable
the only enforcement point. Instead it uses a best-effort bash/grep detector and
blocks anything resembling `gh pr merge`. A security mechanism that disables
itself when a dependency is missing is worse than one that occasionally
over-blocks; the guard chooses over-blocking.

## Where it stops, and what complements it

The guard governs the `gh pr merge` command from an agent shell. It is one layer,
not the whole defense:

- **Branch protection** (applied out-of-band via `gh api`) closes the paths the
  hook cannot see: a direct push, a merge from the GitHub UI, a non-agent shell.
  The guard and branch protection are complementary; neither alone is complete.
- **The independent review gates** decide *whether* the work is correct; the
  guard only enforces that their verdict was recorded before a merge. CI-green is
  necessary but never sufficient, which is the whole reason the marker exists
  rather than the guard just polling CI.

## Test coverage

Every path above is covered in [tests/merge-guard.test.sh](../tests/merge-guard.test.sh):
a non-merge command passes through; a commit body mentioning the words passes
through; a no-marker merge blocks; a valid fresh marker allows; a moved-SHA
marker blocks; an expired marker blocks; production-branch and `--squash` always
block; a non-Bash tool is ignored; and the fail-closed fallback both blocks a
no-marker merge and allows a plain non-merge command with `python3` forced off.

Isolation seams for the tests: `MERGE_GUARD_STATUS_DIR` redirects the marker
directory, `MERGE_GUARD_PR_HEAD_SHA` stubs the live head-SHA lookup, and
`MERGE_GUARD_FORCE_FALLBACK` exercises the no-`python3` path.
