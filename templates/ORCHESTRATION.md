# Orchestration pipeline

The invariant process this harness runs. The specifics (branch names, CI checks,
commands, ticket system) come from `.orchestration/config.yaml`; the rules being
enforced come from this repo's `CLAUDE.md` / `AGENTS.md`. This file is the
process, not the knowledge.

## The unit of work

**One ticket = one agent = one worktree = one branch = one PR.** Agents never
build on each other's unmerged branches. Every feature branch is cut from the
latest integration branch and targets it directly. No stacked PRs (they silently
orphan work when the base merges first).

## The pipeline

```
implement -> code-review -> security-review -> verify -> merge-on-green
(worktree)  (fresh agent)   (fresh agent)      (opt.)   (CI green + marker)
```

1. **Implement** (`orchestration-implementer`). Isolated worktree. TDD
   red-green. Runs the repo's pre-commit self-checks. Opens a PR to the
   integration branch. Returns a structured report ending in a click-path.

2. **Code review** (`orchestration-code-reviewer`). A FRESH agent with no
   implementer context. Re-derives correctness from the ticket + diff, runs the
   repo's review skill, re-runs the self-checks itself, audits against the
   repo's standards. Ends with `VERDICT: PASS` or `VERDICT: FAIL`.

3. **Security review** (`orchestration-security-reviewer`). Another fresh agent.
   Runs only when the change touches a `security_required_when` trigger (auth,
   data isolation, migrations, payments...). Hunts for leaks / privilege
   escalation / isolation breaks. Ends with `VERDICT: PASS` / `FAIL`.

4. **Verify (optional).** For each `verification:` entry whose `when:` matches
   the target, `run-verification.sh <name>` runs the suite on the rebased branch
   and writes a sha-stamped result file (RED = no file = no merge). If the change
   has a user-visible surface, the `orchestration-visual-qa` agent captures the
   click-path headlessly and compares it against the acceptance criteria. Both
   end in a verdict the orchestrator branches on.

5. **Merge on green.** Only after every gate PASSES, every required verification
   is GREEN, **and** the integration CI checks are green. The orchestrator records
   the marker (`merge-guard.sh --record-green`, validated against a result file
   when one exists), then merges via `merge-on-green.sh`. The merge-guard hook
   mechanically blocks a direct `gh pr merge` that has no recorded all-green
   marker, and blocks any direct merge to the production branch.

## The non-negotiables (why this beats "just run CI")

- **CI-green is necessary, not sufficient.** Independent reviews are mandatory.
  CI doesn't catch cross-surface inconsistency, privacy leaks, or a test that
  only mirrors the implementation. The reviewer is a *different* agent than the
  author, on purpose.
- **A finding is reproduced, not trusted.** "tsc clean / tests green" from the
  author is a claim; the gate re-runs it. A "stale" or flaky test is treated as
  a real signal until proven otherwise -- it has more than once been a real bug.
- **The VERDICT contract.** Every gate agent ends with a literal
  `VERDICT: PASS` / `VERDICT: FAIL` last line so the orchestrator can branch
  deterministically.
- **Mechanical enforcement, not just discipline.** The merge-guard hook + branch
  protection make "never merge on red" a mechanism, not a good intention.
- **Worktree isolation + auto-cleanup.** Each agent gets its own git worktree.
  The Stop hook sweeps finished (unlocked + clean) agent worktrees so the fleet
  never bloats; it preserves dirty worktrees so a rate-limited / dead-mid-edit
  agent's work survives for recovery.

## Release

Releasing the integration branch to production is a deliberate step:

1. Open the release PR (integration -> production). It runs the full e2e suite
   (the canonical end-to-end checkpoint).
2. A human squash-merges it to production.
3. **Immediately back-merge production -> integration** (a `-s ours` merge that
   records production as an ancestor + one small doc-diff so the required check
   posts). Skipping this is invisible the same day and produces phantom
   conflicts on the *next* release. The `/orchestration:release` command does
   the squash and the back-merge as ONE flow so it can't be dropped.

## Recovery: a dead agent's work is not lost

A background agent that dies (rate-limit, crash) leaves its work UNCOMMITTED in
its dirty worktree; the Stop-hook sweep skips dirty worktrees, so it survives.
Read its transcript for the verdict, `git -C <worktree> status` for the work,
then commit FROM the worktree path and open the PR yourself.
