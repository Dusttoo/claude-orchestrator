# Sprint controller design contract

The sprint controller is the plugin-owned state machine shared by Claude Code
and Codex. Host adapters query Jira and launch agents; the controller alone
decides which ticket may consume a lane and persists that decision before the
launch.

## Trust boundary

Repository configuration is trusted policy. Jira responses, ticket text,
dependency links, agent reports, and restart-era process state are untrusted
inputs. The controller validates Jira keys, stores text only as JSON data,
constrains checkpoint paths to the repository, and never executes ticket text.
Atomic replacement and a file lock coordinate controller processes using the
same checkpoint. The host must pass arguments without shell interpolation.

Plugin adapters own completeness at external boundaries. The Jira adapter
performs authenticated requests restricted to canonical `jira_base_url` policy,
rejects cross-origin redirects before credentials can follow, exhausts parent,
child, and external-dependency pagination, and stores only sanitized,
content-addressed responses. It constructs query policy from canonical
project/sprint configuration; inventory templates carry no authority. All
scheduler metadata, including sprint identity and dependencies, is derived from
those authenticated responses. `jira_sprint_field` names the REST field that
contains the provider's sprint id/name (often a Jira Cloud custom field). Provider batch
adapters perform authenticated terminal lookup and complete result download.
The controller rejects caller-authored receipts, unknown dependencies, and
duplicate keys rather than inventing those facts.

## Impossible guarantees

The controller cannot protect against a malicious same-UID process or
repository owner that edits checkpoints directly, and does not pretend a
locally available signing key creates authentication. It can require provider
I/O to pass through its adapter boundary and bind raw responses by digest. File
locks coordinate cooperating processes; they are not an authorization boundary.
GitHub branch protection and the plugin's merge guard remain the enforcement
boundary for merges.

Every launch is reserved first, and restart plans surface all running
reservations as `needs_reconcile`. `launch-local` starts the process itself and
records controller-owned evidence bound to the exact repository, sprint,
ticket, and attempt. `attach` consumes only that evidence; it never accepts a
caller-supplied PID. Linux `/proc` start ticks or the macOS kernel process start
time are fingerprinted so PID reuse cannot make a replacement authoritative.
`run_ref` remains display metadata. Native task labels and tasks without a
verified adapter require explicit operator recovery. Unknown inspection errors
also remain fenced; only confirmed absence permits automatic requeue.

## Context and provider efficiency

Before querying Jira, the adapter requests only fields consumed by scheduling
and passes that fixed allowlist as Jira's `fields` request parameter. Jira responses pass through
`sanitize-jira`, which allowlists those issue fields and removes rendered/edit
metadata, changelogs, schemas, and avatar links before ticket data reaches an
LLM.

Before launching, adapters resolve `llm` plus the requested `llm.roles` override
with `context_pipeline.py route`. Desktop routes use the native host agent; API
routes build requests with `context_pipeline.py payload --config ... --role
...` and foreground jobs execute through `api_agent.py run`, which enforces role
tools and run/ticket/sprint budgets. Anthropic Messages and OpenAI Responses
payloads, plus Azure Direct Model Chat Completions payloads, share the same stable order:
role brief, repository rules, then the baseline repository map. Ticket data and
the raw active-branch diff remain in the uncached user message. Anthropic gate
and on-demand payloads place a provider-native explicit cache breakpoint at the
selected stable boundary. OpenAI payloads keep the same stable prefix order but
omit all optional cache-control request fields because supported fields differ
across live OpenAI-compatible routes. A compatible route may still apply its
own automatic caching without request metadata. Optional `--effort` maps to OpenAI reasoning effort
or Anthropic adaptive-thinking effort. Azure Direct Model routes omit the
provider-specific effort field for cross-model compatibility; fixed
`budget_tokens` is intentionally not assumed because support differs across
model generations.

Captain visibility is event-driven by default. The captain blocks on native
worker wait primitives (or the detached process PID on CLI hosts) instead of
spending model turns polling unchanged state. It reports launches, phase or gate
transitions, provider degradation, PR/CI transitions, terminal outcomes,
blockers, and user actions immediately. During an otherwise unchanged run it
emits at most the configured heartbeat; the durable sprint checkpoint remains
available for an on-demand summary at any time.

Non-interactive background lanes can be prepared with `prepare-batch`. The
controller accepts only current `plan.launch` tickets explicitly marked as
background and non-interactive, reserves them under the sprint lock, and writes
an Anthropic Message Batches JSON request or OpenAI Batch JSONL plus a durable
marker beneath the configured checkpoint directory. `submit-batch` invokes the
credential-owning adapter, which uploads or submits the immutable request and
records a content-addressed acceptance receipt. `reconcile-batch` invokes that
same adapter for terminal state and every native result/error page, freezes a
content-addressed normalized bundle, and applies each `custom_id` idempotently
before normal per-ticket `finish` calls. An ambiguous submission, nonterminal
status, or missing terminal row leaves only the unresolved reservations fenced;
successful rows are settled and provider-proven nonexecuted rows are released.

Batch credentials are bound to the providers' built-in API origins. A custom
gateway must be approved in the canonical
`.orchestration/provider-origins.json` operator policy; caller environment base
URLs are ignored by the batch adapter. The policy maps credential names, never
credential values, to HTTPS URLs:

```json
{"schema_version":1,"credentials":{"OPENAI_API_KEY":"https://gateway.example/v1"}}
```

`inspect-batch` migrates a schema-v1 marker to a fail-closed
`legacy_uncertain` marker. `recover-legacy-batch --reason ...` records operator
disposition without releasing its usage reservations; provider uncertainty must
still be resolved outside the legacy marker before those funds can be reused.

## Ready ordering

Inventory tickets may carry an optional integer `priority`, lower being more
urgent. `plan` orders actionable tickets on `(priority, key)` and fills lanes
from the front, so a ranked sprint spends its next lane on its most urgent
unblocked ticket and an unranked sprint behaves exactly as before.

Priority ranks; it does not release. Prerequisites, external dependency status,
`concurrency_max`, and Jira-derived blocked states are all evaluated first, so a
high-priority ticket waits behind an unfinished dependency instead of preempting
it. Priority is refreshed Jira metadata: a resync re-ranks pending tickets and
never disturbs a running or terminal record, because a reservation is durable
and a rank change must not move work that already launched.

The controller orders, and the host obeys. `reserve` still admits any unblocked
pending ticket so a host can reconcile out of order after a restart; it is not
an ordering authority. Hosts must launch the keys `plan.launch` returns.

## Rejected fragile designs

- Host-specific queues were rejected because Claude and Codex would drift.
- Launch-then-checkpoint was rejected because a crash can duplicate a worker.
- Treating every Jira `Blocks` link as a prerequisite was rejected because link
  direction reverses the dependency meaning.
- Inferring missing external dependency status as complete was rejected because
  partial Jira reads would release work incorrectly.
- Clearing running entries on restart was rejected because process liveness is
  outside the controller's trust boundary.
- Stopping the sprint on one blocker was rejected because independent tickets
  remain safely actionable.
- Treating an absent priority as most urgent was rejected because a partial Jira
  read would then outrank an explicit ranking decision.
- Letting priority preempt a running lane was rejected because reservations are
  durable and re-ranking cannot prove a worker is gone.
- Requiring a priority on every ticket was rejected because most projects rank
  only part of a sprint and a forced default is an invented fact.

## Recovery invariant

The controller never admits a new ticket while the running count is at or above
`concurrency_max`. If configuration is lowered below an existing running count,
it reports `over_capacity` and waits instead of killing or launching work. A
ticket can enter `running` only from `pending`, with all prerequisites completed,
under the checkpoint lock.
Terminal results are recorded immediately. A refreshed Jira inventory may add
metadata and tickets, but never overwrites a terminal or running local result.
Tickets removed from a refreshed query become user action instead of silently
launching from stale state.

Local workers run inside a controller-owned execution unit. On Linux the
controller uses a transient user systemd scope backed by cgroup v2 when the host
provides it, and recovery requires that whole cgroup to be unpopulated. The
identity binds the boot id, invocation id, cgroup, and exact supervisor birth.
On macOS `proc_pidinfo` supplies the exact birth identity, but the supervisor
session is cooperative containment: an escaped descendant cannot be disproved,
so automatic recovery is disabled. Launch intent precedes process creation and
the supervisor retains a terminal tombstone, including for workers that exit
before attach.

Exceptional recovery is delegated to
`/usr/local/libexec/orchestration-recovery-authority`, which must be owned by a
root, be non-writable by group/other, and be exposed to the runtime user only
through the narrow sudo policy installed by
`scripts/install-operator-authority.sh`. It atomically consumes a
scope-bound token. If that helper is absent or unsafe, override is disabled;
there is deliberately no repository, home-directory, or same-UID secret.

## Operator continuations

Repository settings can tighten the built-in `$20` ticket pause but cannot
relax it. When a reviewed ticket should receive a bounded continuation, root
issues an expiring capability for an **absolute** total ceiling and pipes it
directly into the controller so it does not appear in shell history:

```text
sudo /usr/local/libexec/orchestration-recovery-authority issue-budget \
  --repository /absolute/repo --ticket PROJ-123 --ceiling-usd 35.08 \
| python3 /absolute/plugin/scripts/sprint-controller.py grant-budget \
  --sprint 65 --ticket PROJ-123 --operator-capability-stdin
```

The active grant changes only the ticket pause and ticket dollar ceiling. It
does not relax per-run or sprint budgets, run-count/reviewer-count breakers,
concurrency, review gates, or merge policy. `revoke-budget` removes it early;
otherwise it expires automatically.

A terminal checkpoint that lost its attempt token or execution-unit identity
also requires a separate one-shot, attempt-bound capability:

```text
sudo /usr/local/libexec/orchestration-recovery-authority issue-recovery \
  --repository /absolute/repo --ticket PROJ-123 --attempt 2 \
| python3 /absolute/plugin/scripts/sprint-controller.py recover-terminal \
  --sprint 65 --ticket PROJ-123 --reason 'verified stopped; preserved worktree' \
  --operator-capability-stdin
```

Only `blocked` and `user_action` entries can use terminal recovery. The command
records the reason, consumes the capability exactly once, clears stale launch
identity, and returns the ticket to `pending`; the next normal `plan`/`reserve`
creates a fresh fenced attempt.

`concurrency_max` is a ticket-lane limit. The host separately admits local
builds, full test suites, and browser runs under `max_heavy_processes`; model
lanes waiting on providers do not justify oversubscribing those local commands.
