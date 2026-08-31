---
description: Report orchestration cost and performance from the usage ledger, and say what to change.
argument-hint: [window] [group-by]
---

Report orchestration usage for $ARGUMENTS. Treat the first argument as the time
window (`30m`, `24h`, `7d`, `2w`, or an ISO 8601 timestamp) and the second as the
grouping field (`role`, `model`, `provider`, `ticket`, `sprint`, `run_id`,
`day`). Default to a `7d` window grouped by `role` when an argument is missing.

1. **Read the ledger.**

   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/api_agent.py report --group-by <field> --since <window>
   ```

   The ledger belongs to the repository, not to a worktree, so this covers every
   concurrent lane. Show the table before interpreting it.

2. **Interpret it. Numbers are not the deliverable; the decision is.** Work
   through these in order and only report what the data actually supports:

   - **`cache_hit` far below its peers.** A role well under the others is
     rebuilding its stable prefix each launch rather than reusing it. Check
     whether that role's `--role-file`, `--rules-file`, and `--repo-map` inputs
     are stable between runs. This is usually the single largest recoverable
     cost.
   - **`cost_per_request_usd` high while `cache_hit` is already good.** The
     prefix is fine and the model is the cost. This is a tiering candidate: move
     the role to a cheaper `llm.roles.<role>.model` and keep the expensive model
     on the gates that decide merges.
   - **`p95_ms` far above `p50_ms`.** Tail latency, not average cost. Correlate
     with `time blocked on provider rate limits`; sustained waiting means the
     lane count has outgrown the provider tier, not that the model is slow.
   - **`budget_blocked` in run outcomes.** Either the ceiling is too low for the
     work or the role is too expensive for the ceiling. Decide which before
     raising `max_usd_per_ticket` -- a raised limit hides the cost, it does not
     reduce it.
   - **`invalid_output` in run outcomes.** A reviewer model is failing the strict
     JSON contract. Check whether that deployment supports structured output; if
     it does not, it needs the repeated end-of-prompt contract instead of a
     cheaper model.
   - **Open reservations.** Spend totals are incomplete until they are settled.
     Say so rather than quoting the total as final, and point at
     `${CLAUDE_PLUGIN_ROOT}/scripts/api_agent.py reconcile`.

3. **Drill in where step 2 found something.** Re-run grouped by `ticket` to find
   which work is expensive, by `day` for a trend, by `model` to compare routes,
   or with `--role <role>` to isolate one lane. Use `--format json` when the
   result feeds a checkpoint or dashboard rather than a human.

   For throughput and repair quality, run
   `${CLAUDE_PLUGIN_ROOT}/scripts/review-ledger.py metrics <pr>` for each
   applicable durable PR ledger. Include first-attempt/cumulative closure, no-op
   repairs, design/review rounds, and repair-review elapsed time beside the
   role/model usage data.

4. **Propose concrete edits.** Recommendations must name the config key and the
   value, in `.orchestration/config.yaml` -- a role's `model`, `effort`, or a
   `budgets` ceiling. Do not edit the config as part of reporting; show the
   change and let the user decide.

**State the coverage limit whenever you report.** The ledger records API-routed
roles only. Roles left on `execution: desktop` run through the Claude Code or
Codex CLI and never reach this ledger, so subscription usage is absent from
these totals. Never present the report as total orchestration spend.
