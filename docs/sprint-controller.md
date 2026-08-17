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

The host adapter is responsible for completeness at the external boundaries:
pagination, Jira link direction, current external dependency statuses, actual
worker identity, PR state, and verified merge outcome. The controller rejects
unknown dependencies and duplicate keys rather than inventing those facts.

## Impossible guarantees

The controller cannot prove that a Jira query was complete, a host-reported run
reference identifies the intended worker, or a worker actually merged its PR.
It also cannot protect against a malicious local process or repository owner
that edits checkpoints directly. File locks coordinate cooperating processes;
they are not an authorization boundary. GitHub branch protection and the
plugin's merge guard remain the enforcement boundary for merges.

Crash recovery cannot safely decide whether an already-launched worker still
exists. Therefore every launch is reserved first, and restart plans surface all
running reservations as `needs_reconcile`. The host must inspect external state;
it may requeue only after proving the old worker is gone. This deliberately
prefers a paused lane over duplicate ticket execution.

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
