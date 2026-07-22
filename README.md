# claude-orchestrator

A reusable harness for running a small team of Claude Code agents against real
tickets, with **independent review and security gates** and a **mechanical
merge-guard**, so that parallel agent work does not degrade what lands on the
integration branch.

It is packaged as a Claude Code plugin. One orchestrator session dispatches
work; each ticket is implemented by one agent in an isolated git worktree, then
reviewed by a *separate* code-review agent and a *separate* security-review
agent that never see the implementer's reasoning, and only merged when every
gate is green. A `PreToolUse` hook physically blocks a merge that has not passed
the gates, so "never merge on red" is a mechanism rather than a request.

> Status: extraction in progress. This repository is being factored out of a
> harness that has been running in production against a multi-tenant SaaS
> codebase. Sections marked _(WIP)_ are not yet ported.

---

## Why this exists

Spinning up parallel coding agents naively degrades output, in four specific
ways:

1. **Shared state.** Agents working in the same checkout overwrite each other's
   branches and files.
2. **Thin context.** Each agent gets a one-line task and re-discovers the repo's
   rules badly, differently, every time.
3. **No independent scrutiny.** The agent that wrote the code also "reviews" it,
   so it confirms its own implementation instead of testing the requirement.
4. **No gates.** Work merges on vibes rather than on a green pipeline.

This harness addresses all four: isolated worktrees, fat per-ticket briefs,
separate review/security agents run against the diff with fresh context, and a
hard all-green gate, enforced by a hook, before anything lands.

## The model

```
                          +---------------------+
                          |   ORCHESTRATOR      |  (one Claude Code session)
                          |   - reads the ticket|
                          |   - dispatches work |
                          |   - owns the queue  |
                          +----------+----------+
                                     | spawns (worktree-isolated agents)
         +---------------+-----------+-----------+---------------+
         v               v           v           v               v
   +-----------+   +-----------+  +-----------+  (N implementers run concurrently,
   |Implementer|   |Implementer|  |Implementer|   each in its own git worktree +
   |  ticket a |   |  ticket b |  |  ticket c |    its own branch off integration)
   +-----+-----+   +-----+-----+  +-----+-----+
         | opens PR -> integration branch
         v
   +------------------------------------------+
   |            GATE PIPELINE (per PR)         |
   |  1. self-check: typecheck + build + test  |
   |  2. CODE REVIEW agent    (fresh context)  |
   |  3. SECURITY REVIEW agent (fresh context) |
   |  4. VISUAL QA agent       (optional)      |
   |  5. full end-to-end suite (optional)      |
   +--------------------+----------------------+
                        | ALL green?
              +---------+---------+
         yes  v                   v  any red
   +-------------------+   +----------------------+
   | ORCHESTRATOR      |   | loop the findings    |
   | merges to         |   | back to a fresh      |
   | integration,      |   | implementer, re-gate |
   | verifies + cleans |   | (never merge on red) |
   +-------------------+   +----------------------+
```

## Design principles

- **The author never reviews their own work.** Review and security agents run
  with fresh context against the diff only.
- **Enforcement is mechanical, not advisory.** The merge-guard is a hook that
  can veto the merge command; it does not rely on the agent choosing to comply.
- **Config over fork.** Repo-specific facts (branch model, gate commands, CI
  check names) live in a per-repo config file. The knowledge (conventions,
  gotchas) lives in the repo's own `CLAUDE.md` / `AGENTS.md`, which the gate
  agents read. The harness itself stays repo-agnostic.
- **Worktree isolation by default.** Parallel agents never share a checkout.

## Components

| Layer | What it is |
|---|---|
| `agents/` | Role briefs: implementer, code-reviewer, security-reviewer, visual-qa |
| `commands/` | `/orchestrate`, `/gate`, `/release`, `/orchestration-init` |
| `hooks/` | `PreToolUse` merge-guard + `Stop` worktree sweep |
| `scripts/` | The mechanics: gate runner, safe-merge, worktree lifecycle, guard |
| `skills/` | Relevance-triggered procedures (e.g. post-release back-merge) _(WIP)_ |
| `templates/` | The per-repo `config.yaml` and `ORCHESTRATION.md` to copy in |

## Configuration _(WIP)_

Per-repo mechanics live in `.orchestration/config.yaml`. See
[`templates/config.yaml`](templates/config.yaml).

## Installation _(WIP)_

## The merge-guard _(WIP)_

The threat model and the hook contract will be documented here: how a raw
`gh pr merge` is blocked unless an all-green marker exists whose SHA matches the
PR head, and why a direct merge to the production branch is always refused.

## Porting to a new repo _(WIP)_

## License

TBD.
