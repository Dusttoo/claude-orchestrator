# Orchestration pipeline

The process this harness runs. The specifics (branch roles, state graph, CI
checks/categories, commands, approvals, adapters, ticket system) come from
`.orchestration/config.yaml`; the rules being enforced come from this repo's
`CLAUDE.md` / `AGENTS.md`. This file is process guidance, not project knowledge.

## The unit of work

**One ticket = one agent = one worktree = one branch = one PR.** Agents never
build on each other's unmerged branches. Branch source and destination roles are
resolved from config. In legacy configs, feature branches are cut from the
legacy configured source branch and target the legacy configured target branch.
No stacked PRs unless the configured workflow explicitly allows them.

## The pipeline

```
implement -> code-review -> security-review -> verify -> merge-on-green
(worktree)  (fresh agent)   (fresh agent)      (opt.)   (CI green + marker)
```

1. **Implement** (`orchestration-implementer`). Isolated worktree. TDD
   red-green. Runs the repo's pre-commit self-checks. Opens a PR to the
   configured target branch. Returns a structured report ending in a click-path.

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
   is GREEN, **and** the configured target CI checks are green. The orchestrator
   records the marker (`merge-guard.sh --record-green`, validated against a
   result file when one exists), then merges via `merge-on-green.sh`. The
   merge-guard hook mechanically blocks a direct `gh pr merge` that has no
   recorded all-green marker, and blocks any merge target or strategy blocked by
   configuration.

## The non-negotiables (why this beats "just run CI")

- **CI-green is necessary, not sufficient.** Independent reviews are mandatory.
  CI doesn't catch cross-surface inconsistency, privacy leaks, or a test that
  only mirrors the implementation. The reviewer is a *different* agent than the
  author, on purpose.
- **A finding is reproduced, not trusted.** "tsc clean / tests green" from the
  author is a claim; the gate re-runs it. A "stale" or flaky test is treated as
  a real signal until proven otherwise -- it has more than once been a real bug.
- **The orchestrator is a lossy relay.** Summarizing agent reports into briefs
  into docs drops the uncertainty marker at each hop. So: never put a `file:line`
  or an unrun code snippet in a brief -- hand over a grep target and let the
  implementer write and verify the code. Label every relayed claim (`verified now`
  / `reported, unverified`). Re-derive any fact before it enters a durable artifact
  (CLAUDE.md, ticket, PR body), and treat anything learned before a merge you
  performed as stale. Two agents agreeing is verification only if the second named
  the evidence it looked at, not the first agent's report.
- **The VERDICT contract.** Every gate agent ends with a literal
  `VERDICT: PASS` / `VERDICT: FAIL` last line so the orchestrator can branch
  deterministically.
- **Mechanical enforcement, not just discipline.** The merge-guard hook + branch
  protection make "never merge on red" a mechanism, not a good intention.
- **Worktree isolation + auto-cleanup.** Each agent gets its own git worktree.
  The Stop hook sweeps finished (unlocked + clean) agent worktrees so the fleet
  never bloats; it preserves dirty worktrees so a rate-limited / dead-mid-edit
  agent's work survives for recovery.

## Release And Candidate Workflows

If this repository uses `schema_version: 2`, release and candidate behavior is a
configured state machine. Before any release mutation, run:

```bash
scripts/orchestration-engine.py validate-config
scripts/orchestration-engine.py adapter-plan --host claude <transition>
```

The plan declares required evidence, CI categories, approval classes, branch
roles, candidate identity, artifact identity, tags, environment roles,
reconciliation, and cleanup. Execute the transition only through
`scripts/orchestration-engine.py transition ...`.

If this repository still uses the legacy schema, continue following the
repository's existing release process. Legacy configs do not automatically adopt
release-candidate states.

## Recovery: a dead agent's work is not lost

A background agent that dies (rate-limit, crash) leaves its work UNCOMMITTED in
its dirty worktree; the Stop-hook sweep skips dirty worktrees, so it survives.
Read its transcript for the verdict, `git -C <worktree> status` for the work,
then commit FROM the worktree path and open the PR yourself.
