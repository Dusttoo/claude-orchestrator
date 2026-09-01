# LLM execution routing

The orchestration policy chooses where each agent runs independently of its
workflow role. Existing repositories default to desktop execution. API use is
opt-in and requires an explicit provider and model.

Repository-specific API credentials belong in the gitignored
`.orchestration/.env` file next to `config.yaml`. Container environment variables
override values from that file.

```yaml
llm:
  execution: desktop
  provider: anthropic
  model: ""
  effort: ""
  fallback: none
  roles:
    implementer:
      execution: api
      provider: anthropic
      model: claude-sonnet-5
      effort: high
      fallback: desktop
    code-reviewer:
      execution: api
      provider: openai
      model: gpt-5.6-sol
      effort: medium
    security-reviewer:
      execution: desktop
    sprint-worker:
      execution: api
      provider: azure_adm
      model: grok-4-6-eval
    design-reviewer:
      execution: api
      provider: bedrock
      model: global.anthropic.claude-opus-5
      effort: high
```

Role overrides inherit each omitted field from `llm`. Built-in role names are
`design-reviewer`, `implementer`, `code-reviewer`, `security-reviewer`,
`visual-qa`, and `sprint-worker`; future adapters may define additional names.

Resolve a route before every role launch:

```text
scripts/context_pipeline.py route --config .orchestration/config.yaml \
  --role code-reviewer
```

- `execution: desktop` launches the existing native Claude Code or Codex agent.
- `execution: api` builds the provider request with `context_pipeline.py payload
  --config ... --role ...`, then submits it through `api_agent.py run`. The
  runner owns the constrained tool-call loop, usage ledger, and budget stops.
- `provider: azure_adm` uses Azure's OpenAI-compatible Chat Completions endpoint.
  Set `AZURE_ADM_API_KEY` and the resource-specific `AZURE_ADM_BASE_URL`; route
  `model` values to Azure deployment names.
- `provider: bedrock` uses the native Converse API and the normal AWS credential
  chain. On EC2, use an instance role; do not put AWS access keys or profiles in
  `.orchestration/.env`. Set `AWS_REGION` in the host environment and use a
  global or geographic inference-profile ID as `model`. Install
  `requirements-bedrock.txt` in the controller's Python environment.
- Azure Direct Model review payloads repeat the strict JSON-only result contract
  as both a system instruction and the final user instruction. This supports
  deployments such as MAI-Thinking that do not enable structured
  `response_format`; the runner still validates the object and fails closed on
  surrounding prose.
- Prompt caching is unconditional. Every payload the pipeline emits carries a
  breakpoint on its stable prefix, and Anthropic tool loops roll a single
  further breakpoint to the end of the conversation each round so the
  accumulated transcript is not re-billed at full input price. No execution
  mode can turn caching off.
- Bedrock Claude routes use native one-hour cache points on stable tools,
  instructions, and the rolling tool transcript. The usage ledger records
  Bedrock's uncached, cache-write, cache-read, and output token fields separately.
- Batch jobs use the same resolved provider/model route before
  `sprint-controller.py prepare-batch` persists the provider-native request.

## Failover invariant

`fallback: desktop` is allowed only when the API adapter proves no provider
accepted the request and no provider/run id exists. A timeout after submission,
an unknown response, a pending batch marker, or any durable API id must remain
reserved for reconciliation. Never launch a desktop copy of uncertain API work.

API credentials never belong in `.orchestration/config.yaml`. Adapters read them
from provider environment variables or an external secret manager.

The desktop app can remain the interactive controller for either route. API
execution is a child worker process, not a requirement to operate the workflow
manually in a terminal. See [API agent runner](api-agent.md) for tool policies,
budget configuration, usage reporting, and uncertain-request reconciliation.
