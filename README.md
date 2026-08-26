# claude-orchestrator

A reusable harness for running a small team of coding agents against real
tickets or a dependency-linked Jira sprint, with **independent review and
security gates** and a **mechanical merge-guard**, so that parallel agent work
does not degrade what lands on the configured target branch.

It is packaged as a Claude Code plugin and as a Codex plugin source tree. One
orchestrator session dispatches work; each ticket is implemented by one agent
role in an isolated git worktree, then reviewed by a *separate* code-review role
and a *separate* security-review role that never see the implementer's
reasoning, and only merged when every gate is green. The sanctioned merge script
validates the marker, plugin version, and exact PR head/base identity itself on
both Claude Code and Codex. A trusted `PreToolUse` hook can additionally block
raw merge commands, while branch protection remains the out-of-band layer.

The harness is **config-driven**: every repo-specific fact (branch model, state
graph, gate commands, CI categories, approvals, adapter seams) lives in one
small config file, and the actual engineering rules live in the target repo's
own `CLAUDE.md` / `AGENTS.md`. The plugin itself carries no project knowledge,
so the same harness ports across codebases.

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
an adversarial test matrix and security-sensitive design gate before code,
separate review/security agents run against the diff with fresh context, and a
hard all-green gate before anything lands. Claude Code and Codex share the same
marker and merge scripts; trusted hooks add local defense in depth when the host
supports them.

## The model

```
                          +---------------------+
                          |   ORCHESTRATOR      |  (one Claude Code/Codex session)
                          |   - reads the ticket|
                          |   - dispatches work |
                          |   - owns the queue  |
                          +----------+----------+
                                     | spawns (worktree-isolated agents)
         +---------------+-----------+-----------+---------------+
         v               v           v           v               v
   +-----------+   +-----------+  +-----------+  (N implementers run concurrently,
   |Implementer|   |Implementer|  |Implementer|   each in its own git worktree +
   |  ticket a |   |  ticket b |  |  ticket c |    and configured source branch)
   +-----+-----+   +-----+-----+  +-----+-----+
         | opens PR -> configured target branch
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
   | target branch,    |   | implementer, re-gate |
   | verify + clean up |   | (never merge on red) |
   +-------------------+   +----------------------+
```

## Design principles

- **The author never reviews their own work.** Review and security agents run
  with fresh context against the diff only.
- **Design before security-sensitive infrastructure.** A pre-code gate records
  trust boundaries, impossible guarantees, failure recovery, rejected fragile
  alternatives, and the adversarial tests that falsify its invariants.
- **Reviews batch the whole sweep.** Finding one blocker never ends a review;
  reviewers finish the diff, checklist, and matrix and report all findings once.
- **Two component failures trigger redesign.** A stable component-key ledger,
  persisted by `scripts/review-ledger.py`, prevents an endless sequence of narrow
  patches to the same broken design.
- **The review loop is bounded and converges.** Round 1 sweeps the whole diff
  with full blocking authority. From round 2 the sweep stays full-diff but only
  open ledger findings, regressions, and security issues may block, so the
  blocking set shrinks. Findings split into blocking and advisory, and
  `max_review_rounds` (default 3) ends the loop at a human rather than a
  merge. See [docs/review-loop.md](docs/review-loop.md).
- **Enforcement is mechanical, not advisory.** The sanctioned merge script
  refuses stale or mismatched evidence even without hooks. A trusted merge-guard
  hook can also veto raw merge commands.
  See [docs/merge-guard.md](docs/merge-guard.md).
- **Config over fork.** Repo-specific facts live in a per-repo config file. The
  knowledge (conventions, gotchas) lives in the repo's own `CLAUDE.md` /
  `AGENTS.md`, which the gate agents read. Workflow policy is declared in
  schema-versioned config and enforced by shared scripts, not by Claude/Codex
  adapter text.
- **Worktree isolation by default.** Parallel agents never share a checkout.
  Cleanup defaults to manual; repositories may opt into explicit post-merge and
  trusted-hook cleanup, which always preserves dirty or locked worktrees.
- **The orchestrator relays grep targets, not stale facts.** Summarizing agent
  reports into briefs into docs drops the uncertainty marker at each hop, so the
  orchestrator never hands down a `file:line` or an unrun snippet (both drift),
  labels every relayed claim's provenance, and re-derives any fact before it enters
  a durable artifact -- anything learned before a merge it performed is stale.

## Components

| Layer | What it is |
|---|---|
| `agents/` | Role briefs: design-reviewer, implementer, code-reviewer, security-reviewer, visual-qa |
| `commands/` | Claude Code slash commands: `/orchestrate`, `/orchestrate-sprint`, `/gate`, `/release`, `/orchestration-init` |
| `hooks/` | Claude Code/Codex `PreToolUse` merge-guard + `Stop` worktree sweep |
| `scripts/` | The mechanics: config reader, sprint controller, workflow engine, gate runner, merge-guard, safe-merge, worktree lifecycle, verification |
| `skills/` | Codex/Claude natural-language procedures: orchestrate ticket/sprint, gate, release, init, scope, recover |
| `templates/` | Configuration template plus the plugin-owned process reference |
| `tests/` | Plugin-owned conformance suites; never copied into target repos |
| `.codex-plugin/` | Codex plugin manifest exposing the `skills/` directory |

## Configuration

Per-repo mechanics live in `.orchestration/config.yaml`
([template](templates/config.yaml)). There are two compatibility levels:

- `schema_version: 1` or no schema version: the legacy ticket pipeline using
  `integration_branch`, `production_branch`, `self_check`, `verification`, and
  `ci_checks_*`. Existing installations stay here until they opt in.
- `schema_version: 2`: the configurable workflow engine using branch roles,
  states, transitions, transition-specific evidence, CI categories, approval
  classes, candidate/artifact identity, tags, environments, and adapters.

Legacy key blocks:

| Key | Purpose |
|---|---|
| `integration_branch` / `production_branch` | the branch model |
| `llm` / `llm.roles` | global desktop/API route, per-role provider/model/tool overrides, hard budgets, and explicit model pricing |
| `merge_to_integration` / `merge_to_production` | `merge` or `squash` per target |
| `ci_checks_integration` / `ci_checks_production` | exact GitHub check-run names that define "CI green" |
| `self_check` | named shell checks run before review (typecheck, build, test, plus any repo convention) |
| `verification` | opt-in heavy suites (e.g. e2e), each gated to a target via `when:` |
| `gates` | which review roles run (`code-review`, `security-review`) |
| `security_required_when` | diff triggers that make the security gate mandatory |
| `sprint_id` / `sprint_*` | configured Jira sprint, dependency/status mapping, and checkpoint location |
| `concurrency_max` | how many ticket workflows or verification chains run at once |
| `worktree_cleanup` | `manual` (safe default) or `auto` for clean, unlocked worktrees |
| `rules_docs` | the docs every gate agent reads (`CLAUDE.md`, `AGENTS.md`) |

The legacy scalar/list parser is pure bash. The schema v2 workflow is validated
by `scripts/orchestration-engine.py`, a shared engine used by both Claude and
Codex adapters. See [docs/workflow-configuration.md](docs/workflow-configuration.md).
Desktop/API selection and inherited per-role overrides are documented in
[docs/llm-routing.md](docs/llm-routing.md). The constrained provider runner,
usage ledger, and recovery procedure are documented in
[docs/api-agent.md](docs/api-agent.md).

## Installation

### Claude Code

The plugin is a marketplace-installable Claude Code plugin. In an interactive
Claude Code session:

```
/plugin marketplace add Dusttoo/claude-orchestrator
/plugin install claude-orchestrator@builtbydusty
```

Installing it activates the agents and commands. Hosts that support plugin hooks
can additionally expose the merge guard and worktree sweep after trust review.
Then, from inside a target repo, scaffold the per-repo wiring:

```
/orchestration-init
```

That detects the branch model and CI checks, writes `.orchestration/config.yaml`
for you to review, confirms a `CLAUDE.md` exists
(the harness provides discipline; `CLAUDE.md` provides the repo's knowledge),
and gitignores the runtime marker directory.

Exact `/plugin` syntax can vary by Claude Code version. Regardless of hook
support, `/orchestration-init` scaffolds the same configuration and the
sanctioned scripts enforce merges and cleanup directly.

### Codex

This repository is also a Codex plugin folder via
[`.codex-plugin/plugin.json`](.codex-plugin/plugin.json). Codex installs plugins
from marketplace roots, so local development typically means cloning or
symlinking this repo to `~/plugins/claude-orchestrator`, ensuring the personal
marketplace entry points at `./plugins/claude-orchestrator`, then running:

```
codex plugin add claude-orchestrator@personal
```

Codex users invoke the same flows in natural language: "orchestrate BL-90 end to
end", "orchestrate the active sprint", "gate PR 123", "advance the configured
release transition", or "bootstrap orchestration in this repo". See
[docs/codex.md](docs/codex.md) for the full marketplace layout and hook trust
notes.

## Running a ticket

```
/orchestrate <ticket-id or description>
```

drives one unit of work through the whole pipeline: scope/adversarial matrix ->
design gate for security-sensitive infrastructure -> implement (worktree, TDD) ->
code review (fresh agent) -> security review (when the diff warrants) -> optional
verification -> record the all-green marker -> merge -> verify it landed. Any
gate returning a structured FAIL result loops back with a complete batch of findings;
the second failure in one component forces redesign instead of another narrow
patch. Nothing merges red.

The slash command is the explicit, deterministic entry point. Natural language
works too: asking to "orchestrate BL-90" or "run this ticket through the
pipeline" triggers the `orchestrate-ticket` skill, which runs the same flow. Use
the slash command when you want to be explicit; use plain English when you don't
want to remember the syntax.

Code and security gates use concise structured results: passing checks carry no
explanation, while actual findings carry the actionable detail. See
[reviewer output](docs/reviewer-output.md).

For API execution, place repository-specific provider keys in the gitignored
`.orchestration/.env` beside `config.yaml`. Cloud/container environment variables
with the same names take precedence. See [API agent runner](docs/api-agent.md).

`/gate <pr>` runs just the review gates on an existing PR. `/release` advances a
configured release or candidate transition through the shared workflow engine.
Schema v1 repositories keep their legacy process until they migrate.

## Running a sprint

`/orchestrate-sprint [sprint]` in Claude Code, or “orchestrate the configured
sprint” in Codex, queries the configured Jira project/sprint and launches the
same one-ticket pipeline for every unblocked ticket. A plugin-owned controller
normalizes dependencies, orders ready tickets by optional per-ticket priority
then key, atomically enforces `concurrency_max`, and checkpoints
running and terminal work under `.orchestration/.sprint-state/`. Restarts
reconcile existing worker references before dispatch, blocked tickets do not
stop independent lanes, and the final report separates completed, blocked, and
user-action items. Repositories provide configuration and project-specific
acceptance criteria; they do not vendor the controller or its tests.
See [docs/sprint-controller.md](docs/sprint-controller.md) for the trust boundary,
impossible guarantees, restart invariant, and rejected fragile designs.

## The merge-guard

The enforcement centerpiece. A raw `gh pr merge` is blocked unless a recorded
all-green marker exists whose SHA matches the PR head and is within a freshness
window. Configured guard policy can also block selected target branch roles or
direct squash merges. It fails closed: if it cannot precisely parse the command
or resolve policy, it blocks anything resembling a merge rather than disabling
itself.

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

The reusable merge-guard, merge-on-green, worktree-cleanup, and host-parity
suites are plugin conformance tests. Initialization can run them from any target repo with
`<plugin>/scripts/run-plugin-conformance.sh`; it does not install test or script
copies. Target repositories supply configuration, their rules and acceptance
criteria, and only tests specific to their own behavior.

## Porting to a new repo

The harness is designed to move across codebases with only a config change. See
[docs/porting.md](docs/porting.md) for the checklist and
[docs/workflow-configuration.md](docs/workflow-configuration.md) for schema v2
workflow design.

## Releasing a change (every PR bumps the version)

**Every PR MUST bump the version** in BOTH manifests, including PRs limited to
documentation, tests, scripts, prompts, or repository tooling. Keep the bump in
the same PR as the change:

- [`.claude-plugin/plugin.json`](.claude-plugin/plugin.json) (`version`)
- [`.codex-plugin/plugin.json`](.codex-plugin/plugin.json) (`version`) -- keep the
  two in lockstep

Use semver: patch for prompt/doc/script fixes, minor for new commands/skills/agents
or behavior changes, major for breaking config or contract changes.

Why this is non-optional: an installed plugin is a **pinned snapshot**, not a live
checkout of this repo (Claude Code caches it under
`~/.claude/plugins/cache/<marketplace>/claude-orchestrator/<version>/`). Update
detection keys off the version string. Merging to `main` without bumping the
version means a user's next plugin update sees the same version and does nothing --
the fix never lands, silently. A merge is not a release; the version bump is.

After merging, users pick up the change by updating the plugin (`/plugin` ->
update the marketplace, then the plugin), not by starting a new session.

## License

MIT. See [LICENSE](LICENSE).
