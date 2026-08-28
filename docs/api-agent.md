# API agent runner

`scripts/api_agent.py` is the execution adapter behind an `llm.execution: api`
route. Claude Code or Codex Desktop can remain the interactive controller: it
builds the role payload, launches this runner as a child process, reports its
state, and asks the user for any required approval. The worker model traffic is
billed to the configured Anthropic, OpenAI, or Azure Direct Model account rather
than the desktop agent surface.

## Run one role

Build the ordered payload and pipe it directly to the runner:

```text
scripts/context_pipeline.py payload \
  --config .orchestration/config.yaml --role code-reviewer \
  --role-file /plugin/agents/orchestration-code-reviewer.md \
  --rules-file AGENTS.md --repo-map .orchestration/repo-map.txt \
  --ticket .orchestration/ticket.json --diff .orchestration/review.diff \
  --mode code-review --execution gate \
| scripts/api_agent.py run --request - \
    --config .orchestration/config.yaml --role code-reviewer \
    --ticket PROJ-123 --run-id PROJ-123-code-review-1
```

Put repository-specific provider credentials in `.orchestration/.env`, beside
`config.yaml`:

```dotenv
ANTHROPIC_API_KEY=your-anthropic-key
OPENAI_API_KEY=your-openai-key
AZURE_ADM_API_KEY=your-azure-resource-key
AZURE_ADM_BASE_URL=https://your-resource.openai.azure.com/openai/v1
```

Only define the provider the repository uses. Optional custom endpoints are
`ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL`; `AZURE_ADM_BASE_URL` is required for
Azure Direct Models. The Azure `model` route value is the deployment name. The
runner parses this file as data; it does not execute shell syntax or expand
variables. Only the documented provider names are loaded. Variables already
supplied by a cloud container or host environment take precedence, making
platform secret injection the highest-priority source.

Always gitignore `.orchestration/.env`. Keys are never copied into
`config.yaml`, run state, logs, or the usage ledger.

The runner retries token-count calls and explicit provider rate-limit/overload
rejections up to `max_pre_ack_retries`, with bounded backoff. It never retries a
model submission timeout or another ambiguous transport/server outcome.

## Role-limited tools

Review roles receive only bounded file reads, exact-string search, raw Git diff,
Git status, and named repository checks. The implementer and sprint-worker roles
may additionally apply a text-only unified patch. No role receives an arbitrary
shell tool. `run_check` can invoke only commands already named in `self_check` or
`verification`; tool output, line reads, paths, rounds, and execution time are
bounded.

`llm.roles.<role>.allowed_tools` may narrow that role's built-in ceiling. It
cannot grant a reviewer write access or name an unknown tool.

The runner is intentionally text/code-only. Keep `visual-qa` on a desktop route
until a separately sandboxed image/browser adapter is configured; an API visual
role fails closed instead of pretending a text-only review inspected the UI.

## Hard budget enforcement

Before each model request, the runner calls the provider's input-token counter.
It atomically reserves the counted input at the most expensive configured input
rate plus the request's full output allowance. Reservations are included in
run, ticket, and sprint checks, preventing concurrent workers from racing past a
shared limit. A request that could exceed any configured ceiling is not sent.

After every response, actual uncached input, cache writes, cache reads, output,
and reasoning usage is recorded under `.orchestration/.llm-usage/usage.jsonl`.
Run `scripts/api_agent.py usage` for totals and open reservations. Model prices
are explicit configuration because guessing the price of an unknown or newly
released model would make a USD limit unsafe. Update and verify the model entry
whenever its provider pricing changes.

## Submission recovery

Run markers are written atomically under `.orchestration/.llm-runs/`. The runner
does not blindly retry a timeout or ambiguous server failure: the original
request may already be running and a retry could duplicate writes and billing.
Its worst-case reservation remains open and the state becomes
`needs_reconcile`.

Check the provider dashboard or API records, then reconcile with evidence:

```text
scripts/api_agent.py reconcile --run-id RUN_ID --outcome not-found \
  --evidence "provider request search and timestamp"
```

If the provider completed the request, use `--outcome completed`, its response
id, and the provider-reported token counts. This settles actual cost, but the
controller must still inspect the recovered result before advancing workflow
state. Never release an uncertain reservation merely to make budget available.
