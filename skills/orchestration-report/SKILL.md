---
name: orchestration-report
description: Report orchestration cost and performance from the API usage ledger and recommend concrete config changes. Use when the user asks how much orchestration is costing, where tokens or money are going, which role or ticket is expensive, whether prompt caching is working, why runs are slow or rate-limited, or for usage insights over a time window, including the equivalent of the Claude /orchestration-report command.
---

# Report orchestration usage and performance

Turn the durable usage ledger into decisions. Numbers are not the deliverable;
the recommendation is.

## Plugin paths

The scripts below live in this plugin. Resolve them from the plugin root, not
from the target repository. In Claude Code that root is `${CLAUDE_PLUGIN_ROOT}`;
in Codex use the installed plugin directory. Paths here are written relative to
this skill file.

## 1. Read the ledger

```bash
../../scripts/api_agent.py report --group-by role --since 7d
```

`--group-by` accepts `role`, `model`, `provider`, `ticket`, `sprint`, `run_id`,
or `day`. `--since` and `--until` accept `30m`, `24h`, `7d`, `2w`, or an ISO 8601
timestamp; a timestamp without a zone is read as UTC. `--role`, `--model`,
`--provider`, `--ticket`, and `--sprint` narrow the window before grouping.
`--top N` keeps the N costliest groups. `--format json` emits the same report for
a checkpoint or dashboard.

The ledger is repository-wide rather than per-worktree, so one report covers
every concurrent lane. Show the table before interpreting it.

## 2. Interpret it

Work through these in order and report only what the data supports:

- **`cache_hit` far below its peers.** That role is rebuilding its stable prefix
  each launch instead of reusing it. Check whether its `--role-file`,
  `--rules-file`, and `--repo-map` inputs are stable between runs. Usually the
  largest recoverable cost in the report.
- **`cost_per_request_usd` high while `cache_hit` is already good.** The prefix
  is fine; the model is the cost. Tiering candidate: move the role to a cheaper
  `llm.roles.<role>.model` and keep the expensive model on merge-deciding gates.
- **`p95_ms` far above `p50_ms`.** A tail-latency problem, not a cost one.
  Correlate with `time blocked on provider rate limits`: sustained waiting means
  the lane count has outgrown the provider tier.
- **`budget_blocked` run outcomes.** Either the ceiling is too low for the work
  or the role is too expensive for the ceiling. Decide which before raising
  `max_usd_per_ticket`; a raised limit hides cost rather than reducing it.
- **`invalid_output` run outcomes.** A reviewer model is failing the strict JSON
  contract. Check whether that deployment supports structured output before
  concluding the model is wrong for the role.
- **Open reservations.** Totals are incomplete until settled. Say so instead of
  quoting the total as final, and point at `../../scripts/api_agent.py reconcile`.

## 3. Drill in

Re-run grouped by `ticket` to find which work is expensive, by `day` for a
trend, by `model` to compare routes, or with `--role <role>` to isolate a lane.

## 4. Recommend

Name the config key and the value in `.orchestration/config.yaml`: a role's
`model`, `effort`, or a `budgets` ceiling. Do not edit the config as part of
reporting. Show the change and let the user decide.

## Coverage limit

State this whenever reporting. The ledger records API-routed roles only. Roles
left on `execution: desktop` run through the Claude Code or Codex CLI and never
reach this ledger, so subscription usage is absent from these totals. Never
present the report as total orchestration spend.
