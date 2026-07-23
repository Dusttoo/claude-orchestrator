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

The harness is **config-driven**: every repo-specific fact (branch model, gate
commands, CI check names) lives in one small config file, and the actual rules
being enforced live in the target repo's own `CLAUDE.md` / `AGENTS.md`. The
plugin itself carries no project knowledge, so the same harness ports across
codebases.

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
   +---------------------------------------------+
   |            GATE PIPELINE (per PR)            |
   |  1. self-check   typecheck + build + test   |
   |  2. code review  (fresh agent, diff only)   |
   |  3. security     (fresh agent, when warranted)|
   |  4. verify       (e2e / visual-qa, optional)|
   +---------------------+-----------------------+
                         | ALL green?
              +----------+----------+
         yes  v                     v  any red
   +-------------------+   +----------------------+
   | record marker +   |   | loop the findings    |
   | merge to          |   | back to a fresh      |
   | integration,      |   | implementer, re-gate |
   | verify + clean up |   | (never merge on red) |
   +-------------------+   +----------------------+
```

## Design principles

- **The author never reviews their own work.** Review and security agents run
  with fresh context against the diff only.
- **Enforcement is mechanical, not advisory.** The merge-guard is a hook that
  can veto the merge command; it does not rely on the agent choosing to comply.
  See [docs/merge-guard.md](docs/merge-guard.md).
- **Config over fork.** Repo-specific facts live in a per-repo config file. The
  knowledge (conventions, gotchas) lives in the repo's own `CLAUDE.md` /
  `AGENTS.md`, which the gate agents read. The harness stays repo-agnostic.
- **Worktree isolation by default.** Parallel agents never share a checkout, and
  a `Stop` hook sweeps finished ones while preserving any with unsaved work.

## Components

| Layer | What it is |
|---|---|
| `agents/` | Role briefs: implementer, code-reviewer, security-reviewer, visual-qa |
| `commands/` | `/orchestrate`, `/gate`, `/release`, `/orchestration-init` |
| `hooks/` | `PreToolUse` merge-guard + `Stop` worktree sweep |
| `scripts/` | The mechanics: config reader, gate runner, merge-guard, safe-merge, worktree lifecycle, verification |
| `skills/` | Relevance-triggered procedures: `orchestrate-ticket`, `scope-ticket`, `recover-agent-work` |
| `templates/` | The per-repo `config.yaml` and `ORCHESTRATION.md` to copy in |
| `tests/` | Shell test suites for the scripts (`bash tests/run.sh`) |

## Configuration

Per-repo mechanics live in `.orchestration/config.yaml`
([template](templates/config.yaml)). The key blocks:

| Key | Purpose |
|---|---|
| `integration_branch` / `production_branch` | the branch model |
| `merge_to_integration` / `merge_to_production` | `merge` or `squash` per target |
| `ci_checks_integration` / `ci_checks_production` | exact GitHub check-run names that define "CI green" |
| `self_check` | named shell checks run before review (typecheck, build, test, plus any repo convention) |
| `verification` | opt-in heavy suites (e.g. e2e), each gated to a target via `when:` |
| `gates` | which review roles run (`code-review`, `security-review`) |
| `security_required_when` | diff triggers that make the security gate mandatory |
| `concurrency_max` | how many verification chains run at once |
| `rules_docs` | the docs every gate agent reads (`CLAUDE.md`, `AGENTS.md`) |

The config parser is pure bash (no YAML dependency); the scripts read only the
mechanics, the agents read the file semantically.

## Installation

The plugin is a marketplace-installable Claude Code plugin. In an interactive
Claude Code session:

```
/plugin marketplace add Dusttoo/claude-orchestrator
/plugin install claude-orchestrator@builtbydusty
```

Installing it activates the agents, commands, and hooks (the merge-guard and the
worktree sweep). Then, from inside a target repo, scaffold the per-repo wiring:

```
/orchestration-init
```

That detects the branch model and CI checks, writes `.orchestration/config.yaml`
for you to review, copies in `ORCHESTRATION.md`, confirms a `CLAUDE.md` exists
(the harness provides discipline; `CLAUDE.md` provides the repo's knowledge),
and gitignores the runtime marker directory.

Exact `/plugin` syntax can vary by Claude Code version; if your version wires
plugins differently, `/orchestration-init` still scaffolds the config and can
add the hooks to `.claude/settings.json` directly.

## Running a ticket

```
/orchestrate <ticket-id or description>
```

drives one unit of work through the whole pipeline: implement (worktree, TDD) ->
code review (fresh agent) -> security review (when the diff warrants) -> optional
verification -> record the all-green marker -> merge -> verify it landed. Any
gate returning `VERDICT: FAIL` loops back to a fresh implementer with the
findings; nothing merges red.

The slash command is the explicit, deterministic entry point. Natural language
works too: asking to "orchestrate BL-90" or "run this ticket through the
pipeline" triggers the `orchestrate-ticket` skill, which runs the same flow. Use
the slash command when you want to be explicit; use plain English when you don't
want to remember the syntax.

`/gate <pr>` runs just the review gates on an existing PR. `/release` ships the
integration branch to production and performs the required back-merge as one
indivisible flow.

## The merge-guard

The enforcement centerpiece. A raw `gh pr merge` is blocked unless a recorded
all-green marker exists whose SHA matches the PR head and is within a freshness
window, and any direct merge to the production branch (or any `--squash`) is
always blocked. It fails closed: if it cannot precisely parse the command, it
still blocks anything resembling a merge rather than disabling itself.

Full threat model, hook contract, and the marker lifecycle:
[docs/merge-guard.md](docs/merge-guard.md).

## Testing

The scripts have shell test suites covering the config parser, the merge-guard
(every gate path, including the fail-closed fallback), the safe-merge guard
rails, the verification handshake, and the worktree lifecycle (the destructive
paths, on real git worktrees).

```
bash tests/run.sh
```

## Porting to a new repo

The harness is designed to move across codebases with only a config change. See
[docs/porting.md](docs/porting.md) for the checklist and what stays repo-side.

## License

MIT. See [LICENSE](LICENSE).
