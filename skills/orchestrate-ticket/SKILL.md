---
name: orchestrate-ticket
description: Drive one ticket or change end-to-end through the full multi-agent orchestration pipeline (implement in an isolated worktree -> independent code review -> security review when warranted -> optional verification -> merge on green). Use when the user asks in natural language to "orchestrate" a ticket, "run it through the pipeline", "take it end to end", or otherwise wants the gated implement-review-merge flow rather than a plain one-off edit. This is the natural-language entry to the same flow as the Claude /orchestrate command and Codex orchestration skill. Do NOT trigger for an ordinary implementation request ("just fix this", "make this change") where the user did not ask for the full gated pipeline.
---

# Orchestrate a ticket end to end

Drive ONE unit of work through the full pipeline. This is the same flow as the
Claude Code `/orchestrate` command, reached by natural language. If the user
named a ticket, that is the target; otherwise treat their description as the
spec.

First read `.orchestration/config.yaml` (branch model, gates, CI checks,
`security_required_when`, `concurrency_max`, ticket system) and the repo's
`rules_docs` (CLAUDE.md / AGENTS.md). Those govern the specifics; this skill is
the shape.

## Relaying information (do not hand down stale facts)

You are a lossy relay. Every hop -- an agent's report into your brief, a brief
into a durable doc -- can drop the uncertainty marker and turn a "maybe" into an
asserted fact. These rules keep drift out of what you hand down. Each costs one
grep; skipping one costs review rounds.

- **Grep targets, not line numbers.** Never put a `file:line` in a brief. Give the
  symbol name or the exact string to grep for. Line numbers drift the moment a
  branch moves; a grep target is self-verifying. (A test `file:line` a reviewer
  produced in its own fresh checkout is fine *for that reviewer*; do not copy it
  forward into a brief or a doc.)
- **Never hand over code you did not run.** No selector, snippet, or query written
  from memory. Describe the intent and the constraint and let the implementer
  write the code and confirm it resolves to what you meant. An unexecuted snippet
  is a guess in the costume of a fact -- and a wrong selector manufactures a false
  green.
- **Label provenance on every claim you pass down.** Mark each as `verified by me
  just now`, `reported by <agent>, unverified`, or `from the ticket, unverified`,
  and tell the receiver to verify every claim and report what they had to drop.
  Where you label, receivers catch your errors; where you assert, they propagate.
- **Agreement is not verification.** Two agents concurring is independent
  confirmation only if the second names the evidence it actually looked at. A
  reviewer who read the implementer's report and agreed has verified nothing --
  that is transitive trust, not a second opinion.
- **Re-derive before any durable write.** A brief is cheap to correct mid-flight;
  CLAUDE.md, a ticket, a PR body are read months later by someone who cannot tell
  which sentences you checked. The bar for a durable artifact is: *I opened the
  file, just now, on this branch, and re-read the fact* -- not "an agent told me
  hours ago."
- **A merge invalidates everything before it.** Any fact you learned before a
  merge you performed (migration maxes, "current" state, line ranges) is stale by
  default. Re-derive after every merge.

## Plugin paths

The role briefs and scripts below live in this plugin, not necessarily in the
target repository. Resolve these paths from this skill file before executing or
reading them:

- `../../agents/orchestration-implementer.md`
- `../../agents/orchestration-code-reviewer.md`
- `../../agents/orchestration-security-reviewer.md`
- `../../agents/orchestration-visual-qa.md`
- `../../scripts/merge-guard.sh`
- `../../scripts/merge-on-green.sh`
- `../../scripts/run-verification.sh`
- `../../scripts/run-visual-qa.sh`

Execute scripts by absolute path while keeping the target repository as the
working directory.

## Steps

1. **Scope check.** If the ticket system is a real tracker, confirm the ticket is
   Ready: a description you could write a failing test from. If it is too thin,
   scope it (see the `scope-ticket` skill) or push back BEFORE cutting a branch.
   With no tracker, treat the request as the spec.

2. **Implement.** Run the implementer role from
   `orchestration-implementer.md`, passing the ticket body and the click-path. If
   the host supports subagents, launch a fresh implementer with
   `isolation: "worktree"`; otherwise perform that role as a distinct pass in an
   isolated git worktree. One agent-role, one ticket, one worktree, one branch
   off the integration branch, one PR to it. Wait for its structured report (PR
   number, branch, worktree, SELF_CHECK).

3. **Gate.** Run the review gates on the PR: a fresh code-review role using
   `orchestration-code-reviewer.md` (no implementer context), then a fresh
   security-review role using `orchestration-security-reviewer.md` when the diff
   hits a `security_required_when` trigger. Both must end `VERDICT: PASS`. On
   any `VERDICT: FAIL`, relay the exact blocking findings to a fresh implementer
   to fix on the same branch, then re-gate. Never merge on a FAIL.

4. **Verify (when configured).** For each `verification:` entry whose `when:`
   matches the integration target, run the resolved `run-verification.sh <name>`
   on the rebased branch (GREEN writes a sha-stamped result file; RED = no
   merge). If the change has a user-visible surface, run the visual-QA role from
   `orchestration-visual-qa.md` against the click-path; it must end
   `VERDICT: PASS`.

5. **Merge on green.** Only after every gate PASSES, every required verification
   is GREEN, and the integration CI checks are green: record the marker
   (`merge-guard.sh --record-green <pr> [result_file]`), then merge with
   `merge-on-green.sh <pr> <branch> all-green <verify_path>`. Claude Code also
   enforces this with the merge-guard hook; Codex relies on the scripted merge
   path plus branch protection for out-of-band enforcement.

6. **Close the loop.** Transition the ticket if there is a tracker. Confirm the
   work landed (`git cat-file -e origin/<integration>:<a new file>`). Report what
   merged and the click path.

Respect `concurrency_max`: at most that many heavy verification chains at once,
on non-conflicting areas. Dual gates on ONE PR run sequentially. CI-green alone
is NOT the gate; the independent VERDICTs are mandatory.
